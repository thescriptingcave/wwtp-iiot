"""The detectors, against hand-built windows. No database, no clock, no sockets.

Each detector is tested on three shapes of data: **normal**, **failing**, and
**degenerate** — the third being the one that matters, because every detector in
`detectors.py` has a way to divide by zero, index past the end, or answer a
question it was never asked, and none of those raise a useful error on their own.

The synthetic windows are built with a `step` function rather than literal lists,
because a test that says "a value falls by 1.0 per hour for three hours" is
readable and a test that says `[10.0, 9.5, 9.0, 8.5]` requires you to do the
arithmetic in your head to check the assertion.

The two tests at the bottom are the ones I would keep if I had to delete the rest:
one asserts a detector cannot do what the contract says it cannot, and the other
asserts the *asymmetry* between a fast alarm and a slow one, which is the fact the
whole fault library exists to model.
"""

from __future__ import annotations

import pytest
from alarms.base import (
    DETECTION_VOCABULARY,
    DETECTORS,
    AlarmRule,
    Sample,
    Window,
    slope_per_hour,
)
from alarms.detectors import REGISTRY, detect
from alarms.synthetic import (
    T0,
    paired,
    ramp,
    rule,
    step,
    window,
)

# ─── window construction ─────────────────────────────────────────────────────
#
# The builders live in `alarms/synthetic.py` rather than here, because three
# modules in this project need them and a cross-test import does not work. The
# reason they are worth a module at all is the bottom of this file: a fixture
# written as `[2.0, 1.9, 1.8]` has to be taken on trust, and one written as
# `ramp(-0.15, hours=2)` can be checked.

# ─── the registry ────────────────────────────────────────────────────────────


def test_every_detector_in_the_contract_is_implemented_or_declared_missing() -> None:
    """The gap between "named in the contract" and "implemented" is explicit.

    `correlation` and `oscillation_detection` are named in
    `contracts/fault-scenarios.yaml` and have no implementation. That is fine;
    it is *undeclared* that would be a problem, so the test asserts the exact
    set of missing ones rather than merely that the registry is non-empty.
    """
    assert set(REGISTRY) == DETECTORS
    assert DETECTORS <= DETECTION_VOCABULARY
    assert {
        "correlation", "oscillation_detection",
    } == DETECTION_VOCABULARY - DETECTORS


def test_a_detector_outside_the_vocabulary_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="not in the contract's vocabulary"):
        AlarmRule(id="x", signal_id="A:1:X", detector="vibes",
                  severity="warning", message="m")


def test_a_declared_but_unimplemented_detector_is_refused_clearly() -> None:
    """A distinct error, because "no such detector" and "not built yet" are
    different problems with different fixes."""
    with pytest.raises(ValueError, match="not implemented"):
        AlarmRule(id="x", signal_id="A:1:X", detector="correlation",
                  severity="warning", message="m")


#: The two detectors for which "no data at all" is the alarm condition rather
#: than an absence of evidence. Silence is the thing they exist to report.
_SILENCE_DETECTORS = {"flatline_detection", "expected_sample_count"}


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_detector_survives_an_empty_window(name: str) -> None:
    """The degenerate case, for all ten at once.

    A detector that raises `IndexError` on no data will, in a running system,
    take down the evaluation loop for every *other* rule in the same batch. The
    cost of one bad rule should be one absent alarm, not a stalled historian.

    The two silence detectors are the deliberate exception: for them, an empty
    window is not a lack of evidence, it *is* the evidence, and they fire. The
    blanket assertion would have been the wrong test, and writing it is what
    surfaced the distinction.
    """
    w = Window(signal_id="A:1:X", points=(), now=T0)
    v = detect(rule(name, limit=1.0, tolerance=1.0, per_hour=1.0,
                    horizon_s=60.0, min_count=1, max_silence_s=60.0), w)
    if name in _SILENCE_DETECTORS:
        assert v.active is True, f"{name} should fire on silence"
    else:
        assert v.active is False, f"{name} should not invent an alarm from no data"
    assert "reason" in v.detail or v.active


