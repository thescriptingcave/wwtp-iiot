"""Tests for the rollup policy.

The database is a fake throughout. That is not a shortcut — the interesting
decisions in a rollup worker are *when to write* and *what to average*, and both
are answerable without InfluxDB. What is left untested here is the SQL text,
which is why it lives in the ``sql/`` course as well: a query that only exists in
a Python string is a query nobody learns from.
"""

from __future__ import annotations

import pytest
from storage.rollup_worker.rollup import (
    ROLLUP_1M,
    TIERS,
    RollupWorker,
    plan,
)

HOUR = 3600
MIN = 60
# A whole number of minutes and hours, so the arithmetic in the tests is obvious.
T0 = 1_700_000_000 // HOUR * HOUR


# ─── which windows are closed ────────────────────────────────────────────────


def test_the_window_containing_now_is_never_closed() -> None:
    """Writing the in-progress window means recording half a minute and calling
    it a minute. Every average downstream of that is quietly wrong, and nothing
    about the resulting number looks wrong."""
    for offset in (0, 1, 30, 59):
        p = plan(T0 + MIN + offset)
        assert (T0 + MIN, T0 + 2 * MIN) in p.partial_windows["1m"]
        assert (T0 + MIN, T0 + 2 * MIN) not in p.closed_windows["1m"]


def test_a_window_is_closed_the_moment_it_ends() -> None:
    """``<=`` and not ``<``. At exactly the boundary the window has received
    every point it will ever receive; waiting for the next tick to write it adds a
    minute of latency and buys no safety."""
    p = plan(T0 + 2 * MIN)
    assert (T0 + MIN, T0 + 2 * MIN) in p.closed_windows["1m"]


def test_partial_windows_can_be_written_deliberately() -> None:
    """The live-dashboard trade: a partial write is overwritten when the window
    closes, so the only cost is that a query *during* the window sees a partial
    average. Usually worth it when someone is watching a trend."""
    p = plan(T0 + MIN + 30, discard_partial=False)
    assert (T0 + MIN, T0 + 2 * MIN) in p.closed_windows["1m"]
    # Still recorded as partial, so the caller knows this one will be corrected.
    assert (T0 + MIN, T0 + 2 * MIN) in p.partial_windows["1m"]


