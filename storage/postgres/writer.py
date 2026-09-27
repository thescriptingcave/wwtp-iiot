"""Batched writes into the hypertable.

Replaces ``storage/influx/writer.py``. The change is not cosmetic: line protocol
is a *text* format for a *tag-column* schema, and neither survives the move to a
relational one. What replaces it is a typed tuple and ``executemany``.

## Why this file is much smaller than the one it replaces

The InfluxDB writer had to:

* escape and encode every tag value, because line protocol has no escaping and a
  stray comma silently changes a line's structure;
* guard against a third field per point, a limit the database enforced and the
  error message did not mention;
* wrap writes in ``SYNCHRONOUS`` because the default write API batches
  asynchronously and drops the last batch on interpreter shutdown, *after*
  returning success;
* carry a hand-rolled ``NULLIF`` discipline for every division.

None of that is a property of the data. It is a property of the format, and it
went away with the format.

## What is left, and it is the part that matters

A batch buffer with two bounds — a row count and a time — and an honest failure
path. The bounds are the same two the Influx writer needed and for the same
reasons: a size bound caps the transaction, a time bound caps the latency, and
without the second a plant that goes quiet leaves its last readings in memory
where a crash loses them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("storage.postgres.writer")

#: Rows per INSERT. Below Postgres' parameter limit with room for a statement's
#: worth of overhead, and large enough that the round trip stops dominating.
DEFAULT_BATCH_ROWS = 2_000

#: Seconds before an unflushed batch goes out regardless of its size.
DEFAULT_BATCH_SECONDS = 5.0

_INSERT = """
INSERT INTO reading (ts, signal_id, value, quality, source)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (ts, signal_id, source) DO UPDATE SET
    value   = EXCLUDED.value,
    quality = EXCLUDED.quality
