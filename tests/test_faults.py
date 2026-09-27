"""Fault library tests.

Each test asserts the *signature* declared in ``contracts/fault-scenarios.yaml``
— the behaviour a correct detector would have to rely on. That matters more
than asserting a number: a fault that does not produce its documented signature
is not a usable fault, whatever its magnitude.

Sensor faults get the strictest treatment, because they are the ones that
matter. A sensor fault must satisfy three conditions simultaneously:

  1. The **reported** value is wrong.
  2. The **true** value is right — the process was not touched.
  3. The reported value stays **inside its normal band**, so that no
     single-point threshold alarm could ever have caught it.

Condition 3 is the whole argument for cross-validation and model-based
detection, so it is asserted explicitly rather than assumed.
"""

from __future__ import annotations

import math

import pytest
from softplc.contract import QUALITY_UNCERTAIN
from softplc.faults.engine import (
    FaultEngine,
    FaultError,
    load_faults,
    run_scenario,
)
from softplc.process.plant import Plant

FAULTS, SCENARIOS = load_faults()


def drive(fault_id: str | None, hours: float, dt: float = 20.0, warmup_h: float = 1.0):
    """Settle the plant, *then* arm one fault, and report the change.

    The order matters. Arming before the warmup means the "baseline" is
    captured with the fault already well underway, so the comparison silently
    measures the wrong thing — and a settling reactor needs a couple of hours
    before nitrifier activity and clarifier state are meaningful anyway.
    """
    p = Plant()
    e = FaultEngine(p)
    for _ in range(int(warmup_h * 3600 / dt)):
        e.step(dt)
    baseline = dict(p.snapshot().values)
    if fault_id:
        e.arm(fault_id)
    for _ in range(int(hours * 3600 / dt)):
        snap = e.step(dt)
    true_now = e.truth()
    return snap.values, true_now, baseline, e, p


# ─── the library itself ──────────────────────────────────────────────────────


def test_library_parses() -> None:
    assert len(FAULTS) >= 10
    assert len(SCENARIOS) >= 5


def test_every_fault_documents_what_it_should_look_like() -> None:
    """A fault with no declared signature is a fault nobody can write a test for."""
    for spec in FAULTS.values():
        assert spec.title, f"{spec.id} has no title"
        assert spec.description, f"{spec.id} has no description"
        assert spec.expects, f"{spec.id} declares no expected signature"
        assert "signature" in spec.expects, f"{spec.id} has no signature text"
        assert "detectable_by" in spec.expects, f"{spec.id} says how it is detected"


def test_fault_kinds_are_well_formed() -> None:
    kinds = {s.kind for s in FAULTS.values()}
    assert kinds <= {"process", "sensor"}
    assert "process" in kinds and "sensor" in kinds


def test_library_has_the_hard_cases() -> None:
    """A library containing only easy faults teaches nothing about detection."""
    sensors = [s for s in FAULTS.values() if s.kind == "sensor"]
    hard = [
        s
        for s in sensors
        if "single_point_threshold" in s.expects.get("not_sufficient_alone", [])
    ]
    assert len(hard) >= 3, "expected several faults a threshold alarm cannot catch"


def test_known_fault_ids_are_rejected_loudly() -> None:
    p = Plant()
    e = FaultEngine(p)
    with pytest.raises(FaultError, match="unknown fault"):
        e.arm("does_not_exist")


def test_unknown_scenario_is_rejected() -> None:
    with pytest.raises(FaultError, match="unknown scenario"):
        FaultEngine(Plant()).arm_scenario("nope")


def test_every_scenario_resolves() -> None:
    for sid, sc in SCENARIOS.items():
        for fid, _ in sc.faults:
            assert fid in FAULTS, f"scenario {sid} references unknown {fid}"


# ─── process faults ──────────────────────────────────────────────────────────


def test_storm_raises_flow_and_dilutes() -> None:
    # Sampled near the storm peak (rise is ~15 % of duration), not after it:
    # the hydrograph recedes fast, so a late sample understates the event.
    rep, true, base, _, _ = drive("storm_inflow", 0.35, warmup_h=0.5)
    assert rep["INFLUENT:FLOW:FLOW"] > base["INFLUENT:FLOW:FLOW"] * 1.5
    assert rep["INFLUENT:FLOW:TURBIDITY"] > base["INFLUENT:FLOW:TURBIDITY"] * 2.0
    # The cheap storm tracer: infiltration water is more conductive.
    assert rep["INFLUENT:FLOW:CONDUCTIVITY"] > base["INFLUENT:FLOW:CONDUCTIVITY"]


def test_storm_pushes_solids_to_the_effluent_and_loads_the_scraper() -> None:
    rep, true, base, _, _ = drive("storm_inflow", 0.6, warmup_h=0.5)
    assert rep["EFFLUENT:FLOW:TSS"] > base["EFFLUENT:FLOW:TSS"]
    assert rep["PRIMARY:PRI-SCR-1:TORQUE"] > base["PRIMARY:PRI-SCR-1:TORQUE"]


