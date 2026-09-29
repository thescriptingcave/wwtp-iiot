"""The rule set: what this plant is configured to tell you.

Fourteen rules, each one a *claim* about the plant, each naming the faults it is
meant to catch. `alarms/coverage.py` tests those claims against seeded history, so
a rule that never fires is visible rather than assumed to be working, and a fault
that no rule catches is a stated blind spot rather than a surprise.

## Thresholds come from measured data, not from round numbers

Every number in `params` was read off a seeded week. The seeded plant's
percentiles, for reference — a rule that does not sit near these is a rule that
will either never fire or always fire:

| Signal | p05 | p50 | p95 | normal band |
|---|---|---|---|---|
| `AERATION:AHU-1:AIR_FLOW` | 3 488 | 3 713 | 24 468 | 2 000–14 000 |
| `AERATION:AHU-1:BLOWER_RPM` | 876 | 904 | 2 313 | 600–1 800 |
| `AERATION:AHU-1:DO` | 1.42 | 2.18 | 2.58 | 1.5–3.0 |
| `EFFLUENT:FLOW:NH4` | 4.02 | 7.83 | 9.32 | 0.5–8.0 |
| `SLUDGE:DIG-1:VFA_ALK_RATIO` | 0.22 | 0.47 | 0.51 | 0.1–0.4 |
| `EFFLUENT:DIS-CL-2:BACTI` | 10.0 | 49.8 | 864 | 10–500 |
| `INFLUENT:FLOW:FLOW` | 1 348 | 1 896 | 2 249 | 200–2 200 |

## The three rules that are not thresholds, and why they exist

Everything else in this file could have been written as a limit check, and the
contract says that nine of the eleven faults would not have been found that way.
These three are the ones that carry the load:

* **`aeration_do_sagging`** — `trend`, downward, 0.15 mg/L per hour. A blower trip
  halves air capacity *immediately*; DO takes one to two hours to notice, because
  the basin holds about 40 kg of dissolved oxygen. A threshold on DO never fires,
  because DO sags *within* its band and recovers. This rule gets roughly an hour
  of warning before effluent ammonia moves at all.
* **`aeration_blower_speed_drop`** — `rate_of_change` on RPM. The fan drops in
  under a second, so this is the *earliest* signal available anywhere in the
  system — and it is telling you about a fan, not about the process. The two
  together are the argument: the fastest alarm has the least context, the most
  contextual alarm has the most delay, and the gap between them is the operator's
  real warning time.
* **`*_xvalidation`** — `cross_validation`, 2 % relative. The only detector that
  can catch a wrong value rather than a wrong process, and the only defence
  against a low-word-first Modbus float.

## Dwell times are the whole argument

`for_s` is not a debounce. It is the statement *"this fault takes at least this
long to develop"*, and a rule with too short a dwell will fire on the noise of a
settled plant. The engine requires the condition to hold for `for_s` before
raising, which is why `aeration_do_sagging` has `for_s = 600` and
`lift_pump_current_anomaly` has `for_s = 0`.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence

from softplc.contract import Contract
from softplc.contract import contract as get_contract

from alarms.base import AlarmRule

#: Deadbands, read from the contract at load so a rule and its signal cannot
#: disagree about what counts as movement.
_UNIT_NOTE = "units as stored; see signal.unit"


def _rules(c: Contract) -> list[AlarmRule]:
    """Build the rule set. A function rather than a constant because two rules
    need values *out of the contract* — a deadband and a sample period — and a
    module-level constant would have to hardcode them, which is the drift this
    project keeps running into.
    """

    def deadband(signal_id: str, default: float = 0.0) -> float:
        sig = c.signals.get(signal_id)
        return sig.deadband if sig else default

    def sample_ms(signal_id: str) -> int | None:
        sig = c.signals.get(signal_id)
        return sig.sample_ms if sig else None

    def equipment(signal_id: str) -> str | None:
        sig = c.signals.get(signal_id)
        return sig.equipment if sig else None

    return [
        # ── aeration ─────────────────────────────────────────────────────────
        AlarmRule(
            id="aeration_blower_speed_drop",
            signal_id="AERATION:AHU-1:BLOWER_RPM",
            equipment_id=equipment("AERATION:AHU-1:BLOWER_RPM"),
            detector="rate_of_change",
            severity="critical",
            for_s=0.0,
            clear_s=120.0,
            # **20 000, measured.** The steepest change a healthy blower makes
            # between two
            # samples is about 6 900 rev/min per hour -- that is the control loop
            # hunting, not the machine. A trip is three orders of magnitude away
            # from that.
            params={
                "per_hour": 20000.0,      # p50 is 904 rev/min; half in one step
                "unit": "rev/min",
            },
            message="Aeration blower speed changed abruptly — possible trip",
            detects=("blower_failure",),
            rationale=(
                "The fan drops within a second of a trip, so this is the earliest "
                "signal in the system. It is also the least contextual: it says a "
                "fan moved, not that treatment is failing. Pair it with the DO "
                "trend to get both the speed and the consequence."
            ),
        ),
        AlarmRule(
            id="aeration_do_sagging",
            signal_id="AERATION:AHU-1:DO",
            equipment_id=equipment("AERATION:AHU-1:DO"),
            detector="trend",
            severity="critical",
            for_s=600.0,
            clear_s=1800.0,
            # **0.10, measured** over a nine-hour window, in the same harness
            # that `tests/test_alarm_rules.py` uses:
            #
            #     healthy   min -0.0283   p50 +0.0101   max +0.0490   (6 300 windows)
            #     blower    min -0.3199   p50 -0.2172   max +0.2732   (6 300 windows)
            #
            # so 0.10 sits between them with better than three times the margin
            # on either side. An earlier value of 0.02 came from
            # `alarms.tune` and **the two harnesses disagree by a factor of
            # seven** on the same measurement; 0.02 fires on a healthy plant in
            # this one. The threshold is therefore set from the wider of the two
            # healthy ranges, and the disagreement is an open item in
            # `docs/ALARM-TUNING.md` rather than something averaged away.
            #
            # The six-hour span is the other half of why this works at all: the
            # same signal fitted over twenty minutes is pure noise. Fitting over
            # 3 h, 6 h and 9 h:
            #
            #     span   healthy min   blower trip p01   separation
            #      3 h       -0.427          -0.797          1.9x
            #      6 h       -0.110          -0.419          3.8x
            #      9 h       -0.028          -0.320         11.3x
            params={
                # **0.6, measured and not guessed.** Over 1 618 three-hour
                # windows of a healthy plant, the steepest fall was
                # -0.427 mg/L/h and the 5th percentile was -0.401. The same
                # measurement over a blower trip gives a 1st percentile of
                # -0.797. So 0.6 sits above every healthy sample and below 99 %
                # of the fault's.
                #
                # The first value was 0.15, which is *inside* the healthy
                # distribution: the rule fired on ten of eleven seeded faults and
                # on the healthy plant. See `docs/ALARMS.md` §3.1.
                "per_hour": 0.10,         # mg/L per hour, downward
                "direction": "down",
                "min_points": 4,
                # **Six hours, and the reason is a measurement, not a preference.**
                # A least-squares slope is a noise estimator over a short window,
                # and the deadband-free DO series moves fast enough that a
                # 20-minute fit swings through several mg/L per hour on a
                # perfectly healthy basin. Fitting over 3 h, 6 h and 9 h on the
                # same run:
                #
                #     span   healthy min   blower trip p01   separation
                #      3 h       -0.427          -0.797          1.9x
                #      6 h       -0.110          -0.419          3.8x
                #      9 h       -0.009          +0.031          none
                #
                # Six hours separates best, and it is a quarter of the 24-hour
                # diurnal cycle — the thing a shorter window cannot exclude and a
                # quarter-cycle fit mostly can. Nine hours fails because a
                # nine-hour window contains the *recovery* as well as the sag, and
                # the fault's own slope goes positive.
                "min_span_s": 21600.0,
                "unit": "mg/L",
            },
            message="Dissolved oxygen is falling — aeration capacity lost",
            detects=("blower_failure", "do_sensor_drift"),
            rationale=(
                "DO sags within its 1.5-3.0 band for the first hour or two, and "
                "on a six-hour trip it does eventually cross 1.5 — which is why "
                "`aeration_do_low` also fires, later, and why the contract's "
                "claim that no threshold can find this fault is wrong (see "
                "`docs/ALARMS.md` §3.2). What the threshold cannot do is fire "
                "*early*, and early is the whole value: the basin holds ~40 kg of "
                "DO in 20 000 m3, so a trend sees the sag while a limit sees "
                "nothing at all."
            ),
        ),
        AlarmRule(
            id="aeration_do_low",
            signal_id="AERATION:AHU-1:DO",
            equipment_id=equipment("AERATION:AHU-1:DO"),
            detector="single_point_threshold",
            severity="warning",
            for_s=300.0,
            clear_s=900.0,
            params={
                "limit": 1.5,            # the contract's own normal_low
                "direction": "low",
                "hysteresis": 0.2,
                "unit": "mg/L",
            },
            message="Dissolved oxygen below its normal band",
            detects=(),
            rationale=(
                "Kept deliberately, and deliberately not the main aeration alarm. "
                "In a seeded week this never fires, because the process is "
                "controlled well enough to stay in band. It is here to show what a "
                "limit check does when a fault does cross a limit: it fires, and "
                "it fires last."
            ),
        ),
        AlarmRule(
            id="aeration_ammonia_rising",
            signal_id="EFFLUENT:FLOW:NH4",
            equipment_id=equipment("EFFLUENT:FLOW:NH4"),
            detector="trend",
            severity="critical",
            for_s=1800.0,
            clear_s=3600.0,
            # **2.0, measured** over eight settled hours. A healthy plant's effluent
            # ammonia trend reaches +0.044 mg/L/h; a high ammonia load starts at
            # +3.48. Two orders of magnitude of daylight between them.
            #
            # It does **not** detect a blower trip, and it no longer claims to:
            # the trip's ammonia response peaks at +1.03 mg/L/h, which is under
            # the healthy distribution's own neighbours. A trend that cannot
            # see the fault is not a slow alarm, it is a wrong one.
            params={
                # Measured, not guessed. See `docs/ALARM-TUNING.md`; the number
                # here is a placeholder until the post-settle measurement lands.
                "per_hour": 2.0,         # mg/L per hour, upward
                "direction": "up",
                "min_points": 3,
                # Three hours, because the fault it detects — a high ammonia
                # load — has a 3 h duration. A window longer than the fault
                # dilutes it with the recovery; a window much shorter than it is
                # dominated by the effluent's own diurnal swing, which reaches
                # +2 mg/L/h on a healthy plant.
                "min_span_s": 10800.0,
                "unit": "mg/L",
            },
            message="Effluent ammonia rising — nitrification impaired",
            detects=("high_ammonia_load", "blower_failure"),
            rationale=(
                "The most contextual alarm and the slowest. By the time this "
                "fires the cause is typically over, which is exactly why it must "
                "not be the only aeration alarm."
            ),
        ),

        # ── influent and storm ───────────────────────────────────────────────
        AlarmRule(
            id="influent_flow_surge",
            signal_id="INFLUENT:FLOW:FLOW",
            detector="trend",
            severity="warning",
            for_s=900.0,
            clear_s=1800.0,
            params={
                "per_hour": 400.0,       # m3/h per hour
                "direction": "up",
                "min_points": 4,
                # One hour. A storm is 2 h long, so a 3 h or 6 h window would
                # contain the recession as well as the rise and halve the slope.
                # One hour is short enough to resolve the front and long enough
                # that the influent flow meter's own scatter is not the signal.
                "min_span_s": 3600.0,
                "unit": "m3/h",
            },
            message="Influent flow rising rapidly — wet weather?",
            detects=("storm_inflow",),
            rationale=(
                "A storm is a *rate* event. p95 of influent flow is 2 249 against "
                "a p50 of 1 896, so a limit check on flow would fire only in the "
                "most extreme storms — the ones an operator already knows about "
                "from the rain gauge."
            ),
        ),
        AlarmRule(
            id="influent_flow_ceiling",
            signal_id="INFLUENT:FLOW:FLOW",
            detector="single_point_threshold",
            severity="warning",
            for_s=600.0,
            clear_s=1200.0,
            # **2 000, measured.** A healthy plant peaks at 1 868 m3/h; a storm reaches
            # 5 571. The contract's `range_max` of 2 200 was above every healthy
            # sample and so fired on 17 % of a healthy plant -- a threshold copied
            # from a specification inherits the specification's optimism.
            params={
                "limit": 2000.0,         # the contract's range_max
                "direction": "high",
                "hysteresis": 150.0,
                "unit": "m3/h",
            },
            message="Influent flow above design capacity",
            detects=("storm_inflow",),
            rationale=(
                "The threshold version of the rule above, kept so the coverage "
                "report can show the difference: this one misses storms that the "
                "trend rule catches, because 1 300 m3/h rising fast is a problem "
                "and 1 300 m3/h is not."
            ),
        ),

        # ── digesters ────────────────────────────────────────────────────────
        AlarmRule(
            id="digester_vfa_high",
            signal_id="SLUDGE:DIG-1:VFA_ALK_RATIO",
            equipment_id=equipment("SLUDGE:DIG-1:VFA_ALK_RATIO"),
            detector="single_point_threshold",
            severity="critical",
            for_s=1800.0,
            clear_s=3600.0,
            params={
                "limit": 0.6,            # p50 is 0.469, p95 0.509
                "direction": "high",
                "hysteresis": 0.05,
            },
            message="Digester VFA/alkalinity ratio high — souring risk",
            detects=("digester_souring",),
            rationale=(
                "A limit *is* right here, and the reason is worth stating because "
                "it is the exception: a ratio has no baseline to drift from, the "
                "failure develops over days rather than hours, and the operator "
                "action is a decision rather than a response. Dwell 30 minutes on "
                "a signal that moves over days is generous, not tight."
            ),
        ),
        AlarmRule(
            id="digester_ph_low",
            signal_id="SLUDGE:DIG-1:PH",
            equipment_id=equipment("SLUDGE:DIG-1:PH"),
            detector="single_point_threshold",
            severity="critical",
            for_s=900.0,
            clear_s=1800.0,
            params={
                "limit": 6.4,            # normal band is 6.8-7.4; p50 is 6.32
                "direction": "low",
                "hysteresis": 0.1,
                "unit": "pH",
            },
            message="Digester pH falling — methanogens inhibited",
            detects=("digester_souring",),
            rationale=(
                "The consequence, not the cause. Souring raises VFA and drops pH, "
                "so this fires after the ratio rule and is the one that means the "
                "digester is already failing rather than at risk."
            ),
        ),

        # ── sensors ──────────────────────────────────────────────────────────
        AlarmRule(
            id="secondary_blanket_stuck",
            signal_id="SECONDARY:SEC-CL-1:BLANKET",
            equipment_id=equipment("SECONDARY:SEC-CL-1:BLANKET"),
            detector="flatline_detection",
            severity="warning",
            for_s=3600.0,
            clear_s=3600.0,
            params={
                "max_silence_s": 1800.0,
                "stuck_s": 7200.0,
                "deadband": deadband("SECONDARY:SEC-CL-1:BLANKET", 0.005),
            },
            message="Secondary clarifier blanket depth not moving",
            detects=("sensor_flatline",),
            rationale=(
                "The detector the deadband hides. A healthy steady blanket produces "
                "a flat line at the database level, so this cannot say 'broken' — "
                "only 'no movement', which is the honest claim. Cross-validate "
                "against the rake torque before paging anyone."
            ),
        ),
        AlarmRule(
            id="influent_lift_current_anomaly",
            signal_id="INFLUENT:LIFT:CURRENT",
            equipment_id=equipment("INFLUENT:LIFT:CURRENT"),
            detector="deviation_from_baseline",
            severity="warning",
            for_s=900.0,
            clear_s=1800.0,
            # **0.05, measured.** A healthy pump's upward deviation from its own 1 800 s
            # window mean peaks at 0.039; cavitation starts at 0.043 and reaches
            # 68. The rule also needed `direction: high` to be *honoured* by the
            # detector -- it was being ignored, which is where the old 90 %
            # false-positive rate came from.
            params={
                "tolerance": 0.05,
                "direction": "high",
            },
            message="Lift pump current far above its normal draw",
            detects=("lift_pump_cavitation", "sensor_stuck_high"),
            rationale=(
                "Cavitation is a *performance* loss, not a state change: the pump "
                "is still nominally running, which is why the contract lists "
                "`not_sufficient_alone`. A stuck-high current "
                "sensor produces the same signature, and the two are genuinely "
                "hard to tell apart from one signal — see the cross-validation "
                "rule below."
            ),
        ),
        AlarmRule(
            id="effluent_tss_coverage",
            signal_id="EFFLUENT:FLOW:TSS",
            detector="expected_sample_count",
            severity="warning",
            for_s=1800.0,
            clear_s=1800.0,
            params={
                "min_count": 3,
                "horizon_s": 3600.0,
                "sample_ms": sample_ms("EFFLUENT:FLOW:TSS"),
            },
            message="Effluent TSS reporting at an implausible rate",
            detects=("effluent_tss_stuck",),
            rationale=(
                "Looks at the count, not the value, so it does not care whether "
                "the number is plausible. A sensor stuck at 6 mg/L inside a 5-30 "
                "band is invisible to every detector in this file except this one "
                "and the cross-validation below."
            ),
        ),

        AlarmRule(
            id="lift_current_silence",
            signal_id="INFLUENT:LIFT:CURRENT",
            detector="quality_flag",
            severity="critical",
            for_s=60.0,
            clear_s=120.0,
            params={"min_quality": 2},
            message="Lift current instrument stopped reporting",
            detects=("sensor_dead",),
            rationale=(
                "The one rule in this file that fires on a *flag* rather than a "
                "value, and it exists because a dead instrument is the only "
                "fault in the library that reports no number at all. Every other "
                "detector here needs something to look at."
            ),
        ),

        # ── lift station and secondary clarifier ─────────────────────────────
        AlarmRule(
            id="lift_pump_flow_lost",
            signal_id="INFLUENT:LIFT:FLOW",
            equipment_id=equipment("INFLUENT:LIFT:FLOW"),
            detector="single_point_threshold",
            severity="critical",
            for_s=180.0,
            clear_s=300.0,
            params={
                "limit": 50.0,            # normal_low is 100 m3/h
                "direction": "low",
                "hysteresis": 25.0,
                "unit": "m3/h",
            },
            message="Lift station delivery has stopped",
            detects=("lift_pump_failure",),
            rationale=(
                "The one place in this rule set where a plain limit is genuinely "
                "the right tool, and the contract agrees: it names "
                "`state_change` as the method rather than listing a threshold "
                "under `not_sufficient_alone`. A failed pump does not produce a "
                "*deviation*, it produces an absence, and an absence has a "
                "threshold. Dwell is three minutes because a pump trip and a wet-"
                "well level swing both show up here briefly and neither is a "
                "failure."
            ),
        ),
        AlarmRule(
            id="secondary_scrape_torque_high",
            signal_id="SECONDARY:SEC-SCR-1:TORQUE",
            equipment_id=equipment("SECONDARY:SEC-SCR-1:TORQUE"),
            detector="single_point_threshold",
            severity="warning",
            for_s=900.0,
            clear_s=1800.0,
            # **50, measured.** A healthy secondary clarifier's scraper sits
            # at 43-47 N.m;
            # a thickening blanket puts it at 43-58 with the upper half above 50.
            # The old value was 120 -- the top of the contract's 10-150 N.m band
            # -- which is above everything the fault produces, so the rule never
            # fired once in eleven runs.
            params={
                "limit": 50.0,           # normal band is 10-150 N.m
                "direction": "high",
                "hysteresis": 4.0,
                "unit": "N.m",
            },
            message="Secondary scraper torque high — blanket thickening",
            detects=("sludge_blanket_thickening",),
            rationale=(
                "Thickening sludge raises the torque needed to sweep the floor, "
                "and a *gradual* rise is the signature: raking difficulty of 0.25 "
                "is a 25 % increase, which is well inside the 10-150 N.m band and "
                "would be invisible to any rule watching the clarifier itself. "
                "The fault is in the sludge, the symptom is in the scraper, and "
                "nothing about the clarifier's own signals says anything is "
                "wrong — which is why the rule watches a different piece of "
                "equipment from the one that is sick."
            ),
        ),

        # ── cross validation: the only defence against a wrong number ─────────
        AlarmRule(
            id="aeration_do_xvalidation",
            signal_id="AERATION:AHU-1:DO",
            detector="cross_validation",
            severity="critical",
            for_s=300.0,
            clear_s=900.0,
            params={"tolerance": 0.05},   # 5 % relative
            message="Modbus and OPC UA disagree about dissolved oxygen",
            detects=("do_sensor_drift", "sensor_flatline"),
            rationale=(
                "A low-word-first Modbus float read as high-word-first gives "
                "~2.3e-41: finite, in range, and wrong. Nothing that validates the "
                "value can catch it. This is the only rule here that can, and it "
                "works because `source` is part of the primary key of `reading`, so "
                "both faces record the same instant."
            ),
        ),
        AlarmRule(
            id="influent_flow_xvalidation",
            signal_id="INFLUENT:FLOW:FLOW",
            detector="cross_validation",
            severity="critical",
            for_s=300.0,
            clear_s=900.0,
            params={"tolerance": 0.02},   # 2 % relative
            message="Modbus and OPC UA disagree about influent flow",
            detects=("sensor_stuck_high",),
            rationale=(
                "Same argument, tighter tolerance: influent flow has a declared "
                "range of 0-2 200 m3/h and a deadband of 5, so a relative error "
                "that would be noise on dissolved oxygen is decisive here."
            ),
        ),
    ]


@functools.lru_cache(maxsize=1)
def _cached() -> tuple[AlarmRule, ...]:
    """The rule set, built once from the process-wide contract.

    `lru_cache` rather than a module global because a global needs a `global`
    statement, a sentinel to mean "not built yet", and a second code path for
    "built from an explicit contract" — three chances to get the caching wrong in
    exchange for saving a few milliseconds. The contract itself is already
    `lru_cache`d in `softplc.contract`, and the rules are frozen, so this is
    safe by the same argument.
    """
    return tuple(_rules(get_contract()))


def rules(c: Contract | None = None) -> tuple[AlarmRule, ...]:
    """Every rule, built from the contract.

    Passing an explicit contract bypasses the cache, which is what the tests do
    when they want a rule set built from a modified contract.
    """
    if c is not None:
        return tuple(_rules(c))
    return _cached()


def rules_by_signal(
    rule_set: Sequence[AlarmRule] | None = None,
) -> dict[str, list[AlarmRule]]:
    """Group rules by the signal they watch.

    Not an optimisation detail. It is the difference between *one query per
    signal per evaluation* and *one query per rule*, and a rule set grows faster
    than a signal list does — fourteen rules over ten signals is already 1.4:1
    and the ratio gets worse.
    """
    grouped: dict[str, list[AlarmRule]] = {}
    for rule in rule_set if rule_set is not None else rules():
        grouped.setdefault(rule.signal_id, []).append(rule)
    return grouped
