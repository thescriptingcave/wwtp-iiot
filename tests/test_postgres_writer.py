"""The writer's buffering policy, tested without a database.

The writer takes an ``execute`` callable rather than a connection, which is the
whole reason this file can exist. The SQL is verified against a live Postgres in
``tests/integration/test_postgres.py``; what is verified here is the part that
is *policy* and not *protocol*:

* when a batch goes out (a row count and a time, and both are needed);
* what a failure costs, which is the interesting question;
* what gets counted, because the numbers end up on a health endpoint and a
  health endpoint that lies is worse than no health endpoint.
"""

from __future__ import annotations

from storage.postgres.writer import (
    DEFAULT_BATCH_ROWS,
    PostgresWriter,
    Reading,
)


class Clock:
    """A hand-cranked clock, so the time bound is tested without ``sleep``."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _writer(**kw) -> tuple[PostgresWriter, list, Clock]:
    rows: list[tuple] = []
    clock = Clock()
    w = PostgresWriter(
        lambda batch: (rows.extend(batch), len(batch))[1],
        clock=clock, **kw,
    )
    return w, rows, clock


def _r(i: int = 0, value: float | None = 1.0, quality: int = 0) -> Reading:
    return Reading(ts=1_700_000_000.0 + i, signal_id="A:1:X", value=value,
                   quality=quality)


# ─── the two bounds ──────────────────────────────────────────────────────────


def test_nothing_is_written_before_a_row_is_added() -> None:
    w, rows, _ = _writer()
    assert w.pending() == 0
    assert w.flush() is True
    assert rows == []


def test_a_partial_batch_stays_in_memory() -> None:
    w, rows, _ = _writer(batch_rows=10)
    for i in range(9):
        w.add(_r(i))
    assert rows == [], "wrote before reaching the bound"
    assert w.pending() == 9


def test_the_row_bound_flushes_and_empties_the_buffer() -> None:
    w, rows, _ = _writer(batch_rows=10)
    for i in range(10):
        w.add(_r(i))
    assert len(rows) == 10
    assert w.pending() == 0
    assert w.stats.batches == 1
    assert w.stats.written == 10


def test_the_time_bound_flushes_a_small_batch() -> None:
    """A plant that goes quiet still loses nothing.

    The row bound alone is a bug waiting for a quiet night: a signal that stops
    moving stops producing rows, the buffer sits, and a crash in the morning
    takes the last hour of readings with it. This is the bound that prevents
    that, and it is why there are two.
    """
    w, rows, clock = _writer(batch_rows=10_000, batch_seconds=5.0)
    w.add(_r(0))
    assert rows == []
    assert w.due() is False

    clock.advance(4.9)
    assert w.due() is False
    clock.advance(0.2)
    assert w.due() is True
    w.flush()
    assert len(rows) == 1


def test_due_is_measured_from_the_last_flush_not_from_construction() -> None:
    """Otherwise a long-running writer would flush a full batch on every add.

    Subtle and worth stating: `due()` after a row-triggered flush must reset the
    clock, or the next `add` of a single row satisfies the time bound
    immediately and the batching degrades to one row per statement.
    """
    w, _, clock = _writer(batch_rows=2, batch_seconds=5.0)
    w.add(_r(0))
    w.add(_r(1))
    clock.advance(4.0)
    assert w.due() is False
    w.add(_r(2))
    w.flush()
    clock.advance(4.0)
    assert w.due() is False, "the clock did not reset on flush"


# ─── failure: the part that matters ─────────────────────────────────────────


class Failing:
    """An execute that fails a configurable number of times, then works."""

    def __init__(self, fail_for: int) -> None:
        self.fail_for = fail_for
        self.calls = 0
        self.accepted: list[tuple] = []

    def __call__(self, batch: list[tuple]) -> int:
        self.calls += 1
        if self.calls <= self.fail_for:
            raise RuntimeError("connection refused")
        self.accepted.extend(batch)
        return len(batch)


def test_a_failed_batch_is_kept_and_retried_in_order() -> None:
    """The single most important behaviour in the storage layer.

    The gateway's spool is the durable copy, so losing a batch here is not
    catastrophic — but it is still the one place data can vanish, in the
    component whose entire job is not losing it. And the *order* matters: a
    retry that reorders readings makes a time series a lie about sequence.

    Note this is the same rule the previous Influx writer followed, for the same
    reason. Moving storage engines changed nothing about it, which is a decent
    argument that it was a rule about gateways rather than about databases.
    """
    backend = Failing(fail_for=1)
    clock = Clock()
    w = PostgresWriter(backend, batch_rows=3, batch_seconds=1e9, clock=clock)

    w.add(_r(0))
    w.add(_r(1))
    w.add(_r(2))                     # triggers the flush, which fails
    assert w.pending() == 3, "a failed batch was dropped"
    assert w.stats.failures == 1
    assert w.stats.written == 0

    w.add(_r(3))                     # arrives before the retry
    assert w.flush() is True
    assert w.pending() == 0

    # Ordered by `ts`, which encodes the arrival counter via _r(i). Checking the
    # signal_id column instead — the obvious mistake, made here — would compare
    # four copies of the string "A:1:X" and pass whatever the order was.
    assert [r[0] for r in backend.accepted] == [
        _r(i).as_row()[0] for i in range(4)
    ], "the retry did not preserve arrival order"
    assert w.stats.written == 4
    assert w.stats.failures == 1


def test_a_failure_does_not_stop_the_writer() -> None:
    """One failed batch is an outage, not a bug.

    Worth reading the count carefully: four, not two. The retry is not sent on
    its own — it goes out together with whatever arrived while the database was
    away, because the buffer is one queue and splitting it would mean holding a
    second copy of the backlog in memory for no benefit. The alternative, a
    dedicated retry slot, doubles the memory the outage is most likely to make
    scarce.
    """
    backend = Failing(fail_for=1)
    clock = Clock()
    w = PostgresWriter(backend, batch_rows=2, batch_seconds=5.0, clock=clock)
    w.add(_r(0))
    w.add(_r(1))                       # triggers a flush, which fails
    assert w.stats.written == 0
    assert w.pending() == 2
    w.add(_r(2))                       # automatic flush suppressed by the backoff
    w.add(_r(3))
    assert w.pending() == 4, "rows were dropped or flushed during the backoff"

    clock.advance(5.1)                 # the retry window opens
    w.add(_r(4))                       # at the bound again, so it flushes
    assert w.stats.written == 5
    assert w.stats.batches == 1
    assert [r[0] for r in backend.accepted] == [
        _r(i).as_row()[0] for i in range(5)
    ]


def test_an_outage_costs_one_attempt_per_poll_not_one_per_row() -> None:
    """The bug the test above found, pinned so it cannot come back.

    A failed flush keeps its rows, so the buffer is still over the row bound, so
    every subsequent `add` would retry immediately. At 57 signals a second that
    is 57 doomed round trips per second against a database that is already
    unwell — a self-inflicted denial of service aimed at the thing you most need
    working.
    """
    backend = Failing(fail_for=99)
    clock = Clock()
    w = PostgresWriter(backend, batch_rows=10, batch_seconds=5.0, clock=clock)

    for _ in range(3):                 # three polls, 57 signals each
        for i in range(57):
            w.add(_r(i))

    assert backend.calls == 1, (
        f"{backend.calls} failed write attempts for 171 readings"
    )
    assert w.pending() == 171, "readings were dropped during the outage"
    assert w.stats.failures == 1

    # One explicit flush per poll is the retry cadence, and it is not suppressed.
    clock.advance(5.1)
    w.flush()
    assert backend.calls == 2


def test_the_error_is_reported_not_swallowed() -> None:
    backend = Failing(fail_for=1)
    w = PostgresWriter(backend, batch_rows=1, batch_seconds=1e9, clock=Clock())
    w.add(_r(0))
    assert "connection refused" in w.stats.last_error
    assert w.stats_dict()["failures"] == 1
    assert w.stats_dict()["last_error"]


def test_an_execute_returning_zero_does_not_count_as_a_write() -> None:
    """``executemany`` returns None, not a rowcount.

    psycopg's `cur.rowcount` after `executemany` is the number of rows the last
    statement touched, which for a multi-row statement is not obviously the
    batch size, and is `None` in some paths. Rather than depend on it, the
    writer counts what it handed over — the batch is the unit of accounting
    either way, and the true row count is in the table.
    """
    calls: list[int] = []

    def zero(_batch):
        calls.append(1)
        return 0

    w = PostgresWriter(zero, batch_rows=5, batch_seconds=1e9, clock=Clock())
    for i in range(5):
        w.add(_r(i))
    assert calls == [1]
    assert w.stats.batches == 1
    assert w.stats.written == 5, "counted zero written rows for a full batch"


# ─── the row itself ─────────────────────────────────────────────────────────


def test_a_row_carries_an_aware_datetime_under_both_transports() -> None:
    """COPY parses text; INSERT does not. They must agree.

    This is a bug that was found rather than designed: the parameterised INSERT
    quietly coerced a float epoch to `timestamptz`, and COPY rejected the same
    value with ``InvalidDatetimeFormat: invalid input syntax for type timestamp
    with time zone: "1.0"``. One row type, one representation, both transports
    agreeing — cheaper to design out than to diagnose.
    """
    from datetime import datetime

    ts, signal_id, value, quality, source = _r(0).as_row()
    assert isinstance(ts, datetime)
    assert ts.tzinfo is not None
    assert ts.tzinfo.utcoffset(ts).total_seconds() == 0
    assert signal_id == "A:1:X"
    assert (value, quality, source) == (1.0, 0, "opcua")


def test_a_bad_reading_keeps_its_null_value() -> None:
    """The representation the Influx version arrived at, now by choice."""
    _, _, value, quality, _ = _r(0, value=None, quality=2).as_row()
    assert value is None
    assert quality == 2
    assert quality != 0, "a valueless reading must not claim to be Good"


def test_the_default_batch_is_below_the_parameter_limit() -> None:
    """Postgres allows 65535 bind parameters; a five-column row must fit more
    than 13 000 of them.

    Not a hypothetical: a batch sized by intuition rather than by the protocol
    limit fails with a message about "too many arguments" that does not say
    which query.
    """
    assert DEFAULT_BATCH_ROWS * 5 < 65535
