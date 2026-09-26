"""Couchbase: the plant's memory.

InfluxDB answers "what did the dissolved oxygen do at 03:14". It cannot answer
"what is AHU-1", and it is the wrong shape for the attempt: the answers live in
tags, and InfluxDB's job is to *series*, not to describe them.

So the descriptive half of the contract goes here, in documents, keyed by tag.

## Why two databases rather than one

This is the project's central design decision, so it is worth being explicit
about why it is not redundancy.

| Question | Store | Why that one |
|---|---|---|
| What was DO at 03:14? | InfluxDB | range scan, bucketing, aggregation |
| What is AHU-1? | Couchbase | key lookup, joins, full text |
| Is nitrification degrading this year? | InfluxDB (1 h) | 8 760 points, not 8.7 million |
| Which documents describe the blower? | Couchbase | a relation, not a series |
| What failed last week? | Couchbase | heterogeneous, queryable events |
| When was the DO probe last calibrated? | Couchbase | a fact with no time series |

Running both in one process "because they are both databases" is how a system
ends up with two half-truths and no way to reconcile them. The rule that keeps it
honest: **a fact belongs in exactly one of them**, and the contract says which.

## The documents

| Key | Holds | From the contract |
|---|---|---|
| `equipment::<ID>` | one asset: type, area, duty, fail modes | `equipment` |
| `tag::<ID>` | one signal: unit, range, band, deadband | `signals` |
| `site::<ID>` | the plant, its permit limits, design flow | `site` |
| `event::<ts>::<id>` | an alarm, a fault injection, a state change | generated |

Two things are deliberately *not* documents:

- **Samples.** A sample is a point, and points belong in InfluxDB. Copying them
  here would double the write path for no query anyone would run against the copy.
- **Descriptions of a measurement inside a sample.** Writing ``description`` on
  every point is how a 4.9-million-point-a-day ingest turns into a gigabyte a day
  of the same unchanged sentence.

## The bucket is scoped, and that is a security property

The gateway gets a user with access to `wwtp` and nothing else. It cannot create
buckets, cannot read the admin endpoint, and cannot reach the other databases on
the network. In an architecture where the document store also holds calibration
records and vendor contacts, "which compromise is worse" has an answer: the
document store, because it describes the plant rather than measuring it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from softplc.contract import Contract, Equipment, Register, Signal

log = logging.getLogger("storage.couchbase")

EQUIPMENT = "equipment"
TAG = "tag"
SITE = "site"
EVENT = "event"


def equipment_key(equipment_id: str) -> str:
    return f"{EQUIPMENT}::{equipment_id}"


def tag_key(signal_id: str) -> str:
    return f"{TAG}::{signal_id}"


def site_key(site_id: str) -> str:
    return f"{SITE}::{site_id}"


def event_key(ts_ms: int, event_id: str) -> str:
    return f"{EVENT}::{ts_ms:013d}::{event_id}"


# ─── building the documents ───────────────────────────────────────────────────


def equipment_document(equipment: Equipment, site_id: str) -> dict[str, Any]:
    """One asset, as a document.

    Flat, with no nesting, even though the source has some. A nested document
    that is never queried by a nested path is a document that costs more to read
    and cannot be indexed as usefully. ``state`` and ``run_state`` are *not*
    here: they change every scan, and a document that changes every scan is a
    write-amplification problem pretending to be metadata.
    """
    return {
        "doc_type": EQUIPMENT,
        "id": equipment.id,
        "site": site_id,
        "area": equipment.area,
        "name": equipment.name,
        "type": equipment.type,
        "rated_kw": equipment.rated_kw,
        "duty": equipment.duty,
        "fail_modes": list(equipment.fail_modes),
    }


def tag_document(sig: Signal, register: Register | None = None) -> dict[str, Any]:
    """One signal, as a document.

    This is the join that makes the two databases worth having. Given a point read
    out of InfluxDB — ``aeration,area=AERATION,equipment=AHU-1,field=do_mg_l`` —
    a client fetches ``tag::AERATION:AHU-1:DO`` and learns the engineering unit,
    the range and the permit context, without any of it having been repeated on
    every one of the four million points.

    ``register`` is passed in rather than looked up. The contract does not link a
    signal to the Modbus register that exposes it — a register is a *view* of a
    subset of signals, so the link is many-to-one or one-to-none and cannot be
    derived by comparing names. An earlier version guessed by substring match,
    which is a second, wrong mapping of exactly the kind that hides a real bug
    behind a plausible number.
    """
    reg = register
    return {
        "doc_type": TAG,
        "id": sig.id,
        "area": sig.area,
        "equipment": sig.equipment,
        "measurement": sig.measurement,
        "field": sig.field,
        "unit": sig.eu,
        "range_min": sig.range_min,
        "range_max": sig.range_max,
        "normal_low": sig.normal_low,
        "normal_high": sig.normal_high,
        "deadband": sig.deadband,
        "deadband_mode": sig.deadband_mode,
        "sample_ms": sig.sample_ms,
        "writable": sig.writable,
        "modbus_register": reg.address if reg else None,
        "modbus_word_order": reg.word_order if reg else None,
    }


def site_document(contract: Contract) -> dict[str, Any]:
    """The plant, its permit and its design basis.

    The permit limits are here rather than on the tags because they are a property
    of the *licence*, not of any instrument. A limit that changes when a permit is
    reissued would otherwise need rewriting on every historical point, which is
    both expensive and wrong: the point was compliant with the limit that applied
    at the time.
    """
    site = contract.site
    return {
        "doc_type": SITE,
        "id": site.get("id", ""),
        "name": site.get("name", ""),
        "permit": dict(site.get("permit", {})),
        "design": dict(site.get("design", {})),
        "signal_count": len(contract.signals),
        "equipment_count": len(contract.equipment),
    }


def event_document(ts_ms: int, event_id: str, kind: str, severity: str,
                   message: str, **extra: Any) -> dict[str, Any]:
    """An event worth keeping: an alarm, a fault injection, a state change.

    ``severity`` is a string rather than a number because it is queried by
    meaning ("show me everything critical this week") and a numeric scale nobody
    can remember the order of gets queried wrongly.
    """
    return {
        "doc_type": EVENT,
        "id": event_id,
        "ts": ts_ms,
        "kind": kind,
        "severity": severity,
        "message": message,
        **extra,
    }


# ─── the writer ───────────────────────────────────────────────────────────────

#: ``(key, document) -> None``. A protocol rather than a cluster handle, so the
#: document shapes — where the design decisions live — are testable without a
#: database and the only thing left untested is the SDK call.
UpsertFn = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class SeedStats:
    equipment: int = 0
    tags: int = 0
    sites: int = 0
    events: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "equipment": self.equipment,
            "tags": self.tags,
            "sites": self.sites,
            "events": self.events,
            "errors": self.errors,
        }


class MetadataWriter:
    """Writes contract metadata into Couchbase.

    Takes an ``upsert`` callable rather than a cluster handle, so the document
    *shapes* — which is where the design decisions live — can be tested without
    a database, and the only thing left untested is the SDK call.
    """

    def __init__(self, upsert: UpsertFn) -> None:
        self._upsert = upsert
        self.stats = SeedStats()

    def seed_contract(self, contract: Contract) -> SeedStats:
        """Write every site, equipment item and tag from the contract.

        Idempotent by key. Re-running after a contract change updates the affected
        documents and leaves the rest alone, which matters because a seeder that
        only appends turns into a second, disagreeing copy of the contract.
        """
        site_id = str(contract.site.get("id", ""))
        try:
            self._upsert(site_key(site_id), site_document(contract))
            self.stats.sites += 1
        except Exception as exc:  # one document, not a whole seed
            log.warning("site document failed: %s", exc)
            self.stats.errors += 1

        for eq_id, eq in contract.equipment.items():
            try:
                self._upsert(equipment_key(eq_id),
                             equipment_document(eq, site_id))
                self.stats.equipment += 1
            except Exception as exc:
                log.warning("equipment %s failed: %s", eq_id, exc)
                self.stats.errors += 1

        for sig_id, sig in contract.signals.items():
            try:
                self._upsert(tag_key(sig_id), tag_document(sig))
                self.stats.tags += 1
            except Exception as exc:
                log.warning("tag %s failed: %s", sig_id, exc)
                self.stats.errors += 1
        return self.stats

    def add_event(self, ts_ms: int, event_id: str, kind: str, severity: str,
                  message: str, **extra: Any) -> None:
        try:
            self._upsert(event_key(ts_ms, event_id),
                         event_document(ts_ms, event_id, kind, severity,
                                        message, **extra))
            self.stats.events += 1
        except Exception as exc:
            log.warning("event %s failed: %s", event_id, exc)
            self.stats.errors += 1
