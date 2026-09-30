"""The instrument-fault schedule, and the promise that two callers agree on it.

`storage/seed/schedule.py` exists so the seeder and the ground truth derive the
fault schedule from one function. Before it, `notebooks/_data.py::known_events()`
rebuilt the schedule from its own arithmetic while the seeder built it from
`DEFAULT_INSTRUMENT_FAULTS` — a dataset whose answer key is one edit away from
being silently wrong, and nothing in the build would notice, because the readings
would seed correctly and only the labels would be stale.

So the first test here is the one that matters and the rest are arithmetic: **the
schedule the seeder arms and the schedule the ground truth reports are the same
function's output.** The second is that this is a no-op for the pinned week, which
eleven notebooks' claimed numbers depend on.

Everything else in the file guards the two ways a recurring schedule can be wrong
without looking wrong: instances that arm past the end of the replay and produce no
rows, and instances that overlap and double a fault's effect.
"""

from __future__ import annotations

import pytest
from storage.seed.schedule import (
    DEFAULT_INSTRUMENT_FAULTS,
    event_windows,
    minimum_interval_s,
    recurring_faults,
)

WEEK_S = 7 * 86_400.0
FLOOR = minimum_interval_s()


# ── the two callers must not disagree ────────────────────────────────────────


def test_the_ground_truth_is_the_seeder_schedule() -> None:
    """`event_windows` is a view of `recurring_faults`, not a second implementation.

    The whole reason this module exists. If the two were derived separately they
    would agree today and diverge the first time somebody edited a duration, and the
    divergence would be *invisible from the data* — the readings are seeded from
    one schedule and labelled from the other, so every detector scored against it
    is confidently wrong.
    """
    every_s = 36 * 3600.0
    armed = recurring_faults(WEEK_S, every_s)
    windows = event_windows(WEEK_S, every_s)

    assert len(armed) == len(windows)
    for spec, (fault, onset, ends, _nth) in zip(armed, windows, strict=True):
        assert fault == spec["fault"]
        # A fraction of the run, converted back to seconds. Round-tripped through
        # float, so compared with a tolerance rather than for equality.
        assert onset == pytest.approx(spec["at_fraction"] * WEEK_S, abs=1e-6)
        assert ends == pytest.approx(onset + spec["duration_s"], abs=1e-6)


def test_the_default_schedule_is_unchanged_by_this_module() -> None:
    """`event_windows(..., None)` reproduces what `known_events()` reads today.

    The pinned week is the constraint that makes every other test in this file
    allowed to exist: if moving the constant out of `main.py` had altered it, all
    eleven notebooks would fail on their claimed numbers.
    """
    specs = list(DEFAULT_INSTRUMENT_FAULTS)
    assert [s["at_fraction"] for s in specs] == [0.30, 0.55, 0.78]
    assert [s["duration_s"] for s in specs] == [5400.0, 7200.0, 3600.0]

    windows = event_windows(WEEK_S, None)
    assert [w[0] for w in windows] == [
        "do_sensor_drift", "effluent_tss_stuck", "sensor_dead",
    ]
    assert windows[0][1] == pytest.approx(0.30 * WEEK_S)
    assert windows[0][2] - windows[0][1] == pytest.approx(5400.0)


# ── determinism ──────────────────────────────────────────────────────────────


def test_the_schedule_is_deterministic() -> None:
    """Two calls, equal results. No random source anywhere in the module.

    Not a style assertion. A workshop whose fault schedule moved between cohorts
    could not be compared, and the ground truth would have to be regenerated with
    the data every time — so the determinism is load-bearing for anything built on
    this.
    """
    first = recurring_faults(WEEK_S, 36 * 3600.0)
    second = recurring_faults(WEEK_S, 36 * 3600.0)
    assert first == second


def test_the_kinds_cycle_in_the_order_they_are_declared() -> None:
    """Instance *n* is `base[n % 3]`, so variety comes from rotation not sampling.

    With an ordered rotation the sequence of fault kinds for a 12-instance window is
    fixed, which is what lets `nth` in `event_windows` be a usable label.
    """
    got = [s["fault"] for s in recurring_faults(WEEK_S, FLOOR)]
    kinds = [s["fault"] for s in DEFAULT_INSTRUMENT_FAULTS]
    assert got[:6] == kinds[:3] * 2
    assert all(got[i] == kinds[i % 3] for i in range(len(got)))


def test_the_first_instance_is_one_interval_in() -> None:
    """Not at zero: a schedule that faults immediately gives no healthy baseline.

    A detector scored on a window that opens mid-fault has nothing to be right
    about, and a reader has no ordinary plant to compare against.
    """
    every_s = 36 * 3600.0
    first = recurring_faults(WEEK_S, every_s)[0]
    assert first["at_fraction"] * WEEK_S == pytest.approx(every_s)


# ── the two ways a recurring schedule is wrong without looking wrong ─────────