def test_no_data_never_invents_an_alarm_from_a_value_detector() -> None:
    """The complement, spelled out for the eight detectors that read values.

    Worth its own test because the failure it guards against is the expensive
    one: an alarm that fires because nothing arrived will fire *again* on the
    next evaluation, and an operator who sees an alarm they cannot explain stops
    reading the alarm.
    """
    w = Window(signal_id="A:1:X", points=(), now=T0)
    for name in sorted(set(REGISTRY) - _SILENCE_DETECTORS):
        v = detect(rule(name, limit=1.0, tolerance=1.0, per_hour=1.0,
                        horizon_s=60.0, min_count=1, max_silence_s=60.0), w)
        assert v.active is False, name
        assert v.observed is None, name


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_detector_survives_all_bad_quality_readings(name: str) -> None:
    """Every sample Bad is not the same as no samples, and must not crash."""
    w = window(step([1.0, 2.0, 3.0], quality=2))
    v = detect(rule(name, limit=1.0, tolerance=1.0, per_hour=1.0,
                    horizon_s=60.0, min_count=1, max_silence_s=60.0), w)
    assert isinstance(v.active, bool)


# ─── single point threshold ──────────────────────────────────────────────────


def test_threshold_fires_on_a_high_limit() -> None:
    v = detect(rule("single_point_threshold", limit=3.0, direction="high"),
               window(step([1.0, 2.0, 3.5])))
    assert v.active is True
    assert v.observed == 3.5
    assert v.detail["limit"] == 3.0


def test_threshold_fires_on_a_low_limit() -> None:
    v = detect(rule("single_point_threshold", limit=1.5, direction="low"),
               window(step([2.0, 1.8, 1.2])))
    assert v.active is True


def test_threshold_respects_hysteresis_in_the_reporting() -> None:
    """Hysteresis is reported so a caller can tell *why* a value is not firing."""
    v = detect(rule("single_point_threshold", limit=3.0, direction="high",
                    hysteresis=0.5), window(step([2.9])))
    assert v.active is False
    assert v.detail["clears_at"] == 2.5


# ─── deviation from baseline ─────────────────────────────────────────────────


def test_deviation_from_a_fixed_baseline() -> None:
    v = detect(rule("deviation_from_baseline", baseline=2.0, tolerance=0.3),
               window(step([2.0, 2.1, 2.05, 2.4])))
    assert v.active is True
    assert v.observed == pytest.approx(0.4)
    assert v.detail["how"] == "fixed"


def test_deviation_from_a_window_mean_catches_a_slow_drift() -> None:
    """The reason this is not a threshold with extra steps.

    A sensor that drifts by 0.7 mg/L over a week never leaves any single band. A
    fixed baseline catches it only if the fixed number is still right. The window
    mean catches it because the window *is* where the process currently is.
    """
    v = detect(rule("deviation_from_baseline", tolerance=0.2),
               window(step([2.0, 2.0, 2.6, 2.6, 2.6])))
    assert v.active is True
    assert v.detail["how"] == "window_mean"


def test_deviation_needs_three_points() -> None:
    v = detect(rule("deviation_from_baseline", tolerance=0.01),
               window(step([1.0, 9.0])))
    assert v.active is False
    assert "3 usable" in v.detail["reason"]


# ─── trend ───────────────────────────────────────────────────────────────────


def test_trend_detects_a_falling_ramp() -> None:
    v = detect(rule("trend", per_hour=10.0, direction="down", min_points=4),
               window(ramp(-10.0, 3.0, dt=300.0)))
    assert v.active is True
    assert v.observed == pytest.approx(-10.0, rel=0.02)


def test_trend_ignores_a_flat_signal() -> None:
    v = detect(rule("trend", per_hour=1.0, direction="down"),
               window(step([2.0] * 10, dt=60.0)))
    assert v.active is False
    assert v.observed == pytest.approx(0.0, abs=1e-9)


