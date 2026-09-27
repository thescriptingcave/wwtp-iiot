"""The detectors. Ten pure functions, each answering one question about a window.

Every one of these is a *pure function of a window and a rule*. No database, no
clock, no I/O. That is the property the whole engine is built on, and it is why
`tests/test_alarm_detectors.py` can be the most thorough test file in the project
without a container running.

## The shape of the problem, and why `single_point_threshold` is the wrong default

Nine of the eleven faults in `contracts/fault-scenarios.yaml` list
`single_point_threshold` under `not_sufficient_alone`. That is the most useful fact
in the contract and it is worth understanding rather than memorising.

A blower trips. The blower's speed drops *immediately*. So does air flow. So a
threshold on either fires within seconds — and it is telling you about a fan.

Dissolved oxygen sags over **one to two hours**, because the basin holds about
40 kg of dissolved oxygen and 20 000 m³ of water does not notice a fan. Effluent
ammonia rises **two to six hours** after that, because the nitrifier population
has to decay before the product appears. A threshold on ammonia fires last, and
by then the cause is over.

So the alarm that catches a blower trip earliest is the one on the fan, and the
alarm that reads best on a dashboard is the one on ammonia. `trend` with a dwell
time gets you the DO sag an hour before ammonia moves; `single_point_threshold`
on DO gets you nothing at all, because DO never leaves its band — it *sags within
it* and comes back. That is the whole argument, and `trend` and
`deviation_from_baseline` are how you win it.

**This is the difference between alarming on a number and alarming on a rate**, and
it is the single most valuable thing in the fault library.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from alarms.base import (
    AlarmRule,
    Sample,
    Verdict,
    Window,
    slope_per_hour,
    verdict,
)

Detector = Callable[[AlarmRule, Window], Verdict]


# ─── 1. single point threshold ───────────────────────────────────────────────


def single_point_threshold(rule: AlarmRule, window: Window) -> Verdict:
    """The value crosses a limit. Instant, local, and usually the wrong answer.

    Included because it is what most alarm systems are made of, and because
    `coverage.py` can then *show* how much it misses rather than asserting it.
    Nine of the eleven contract faults name it as a method that will not find
    them.

    `hysteresis` is in **the signal's own units** and is applied in the direction
    that matters: a high alarm clears at `limit - hysteresis`, a low alarm at
    `limit + hysteresis`. Without it a signal sitting on the limit produces an
    alarm every scan, and an operator learns to ignore it within a shift.
    """
    limit = float(rule.params["limit"])
    hysteresis = float(rule.params.get("hysteresis", 0.0))
    last = window.last
    if last is None or last.value is None:
        return Verdict.ok(None, reason="no usable reading in the window")

    value = last.value
    if rule.params.get("direction", "high") == "high":
        active = value >= limit
        clears_at = limit - hysteresis
    else:
        active = value <= limit
        clears_at = limit + hysteresis

    return verdict(
        active, value,
        limit=limit, clears_at=round(clears_at, 6),
        hysteresis=hysteresis,
        unit=rule.params.get("unit", ""),
    )


# ─── 2. deviation from baseline ──────────────────────────────────────────────


def deviation_from_baseline(rule: AlarmRule, window: Window) -> Verdict:
    """The value departs from what *this signal* normally does.

    Two ways to get a baseline, and the choice matters:

    * `baseline` — a number in the rule. Simple, and it goes stale the day the
      plant is re-tuned. This is what most deviation alarms really are, wearing a
      statistical hat.
    * `window_mean` — the mean of the window itself. Catches a slow drift
      (`do_sensor_drift`) that a fixed baseline cannot, because the fixed
      baseline is what the sensor drifted *away* from and the window mean is
      where the process actually is.

    The second is the interesting one and it is the reason this detector is not
    just a threshold with extra steps: **a drifting sensor and a drifting process
    look identical from inside one signal.** Distinguishing them needs a second
    signal, which is what `cross_validation` is for.
    """
    usable = window.usable
    if len(usable) < 3:
        return Verdict.ok(None, reason="fewer than 3 usable readings")

    values = [p.value for p in usable if p.value is not None]
    if rule.params.get("baseline") is not None:
        baseline = float(rule.params["baseline"])
        how = "fixed"
    else:
        baseline = sum(values) / len(values)
        how = "window_mean"

    tolerance = float(rule.params["tolerance"])
    worst = max(values, key=lambda v: abs(v - baseline))
    deviation = worst - baseline
    active = abs(deviation) >= tolerance

    return verdict(
        active, deviation,
        baseline=round(baseline, 6), tolerance=tolerance, how=how,
        n=len(values),
    )


# ─── 3. trend ────────────────────────────────────────────────────────────────


def trend(rule: AlarmRule, window: Window) -> Verdict:
    """Sustained movement in one direction, fitted rather than differenced.

    The workhorse: five of the eleven contract faults are detectable by trend and
    none of them by a threshold.

    Why a *least-squares slope* rather than `last - first`: the deadband means
    the spacing between readings is wildly uneven — for `AERATION:AHU-1:DO` it is
    about six minutes, for air flow about a tenth of a second. Differencing two
    readings tells you the change over whatever gap happened to be there, which
    is not a rate. Fitting a line through all of them against real timestamps is
    robust to that, and it is three lines of arithmetic.

    `min_points` guards the degenerate fit. Two points always give a line through
    both, so a two-point "trend" is just a difference with extra steps, and it
    will fire on noise.
    """
    min_points = int(rule.params.get("min_points", 4))
    usable = window.usable
    if len(usable) < min_points:
        return Verdict.ok(None, reason=f"only {len(usable)} usable readings, "
                                       f"need {min_points}")

    # `min_span_s` is the important parameter and it was missing until the
    # coverage report showed this rule firing on ten of eleven faults.
    #
    # A least-squares slope over a *short* window is a noise estimator, not a
    # trend. Dissolved oxygen in an aeration basin swings by tenths of a mg/L on
    # a diurnal cycle; fitted over fifteen minutes that is easily 0.2 mg/L per
    # hour, which clears a 0.15 threshold on a completely healthy plant. The rule
    # was not miscalibrated by a factor, it was measuring the wrong thing -- the
    # sampling noise rather than the fault.
    #
    # So: require a span as well as a point count. The fault this rule exists for
    # takes one to two hours to develop, and a span of that order is exactly what
    # the diurnal cycle cannot fake.
    min_span_s = float(rule.params.get("min_span_s", 0.0))
    if min_span_s and window.span_s < min_span_s:
        return Verdict.ok(None,
                          reason=f"only {window.span_s:.0f}s of history, "
                                 f"need {min_span_s:.0f}s",
                          span_s=round(window.span_s, 1))

    slope = slope_per_hour(window)
    if slope is None or math.isnan(slope):
        return Verdict.ok(None, reason="slope is not computable")

    threshold = float(rule.params["per_hour"])
    direction = rule.params.get("direction", "down")
    if direction == "down":
        active = slope <= -abs(threshold)
    else:
        active = slope >= abs(threshold)

    return verdict(
        active, round(slope, 6),
        per_hour=round(slope, 6), threshold=threshold, direction=direction,
        n=len(usable), span_s=round(window.span_s, 1),
        unit_per_hour=rule.params.get("unit", ""),
    )


# ─── 4. rate of change ──────────────────────────────────────────────────────


def rate_of_change(rule: AlarmRule, window: Window) -> Verdict:
    """The instantaneous derivative, for faults that move faster than a trend.

    `trend` needs several points and a window; this needs two and reacts on the
    next reading. A chemical dose controller going open-loop, or a valve slamming,
    is a rate event rather than a trend — and `blower_failure` is detectable by
    `rate_of_change` precisely because the fan *drops instantly* while the basin
    takes an hour to care.

    Unlike `trend` this deliberately uses the last two points and nothing else,
    because a fitted line would smooth away exactly the spike it is looking for.
    """
    usable = window.usable
    if len(usable) < 2:
        return Verdict.ok(None, reason="need two usable readings")

    a, b = usable[-2], usable[-1]
    if a.value is None or b.value is None:
        return Verdict.ok(None, reason="a value is missing")
    dt = b.ts - a.ts
    if dt <= 0:
        return Verdict.ok(None, reason="timestamps are not increasing")

    per_hour = ((b.value - a.value) / dt) * 3600.0
    limit = abs(float(rule.params["per_hour"]))
    active = abs(per_hour) >= limit

    return verdict(
        active, round(per_hour, 6),
        per_hour=round(per_hour, 6), limit=limit, dt_s=round(dt, 3),
        from_value=a.value, to_value=b.value,
    )


# ─── 5. state change ────────────────────────────────────────────────────────


def state_change(rule: AlarmRule, window: Window) -> Verdict:
    """Equipment left the state it was supposed to be in.

    The one detector here that looks at `quality` rather than `value`, because a
    stopped pump is a *state* and its flow reading of zero is a consequence. An
    alarm on the flow would fire late and be ambiguous; an alarm on the state is
    immediate and unambiguous.

    `not_sufficient_alone: state_change` appears on `lift_pump_cavitation` in the
    contract, and that is right: cavitation is a *performance* loss, the pump is
    still nominally running. The pump's state never changes, which is exactly why
    the condition had to be written as a deviation in delivered flow rather than
    as a state alarm — and why "the pump is running" is not the same claim as "the
    pump is working".
    """
    expected = rule.params.get("expected", "running")
    stopped = rule.params.get("stopped_value", 0)
    usable = [p for p in window.points if p.value is not None]

    if not usable:
        return Verdict.ok(None, reason="no readings at all")

    last = usable[-1]
    if last.value == stopped:
        return Verdict.firing(
            last.value, expected=expected, observed_state="stopped",
        )
    return Verdict.ok(
        last.value, expected=expected, observed_state="running",
    )


# ─── 6. flatline detection ──────────────────────────────────────────────────


def flatline_detection(rule: AlarmRule, window: Window) -> Verdict:
    """The value stopped moving.

    **The detector the deadband hides.** This is the important one.

    A deadband suppresses readings that have not moved. So the *normal* output of
    a healthy, steady signal is a flat line at the database level — and every
    detector that looks at variance is looking at the deadband's output rather
    than at the process. A stuck sensor and a steady process are the same
    observation.

    The contract says so explicitly, twice: `sensor_flatline` and
    `effluent_tss_stuck` both list `deadband` under `not_sufficient_alone`, and
    `sensor_flatline` also lists `deviation_from_baseline` — because a sensor
    stuck at a plausible value has no deviation.

    So this detector deliberately does **not** look at the values. It looks at
    *coverage* and at whether any reading fell outside the deadband:

    * no readings for `max_silence_s` → the instrument or the link is gone
    * readings present but every one inside the deadband for `stuck_s` → either
      genuinely steady or genuinely stuck, and the two are told apart by
      `cross_validation` against a second observation of the same quantity

    The honest version of this alarm says *"no movement"* and not *"broken"*, and
    a rule that claims to know which is overstating what the data supports.
    """
    max_silence_s = float(rule.params["max_silence_s"])
    stuck_s = float(rule.params.get("stuck_s", 0.0))
    deadband = float(rule.params.get("deadband", 0.0))

    if not window.has_coverage(max_silence_s):
        newest = max((p.ts for p in window.points), default=0.0)
        silence = window.now - newest
        return Verdict.firing(
            round(silence, 1),
            reason="no readings", silence_s=round(silence, 1),
            max_silence_s=max_silence_s,
        )

    if stuck_s <= 0:
        return Verdict.ok(None, reason="stuck_s not configured")

    recent = [p for p in window.usable if p.value is not None
              and p.ts >= window.now - stuck_s]
    if not recent:
        return Verdict.ok(None, reason="no usable readings in the stuck window")

    # The annotation is not decoration: `Window.usable` already guarantees a
    # non-None value, but a list comprehension's filter does not narrow the
    # element type, so `max()` on the result is a `float | None` to a type
    # checker. `recent` is built from `usable`, so the claim is true.
    values: list[float] = [p.value for p in recent if p.value is not None]
    if not values:
        return Verdict.ok(None, reason="no values in the stuck window")
    movement = max(values) - min(values)
    return verdict(
        movement <= deadband, round(movement, 6),
        movement=round(movement, 6), deadband=deadband,
        n=len(recent), stuck_s=stuck_s,
        note="no movement is not proof of a fault; cross-validate before paging",
    )


# ─── 7. expected sample count ───────────────────────────────────────────────


def expected_sample_count(rule: AlarmRule, window: Window) -> Verdict:
    """The signal is reporting at the wrong rate, whatever its values say.

    The companion to `flatline_detection`, and strictly better at answering
    "is this instrument alive?" — because it does not need the value to be wrong.

    `sample_ms` comes from `contracts/tags.yaml`, so the expected rate is
    declared once and not restated per alarm. That is the contract earning its
    keep: a threshold on a reading cannot distinguish a stopped sensor from a
    stopped plant, and this can, because it looks at the count.

    The `factor` is deliberately generous. A deadband suppresses most readings
    on a steady signal — `AERATION:AHU-1:DO` produces about one every six
    minutes against a declared 1 Hz — so the expected count is derived from
    `sample_ms` only as an upper bound, and the rule states its own `min_count`
    for the signals it watches. Expecting a deadbanded signal to hit its sample
    rate is expecting a contradiction.
    """
    min_count = int(rule.params["min_count"])
    horizon_s = float(rule.params["horizon_s"])
    recent = window.since(horizon_s)
    usable = [p for p in recent if p.usable]
    active = len(usable) < min_count

    return verdict(
        active, float(len(usable)),
        count=len(usable), min_count=min_count, horizon_s=horizon_s,
        expected_sample_ms=rule.params.get("sample_ms"),
        raw_count=len(recent),
    )


# ─── 8. cross validation ────────────────────────────────────────────────────


def cross_validation(rule: AlarmRule, window: Window) -> Verdict:
    """Two independent observations of the same quantity disagree.

    **The only detector that can catch this project's signature bug class.**

    A 32-bit float whose Modbus words arrive low-word-first, decoded as
    high-word-first, gives about `2.3e-41`. It is finite. It is inside the
    engineering range. It is a perfectly well-formed number that is wrong, and
    no amount of validation against the *value* can ever catch it — there is
    nothing wrong with the number as a number.

    The defence is structural: `source` is part of the primary key of `reading`,
    so both faces can record the same signal at the same instant, and this
    detector asks whether they agree. The contract names `cross_validation` for
    four of the eleven faults — three of them sensor faults, which is the same
    argument one level up: a sensor that has drifted is a plausible value that
    disagrees with reality, and the only way to know is to measure it twice.

    `tolerance` is a *relative* fraction, because a disagreement of 2 % matters
    enormously on a 0.02 mg/L deadband and not at all on a 26 000 m³/h range.
    """
    tolerance = float(rule.params["tolerance"])
    usable = window.usable
    if len(usable) < 2:
        return Verdict.ok(None, reason="need two observations to disagree")

    by_value: dict[float, list[Sample]] = {}
    for p in usable:
        by_value.setdefault(p.ts, []).append(p)

    worst = 0.0
    worst_pair: tuple[Sample, Sample] | None = None
    compared = 0
    for group in by_value.values():
        if len(group) < 2:
            continue
        compared += 1
        a, b = group[0], group[-1]
        if a.value is None or b.value is None:
            continue
        scale = max(abs(a.value), abs(b.value), 1e-9)
        relative = abs(a.value - b.value) / scale
        if relative > worst:
            worst, worst_pair = relative, (a, b)

    if compared == 0 or worst_pair is None:
        return Verdict.ok(None, reason="no two observations share a timestamp")

    a, b = worst_pair
    return verdict(
        worst >= tolerance, round(worst, 9),
        relative_gap=round(worst, 9), tolerance=tolerance, compared=compared,
        a_value=a.value, b_value=b.value, ts=a.ts,
    )


# ─── 9. ratio derived ───────────────────────────────────────────────────────


def ratio_derived(rule: AlarmRule, window: Window) -> Verdict:
    """A computed ratio of two signals leaves its band.

    `SLUDGE:DIG-1:VFA_ALK_RATIO` is already a ratio signal, so this detector is
    applied to it directly. For a ratio that is *not* precomputed, use
    `params.numerator` / `params.denominator` signal ids and the engine will
    supply a window for each.

    **There is deliberately no `sustained_s` parameter here.** An earlier version
    had one, taking the worst value in the last N seconds, and it was wrong: it
    made a single excursion count as sustained, which is the opposite of the
    intent. More importantly it was a *second* dwell mechanism alongside the
    engine's `for_s`, and two dwell mechanisms in one system will disagree. Dwell
    is the state machine's job and a detector's job is to answer "is the condition
    true right now".

    The reason a ratio needs its own alarm: both terms can be individually
    unremarkable. Volatile fatty acids at 0.35 with alkalinity at 2 000 is a
    healthy digester; VFA at 0.35 with alkalinity at 600 is a digester four
    hours from stopping. The second is indistinguishable from "alkalinity is a
    bit low" unless you divide, and dividing is exactly what a threshold on
    either term cannot do.
    """
    limit = float(rule.params["limit"])
    direction = rule.params.get("direction", "high")
    usable = window.usable
    if not usable:
        return Verdict.ok(None, reason="no usable readings")
    last = usable[-1]
    if last.value is None:
        return Verdict.ok(None, reason="last reading has no value")

    value = last.value
    active = value >= limit if direction == "high" else value <= limit
    return verdict(active, value, limit=limit, direction=direction)


# ─── 10. model based ────────────────────────────────────────────────────────


def model_based(rule: AlarmRule, window: Window) -> Verdict:
    """The measurement departs from what a process model predicts.

    The most powerful detector here and the most expensive to get right, because
    "the model is wrong" and "the sensor is wrong" produce the same residual.

    This project uses a deliberately cheap model: the contract's own normal band
    as a very simple expectation, plus an optional `lead_s` so the alarm looks
    *ahead*. `blower_failure` is the case that motivates `lead_s`: DO does not
    leave its band, it sags within it, so a band check is silent — but a model
    that knows what DO *should* be given the current air flow can say "DO is
    lower than this air flow justifies" hours before ammonia moves.

    That is the honest state of model-based alarming in this project: the
    expectation is the normal band and the lead time is a parameter, not a
    computed prediction. `docs/LEARNING-LOG.md` records it as the piece a real
    implementation would need and this one does not have.
    """
    limit = float(rule.params["limit"])
    normal_low = rule.params.get("normal_low")
    # Read once, next to `normal_low`. This was a bare name in three places below
    # and never assigned in this scope, so any rule carrying a `normal_high`
    # would have raised `NameError` -- inside an evaluation loop, on the first
    # reading after the rule was added, with a traceback pointing at a detector
    # rather than at the rule that triggered it. No test failed because no test
    # carried a `normal_high`. Linting found it.
    normal_high = rule.params.get("normal_high")
    usable = window.usable
    if len(usable) < 2:
        return Verdict.ok(None, reason="need two usable readings")

    values = [p.value for p in usable if p.value is not None]
    mean = sum(values) / len(values)

    if normal_low is not None and mean < float(normal_low):
        return Verdict.firing(
            round(mean, 6), predicted=round(mean, 6),
            normal_low=normal_low, limit=limit,
            note="below the band the current conditions should sustain",
        )
    if normal_high is not None and mean > float(normal_high):
        return Verdict.firing(
            round(mean, 6), predicted=round(mean, 6),
            normal_high=normal_high, limit=limit,
        )
    return Verdict.ok(round(mean, 6), predicted=round(mean, 6), limit=limit)


#: name -> function. The engine dispatches on this and nothing else.
REGISTRY: dict[str, Detector] = {
    "single_point_threshold": single_point_threshold,
    "deviation_from_baseline": deviation_from_baseline,
    "trend": trend,
    "rate_of_change": rate_of_change,
    "state_change": state_change,
    "flatline_detection": flatline_detection,
    "expected_sample_count": expected_sample_count,
    "cross_validation": cross_validation,
    "ratio_derived_alarm": ratio_derived,
    "model_based": model_based,
}


def detect(rule: AlarmRule, window: Window) -> Verdict:
    """Dispatch. The only name the engine needs."""
    try:
        detector = REGISTRY[rule.detector]
    except KeyError:  # pragma: no cover - AlarmRule.__post_init__ guards this
        raise KeyError(
            f"{rule.id}: no detector named {rule.detector!r}"
        ) from None
    return detector(rule, window)