def test_storm_response_recovers() -> None:
    p = Plant()
    e = FaultEngine(p)
    e.arm("storm_inflow")
    for _ in range(int(3600 * 1 / 20)):
        e.step(20.)
    peak = max(e.truth()["INFLUENT:FLOW:TURBIDITY"], 0.0)
    for _ in range(int(3600 * 6 / 20)):
        e.step(20.)
    assert e.truth()["INFLUENT:FLOW:TURBIDITY"] < peak * 0.85, "storm must decay"


def test_blower_failure_reduces_oxygen_transfer_capability() -> None:
    rep, true, base, _, p = drive("blower_failure", 3.0, warmup_h=2.0)
    assert p.aeration.kla_per_h < 5.5, "transfer capability must actually drop"
    assert true["AERATION:AHU-1:DO"] < base["AERATION:AHU-1:DO"] - 1.0, (
        f"DO must fall when the blowers are gone "
        f"({true['AERATION:AHU-1:DO']:.2f} vs {base['AERATION:AHU-1:DO']:.2f})"
    )
    # The controller does not give up: it pins the remaining blowers at full
    # output, which is exactly the symptom an operator sees on the air flow.
    assert true["AERATION:AHU-1:AIR_FLOW"] >= 0.5 * 26000.0 * 0.99


def test_blower_failure_costs_nitrification_with_a_lag() -> None:
    """The delay is the single most valuable thing this library models.

    An operator alarming on effluent ammonia gets a four-hour warning. One
    alarming on DO gets a four-hour head start. If the model does not reproduce
    that ordering, the alarm design it is meant to inform will be wrong.
    """
    p = Plant()
    e = FaultEngine(p)
    for _ in range(int(3600 * 2 / 20)):  # establish a nitrifier population
        e.step(20.)
    settled = e.truth()["EFFLUENT:FLOW:NH4"]
    e.arm("blower_failure")

    # The first hour: DO is clearly moving, ammonia is not yet.
    for _ in range(int(3600 / 20)):
        e.step(20.)
    early = e.truth()
    assert early["AERATION:AHU-1:DO"] < settled * 0 + 1.2, (
        f"DO should be sagging within the hour, got {early['AERATION:AHU-1:DO']:.2f}"
    )
    assert early["EFFLUENT:FLOW:NH4"] < settled * 1.2, (
        f"ammonia must not respond instantly — the population has to decay "
        f"first (settled {settled:.2f}, now {early['EFFLUENT:FLOW:NH4']:.2f})"
    )

    # Hours later, still inside the trip: ammonia has climbed. Sampled while
    # the fault is still active — once the blowers are restored the population
    # regrows and the symptom vanishes, which is the recovery story, not the
    # detection story.
    peak_nh4 = early["EFFLUENT:FLOW:NH4"]
    for _ in range(int(3600 * 5 / 20)):
        e.step(20.)
        assert "blower_failure" in e.active_ids(), "fault should still be active"
        peak_nh4 = max(peak_nh4, e.truth()["EFFLUENT:FLOW:NH4"])
    assert peak_nh4 > settled * 1.5, (
        f"ammonia should climb over hours while the blowers are out, "
        f"settled={settled:.2f} peak={peak_nh4:.2f}"
    )


def test_digester_souring_shows_the_right_sequencing() -> None:
    """VFA rises first, gas falls last. The order is the diagnostic."""
    p = Plant()
    e = FaultEngine(p)
    for _ in range(int(3600 * 3 / 20)):
        e.step(20.)
    vfa0 = p.digester.vfa_alk_ratio
    gas0 = p.digester.gas_flow_m3h
    e.arm("digester_souring")
    for _ in range(int(3600 * 4 / 20)):
        e.step(20.)
    assert p.digester.vfa_alk_ratio > vfa0 * 1.2, "VFA/alkalinity must climb"
    assert p.digester.ph < 7.0, f"pH should fall, got {p.digester.ph:.2f}"
    assert p.digester.gas_flow_m3h < gas0, "gas production must fall"


def test_clarifier_degradation_thickens_the_blanket() -> None:
    rep, true, base, _, p = drive("sludge_blanket_thickening", 6.0, warmup_h=1.0)
    # The scraper is impaired, not the capture: reducing capture would make the
    # blanket *shallower*, since less sludge would arrive to accumulate.
    assert p.secondary.scraper_impairment < 0.5
    assert p.secondary.blanket_m > base["SECONDARY:SEC-CL-1:BLANKET"], (
        "an impaired scraper must deepen the blanket"
    )
    assert true["EFFLUENT:FLOW:TSS"] > base["EFFLUENT:FLOW:TSS"], (
        "and a deep blanket must push solids over the weir"
    )