def test_trend_ignores_noise_that_a_dumb_difference_would_see() -> None:
    """Why a least-squares fit rather than `last - first`.

    Alternating noise has a large endpoint difference and no slope. A detector
    that differences consecutive points pages on this; one that fits a line does
    not. The deadband makes uneven sampling the normal case, so this is not a
    contrived input.
    """
    noisy = [2.0, 2.4, 1.6, 2.4, 1.6, 2.4, 1.6, 2.4, 1.6, 2.4]
    w = window(step(noisy, dt=60.0))
    assert abs(noisy[-1] - noisy[0]) > 0.3, "the endpoint difference is large"
    v = detect(rule("trend", per_hour=2.0, direction="down"), w)
    assert v.active is False
    assert abs(slope_per_hour(w) or 0.0) < 2.0


def test_trend_refuses_to_fit_two_points() -> None:
    """Two points always define a line, so a two-point "trend" is a difference
    with extra steps — and it will fire on noise."""
    v = detect(rule("trend", per_hour=1.0, min_points=4), window(step([1.0, 5.0])))
    assert v.active is False
    assert "need 4" in v.detail["reason"]


def test_trend_reports_its_own_sample_count_and_span() -> None:
    """A trend over two points an hour apart and a trend over two hundred points
    a second apart are both "a trend", and they are not equally trustworthy."""
    v = detect(rule("trend", per_hour=1.0, min_points=3),
               window(step([1.0, 2.0, 3.0], dt=600.0)))
    assert v.detail["n"] == 3
    assert v.detail["span_s"] == pytest.approx(1200.0)


# ─── rate of change ──────────────────────────────────────────────────────────


def test_rate_of_change_fires_on_an_instant_drop() -> None:
    """The contract's blower trip halves capacity: 904 rev/min to about 450.

    That is -450 in 30 s of ramp, or -54 000 per hour, against a limit of 1 800.
    The margin is enormous and that is the point: a fan either trips or it does
    not, and a rate detector catches it on the first sample pair.
    """
    v = detect(rule("rate_of_change", per_hour=1800.0),
               window(step([904.0, 871.0, 838.0, 455.0])))
    assert v.active is True
    assert v.detail["dt_s"] == 60.0
    assert abs(v.observed) > 20_000, v.observed


def test_rate_of_change_uses_the_last_two_points_only() -> None:
    """A fitted line would smooth away exactly the spike this looks for."""
    v = detect(rule("rate_of_change", per_hour=100.0),
               window(step([900.0, 900.0, 900.0, 100.0])))
    assert v.active is True


def test_rate_of_change_will_not_divide_by_zero() -> None:
    """Two samples with the same timestamp. Real after a replayed backlog."""
    dup = (Sample(ts=T0, value=1.0), Sample(ts=T0, value=9.0))
    v = detect(rule("rate_of_change", per_hour=1.0), window(dup))
    assert v.active is False
    assert "not increasing" in v.detail["reason"]


# ─── state change ────────────────────────────────────────────────────────────


def test_state_change_fires_when_equipment_stops() -> None:
    v = detect(rule("state_change", expected="running", stopped_value=0),
               window(step([1487.0, 1480.0, 0.0])))
    assert v.active is True
    assert v.detail["observed_state"] == "stopped"


def test_state_change_is_quiet_while_equipment_runs() -> None:
    v = detect(rule("state_change", expected="running", stopped_value=0),
               window(step([1487.0, 1490.0])))
    assert v.active is False


def test_state_change_is_not_confused_by_a_missed_reading() -> None:
    """`None` is a missing value, not a stopped pump. Zero is a stopped pump.

    Conflating them is the exact bug the quality scale exists to prevent, and it
    is worth a test at the detector level rather than only in the SQL course.
    """
    samples = (
        Sample(ts=T0, value=1487.0),
        Sample(ts=T0 + 60, value=None, quality=2),
    )
    v = detect(rule("state_change", expected="running", stopped_value=0),
               window(samples))
    assert v.active is False


# ─── flatline ────────────────────────────────────────────────────────────────


