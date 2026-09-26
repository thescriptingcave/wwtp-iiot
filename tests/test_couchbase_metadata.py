"""Tests for the Couchbase document shapes.

The database is absent throughout — the writer takes an ``upsert`` callable, so
what is tested here is the *content* of the documents, which is where the
two-database design decision actually lives.

The tests that matter are the negative ones: what must **not** be in a document.
A metadata store that has been contaminated with time-series data is worse than
one that is empty, because it looks like it is working.
"""

from __future__ import annotations

import pytest
from softplc.contract import contract
from storage.couchbase.metadata import (
    EQUIPMENT,
    EVENT,
    SITE,
    TAG,
    MetadataWriter,
    equipment_document,
    equipment_key,
    event_document,
    event_key,
    site_document,
    site_key,
    tag_document,
    tag_key,
)

C = contract()


# ─── keys ─────────────────────────────────────────────────────────────────────


def test_keys_are_namespaced_by_kind() -> None:
    """``equipment::AHU-1`` and ``tag::AERATION:AHU-1:DO`` cannot collide, and a
    single bucket holding two kinds of document is much easier to query wrong."""
    keys = {
        equipment_key("AHU-1"),
        tag_key("AERATION:AHU-1:DO"),
        site_key("PLANT-A"),
        event_key(1_700_000_000_000, "fault-1"),
    }
    assert len(keys) == 4, "a key collision across document types"
    for key in keys:
        assert key.split("::", 1)[0] in (EQUIPMENT, TAG, SITE, EVENT)


def test_event_keys_sort_chronologically() -> None:
    """The key is zero-padded to 13 digits, so lexical order is temporal order.
    Without the padding, event 9 sorts after event 10 and a range query returns
    them in the wrong order with no error."""
    keys = [event_key(ts, "x") for ts in (9, 10, 100, 1_700_000_000_000)]
    assert keys == sorted(keys)


# ─── what a document holds ───────────────────────────────────────────────────


def test_a_tag_document_carries_everything_a_point_does_not() -> None:
    """This is the join. Given a point out of InfluxDB, a client fetches this and
    learns the unit and the range — neither of which is on the point, because
    repeating them on four million points a day is how a database dies."""
    doc = tag_document(C.signals["AERATION:AHU-1:DO"])
    sig = C.signals["AERATION:AHU-1:DO"]
    assert doc["unit"] == sig.eu
    assert doc["range_min"] == sig.range_min
    assert doc["range_max"] == sig.range_max
    assert doc["normal_low"] == sig.normal_low
    assert doc["deadband"] == sig.deadband
    assert doc["sample_ms"] == sig.sample_ms
    assert doc["doc_type"] == TAG


def test_a_tag_document_has_no_sample_values_in_it() -> None:
    """The contamination test. A document that carries readings duplicates the
    time series, doubles the write path, and answers trend questions wrongly
    because two copies drift."""
    doc = tag_document(C.signals["AERATION:AHU-1:DO"])
    for forbidden in ("value", "values", "samples", "last_value", "readings",
                      "history", "ts", "time"):
        assert forbidden not in doc, (
            f"{forbidden!r} in a tag document: the time series does not belong here"
        )


def test_a_tag_document_does_not_invent_a_modbus_link() -> None:
    """The contract does not link a signal to the register exposing it, so the
    field is absent rather than guessed. A substring match on the field name
    would have linked ``do_mg_l`` to ``AERATION_DO`` by luck and to nothing at all
    by design."""
    doc = tag_document(C.signals["AERATION:AHU-1:DO"])
    assert doc["modbus_register"] is None
    # And when one *is* supplied, it is used verbatim.
    reg = C.register("AERATION_DO")
    linked = tag_document(C.signals["AERATION:AHU-1:DO"], register=reg)
    assert linked["modbus_register"] == reg.address
    assert linked["modbus_word_order"] == reg.word_order


def test_an_equipment_document_has_no_live_state() -> None:
    """``state`` changes every scan. A document that changes every scan is a
    write-amplification problem wearing a metadata costume — and Couchbase will
    happily rebalance a bucket for you while it does it."""
    doc = equipment_document(C.equipment["AHU-1"], "PLANT-A")
    for forbidden in ("state", "run_state", "running", "last_update"):
        assert forbidden not in doc, (
            f"{forbidden!r} in an equipment document: live state belongs in "
            "InfluxDB as a separate measurement"
        )
    assert doc["type"] == C.equipment["AHU-1"].type
    assert doc["area"] == C.equipment["AHU-1"].area


