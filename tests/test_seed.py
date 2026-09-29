"""The seeder's policy: when the storm happens, and which instant the week ends at.

These need no database — `Seeder` takes an `execute` callable — and they exist
because the seeder had **no tests at all**, which is how two defects lived in it.

* The storm never fired. `run_days` moved a fault's `start_s` and then computed
  `end_s` from it, so `end_s` collapsed to the *original* end, hours before the
  new start. The fault's window was empty, so it simply never happened. Nothing
  raised, and the scenario that exists to be "the canonical IIoT event" left one
  row in `INFLUENT:FLOW:CONDUCTIVITY` for a week.
* The window ended at `time.time()`, so every re-seed slid every timestamp and
  everything written down about a previous seed was stale on arrival.
"""

from __future__ import annotations

from typing import Any

import pytest
from softplc.contract import contract as get_contract
from storage.seed.main import Seeder

#: Six simulated hours, with the storm armed two hours from the end.
DAYS = 0.25
END = 1_790_000_000.0  # an arbitrary fixed instant


def _run(*, end_ts: float | None, storm_after_h: float | None = 2.0) -> list[Any]:
    captured: list[Any] = []

    def execute(rows: list[Any]) -> int:
        captured.extend(rows)
        return len(rows)

    seeder = Seeder(get_contract(), execute, storm_after_h=storm_after_h,
                    instrument_faults=[], end_ts=end_ts)
    seeder.run_days(DAYS)
    return captured


@pytest.fixture(scope="module")
def stormy() -> list[Any]:
    return _run(end_ts=END)


def _series(rows: list[Any], signal: str) -> list[tuple[float, float]]:
    return [(r[0].timestamp(), r[2]) for r in rows
            if r[1] == signal and r[2] is not None]


def test_the_storm_actually_fires(stormy: list[Any]) -> None:
    """Influent flow inside the storm window exceeds a **no-storm control run**.

    A control, because the first version compared "during" against "before" and
    passed under the bug: the plant's own daily cycle lifts influent flow by more
    than the threshold over six hours, so a storm that never happened still
    looked like one. The model is deterministic, so the same seed without a storm
    is an exact baseline and any difference is the storm's.

    Asserts the *effect*, not the arming. The bug was in the arithmetic that
    positioned the storm, so "a fault was armed" was true throughout — armed, with
    an empty window.
    """
    control = _run(end_ts=END, storm_after_h=None)
    storm_start = END - 2 * 3600 + 1800          # 30 min in, past the ramp's start

    def window_mean(rows: list[Any]) -> float:
        values = [v for t, v in _series(rows, "INFLUENT:FLOW:FLOW")
                  if t >= storm_start]
        assert values, "no flow readings inside the storm window"
        return sum(values) / len(values)

    assert window_mean(stormy) > 1.10 * window_mean(control), (
        f"storm window mean flow {window_mean(stormy):.0f} vs control "
        f"{window_mean(control):.0f}: the storm did not fire"
    )


def test_the_storm_moves_its_own_tracer(stormy: list[Any]) -> None:
    """`INFLUENT:FLOW:CONDUCTIVITY` is the scenario's declared cheap tracer.

    Under the broken arithmetic it had a single row for the whole run, because a
    change-triggered historian stores a signal only when it moves and nothing did.
    More than one row is the smallest possible evidence that something moved.
    """
    rows = _series(stormy, "INFLUENT:FLOW:CONDUCTIVITY")
    assert len(rows) > 1, (
        "the storm's own tracer has one row; the storm did not fire"
    )


def test_the_window_ends_where_it_is_told_to(stormy: list[Any]) -> None:
    """`end_ts` pins the last timestamp, and the first is `days` before it."""
    times = [r[0].timestamp() for r in stormy]
    assert max(times) < END
    assert max(times) > END - 5
    assert min(times) == pytest.approx(END - DAYS * 86_400.0, abs=1.0)


def test_two_seeds_with_the_same_end_are_identical(stormy: list[Any]) -> None:
    """Reproducibility is the property the notebooks' numbers rest on."""
    again = _run(end_ts=END)
    assert len(again) == len(stormy)
    assert again == stormy


def test_without_a_pinned_end_the_window_follows_the_clock() -> None:
    """The default is unchanged: the SQL course's `now()` queries depend on it."""
    import time  # noqa: PLC0415

    before = time.time()
    rows = _run(end_ts=None, storm_after_h=None)
    latest = max(r[0].timestamp() for r in rows)
    assert before - 5 < latest < time.time() + 1