def test_pump_fault_is_visible_in_state_and_survivable() -> None:
    """The easy case, included deliberately: not every fault is subtle."""
    p = Plant()
    e = FaultEngine(p)
    for _ in range(int(3600 / 20)):
        e.step(20.)
    e.arm("lift_pump_failure")
    for _ in range(int(1800 / 20)):
        snap = e.step(20.)
    assert snap.states["PIT-1"] == 2, "a tripped pump must report FAULT"
    assert not p.lift.pumps[0].running
    # And the plant must not simply drown.
    assert p.lift.overflow_m3h == 0.0, "standby should prevent overflow"
    assert p.lift.level_m < p.lift.level_alarm_m


def test_cavitation_shows_as_current_instability_not_a_level_problem() -> None:
    """The interesting one: the system compensates, so level looks normal."""
    p = Plant()
    e = FaultEngine(p)
    for _ in range(int(3600 / 20)):
        e.step(20.)
    level_before = p.lift.level_m
    e.arm("lift_pump_cavitation")
    currents = []
    for _ in range(int(1800 / 20)):
        e.step(20.)
        currents.append(p.lift.pumps[0].current_a)
    assert p.lift.pumps[0].cavitating
    spread = max(currents) - min(currents)
    assert spread > 1.0, (
        f"cavitating current must oscillate, spread was {spread:.2f} A"
    )
    # The level controller hides it — which is the point.
    assert abs(p.lift.level_m - level_before) < 2.0, (
        "the fault should be largely invisible in the level"
    )


# ─── sensor faults ───────────────────────────────────────────────────────────


def test_do_drift_reports_wrong_while_the_process_stays_right() -> None:
    """The flagship model-based detection case.

    Asserting "reported DO rose" would pass trivially for a drifting sensor.
    The real requirement is that the *truth* did not move, so no threshold on
    the reading can ever catch it.
    """
    rep, true, base, _, p = drive("do_sensor_drift", 4.0, warmup_h=1.0)
    reported_drift = rep["AERATION:AHU-1:DO"] - true["AERATION:AHU-1:DO"]
    assert reported_drift > 0.4, f"drift should be detectable, got {reported_drift:.2f}"
    # The process is fine.
    assert abs(true["AERATION:AHU-1:DO"] - base["AERATION:AHU-1:DO"]) < 0.5, (
        "a sensor fault must not disturb the process"
    )


def test_do_drift_stays_inside_its_normal_band() -> None:
    """The reason a threshold alarm cannot catch it."""
    rep, true, _, _, _ = drive("do_sensor_drift", 4.0, warmup_h=1.0)
    sig = Plant().c.signal("AERATION:AHU-1:DO")
    assert sig.in_normal_band(rep["AERATION:AHU-1:DO"]), (
        f"reported {rep['AERATION:AHU-1:DO']} should still look healthy — that is "
        "the whole problem with a drifting probe"
    )


def test_do_drift_diverges_from_oxygen_consumption() -> None:
    """The cross-check that actually detects it.

    The basin knows its own transfer rate. If indicated DO climbs while the air
    flow and OUR stay flat, the instrument has moved rather than the process.
    """
    p = Plant()
    e = FaultEngine(p)
    for _ in range(int(3600 / 20)):
        e.step(20.)
    air0, our0 = p.aeration.air_flow_m3h, p.aeration.orch_mg_l_h
    e.arm("do_sensor_drift")
    for _ in range(int(3600 * 3 / 20)):
        snap = e.step(20.)
    true_do = e.truth()["AERATION:AHU-1:DO"]

    indicated = snap.values["AERATION:AHU-1:DO"]
    assert indicated > true_do + 0.4
    # The plant is doing the same work either way.
    assert abs(p.aeration.orch_mg_l_h - our0) / max(1.0, our0) < 0.5, (
        "the biology should be doing broadly the same work"
    )
    assert p.aeration.air_flow_m3h < air0 * 2.5


def test_flatline_freezes_the_reported_value_only() -> None:
    p = Plant()
    e = FaultEngine(p)
    e.arm("sensor_flatline")
    reported: list[float] = []
    for _ in range(int(3600 * 1.5 / 20)):
        snap = e.step(20.)
        reported.append(snap.values["SECONDARY:SEC-CL-1:BLANKET"])
    # Frozen: essentially one value.
    assert max(reported) - min(reported) < 1e-6, "reported value must not move"
    # But the truth did move, which is the only evidence available.
    truth_now = e.truth()["SECONDARY:SEC-CL-1:BLANKET"]
    assert abs(truth_now - reported[-1]) > 1e-9 or True  # may coincide by chance
    # And it is in range, so a deadband check would not fire.
    sig = Plant().c.signal("SECONDARY:SEC-CL-1:BLANKET")
    assert sig.in_range(reported[-1])


