"""What an alarm *is*, as three small types with no database in sight.

The whole engine is built on one decision: **a detector is a pure function from a
window of readings to a verdict**, and the state machine that turns verdicts into
alarms is separate. That split is why this module has no imports from `storage`
and why `detectors.py` is testable with hand-built windows.

The alternative — an alarm rule that reaches into the database, evaluates, decides
and records — is the shape almost alarm systems have, and it is why they are hard
to test and harder to reason about: you cannot ask "would this rule have fired?"
about a fault you ran last Tuesday without re-running last Tuesday.

## The vocabulary

`contracts/fault-scenarios.yaml` names, per fault, how the fault *is* detectable
and how it is **not**. That vocabulary is the specification and it is enforced:

===============  ==========================================================
`single_point_threshold`   the value crosses a limit
`deviation_from_baseline`  the value departs from what it normally does
`trend`                    sustained movement in one direction
`rate_of_change`           the derivative exceeds a limit
`state_change`             equipment run state flips
`flatline_detection`       the value stops moving
`expected_sample_count`    the number of readings in a window is wrong
`cross_validation`         two independent measurements disagree
`ratio_derived_alarm`      a computed ratio leaves its band
`model_based`              the measurement departs from a model prediction
`correlation`              two signals stop co-moving
`oscillation_detection`    the value cycles
===============  ==========================================================

`DETECTORS` is the set this project implements. Two of the twelve —
`correlation` and `oscillation_detection` — are **declared in the contract and not
implemented**, and `coverage.py` reports that as a gap rather than pretending
otherwise. A vocabulary you claim and a vocabulary you have are different things
and the difference should be visible.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Every detection method the contract names. Used to validate rules at load.
DETECTION_VOCABULARY: frozenset[str] = frozenset({
    "single_point_threshold",
    "deviation_from_baseline",
    "trend",
    "rate_of_change",
    "state_change",
    "flatline_detection",
    "expected_sample_count",
    "quality_flag",
    "cross_validation",
    "ratio_derived_alarm",
    "model_based",
    "correlation",
    "oscillation_detection",
})

#: The ones that exist as code. See `DETECTION_VOCABULARY` for the rest.
DETECTORS: frozenset[str] = frozenset(DETECTION_VOCABULARY - {
    "correlation",
    "oscillation_detection",
})

#: The three severities, matching the `event.severity` CHECK constraint. A word
#: rather than a number, because it is queried by meaning — "everything critical
#: this week" — and a numeric scale nobody remembers the order of gets queried
#: backwards.
SEVERITIES: tuple[str, ...] = ("info", "warning", "critical")

#: Rule states. `latched` is the one that surprises people: a critical alarm
#: stays on the operator's list until someone acknowledges it, even after the
#: condition has cleared. An alarm that disappears by itself is an alarm nobody
#: has to act on, which is indistinguishable from an alarm that never fired.
ACTIVE = "active"
ACKNOWLEDGED = "acknowledged"
CLEARED = "cleared"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Sample:
    """One reading, as the detectors see it."""

    ts: float                 # epoch seconds
    value: float | None       # None means no value; see quality
    quality: int = 0          # 0 Good, 1 Uncertain, 2 Bad

    @property
    def usable(self) -> bool:
        """Whether this sample can support a decision.

        Two reasons to say no, and they are different: a Bad reading is a *known*
        failure and is a finding in its own right, while a `NULL` value is the
        representation of that same thing (see
        ``sql/00-foundations/00-03_quality_is_data.md``). A rule that silently
        skipped Bad readings would be blind to exactly the faults that produce
        them.
        """
        return self.value is not None and self.quality == 0


@dataclass(frozen=True, slots=True)
class Window:
    """The readings a rule is allowed to look at, and the clock they are on.

    ``now`` is explicit rather than taken from `time.time()`. A detector that
    read the wall clock itself would not be a pure function, and the difference
    between "the condition held for four minutes" and "the condition held" is
    most of what a dwell time is for.
    """

    signal_id: str
    #: Any sequence of `Sample`. Not necessarily a tuple: a caller holding a
    #: `deque` should not pay to copy it, and a rule's lookback is applied before
    #: the detectors iterate rather than after.
    points: Sequence[Sample]
    now: float

    @property
    def usable(self) -> tuple[Sample, ...]:
        return tuple(p for p in self.points if p.usable)

    @property
    def span_s(self) -> float:
        """Wall-clock seconds covered. Not `len(points)`.

        This distinction is not academic. `AERATION:AHU-1:DO` produces about one
        reading every six minutes in a seeded week, because the deadband is
        0.02 mg/L and the basin drifts by hundredths. A dwell time expressed in
        samples would therefore mean something completely different for DO than
        for air flow, and the alarm with the "same" 5-sample dwell would be
        thirty times slower on one signal than the other. Dwell is in seconds,
        always.
        """
        if len(self.points) < 2:
            return 0.0
        return self.points[-1].ts - self.points[0].ts

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(p.value for p in self.usable if p.value is not None)

    @property
    def last(self) -> Sample | None:
        return self.usable[-1] if self.usable else None

    def since(self, seconds: float) -> tuple[Sample, ...]:
        """The points within ``seconds`` of ``now``."""
        cutoff = self.now - seconds
        return tuple(p for p in self.points if p.ts >= cutoff)

    def recent(self, seconds: float) -> Window:
        """A trimmed view of the same window, holding only the last ``seconds``.

        Every rule wants a different amount of history and a `trend` with
        `min_points = 4` wants four points, not four hours of them. Trimming per
        rule is what keeps the evaluation cost proportional to what the rule
        actually reads: without it, a rule with a two-hour lookback made every
        other rule iterate two hours of samples too.
        """
        if seconds <= 0 or not self.points:
            return self
        # If the window does not reach back that far anyway, the filtered tuple
        # would be the same list. Returning `self` skips the copy, and for the
        # slow signals — dissolved oxygen arrives every six minutes — that is
        # most of the time.
        if self.span_s <= seconds:
            return self
        cutoff = self.now - seconds
        return Window(
            signal_id=self.signal_id,
            points=tuple(p for p in self.points if p.ts >= cutoff),
            now=self.now,
        )

    def has_coverage(self, seconds: float, tolerance: float = 0.5) -> bool:
        """Whether the window actually covers the last ``seconds``.

        The distinction between *"the value is normal"* and *"we have not heard
        from this signal in an hour"* is the difference between a healthy plant
        and a failed instrument, and it is invisible unless asked for. See
        `expected_sample_count` in `detectors.py`.
        """
        if not self.points:
            return False
        newest = max(p.ts for p in self.points)
        return (self.now - newest) <= seconds * (1.0 + tolerance)


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a detector concluded, and why.

    `active` is the current truth. `observed` and `detail` are for the human: an
    alarm that says "DO 1.31 mg/L (limit 1.50)" is actionable and one that says
    "threshold exceeded" is not.
    """

    active: bool
    observed: float | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, observed: float | None = None, **detail: Any) -> Verdict:
        return cls(False, observed, detail)

    @classmethod
    def firing(cls, observed: float | None = None, **detail: Any) -> Verdict:
        return cls(True, observed, detail)


