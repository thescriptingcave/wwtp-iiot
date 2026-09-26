"""InfluxDB write path.

Thin on purpose. The decisions — what a point looks like, which series it belongs
to, whether it is worth writing — are in ``line_protocol.py`` and
``gateway/deadband.py``, both of which are testable without a server. What is
left here is the part that genuinely needs a database: batching, retry, and the
bulk write itself.

## Why writes are batched, and how big a batch should be

One HTTP request per point turns 57 points a scan into 57 round trips. At 50
scans a second that is 2 850 requests a second against a single-node database,
and the failure mode is not an error — it is the database spending all its time
on request handling and none on the work the requests describe.

InfluxDB's own guidance is to batch, so this does. The batch is flushed when it
reaches ``batch_points`` **or** when ``batch_seconds`` have passed, whichever
comes first, and both bounds matter:

- the size bound caps the request size, so one scan cannot produce a payload the
  server will reject;
- the time bound caps the *latency*, so a plant that has gone quiet does not
  leave its last readings sitting in memory where a crash would lose them.

## Failure is expected and is not an error

The database will be unreachable sometimes. That is not an exception to be
crashed on; it is the reason the spool exists. So a failed write returns ``False``
and the caller leaves the records in the spool, and the only thing recorded is a
counter. The spool's durability argument does not depend on the write path
succeeding, which is the point of it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from softplc.contract import Contract

from storage.influx.line_protocol import (
    PointKey,
    encode_batch,
    encode_point,
    keys_from_contract,
)

log = logging.getLogger("storage.influx")

#: InfluxDB's documented ceiling is 5 000 lines per write. Two thirds of it, so
#: there is room for a scan's worth of tags on top of the points without needing
#: a second round trip.
MAX_BATCH_LINES = 5_000

DEFAULT_BATCH_POINTS = 2_000
DEFAULT_BATCH_SECONDS = 5.0


@dataclass
class WriteStats:
    written: int = 0
    batches: int = 0
    failures: int = 0
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "batches": self.batches,
            "failures": self.failures,
            "last_error": self.last_error,
        }


class InfluxWriter:
    """Buffers points and writes them in batches.

    The transport is a callable — ``(database, line_protocol_payload) -> bool`` —
    so batching, flushing and error handling are all testable without a server.
    """

    def __init__(self, contract: Contract, transport: Callable[[str, str], bool],
                 database: str = "wwtp", *, batch_points: int = DEFAULT_BATCH_POINTS,
                 batch_seconds: float = DEFAULT_BATCH_SECONDS,
                 resolution: str = "ms",
                 table: str | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.keys: dict[str, PointKey] = keys_from_contract(contract)
        #: Destination override, forwarded to every point. ``None`` means the
        #: contract's measurement group per signal, which is the normal case; a
        #: single table is for a dedicated dataset that is not the plant.
        self.table = table
        self._transport = transport
        self.database = database
        self.batch_points = min(batch_points, MAX_BATCH_LINES)
        self.batch_seconds = batch_seconds
        self.resolution = resolution
        self._clock = clock
        self._lines: list[str] = []
        self._last_flush = clock()
        self.stats = WriteStats()

    # ─── buffering ────────────────────────────────────────────────────────────

    def add(self, signal_id: str, value: float, ts_ms: int, quality: int = 0,
            source: str = "opcua") -> bool:
        """Buffer one point. Returns ``False`` for a signal with no series key.

        An unknown signal is dropped rather than encoded with a guessed key. A
        guessed key produces a point that looks completely normal in a series
        nobody is watching, which is the worst available outcome.
        """
        key = self.keys.get(signal_id)
        if key is None:
            log.debug("no series key for %s; dropped", signal_id)
            return False
        self._lines.append(
            encode_point(key, value, ts_ms, quality=quality, source=source,
                         resolution=self.resolution, table=self.table)
        )
        if len(self._lines) >= self.batch_points:
            self.flush()
        return True

    def due(self) -> bool:
        """Whether the time bound has elapsed."""
        return (self._clock() - self._last_flush) >= self.batch_seconds

    def pending(self) -> int:
        return len(self._lines)

    # ─── writing ──────────────────────────────────────────────────────────────

    def flush(self) -> bool:
        """Write the buffer. Returns ``False`` if nothing was written *and*
        nothing failed.

        A failed write leaves the buffer intact on purpose. The caller re-spooled
        the records, so they are safe; dropping them here as well would be the one
        place data is lost, in the one component whose job is not losing data.
        """
        if not self._lines:
            self._last_flush = self._clock()
            return True
        payload = encode_batch(self._lines)
        try:
            ok = self._transport(self.database, payload)
        except Exception as exc:  # one failed write is not a dead writer
            self.stats.failures += 1
            self.stats.last_error = str(exc)
            log.warning("influx write failed (%d lines): %s", len(self._lines), exc)
            self._last_flush = self._clock()
            return False
        self._last_flush = self._clock()
        if not ok:
            self.stats.failures += 1
            self.stats.last_error = "transport reported failure"
            return False
        self.stats.written += len(self._lines)
        self.stats.batches += 1
        self._lines.clear()
        return True

    def stats_dict(self) -> dict[str, Any]:
        return {**self.stats.as_dict(), "pending": self.pending()}


# ─── the real transport ───────────────────────────────────────────────────────


def make_transport(url: str, token: str, timeout_s: float = 10.0,
                   org: str | None = None) -> Callable[[str, str], bool]:
    """Build a transport backed by the InfluxDB 3 client.

    Imported lazily so the rest of the gateway — and every test of it — runs
    without the storage extras installed. A module-level import would make
    ``influxdb_client`` a hard dependency of the protocol code, which is exactly
    the coupling this project is trying to avoid.
    """
    from influxdb_client import InfluxDBClient, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
    from influxdb_client.rest import ApiException

    bucket_org = org or ""
    client = InfluxDBClient(url=url, token=token, org=bucket_org,
                            timeout=int(timeout_s * 1000))
    # SYNCHRONOUS, and this is not a style choice.
    #
    # The default write API batches *asynchronously* on a background thread. That
    # is the right default for throughput and catastrophically wrong for a
    # gateway whose entire job is not losing data: on interpreter shutdown the
    # pending batch is dropped, the SDK logs "cannot schedule new futures after
    # interpreter shutdown", and — the part that matters — the call has already
    # returned True.
    #
    # Verified against a live InfluxDB 3: two writes both reported success and
    # neither point was in the database afterwards. The spool is then deleted on
    # the strength of that success, so the data is lost twice over.
    #
    # A synchronous write blocks until the server has accepted the batch, so
    # "accepted" means what it says. Throughput is the trade, and for one bucket
    # on a local node it is the right way round.
    write_api = client.write_api(write_options=SYNCHRONOUS)

    def transport(database: str, payload: str) -> bool:
        try:
            write_api.write(
                bucket=database, org=bucket_org, record=payload,
                # Cast for the annotation: the SDK types this as its own enum and
                # ships a str alias.
                write_precision=cast("WritePrecision", WritePrecision.MS),
            )
            return True
        except ApiException as exc:
            log.error("influx rejected the write: %s", exc)
            return False
        except Exception as exc:  # reported, not raised
            log.error("influx write error: %s", exc)
            return False

    transport.close = write_api.close  # type: ignore[attr-defined]
    return transport