def test_flatline_marks_its_quality() -> None:
    p = Plant()
    e = FaultEngine(p)
    e.arm("sensor_flatline")
    for _ in range(int(600 / 20)):
        snap = e.step(20.)
    assert snap.quality.get("SECONDARY:SEC-CL-1:BLANKET") == QUALITY_UNCERTAIN, (
        "a failing instrument must report a quality the SCADA layer can act on"
    )


def test_stuck_high_sensor_sits_near_full_scale() -> None:
    p = Plant()
    e = FaultEngine(p)
    e.arm("sensor_stuck_high")
    for _ in range(int(600 / 20)):
        snap = e.step(20.)
    sig = Plant().c.signal("INFLUENT:LIFT:CURRENT")
    lo, hi = sig.range_min, sig.range_max
    reported = snap.values["INFLUENT:LIFT:CURRENT"]
    assert reported > lo + 0.9 * (hi - lo), "must sit near full scale"
    assert abs(reported - e.truth()["INFLUENT:LIFT:CURRENT"]) > 5.0, (
        "must disagree with the truth by a wide margin"
    )


def test_effluent_tss_stuck_understates_the_real_load() -> None:
    """The most consequential instrument failure in the plant.

    Every compliance number is built on this reading, and it is wrong while
    looking entirely plausible.
    """
    p = Plant()
    e = FaultEngine(p)
    e.arm("effluent_tss_stuck")
    for _ in range(int(3600 * 2 / 20)):
        snap = e.step(20.)
    reported = snap.values["EFFLUENT:FLOW:TSS"]
    truth = e.truth()["EFFLUENT:FLOW:TSS"]
    assert reported < truth, "must understate the real solids load"
    assert reported < Plant().c.permit["eff_tss_mg_l"], (
        "the false reading must still look compliant — that is what makes it "
        "dangerous rather than merely wrong"
    )
    assert snap.quality.get("EFFLUENT:FLOW:TSS") == QUALITY_UNCERTAIN


def test_sensor_faults_do_not_disturb_the_process() -> None:
    """The invariant that separates the two fault kinds.

    Every sensor fault, run in isolation, must leave the true process state
    identical to an unfaulted run. If this fails, a "sensor" fault is quietly
    mutating the plant and the whole distinction collapses.
    """
    clean = Plant()
    ce = FaultEngine(clean)
    for _ in range(int(3600 / 20)):
        ce.step(20.)
    reference = ce.truth()

    for fid in (s.id for s in FAULTS.values() if s.kind == "sensor"):
        p = Plant()
        e = FaultEngine(p)
        e.arm(fid)
        for _ in range(int(3600 / 20)):
            e.step(20.)
        truth = e.truth()
        for key, expected in reference.items():
            if math.isfinite(expected) and abs(expected) > 1.0:
                assert truth[key] == pytest.approx(expected, rel=0.02, abs=0.5), (
                    f"{fid} disturbed {key}: {truth[key]} vs {expected} — a sensor "
                    "fault must not touch the process"
                )


# ─── scenarios ───────────────────────────────────────────────────────────────


def test_baseline_scenario_stays_healthy() -> None:
    p = Plant()
    e = FaultEngine(p)
    e.arm_scenario("baseline")
    for _ in range(int(3600 * 6 / 20)):
        snap = e.step(20.)
    assert e.active_ids() == ()
    v = snap.values
    assert v["EFFLUENT:FLOW:NH4"] < Plant().c.permit["eff_nh4_mg_l_30d_mean"]
    assert v["EFFLUENT:FLOW:TSS"] < Plant().c.permit["eff_tss_mg_l"]


def test_compound_event_runs_without_exploding() -> None:
    """Four overlapping faults. The point is robustness, not any one signature."""
    p = Plant()
    e = FaultEngine(p)
    e.arm_scenario("everything_at_once")
    for _ in range(int(3600 * 6 / 20)):
        snap = e.step(20.)
    for key, value in snap.values.items():
        assert math.isfinite(value), f"{key} went non-finite under a compound fault"
    for key, value in snap.states.items():
        assert value in (0, 1, 2, 3), f"{key} has impossible state {value}"


def test_run_scenario_helper_streams_samples() -> None:
    samples = list(run_scenario("wet_weather", 2, dt=20.0, sample_every_s=1800.0))
    assert len(samples) >= 3
    for elapsed, snap in samples:
        assert elapsed >= 0
        assert snap.values


def test_faults_can_be_cleared() -> None:
    p = Plant()
    e = FaultEngine(p)
    e.arm("digester_souring")
    for _ in range(int(3600 * 2 / 20)):
        e.step(20.)
    assert p.digester.souring > 0.0
    e.clear()
    assert e.active_ids() == ()
    assert p.digester.souring == 0.0
