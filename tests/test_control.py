"""Control block tests.

The blocks are tested against the properties a controller must guarantee, not
against outputs recorded from a previous run. These blocks sit between a process
model and a PLC, and a control loop that is *slightly* wrong in a way a
snapshot test would not catch is the kind of defect that shows up as an
unexplainable process upset weeks later.
"""

from __future__ import annotations

import pytest
from softplc.blocks.control import (
    AerationControl,
    DutyRotator,
    HysteresisSwitch,
    InterlockSet,
    LatchedTrip,
    LiftStationControl,
    OnOffDelay,
    PIController,
    build_blocks,
)
from softplc.scanloop import IOImage, ScanLoop

# ─── latched trips ───────────────────────────────────────────────────────────


def test_trip_latches_until_reset() -> None:
    t = LatchedTrip("overtemp")
    assert t.update(True, 0.0) is True
    # The cause has gone, but the trip must hold.
    assert t.update(False, 1.0) is True
    assert t.update(False, 10.0) is True
    # Only an explicit reset clears it.
    assert t.update(False, 11.0, reset=True) is False


def test_trip_records_when_it_was_raised() -> None:
    t = LatchedTrip("overtemp")
    t.update(True, 123.0)
    assert t.raised_at_s == 123.0


def test_trip_hold_time_defers_the_reset() -> None:
    """A chattery fault must not retrigger on every cycle after a reset."""
    t = LatchedTrip("chatter", hold_s=30.0)
    t.update(True, 0.0)
    assert t.update(False, 1.0, reset=True) is True, "reset too early is ignored"
    assert t.update(False, 40.0, reset=True) is False, "honoured after the hold"


def test_trip_that_clears_itself_is_the_failure_mode() -> None:
    """Documents *why* the latch exists.

    Without it, a fault that recovers leaves no trace and the historian shows a
    flat line where the interesting part was.
    """
    t = LatchedTrip("no_latch")
    t.update(True, 0.0)
    assert t.update(False, 1.0) is True, "the evidence must survive the cause"


# ─── interlocks ──────────────────────────────────────────────────────────────


def test_interlock_requires_every_permissive() -> None:
    i = InterlockSet("start")
    i.update(a=True, b=True, c=False)
    assert not i.satisfied()
    assert i.missing() == ["c"]
    i.update(c=True)
    assert i.satisfied()
    assert i.missing() == []


def test_interlock_reports_which_permissive_failed() -> None:
    """An interlock that blocks without saying why costs an afternoon."""
    i = InterlockSet("start")
    i.update(estop=False, overload=True, level_ok=True)
    assert i.missing() == ["estop"]


def test_lift_station_will_not_start_on_an_estop() -> None:
    c = LiftStationControl()
    c.start_permissives(estop=True, overload=False, level_ok=True, suction_ok=True)
    assert not c.can_start()
    assert "not_estop" in c.why_blocked()


# ─── PI control ──────────────────────────────────────────────────────────────


def test_pi_proportional_response() -> None:
    p = PIController("t", kp=10.0, ki=0.0, output_min=0.0, output_max=100.0)
    # Process 5 *below* the setpoint of 7, kp 10 → wants 50.
    assert p.update(7.0, 2.0, 1.0) == pytest.approx(50.0)
    # And the mirror image, clamped at zero.
    assert p.update(2.0, 7.0, 1.0) == pytest.approx(0.0)


def test_pi_eliminates_steady_state_error() -> None:
    """The integral term is what makes a PI loop actually hold a setpoint.

    A pure proportional loop always leaves an error proportional to the load —
    which is why a P-only aeration loop sits at a DO that drifts with the
    diurnal load.
    """
    p = PIController("t", kp=1.0, ki=0.5, output_min=0.0, output_max=100.0)
    for _ in range(4000):
        out = p.update(2.0, 1.0, 1.0)  # constant 1.0 offset
    assert out > 90.0, "integral should have wound up to cover the error"
    # and the error itself is still reported honestly
    assert p.error == pytest.approx(1.0)