def test_an_instance_that_would_outrun_the_window_is_dropped_not_truncated() -> None:
    """No rows behind the label, and no label shortened to fit.

    An instance armed past the end of the replay produces **zero rows**, which is
    the same failure the storm schedule once had and recorded: *"a fault whose
    window is empty is a fault that simply never happens."* Truncating instead
    would arm it for part of its duration and label it with all of it — a fault
    that is silently shorter than the answer key says.

    ## Two intervals, and the second one is the point

    The obvious case — 36 h, which is what the workshop plan uses — **cannot reach
    the branch this test is about.** The onsets run 129 600, 259 200, 388 800,
    518 400 and the loop stops at 648 000, past the end of a 604 800 s week. The
    last instance ends at 523 800 s, comfortably inside, so nothing is ever dropped
    and the truncation path is dead code for that input.

    Two mutations confirmed it: truncating the tail instead of dropping it passed
    every assertion, twice, on a test written specifically to catch it. The
    interval has to put an onset inside `(WEEK - longest duration, WEEK)`, which
    for a week and a 7 200 s fault means somewhere below 150 000 s per gap's
    multiples — 150 000 does it, landing the fourth instance at 600 000 s.
    """
    declared = {s["fault"]: s["duration_s"] for s in DEFAULT_INSTRUMENT_FAULTS}

    # A gap that *does* overrun, so the branch is reached at all.
    every_s = 150_000.0
    schedule = recurring_faults(WEEK_S, every_s)
    overrun_onset = 4 * every_s
    assert WEEK_S - 7200 < overrun_onset < WEEK_S, (
        f"this test's interval no longer lands an onset inside the overrun band, "
        f"so the drop path is not being exercised: onset={overrun_onset}"
    )
    assert len(schedule) == 3, (
        f"the fourth instance starts at {overrun_onset:.0f}s and would run past "
        f"{WEEK_S:.0f}s, so it must be dropped; got {len(schedule)} instances"
    )

    for spec in schedule:
        onset = spec["at_fraction"] * WEEK_S
        assert onset + spec["duration_s"] <= WEEK_S, (
            f"{spec['fault']} ends at {onset + spec['duration_s']:.0f}s in a "
            f"{WEEK_S:.0f}s window, so it would be labelled with no data behind it"
        )
        assert spec["duration_s"] == declared[spec["fault"]], (
            f"{spec['fault']} was scheduled for {spec['duration_s']}s but declares "
            f"{declared[spec['fault']]}s. A truncated instance is a fault that is "
            f"shorter than its own label."
        )

    # And the same invariants hold for the interval the workshop will use.
    for spec in recurring_faults(WEEK_S, 36 * 3600.0):
        assert spec["duration_s"] == declared[spec["fault"]]


def test_a_window_shorter_than_one_interval_schedules_nothing() -> None:
    """Empty, not an error and not a partial schedule."""
    assert recurring_faults(3600.0, 36 * 3600.0) == []


def test_an_overlapping_gap_is_refused_because_drift_compounds() -> None:
    """`sensor_drift` applies to the *already biased* value, so overlap doubles it.

    The fault engine runs every active instance in sequence over the same published
    value, and `_corrupt` computes `clamp(true_value + bias)` from whatever is
    there. Two overlapping drifts therefore bias the reading twice. The other two
    injectors are idempotent, so the threshold is taken from the longest duration
    anyway — a rule that held for some kinds and not others is a rule nobody can
    predict.
    """
    with pytest.raises(ValueError, match="overlap"):
        recurring_faults(WEEK_S, FLOOR - 1.0)
    assert recurring_faults(WEEK_S, FLOOR), (
        "the exact longest duration must still be allowed: instances may touch "
        "without overlapping, and refusing it would be needlessly strict"
    )


def test_a_non_positive_interval_is_refused() -> None:
    """Zero would otherwise spin: `while True` with a zero step never terminates."""
    for bad in (0.0, -3600.0):
        with pytest.raises(ValueError, match="positive"):
            recurring_faults(WEEK_S, bad)


def test_nth_distinguishes_repeated_occurrences_of_one_kind() -> None:
    """A recurring schedule has several of each kind, and they need telling apart.

    `known_events()` cannot carry this — it is a dict keyed by fault name, so a
    second occurrence of `sensor_dead` would silently overwrite the first, and the
    ground truth would describe one event where the data has two. That is why
    `event_windows` returns a list and keeps `known_events()` unchanged.
    """
    windows = event_windows(WEEK_S, 36 * 3600.0)
    per_kind: dict[str, list[int]] = {}
    for fault, _onset, _ends, nth in windows:
        per_kind.setdefault(fault, []).append(nth)
    assert all(nths == list(range(len(nths))) for nths in per_kind.values())
    assert max(len(v) for v in per_kind.values()) > 1, (
        "the test needs a kind to recur, or it is checking nothing"
    )
    # Onsets strictly increase, so the list is ordered in time.
    onsets = [w[1] for w in windows]
    assert onsets == sorted(onsets)
    assert len(set(onsets)) == len(onsets)