def test_tiers_use_different_window_sizes() -> None:
    # Half past the hour, so the two tiers are not both landing on a boundary and
    # the comparison below is about window size rather than about the clock.
    p = plan(T0 + 2 * HOUR + HOUR // 2)
    one_min = p.closed_windows["1m"]
    one_hour = p.closed_windows["1h"]
    assert all(e - s == MIN for s, e in one_min)
    assert all(e - s == HOUR for s, e in one_hour)
    # The hourly tier lags further behind, because its own windows are longer:
    # at this instant the last closed minute ends 30 minutes before the last
    # closed hour does. A tier that ran ahead of its source would aggregate a
    # window that is not written yet and report an average of zero.
    assert one_hour[-1][1] == T0 + 2 * HOUR
    assert one_min[-1][1] == T0 + 2 * HOUR + HOUR // 2


def test_a_stopped_worker_catches_up_on_restart() -> None:
    """A missed window is a permanent hole in the record. Re-deriving it costs a
    query; explaining its absence costs a conversation."""
    p = plan(T0 + 20 * MIN)
    assert len(p.closed_windows["1m"]) == 12, "expected a full catch-up window"
    # Oldest first: the window that ended 12 minutes ago, not the most recent
    # one. The bound is deliberate - an hour of catch-up for the minute tier, and
    # no further, so a worker stopped over a long weekend does not try to rebuild
    # it all at once and knock the database over doing it.
    oldest_start, oldest_end = p.closed_windows["1m"][0]
    assert oldest_start == T0 + 8 * MIN
    assert oldest_end == T0 + 9 * MIN
    assert p.closed_windows["1m"][-1][1] == T0 + 20 * MIN


def test_no_window_is_written_twice() -> None:
    p = plan(T0 + 5 * MIN)
    for tier, windows in p.closed_windows.items():
        assert len(windows) == len(set(windows)), tier
        starts = [s for s, _ in windows]
        assert starts == sorted(starts), f"{tier} windows are out of order"


# ─── the weighted average, which is the bug everybody ships ──────────────────


def test_the_documented_query_weights_by_count() -> None:
    """The most common hand-rolled-rollup bug, pinned as a string assertion.

    Arithmetic mean of means is wrong whenever two windows hold different numbers
    of points — and they always do, because the deadband filtered some of them
    and the last window of a run is short. The symptom is a trend that is very
    slightly wrong, which is the hardest kind of wrong to notice.
    """
    assert 'SUM("mean" * "_count")' in ROLLUP_1M
    assert "/ NULLIF(SUM(\"_count\"), 0)" in ROLLUP_1M
    # And explicitly *not* a bare AVG of the means.
    assert 'AVG("mean")' not in ROLLUP_1M


def test_a_weighted_average_is_not_an_average_of_averages() -> None:
    """The arithmetic, spelled out, so the SQL above is not taken on trust.

    Two minutes: 9 points averaging 10, and 1 point averaging 100.
    Mean of means: 55.  Weighted: (90 + 100) / 10 = 19.
    The plant did not experience an hour at 55; it experienced a settled reading
    and then one spike.
    """
    # Two minutes: the first holds 9 points averaging 10, the second holds a
    # single point at 100. Unequal counts are the entire point - with equal counts
    # the two methods agree, which is why the bug survives a casual test.
    means = [10.0, 100.0]
    counts = [9, 1]
    mean_of_means = sum(means) / len(means)
    weighted = sum(m * c for m, c in zip(means, counts, strict=True)) / sum(counts)
    assert mean_of_means == pytest.approx(55.0)
    assert weighted == pytest.approx(19.0)
    assert weighted != pytest.approx(mean_of_means)


def test_an_empty_window_yields_null_rather_than_a_zero() -> None:
    """``SUM(...) / SUM(...)`` with no rows divides by zero. NULLIF turns that
    into NULL, which a chart renders as "no data" — the truth — instead of 0.0,
    which a chart renders as "the process was at zero", which is a lie that looks
    like data."""
    assert "NULLIF" in ROLLUP_1M


# ─── the worker's behaviour ──────────────────────────────────────────────────


def test_a_tick_writes_every_closed_window() -> None:
    calls: list[tuple[str, int, int]] = []

    def execute(tier: str, start: int, end: int) -> int:
        calls.append((tier, start, end))
        return 57

    w = RollupWorker(execute, clock=lambda: T0 + 2 * MIN)
    w.tick()
    assert calls
    assert all(t in ("1m", "1h") for t, _, _ in calls)
    assert w.stats.windows_written == len(calls)
    assert w.stats.points_written == 57 * len(calls)


def test_one_bad_window_does_not_stop_the_worker() -> None:
    """A worker that dies on a transient error needs a supervisor, an alert and a
    runbook. A worker that counts the error and continues is a worker that just
    works."""
    seen: list[str] = []

    def execute(tier: str, start: int, end: int) -> int:
        seen.append(tier)
        if len(seen) == 2:
            raise RuntimeError("connection reset by peer")
        return 10

    w = RollupWorker(execute, clock=lambda: T0 + 2 * MIN)
    w.tick()
    assert w.stats.errors == 1
    assert len(seen) > 2, "the worker stopped at the first failure"
    assert w.stats.windows_written == len(seen) - 1


def test_rewriting_a_window_is_safe() -> None:
    """Rollups must be idempotent. Re-running a window overwrites it; appending
    would double-count, and the only symptom is a trend that is slightly too high
    on the days the worker restarted.

    The property is enforced by the SQL (``INSERT`` into a bucket keyed on
    ``_start``), so what is asserted here is that the worker *does* hand the same
    window over again — which is what makes the overwrite happen.
    """
    windows: list[tuple[int, int]] = []
    w = RollupWorker(
        lambda t, s, e: (windows.append((s, e)), 1)[1],
        clock=lambda: T0 + 2 * MIN,
    )
    w.tick()
    first = list(windows)
    w.tick()
    assert windows[: len(first)] == first, "the same windows must be offered again"
    assert len(windows) == 2 * len(first)


def test_the_stats_say_what_happened() -> None:
    w = RollupWorker(lambda t, s, e: 5, clock=lambda: T0 + 2 * MIN)
    w.tick()
    d = w.stats.as_dict()
    assert d["windows_written"] > 0
    assert d["errors"] == 0
    assert d["last_duration_ms"] >= 0.0


def test_tiers_are_declared_fine_to_coarse() -> None:
    """The order matters: the hourly tier reads the minute tier, so a tier whose
    source has not been written yet would aggregate nothing and report an
    average of zero — a flat line at the bottom of the chart, which reads as a
    process that has stopped."""
    assert [t["name"] for t in TIERS] == ["1m", "1h"]
    assert TIERS[0]["source"] is None, "the finest tier reads raw"
    assert TIERS[1]["source"] == "1m", "the coarser tier reads the finer one"
    assert all(
        TIERS[i]["every"] < TIERS[i + 1]["every"] for i in range(len(TIERS) - 1)
    )