def test_pi_does_not_wind_up_while_saturated() -> None:
    """Anti-windup. Without it the controller slams to the limit on recovery."""
    p = PIController("t", kp=1.0, ki=1.0, output_min=0.0, output_max=100.0)
    for _ in range(5000):
        p.update(100.0, 0.0, 1.0)  # demand far above capacity
    assert p.output == 100.0
    assert p.saturated
    integral_while_saturated = p.integral
    assert integral_while_saturated <= 100.0 + 1.0, (
        "the integral must be bounded, not allowed to grow without limit"
    )
    # On recovery it must return promptly rather than staying pinned.
    p.reset()
    assert p.output == 0.0
    assert p.update(2.0, 2.0, 1.0) == pytest.approx(0.0)


def test_pi_respects_output_limits() -> None:
    p = PIController("t", kp=100.0, ki=0.0, output_min=0.0, output_max=10.0)
    assert p.update(100.0, 0.0, 1.0) == 10.0
    assert p.update(0.0, 100.0, 1.0) == 0.0


def test_pi_reset_clears_history() -> None:
    p = PIController("t", kp=1.0, ki=1.0)
    for _ in range(100):
        p.update(10.0, 0.0, 1.0)
    p.reset()
    assert p.integral == 0.0
    assert p.output == 0.0
    assert not p.saturated


def test_derivative_term_is_absent_by_design() -> None:
    """A DO loop has minutes of lag; differentiating that amplifies noise."""
    p = PIController("t", kp=1.0, ki=0.0)
    assert not hasattr(p, "kd"), "a derivative term should not be available"


# ─── hysteresis ──────────────────────────────────────────────────────────────


def test_hysteresis_does_not_chatter() -> None:
    """The single most valuable primitive here.

    A value sitting exactly on a threshold must produce one transition, not a
    stream of them — otherwise the alarm floods and the operator mutes it.
    """
    h = HysteresisSwitch("t", on_threshold=85.0, off_threshold=80.0)
    transitions = 0
    prev = h.state
    for v in (84.9, 85.1, 84.9, 85.1, 85.05, 84.95, 85.0, 85.1, 79.9, 81.0, 80.5, 79.0):
        new = h.update(v)
        if new != prev:
            transitions += 1
            prev = new
    assert transitions <= 2, f"chattered {transitions} times around the threshold"
    assert h.state is False, "should have cleared once it fell below 80"


def test_hysteresis_holds_state_in_the_band() -> None:
    h = HysteresisSwitch("t", on_threshold=85.0, off_threshold=80.0)
    h.update(86.0)
    assert h.state
    # Between the two thresholds the state must not change.
    for v in (84.0, 82.0, 81.0):
        assert h.update(v) is True
    assert h.update(79.0) is False


def test_hysteresis_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError, match="on_threshold"):
        HysteresisSwitch("bad", on_threshold=10.0, off_threshold=20.0)


# ─── on/off delay ────────────────────────────────────────────────────────────


def test_on_delay_suppresses_transients() -> None:
    """A brief spike must not annunciate. Real processes are full of them."""
    d = OnOffDelay("t", on_delay_s=5.0, off_delay_s=30.0)
    for t in range(0, 4):
        assert d.update(True, float(t)) is False, "spike must not annunciate"
    d.update(False, 4.5)
    assert d.active is False


def test_on_delay_annunciates_after_the_delay() -> None:
    d = OnOffDelay("t", on_delay_s=5.0, off_delay_s=30.0)
    for t in range(0, 7):
        d.update(True, float(t))
    assert d.active is True


def test_off_delay_prevents_flapping() -> None:
    d = OnOffDelay("t", on_delay_s=0.0, off_delay_s=30.0)
    d.update(True, 0.0)
    assert d.active
    # Condition goes away briefly, then returns.
    d.update(False, 1.0)
    d.update(True, 2.0)
    assert d.active is True, "must not clear during a brief recovery"
    # The condition must then stay away for the full off-delay.
    d.update(False, 40.0)
    assert d.active is True, "still inside the off-delay"
    d.update(False, 80.0)
    assert d.active is False


# ─── duty rotation ───────────────────────────────────────────────────────────