def test_flatline_fires_on_total_silence() -> None:
    w = Window(signal_id="A:1:X", points=step([1.0, 1.0]), now=T0 + 7200)
    v = detect(rule("flatline_detection", max_silence_s=1800.0), w)
    assert v.active is True
    assert v.detail["reason"] == "no readings"


def test_flatline_fires_when_nothing_moves_but_data_arrives() -> None:
    """The case the deadband creates: a steady signal looks exactly like a
    stuck one at the database level."""
    v = detect(rule("flatline_detection", max_silence_s=1800.0,
                    stuck_s=7200.0, deadband=0.005),
               window(step([2.0] * 40, dt=300.0)))
    assert v.active is True
    assert v.detail["movement"] == 0.0


def test_flatline_is_quiet_when_the_signal_moves_past_its_deadband() -> None:
    v = detect(rule("flatline_detection", max_silence_s=1800.0,
                    stuck_s=7200.0, deadband=0.005),
               window(step([2.0, 2.4, 2.8, 3.1, 2.2, 2.0], dt=300.0)))
    assert v.active is False


def test_flatline_says_no_movement_not_broken() -> None:
    """The claim the detector is entitled to make.

    A deadband suppresses readings that have not moved, so a healthy steady
    signal and a failed instrument are the *same observation*. A detector that
    reported "broken" would be overstating what the data supports, and an
    operator who is caught out by that once stops reading the alarm.
    """
    v = detect(rule("flatline_detection", max_silence_s=1800.0,
                    stuck_s=7200.0, deadband=0.005),
               window(step([2.0] * 40, dt=300.0)))
    assert "not proof of a fault" in v.detail["note"]


# ─── expected sample count ───────────────────────────────────────────────────


def test_sample_count_fires_when_too_little_arrives() -> None:
    v = detect(rule("expected_sample_count", min_count=5, horizon_s=3600.0),
               window(step([15.0, 15.1, 15.2], dt=60.0)))
    assert v.active is True
    assert v.detail["count"] == 3


def test_sample_count_is_quiet_at_the_expected_rate() -> None:
    v = detect(rule("expected_sample_count", min_count=5, horizon_s=3600.0),
               window(step([15.0] * 20, dt=60.0)))
    assert v.active is False


def test_sample_count_ignores_bad_quality_towards_its_count() -> None:
    """`raw_count` and `count` are both reported, and they differ.

    A window full of Bad readings is not a healthy signal reporting often — it is
    a signal that has said, repeatedly and explicitly, that it does not know.
    """
    v = detect(rule("expected_sample_count", min_count=3, horizon_s=3600.0),
               window(step([15.0] * 10, dt=60.0, quality=2)))
    assert v.active is True
    assert v.detail["raw_count"] == 10
    assert v.detail["count"] == 0


# ─── cross validation ────────────────────────────────────────────────────────


def test_cross_validation_is_quiet_when_the_faces_agree() -> None:
    v = detect(rule("cross_validation", tolerance=0.02),
               window(paired([2.0, 2.1, 2.2], [2.0, 2.11, 2.19])))
    assert v.active is False


def test_cross_validation_catches_the_signature_bug() -> None:
    """A low-word-first Modbus float, decoded as high-word-first.

    The value is finite, non-null, and inside a 0-20 mg/L engineering range. No
    detector in this file that looks at the *value* can find it, because there is
    nothing wrong with the number as a number. This one can, and it is the only
    one that can.
    """
    v = detect(rule("cross_validation", tolerance=0.05),
               window(paired([2.10, 2.11, 2.09], [2.3e-41] * 3)))
    assert v.active is True
    assert v.detail["compared"] == 3
    assert v.detail["b_value"] == pytest.approx(2.3e-41)


def test_cross_validation_needs_two_observations_of_the_same_instant() -> None:
    """One source, many timestamps: nothing to compare, and it must say so
    rather than reporting agreement."""
    v = detect(rule("cross_validation", tolerance=0.02),
               window(step([2.0, 2.1, 2.2, 2.3])))
    assert v.active is False
    assert "share a timestamp" in v.detail["reason"]