def test_equipment_documents_are_flat() -> None:
    """A nested field that is never queried by a nested path costs more to read
    and indexes less usefully than a flat one."""
    doc = equipment_document(C.equipment["AHU-1"], "PLANT-A")

    def depth(value: object) -> int:
        if isinstance(value, dict):
            return 1 + max((depth(v) for v in value.values()), default=0)
        return 0

    assert depth(doc) == 1, "equipment documents are flat by design"


def test_the_site_document_owns_the_permit() -> None:
    """The permit is a property of the licence, not of any instrument. A limit
    that changed when the permit was reissued would otherwise need rewriting on
    every historical point — expensive, and wrong, because the point was
    compliant with the limit that applied at the time."""
    doc = site_document(C)
    assert doc["permit"] == C.site["permit"]
    assert doc["design"] == C.site["design"]
    assert doc["signal_count"] == len(C.signals)
    assert doc["doc_type"] == SITE


def test_an_event_severity_is_a_word_not_a_number() -> None:
    """It is queried by meaning — "show me everything critical this week" — and a
    numeric scale nobody remembers the order of gets queried backwards."""
    doc = event_document(1_700_000_000_000, "e1", "alarm", "critical",
                         "Effluent ammonia above permit")
    assert doc["severity"] == "critical"
    assert doc["kind"] == "alarm"
    assert doc["ts"] == 1_700_000_000_000
    assert "ammonia" in doc["message"]


def test_event_extra_fields_are_kept() -> None:
    doc = event_document(1, "e", "fault", "warning", "blower trip",
                         equipment="BLW-1", scenario="aeration_loss")
    assert doc["equipment"] == "BLW-1"
    assert doc["scenario"] == "aeration_loss"


# ─── the writer ──────────────────────────────────────────────────────────────


def test_seeding_writes_every_contract_object() -> None:
    written: dict[str, dict] = {}
    w = MetadataWriter(lambda k, d: written.__setitem__(k, d))
    stats = w.seed_contract(C)
    assert stats.sites == 1
    assert stats.tags == len(C.signals)
    assert stats.equipment == len(C.equipment)
    assert stats.errors == 0
    assert len(written) == 1 + len(C.signals) + len(C.equipment)


def test_seeding_is_idempotent_by_key() -> None:
    """A seeder that appends becomes a second, disagreeing copy of the contract —
    and the copy nobody remembers to update."""
    written: dict[str, dict] = {}
    w = MetadataWriter(lambda k, d: written.__setitem__(k, d))
    w.seed_contract(C)
    first = dict(written)
    w.seed_contract(C)
    assert written == first
    assert len(written) == len(first)


def test_one_failing_document_does_not_abort_the_seed() -> None:
    """A metadata store with 56 of 57 tags is far more useful than an empty one,
    and far more useful than a seeder that gives up on the first error."""

    def flaky(key: str, doc: dict) -> None:
        if "TSS" in key:
            raise RuntimeError("document too large")

    w = MetadataWriter(flaky)
    stats = w.seed_contract(C)
    assert stats.errors == 1
    assert stats.tags == len(C.signals) - 1
    assert stats.equipment == len(C.equipment)


def test_the_writer_never_raises() -> None:
    def always_fails(key: str, doc: dict) -> None:
        raise RuntimeError("cluster unavailable")

    w = MetadataWriter(always_fails)
    stats = w.seed_contract(C)
    assert stats.errors == 1 + len(C.signals) + len(C.equipment)
    assert stats.as_dict()["errors"] == stats.errors


def test_events_are_counted_separately() -> None:
    keys: list[str] = []
    w = MetadataWriter(lambda k, d: keys.append(k))
    w.add_event(1, "e1", "alarm", "warning", "one")
    w.add_event(2, "e2", "alarm", "critical", "two")
    assert w.stats.events == 2
    assert len(set(keys)) == 2


@pytest.mark.parametrize("doc_type", [EQUIPMENT, TAG, SITE, EVENT])
def test_every_document_declares_its_type(doc_type: str) -> None:
    """``doc_type`` is what makes one bucket queryable for several kinds of thing
    without a collection per kind."""
    builders = {
        EQUIPMENT: equipment_document(C.equipment["AHU-1"], "PLANT-A"),
        TAG: tag_document(C.signals["AERATION:AHU-1:DO"]),
        SITE: site_document(C),
        EVENT: event_document(1, "e", "alarm", "info", "m"),
    }
    assert builders[doc_type]["doc_type"] == doc_type
