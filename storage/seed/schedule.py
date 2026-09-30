"""When each instrument fault fires, and the one place that decides.

    from storage.seed.schedule import DEFAULT_INSTRUMENT_FAULTS, recurring_faults

**This module exists so the seeder and the ground truth cannot disagree.** A dataset
whose fault schedule is written down in two places and derived twice is a dataset
whose labels are wrong the moment one of them is edited, and nothing in the build
notices: the readings are seeded correctly and the answer key is quietly stale. That
is the worst possible failure in a teaching dataset, and it is exactly what would
have happened if `notebooks/_data.py::known_events()` kept rebuilding the schedule
from its own arithmetic while the seeder built it from this one.

So: the seeder arms what `recurring_faults()` returns, the ground truth is
`event_windows()` over the *same* call, and neither recomputes anything.

## Why the default three are here and not in ``main``

They moved so that this module is self-contained — it can answer "when does this
fault fire" without importing the seeder, which imports psycopg, the contract and
the plant. `storage.seed.main` re-exports the name, so every existing import keeps
working and nothing outside has to know this file exists.

## What a "recurring" fault schedule is for

The ML workshop in `/Users/dev/.opencode/plan/ml-workshop.md` needs more labelled
events than one week contains. A week has three, and a classifier cannot be taught
on three.

**The obvious fix is wrong, and it is wrong in a way worth writing down.** The
seeder replays a *deterministic* plant — no `random`, `normal` or `uniform` anywhere
in the path, and `Plant.rng` is never read — so seeding twenty-five times gives the
same trajectory twenty-five times, shifted in time. That is not twenty-five
independent weeks, it is one week with a shifted clock, and any "does it generalise
to a new week" result measured on it would be a fiction.

What *is* available is variation in **where and when the plant is broken**. The
plant is one plant; the fault schedule is the thing that can repeat, and repeating
it produces genuinely different event instances: different target, different time of
day, different position in the daily cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

#: Instrument faults armed partway through a seeded run.
#:
#: Three faults, three different shapes, deliberately spread across the week: a
#: drift that a threshold cannot see, a flatline that every naive health check
#: passes, and a dead sensor. `at_fraction` is a fraction of the run rather than a
#: timestamp, so the spread follows `--days` instead of being pinned to a date.
#:
#: **The order matters**, because `recurring_faults` cycles through it. Instance 0 is
#: a drift, instance 1 a flatline, instance 2 a dead sensor, instance 3 a drift
#: again — so a recurring schedule presents the kinds in a fixed, reproducible
#: rotation rather than in whatever order a set happened to iterate.
DEFAULT_INSTRUMENT_FAULTS: list[dict[str, Any]] = [
    {"fault": "do_sensor_drift", "at_fraction": 0.30, "duration_s": 5400.0},
    {"fault": "effluent_tss_stuck", "at_fraction": 0.55, "duration_s": 7200.0},
    {"fault": "sensor_dead", "at_fraction": 0.78, "duration_s": 3600.0},
]


def minimum_interval_s(base: Sequence[dict[str, Any]] | None = None) -> float:
    """The shortest gap that keeps two instances of the same fault from overlapping.

    Not a style rule. **`sensor_drift` compounds**, and the fault engine applies every
    active instance to the same published value in sequence, so two overlapping
    drifts bias the reading twice:

        bias = bias_max * intensity
        snap.values[target] = clamp(true_value + bias)

    The second instance reads the *already-biased* value, so the bias doubles (until
    it saturates at the signal's range). The other two injectors are idempotent —
    `sensor_dead` sets ``None`` and stays ``None``, and a flatline re-freezes at the
    value it already froze — so only the drift makes overlap a correctness problem.

    One threshold for all three kinds anyway, because a schedule that permits
    overlap for some faults and forbids it for others is a schedule nobody can
    predict, and the cost of the conservative rule is one flag argument.
    """
    specs = DEFAULT_INSTRUMENT_FAULTS if base is None else list(base)
    return max(float(spec["duration_s"]) for spec in specs)


def recurring_faults(
    total_s: float,
    every_s: float,
    base: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """A recurring instrument-fault schedule, as `at_fraction` specs.

    `total_s` is the window length and `every_s` the gap between the *onsets* of
    successive instances. The result is in the same shape as
    `DEFAULT_INSTRUMENT_FAULTS`, so the seeder's arming loop is untouched by this
    whole change.

    **Deterministic, with no random source anywhere.** Two calls with the same
    arguments return equal lists, and the schedule for a given window is the same on
    every machine and every run. That is not a nicety: a workshop whose fault
    schedule moved between cohorts could not be compared, and the ground truth
    would have to be regenerated with the data every time. The kinds cycle in the
    order of `base`, so variety comes from the *rotation* rather than from sampling.

    ## The first instance is one interval in, not at zero

    A schedule that starts faulting immediately gives a detector nothing healthy to
    be right about, and gives the reader no baseline. Starting at `every_s` means
    every recurring window opens with a full interval of ordinary plant.

    ## Instances that would not finish are dropped, not clamped

    An instance whose onset plus duration runs past the end of the window arms
    against a clock that has stopped: it produces **zero rows**, which is the same
    failure the storm schedule once had and recorded — *"a fault whose window is
    empty is a fault that simply never happens."* Truncating it instead would arm a
    fault for a fraction of its intended duration and label it with the full one, so
    the ground truth would overstate it. The tail is simply not scheduled, and
    `len(result)` says how many survived.

    ## Overlap is rejected rather than tolerated

    `every_s` below `minimum_interval_s()` raises. See that function for why the
    drift injector specifically makes this a correctness question rather than a
    taste one.

    ## A fault may still land on the storm, and that is deliberate

    Nothing here avoids the wet-weather window, and the two can overlap. For the
    pinned week that cannot happen — the three defaults sit at 0.30, 0.55 and 0.78
    of a week and the storm is armed two hours from the end — but on a dense
    recurring schedule it can.

    It is left to the caller rather than avoided here, because a schedule that
    silently dodges the storm is a schedule whose event count is not the number you
    asked for, and a caller building a labelled set needs to *know* an event is
    ambiguous rather than have it removed. The workshop builder marks those
    instances; this module's only promise is that what it returns is what fires.
    """
    if every_s <= 0:
        raise ValueError(f"--fault-every-hours must be positive, got {every_s}")
    specs = DEFAULT_INSTRUMENT_FAULTS if base is None else list(base)
    if not specs:
        return []

    floor = minimum_interval_s(specs)
    if every_s < floor:
        raise ValueError(
            f"a gap of {every_s:g}s would overlap two instances of the same "
            f"fault; the longest runs for {floor:g}s. sensor_drift compounds when "
            f"it overlaps, so the second instance would bias the reading twice. "
            f"Use a gap of at least {floor / 3600:g}h."
        )

    scheduled: list[dict[str, Any]] = []
    index = 0
    while True:
        onset = (index + 1) * every_s
        if onset >= total_s:
            break
        spec = specs[index % len(specs)]
        if onset + float(spec["duration_s"]) > total_s:
            # Runs past the end of the replay. Arming it would produce no rows and
            # a label with no data behind it; stopping here drops it and every later
            # one with it, since the schedule is strictly increasing in onset.
            break
        scheduled.append({
            "fault": spec["fault"],
            "at_fraction": onset / total_s,
            "duration_s": float(spec["duration_s"]),
        })
        index += 1
    return scheduled


def event_windows(
    total_s: float,
    every_s: float | None,
    base: Sequence[dict[str, Any]] | None = None,
) -> list[tuple[str, float, float, int]]:
    """Every instrument fault that will fire, as `(fault, onset_s, ends_s, nth)`.

    The ground truth, derived from `recurring_faults` rather than from any
    arithmetic of its own. `every_s=None` gives the three defaults, which is what the
    pinned week uses and what `notebooks/_data.py::known_events` reads.

    `nth` is the zero-based instance number of that fault kind, because a recurring
    schedule has several occurrences of one kind and a caller needs to tell them
    apart — and because an index into the rotation is a more stable label than a
    timestamp, which moves when the window does.
    """
    specs = (
        list(DEFAULT_INSTRUMENT_FAULTS if base is None else base)
        if every_s is None
        else recurring_faults(total_s, every_s, base)
    )
    seen: dict[str, int] = {}
    windows: list[tuple[str, float, float, int]] = []
    for spec in specs:
        fault = str(spec["fault"])
        onset = float(spec["at_fraction"]) * total_s
        nth = seen.get(fault, 0)
        seen[fault] = nth + 1
        windows.append((fault, onset, onset + float(spec["duration_s"]), nth))
    return windows