def test_duty_rotator_evens_out_runtime() -> None:
    r = DutyRotator("pit", rotation_threshold_h=50.0)
    r.update([1000.0, 980.0, 950.0])
    assert r.lead_index == 0, "a 50 h spread is within tolerance"
    # Now the lead is 100 h clear of the next-laggard.
    r.update([1000.0, 900.0, 950.0])
    assert r.lead_index != 0, "the lead has run 100 h longer; must rotate"
    assert r.rotations >= 1


def test_duty_rotator_ignores_a_single_pump() -> None:
    r = DutyRotator("solo")
    r.update([500.0])
    assert r.rotations == 0


# ─── aeration control ────────────────────────────────────────────────────────


def test_aeration_loop_holds_its_setpoint() -> None:
    """A closed-loop check on a simple plant model, not a snapshot value.

    Process: DO rises towards saturation with air, and the biology consumes a
    roughly constant load. The loop must find the air flow that holds the
    setpoint — which is the only thing a DO controller exists to do.
    """
    control = AerationControl(setpoint_mg_l=2.0, kp=20.0, ki=0.5)
    c_star, load = 9.0, 3.0
    do = 2.0
    for i in range(4000):
        air = control.update(do, 4000.0, 26000.0, 1.0)
        # Simple first-order response to delivered air.
        transfer = min(air / 20000.0, 1.0) * 8.0
        do += ((transfer - load) * 0.05) if do < c_star else 0.0
        do = max(0.0, do)
    assert abs(do - 2.0) < 0.35, f"DO {do:.2f} did not converge on 2.0"


def test_aeration_loop_reports_when_it_is_limited() -> None:
    """A controller that quietly saturates hides the one thing an operator needs."""
    control = AerationControl(setpoint_mg_l=2.0)
    control.update(0.2, 40000.0, 26000.0, 1.0)  # wants 40k, only 26k available
    assert control.aeration_limited is True
    control.update(2.0, 2000.0, 26000.0, 1.0)  # within capacity
    assert control.aeration_limited is False


# ─── program assembly ────────────────────────────────────────────────────────


def test_blocks_are_assembled_in_dependency_order() -> None:
    """Order is the design, not an accident: trips gate interlocks, and both
    gate the outputs."""
    control, lift = AerationControl(), LiftStationControl()
    blocks = build_blocks(control, lift)
    names = [b.name for b in blocks]
    assert names.index("trips") < names.index("interlocks")
    assert names.index("interlocks") < names.index("wet_well_level")
    assert names.index("dissolved_oxygen") < names.index("wet_well_level")


def test_a_trip_blocks_the_start_permissive_end_to_end() -> None:
    """The whole chain: fault raised → trip latched → interlock refuses start."""
    control, lift = AerationControl(), LiftStationControl()
    blocks = build_blocks(control, lift)
    plc = ScanLoop(scan_ms=20)
    for b in blocks:
        plc.add_block(b)

    img = IOImage()
    img.inputs["INFLUENT:LIFT:WETWELL_LEVEL"] = 4.0
    plc.set_input_reader(lambda i: None)
    plc.add_listener(lambda i, hb: None)

    # No fault: the start is permitted.
    plc.image = img
    for b in blocks:
        b.fn(img)
    assert lift.can_start()

    # Now raise the e-stop.
    img.inputs["estop_active"] = 1.0
    for b in blocks:
        b.fn(img)
    assert lift.estop.latched
    assert not lift.can_start()
    assert "not_estop" in lift.why_blocked()


def test_control_blocks_stay_inside_the_cycle_budget() -> None:
    """A control program that overruns the scan is a control problem."""
    control, lift = AerationControl(), LiftStationControl()
    plc = ScanLoop(scan_ms=20)
    for b in build_blocks(control, lift):
        plc.add_block(b)
    plc.set_input_reader(lambda i: None)
    for _ in range(500):
        plc.scan_once()
    h = plc.health()
    assert h["budget_used_pct"] < 50.0, (
        f"control program used {h['budget_used_pct']}% of the scan budget"
    )
    assert h["overruns"] == 0