def verdict(active: bool, observed: float | None = None,
            **detail: Any) -> Verdict:
    """Build a `Verdict` from loose keyword arguments.

    `Verdict` takes a *mapping* for its detail, and the natural way to write a
    detector is `Verdict(active, value, limit=limit, tolerance=…)`. Those two do
    not meet: the keywords land on `__init__`, which takes `detail`, and every
    detector in the file raises `TypeError` on its first call. This is the
    function that makes the readable form work, and it exists because the
    unreadable alternative was tried first.
    """
    return Verdict(active, observed, detail)


@dataclass(frozen=True, slots=True)
class AlarmRule:
    """One thing worth knowing, stated so it can be checked.

    A rule is a *claim* about the plant. `detects` names the faults it is meant
    to catch, and `coverage.py` tests that claim against seeded history — which
    is the difference between an alarm system and a list of thresholds.
    """

    id: str
    signal_id: str
    detector: str
    severity: str
    #: Seconds the condition must hold before the alarm raises. This is the
    #: mechanism behind the contract's central finding: a delayed fault is not
    #: detectable by a fast detector, and `for_s` is where "fast" is decided.
    for_s: float = 0.0
    #: Seconds the condition must be false before the alarm clears. Always
    #: larger than zero, because a rule with no clear hysteresis flaps, and a
    #: flapping alarm is one operators learn to ignore — which is the same as
    #: not having it.
    clear_s: float = 300.0
    params: Mapping[str, Any] = field(default_factory=dict)
    message: str = ""
    equipment_id: str | None = None
    #: Fault ids this rule is intended to catch. Checked against the contract.
    detects: tuple[str, ...] = ()
    #: Free text, for the coverage report.
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.detector not in DETECTION_VOCABULARY:
            raise ValueError(
                f"{self.id}: detector {self.detector!r} is not in the contract's "
                f"vocabulary. Valid: {sorted(DETECTION_VOCABULARY)}"
            )
        if self.detector not in DETECTORS:
            raise ValueError(
                f"{self.id}: detector {self.detector!r} is declared in the "
                f"contract but not implemented. Implemented: {sorted(DETECTORS)}"
            )
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"{self.id}: severity {self.severity!r} not in {SEVERITIES}"
            )
        if self.for_s < 0 or self.clear_s <= 0:
            raise ValueError(f"{self.id}: for_s must be >= 0 and clear_s > 0")
        if not self.message:
            raise ValueError(f"{self.id}: a rule with no message pages nobody")

    @property
    def implemented(self) -> bool:
        return self.detector in DETECTORS

    def lookback_s(self) -> float:  # noqa: PLR0911 - a dispatch, not a decision
        """How much history this rule needs, and so how much it should be given.

        Derived from what the detector reads rather than declared separately,
        because a second number to keep in step with the first is a second thing
        to get wrong. A rule that is given *more* history than it needs is merely
        wasteful; a rule given *less* is wrong, and the failure is silent.
        """
        p = self.params
        if self.detector == "flatline_detection":
            return max(
                float(p.get("max_silence_s", 0.0)),
                float(p.get("stuck_s", 0.0)) * 2.0,
            )
        if self.detector == "expected_sample_count":
            return float(p.get("horizon_s", 0.0)) * 2.0
        if self.detector == "trend":
            # `min_span_s` is not optional and cannot be dropped from this
            # maximum: a rule asked for a two-hour span and handed an hour of
            # window can never satisfy it, and would report "not enough history"
            # forever. The window has to be at least as long as the condition the
            # rule is asking about.
            return max(
                self.for_s * 2.0,
                float(p.get("min_span_s", 0.0)) * 1.5,
                float(p.get("min_points", 4)) * 60.0,
                900.0,
            )
        if self.detector in ("deviation_from_baseline", "model_based"):
            return max(self.for_s * 2.0, 3600.0)
        if self.detector == "cross_validation":
            return max(self.for_s * 2.0, 900.0)
        if self.detector == "state_change":
            return 600.0
        return max(self.for_s * 2.0, 600.0)


def slope_per_hour(window: Window) -> float | None:
    """Least-squares slope in units per hour, or ``None`` if there is too little.

    Centred on the window's own midpoint so the intercept is meaningful and the
    slope does not depend on *where* the window starts — a slope fitted against
    absolute epoch seconds is fine numerically at 1.7e9 but is not something you
    can reason about.
    """
    usable = window.usable
    if len(usable) < 3:
        return None
    t0 = usable[0].ts
    times = [p.ts - t0 for p in usable]
    values = [p.value for p in usable if p.value is not None]
    if len(values) < 3:
        return None
    # Fitted directly against *seconds*, not against sample index. This looks
    # like the kind of arithmetic that wants a helper, and there was one: a
    # `linear()` that fitted against index, whose only caller computed the result
    # and then discarded it in favour of the four lines below. It is gone, and
    # the reasoning is here so the next person does not add it back.
    mean_t = sum(times) / len(times)
    mean_v = sum(values) / len(values)
    num = sum(
        (t - mean_t) * (v - mean_v)
        for t, v in zip(times, values, strict=True)
    )
    den = sum((t - mean_t) ** 2 for t in times)
    if den == 0 or math.isnan(num) or math.isnan(den):
        return None
    return (num / den) * 3600.0