"""


@dataclass
class WriteStats:
    written: int = 0
    batches: int = 0
    failures: int = 0
    last_error: str = ""
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "batches": self.batches,
            "failures": self.failures,
            "last_error": self.last_error,
            "total_ms": round(self.total_ms, 1),
        }


@dataclass(slots=True)
class Reading:
    """One row. A typed tuple, which is the entire replacement for a text encoder.

    ``ts`` is epoch *seconds* as a float, because that is what the protocol
    clients hand back and converting once at the boundary is cheaper and clearer
    than converting in four call sites.
    """

    ts: float            # epoch seconds
    signal_id: str
    value: float | None
    quality: int = 0
    source: str = "opcua"

    def as_row(self) -> tuple[Any, ...]:
        """The row, timestamp as an aware ``datetime``.

        Deliberately not the raw float. A parameterised ``INSERT`` will quietly
        coerce a float to ``timestamptz``; ``COPY`` will not, because ``COPY``
        parses its input as text, and the failure arrives as::

            InvalidDatetimeFormat: invalid input syntax for type timestamp
            with time zone: "1.0"

        That asymmetry cost a debugging round here, and it is the kind of thing
        that is much cheaper to design out than to diagnose: one row type, one
        representation, both transports agreeing.
        """
        return (
            datetime.fromtimestamp(self.ts, tz=UTC),
            self.signal_id, self.value, self.quality, self.source,
        )


class PostgresWriter:
    """Buffers readings and writes them in batches.

    Takes an ``execute`` callable rather than a connection, so the *buffering*
    policy — when to flush, what happens on failure, what is counted — is testable
    with a list and no database. That was the right call for the Influx writer
    too, and it is why the storage layer could be unit-tested while its SQL went
    unexecuted for three days.
    """

    def __init__(self, execute: Callable[[list[tuple[Any, ...]]], int],
                 *, batch_rows: int = DEFAULT_BATCH_ROWS,
                 batch_seconds: float = DEFAULT_BATCH_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._execute = execute
        self.batch_rows = batch_rows
        self.batch_seconds = batch_seconds
        self._clock = clock
        self._rows: list[tuple[Any, ...]] = []
        self._last_flush = clock()
        # Earliest time an *automatic* flush may be attempted again, set on
        # failure. See `add`.
        self._retry_not_before = 0.0
        self.stats = WriteStats()

    # ─── buffering ────────────────────────────────────────────────────────────

    def add(self, reading: Reading) -> bool:
        """Buffer one reading.

        There is no unknown-signal case any more, and that is worth noticing: the
        old writer had to check a series-key table and silently drop readings that
        were not in it. Here the foreign key does it, loudly, at the database.
        """
        self._rows.append(reading.as_row())
        # The row bound is suppressed until the retry deadline. Without this the
        # behaviour during an outage is pathological in a way that is easy to miss
        # by reading the code: a failed flush *keeps* its rows, so the buffer is
        # still at the bound, so the very next `add` triggers another flush, which
        # also fails. At one poll per second over 57 signals that is 57 doomed
        # round trips and 57 warnings a second, against a database that is already
        # struggling, for the whole duration of the outage.
        #
        # An explicit `flush()` — which is what the gateway's loop calls, once per
        # poll — is not suppressed. The retry rate is therefore one attempt per
        # poll, which is the right cadence to discover that the database is back.
        if len(self._rows) >= self.batch_rows and self._may_attempt():
            self.flush()
        return True

    def _may_attempt(self) -> bool:
        return self._clock() >= self._retry_not_before

    def due(self) -> bool:
        """Whether the time bound has elapsed."""
        return (self._clock() - self._last_flush) >= self.batch_seconds

    def pending(self) -> int:
        return len(self._rows)

    # ─── writing ──────────────────────────────────────────────────────────────

    def flush(self) -> bool:
        """Write the buffer. Returns ``False`` if the write failed.

        On failure the buffer is **kept**. The gateway's spool is the durable
        copy, and dropping rows here as well as leaving them in the spool would be
        the one place data is lost — in the component whose entire job is not
        losing data. That is the same rule the Influx writer followed, for the
        same reason, and it is still the rule worth getting right.
        """
        if not self._rows:
            self._last_flush = self._clock()
            return True
        rows, self._rows = self._rows, []
        started = time.perf_counter()
        try:
            count = int(self._execute(rows) or 0)
        except Exception as exc:  # one failed batch is not a dead writer
            self._rows = rows + self._rows      # put it back, in order
            self.stats.failures += 1
            self.stats.last_error = str(exc)
            log.warning("postgres write of %d rows failed: %s", len(rows), exc)
            now = self._clock()
            self._last_flush = now
            self._retry_not_before = now + self.batch_seconds
            return False
        self.stats.written += count or len(rows)
        self.stats.batches += 1
        self.stats.total_ms += (time.perf_counter() - started) * 1000.0
        self._last_flush = self._clock()
        return True

    def stats_dict(self) -> dict[str, Any]:
        return {**self.stats.as_dict(), "pending": self.pending()}


def make_copy_execute(dsn_str: str) -> Callable[[list[tuple[Any, ...]]], int]:
    """Build an execute callable that uses ``COPY``.

    Bulk path, for the seeder. ``COPY`` is not a slightly faster ``INSERT`` — it
    is a different protocol path inside Postgres, parsing the whole batch in one
    server-side pass, and it is typically an order of magnitude faster than
    ``executemany`` over the wire.

    It also has a real cost, which is why the live path does not use it: ``COPY``
    cannot do ``ON CONFLICT DO UPDATE``, so a duplicate key is an error rather
    than an update. For the seeder that is the *right* behaviour — a re-run
    should not silently double the history — and for the gateway, which may
    re-send a batch from its spool, it would be wrong.
    """
    import psycopg

    conn = psycopg.connect(dsn_str, autocommit=False)

    columns = "ts, signal_id, value, quality, source"

    def execute(rows: list[tuple[Any, ...]]) -> int:
        with conn.cursor() as cur, cur.copy(
            f"COPY reading ({columns}) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row(row)
        conn.commit()
        return len(rows)

    execute.close = conn.close  # type: ignore[attr-defined]
    return execute


def make_execute(dsn_str: str) -> Callable[[list[tuple[Any, ...]]], int]:
    """Build an execute callable backed by a real connection.

    One connection, reused. Postgres connections are expensive to establish and
    this writer is single-threaded by construction, so a pool would be
    complexity for nothing. The trade is that it is not safe to share across
    threads — which is why the gateway calls it from one place.
    """
    import psycopg

    conn = psycopg.connect(dsn_str, autocommit=False)

    def execute(rows: list[tuple[Any, ...]]) -> int:
        with conn.cursor() as cur:
            cur.executemany(_INSERT, rows)
        conn.commit()
        return len(rows)

    execute.close = conn.close  # type: ignore[attr-defined]
    return execute