def test_cross_validation_tolerance_is_relative() -> None:
    """2 % of a 26 000 m3/h range is 520 m3/h; 2 % of 0.02 mg/L is nothing.

    An absolute tolerance would have to be re-tuned per signal and would be
    wrong on both ends of this plant.
    """
    big = detect(rule("cross_validation", tolerance=0.02),
                 window(paired([26000.0] * 3, [25000.0] * 3)))
    assert big.active is True, "500 m3/h on a flow signal matters"
    small = detect(rule("cross_validation", tolerance=0.02),
                   window(paired([2.0] * 3, [2.02] * 3)))
    assert small.active is False, "0.02 on a DO signal does not"


# ─── ratio and model based ───────────────────────────────────────────────────


def test_ratio_alarm_on_a_high_limit() -> None:
    v = detect(rule("ratio_derived_alarm", limit=0.6, direction="high"),
               window(step([0.35, 0.40, 0.65])))
    assert v.active is True


def test_ratio_alarm_can_require_sustainment() -> None:
    """A single excursion in a signal that moves over days is not an event."""
    spiky = [0.35, 0.65, 0.36, 0.37, 0.35, 0.36]
    sustained = rule("ratio_derived_alarm", limit=0.6, direction="high",
                     sustained_s=600.0)
    assert detect(sustained, window(step(spiky, dt=60.0))).active is False
    assert detect(sustained, window(step([0.7] * 12, dt=60.0))).active is True


def test_model_based_fires_below_the_expected_band() -> None:
    v = detect(rule("model_based", limit=1.0, normal_low=1.5),
               window(step([1.2, 1.1, 1.3, 1.2])))
    assert v.active is True
    assert "should sustain" in v.detail["note"]


# ─── the two tests worth keeping ─────────────────────────────────────────────


def test_slope_fitting_is_robust_to_the_deadbands_uneven_sampling() -> None:
    """Six-minute gaps and tenth-of-a-second gaps in the same window.

    `AERATION:AHU-1:DO` produces about one reading every six minutes in a seeded
    week; air flow produces thousands a second. A rate computed by differencing
    points means something completely different on each, and a slope fitted
    against real timestamps does not.
    """
    slow = window(ramp(-1.0, 2.0, dt=360.0, start=2.0))
    fast = window(ramp(-3600.0, 2.0, dt=1.0, start=2.0))
    assert len(slow.usable) != len(fast.usable)
    assert slope_per_hour(slow) == pytest.approx(-1.0, rel=0.05)
    assert slope_per_hour(fast) == pytest.approx(-3600.0, rel=0.05)


def test_a_fast_alarm_and_a_slow_alarm_cannot_be_interchanged() -> None:
    """The asymmetry the fault library exists to model, in one test.

    A blower trip moves the fan in under a second, DO over one to two hours, and
    effluent ammonia two to six hours after that. `rate_of_change` on RPM fires
    immediately and `trend` on DO fires an hour later; `single_point_threshold`
    on ammonia fires last of all, hours after the cause is over.

    The assertion is that the three fire in that order, and that the ammonia
    threshold does *not* fire at all in the first hour — which is the whole
    argument for alarming on rates rather than limits.
    """
    # A blower trip: half the capacity, gone in one 30 s ramp.
    fan_rpm = [904.0, 900.0, 896.0, 880.0, 640.0, 470.0, 455.0, 452.0, 450.0]

    # DO: sags 2.2 -> 1.9 over 2 hours, one reading every 5 minutes. That is
    # 0.15 mg/L per hour, and it is deliberately a sag *within* the 1.5-3.0
    # band: 0.3 mg/L of loss in a basin holding 40 kg is a real aeration failure
    # that no limit check on DO will ever see.
    do = [2.2 - 0.3 * (i * 300) / 3600.0 for i in range(25)]   # 25 x 5 min
    # NH4: starts rising only after 90 minutes, as nitrifiers decay.
    nh4 = (
        [7.9] * 19                                              # 95 minutes flat
        + [7.9 + 0.3 * ((i - 18) * 300) / 3600.0 for i in range(19, 25)]
    )

    def fired(detector: str, samples, **params) -> float | None:
        r = AlarmRule(id="x", signal_id="A:1:X", detector=detector,
                      severity="critical", message="m", params=params)
        for i in range(2, len(samples)):
            w = window(samples[: i + 1], signal_id=r.signal_id)
            if detect(r, w).active:
                return w.now
        return None

    t_fan = fired("rate_of_change", step(fan_rpm, dt=60.0), per_hour=1800.0)
    t_do = fired("trend", step(do, dt=300.0), per_hour=0.15, direction="down",
                 min_points=4)
    t_nh4 = fired("single_point_threshold", step(nh4, dt=300.0), limit=8.0,
                  direction="high")

    assert t_fan is not None, "the fan alarm should fire"
    assert t_do is not None, "the DO trend should fire"
    assert t_nh4 is not None, "the ammonia threshold should eventually fire"

    # The ordering, which is the fact worth having.
    assert t_fan < t_do < t_nh4, (t_fan, t_do, t_nh4)

    # And the part that justifies a trend rule existing at all: DO never leaves
    # its 1.5-3.0 band, so a threshold on it would never fire.
    assert min(do) > 1.5, "DO stays in band throughout — a threshold cannot see it"
    assert fired("single_point_threshold", step(do, dt=300.0), limit=1.5,
                 direction="low") is None


def test_model_based_handles_a_high_side_expectation() -> None:
    """A regression test for a `NameError` that lint found and tests did not.

    `model_based` read `normal_high` in three places without ever assigning it,
    so any rule carrying one would have raised on its first evaluation — inside a
    loop, at 3am, with a stack trace pointing at a detector rather than at the
    rule that triggered it. No test failed because no test carried a
    `normal_high`.

    The test exists so that the *shape* is covered rather than the value.
    """
    v = detect(rule("model_based", limit=1.0, normal_high=3.0),
               window(step([4.0, 4.1, 4.2, 4.1])))
    assert v.active is True
    assert v.detail["normal_high"] == 3.0

    quiet = detect(rule("model_based", limit=1.0, normal_high=3.0),
                   window(step([2.0, 2.1, 2.2, 2.1])))
    assert quiet.active is False


def test_trend_refuses_to_fit_a_window_shorter_than_min_span() -> None:
    """The fix for a rule that fired on ten of eleven faults.

    A least-squares slope over a short window is a noise estimator. Dissolved
    oxygen in an aeration basin swings on a diurnal cycle, and fitted over fifteen
    minutes that is easily 0.2 mg/L per hour — which clears a 0.15 threshold on a
    perfectly healthy plant. The rule was not miscalibrated by a factor, it was
    measuring the wrong thing.

    The remedy is a *span* requirement, not a bigger number, because the diurnal
    cycle cannot produce a two-hour fall and noise cannot either.
    """
    short = window(ramp(-1.0, hours=0.5, dt=300.0, start=2.0))   # 30 min
    v = detect(rule("trend", per_hour=0.15, direction="down",
                    min_span_s=7200.0), short)
    assert v.active is False
    assert "need 7200" in v.detail["reason"]

    long = window(ramp(-1.0, hours=3.0, dt=300.0, start=2.0))    # 3 hours
    fired = detect(rule("trend", per_hour=0.15, direction="down",
                        min_span_s=7200.0), long)
    assert fired.active is True, "the real fault still has to be catchable"


def test_lookback_is_at_least_the_min_span_a_trend_rule_demands() -> None:
    """A rule asked for a two-hour span cannot be handed an hour of window.

    Without this, `min_span_s` would be a parameter no rule could ever satisfy
    and every trend alarm would be permanently silent — the most dangerous kind of
    bug, because nothing is red.
    """
    r = rule("trend", per_hour=1.0, direction="down",
             min_span_s=7200.0, for_s=0.0)
    assert r.lookback_s() >= 7200.0
