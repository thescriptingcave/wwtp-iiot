"""Control logic as function blocks.

These are the blocks a real controller would hold in program memory, and they
follow the same discipline: fixed execution order, no side effects outside the
image, and no hidden state. A block that needs to remember something keeps it in
a dataclass field, not in a module global.

The patterns here are the ones that distinguish control logic from arithmetic:

* **Interlock** — a permissive that must be true before a command is granted.
* **Latching trip** — once tripped, stays tripped until explicitly reset. A
  recomputed condition is not a trip: a fault that clears itself when the
  measurement returns to normal is a fault nobody can investigate.
* **Hard stop vs request** — a trip forces the output low *now*; it is not a
  request the next block may overrule.
* **Lead/lag duty rotation** — levelling runtime so no machine wears out early.
* **Hysteresis** — one threshold to switch on, another to switch off. A single
  threshold chatters whenever a value sits on it.
* **PI control** — proportional and integral, with the integral clamped so a
  controller cannot wind up while its actuator is saturated.

Written here as Structured Text first, then Python. The pairing is the lesson:
the ST is what a PLC would actually hold, and the Python is what runs in the
soft PLC. Reading them side by side is the fastest way to understand why control
code is written the way it is — every construct that looks awkward in ST exists
because the controller evaluates in a fixed order with a bounded cycle time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from softplc.scanloop import IOImage, LogicBlock, ScanState


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ═════════════════════════════════════════════════════════════════════════════
# Interlocks and latching
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class LatchedTrip:
    """A trip that holds until reset.

        (* Structured Text *)
        IF trip_condition THEN
            trip_latched := TRUE;
        END_IF;
        IF NOT reset_requested THEN
            trip_latched := FALSE;
        END_IF;

    The reset is *deliberately* explicit. A trip that clears itself the moment
    the offending reading recovers destroys the evidence: the alarm is gone
    before anyone has looked, and a plant that trips and returns to normal with
    no record produces a historian full of flat lines and no explanation.
    """

    name: str
    latched: bool = False
    #: When the trip was first raised, for the event log.
    raised_at_s: float = 0.0
    #: Set once and then requires a reset; prevents auto-retriggers within a
    #: cooldown window when a fault chatters.
    hold_s: float = 0.0
    _armed_at_s: float | None = None

    def update(self, condition: bool, now_s: float, reset: bool = False) -> bool:
        if condition and not self.latched:
            self.latched = True
            self.raised_at_s = now_s
            self._armed_at_s = now_s
        elif self.latched and reset and (self._armed_at_s is None or
                                         now_s - self._armed_at_s >= self.hold_s):
            self.latched = False
            self._armed_at_s = None
        return self.latched


@dataclass(slots=True)
class InterlockSet:
    """A named set of permissives gating one command.

        (* Structured Text *)
        IF perm_1 AND perm_2 AND NOT trip THEN
            cmd_granted := TRUE;
        ELSE
            cmd_granted := FALSE;
        END_IF;

    The distinction that matters: an interlock is a *condition*, evaluated fresh
    every scan. A trip is a *state*, latched. Conflating them is why some plants
    cannot explain how a motor started with a failed interlock.
    """

    name: str
    permissives: dict[str, bool] = field(default_factory=dict)

    def satisfied(self) -> bool:
        return all(self.permissives.values())

    def missing(self) -> list[str]:
        return [k for k, v in self.permissives.items() if not v]

    def update(self, **permissives: bool) -> None:
        self.permissives.update(permissives)


# ═════════════════════════════════════════════════════════════════════════════
# Proportional-integral control
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PIController:
    """PI control with anti-windup and a derivative-free output limit.

        (* Structured Text *)
        error := setpoint - process;
        integral := integral + Ki * error * cycle_time;
        integral := LIMIT(0, integral, integral_max);
        output := Kp * error + integral;
        output := LIMIT(output_min, output, output_max);

    Anti-windup is the part that is easy to leave out and always matters. While
    the actuator is saturated the error keeps accumulating, and when the
    condition clears the controller slams to the output limit and stays there —
    which looks like a control loop going unstable, and is entirely an artefact
    of the integral term.

    The derivative term is deliberately absent. A DO loop has minutes of lag
    between air flow and the measurement; differentiating that amplifies noise
    and does nothing useful.
    """

    name: str
    kp: float
    ki: float
    output_min: float = 0.0
    output_max: float = 100.0
    integral: float = 0.0
    output: float = 0.0
    error: float = 0.0
    #: Saturated output tracking, for diagnosing windup in the tests.
    saturated: bool = False

    def update(self, setpoint: float, process: float, dt_s: float,
               output_min: float | None = None,
               output_max: float | None = None) -> float:
        lo = self.output_min if output_min is None else output_min
        hi = self.output_max if output_max is None else output_max

        self.error = setpoint - process
        self.integral += self.ki * self.error * dt_s
        # Clamp the integral to the output range, in output units. Integrating
        # without this bound is how a controller winds up to a huge number
        # during a fault and then takes minutes to unwind.
        self.integral = _clamp(self.integral, lo - self.kp * self.error, hi)

        raw = self.kp * self.error + self.integral
        self.saturated = raw < lo or raw > hi
        self.output = _clamp(raw, lo, hi)
        return self.output

    def reset(self) -> None:
        self.integral = 0.0
        self.output = 0.0
        self.error = 0.0
        self.saturated = False


# ═════════════════════════════════════════════════════════════════════════════
# Hysteresis
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class HysteresisSwitch:
    """A switch with separate on and off thresholds.

        (* Structured Text *)
        IF (NOT state) AND value > on_threshold THEN
            state := TRUE;
        ELSIF state AND value < off_threshold THEN
            state := FALSE;
        END_IF;

    This is the alarm-engine primitive, and the reason a single-threshold alarm
    chatters when a value sits on its limit. Every state-based alarm in this
    plant uses one, which is why an operator can act on them.
    """

    name: str
    on_threshold: float
    off_threshold: float
    state: bool = False

    def __post_init__(self) -> None:
        if self.on_threshold < self.off_threshold:
            raise ValueError(
                f"{self.name}: on_threshold must be >= off_threshold, "
                f"got on={self.on_threshold} off={self.off_threshold}"
            )

    def update(self, value: float) -> bool:
        if not self.state and value > self.on_threshold:
            self.state = True
        elif self.state and value < self.off_threshold:
            self.state = False
        return self.state


@dataclass(slots=True)
class OnOffDelay:
    """Delay before an alarm annunciates, and before it clears.

    Both directions matter. On-delay filters a transient spike that a real
    process always contains; off-delay stops an alarm flapping on and off around
    its threshold as a value drifts. An alarm with neither is a nuisance alarm,
    and a plant with enough of them has an alarm system nobody reads.
    """

    name: str
    on_delay_s: float = 0.0
    off_delay_s: float = 0.0
    _true_since: float | None = None
    _false_since: float | None = None
    active: bool = False

    def update(self, condition: bool, now_s: float) -> bool:
        if condition:
            self._false_since = None
            if self._true_since is None:
                self._true_since = now_s
            if not self.active and now_s - self._true_since >= self.on_delay_s:
                self.active = True
        else:
            self._true_since = None
            if self._false_since is None:
                self._false_since = now_s
            if self.active and now_s - self._false_since >= self.off_delay_s:
                self.active = False
        return self.active


# ═════════════════════════════════════════════════════════════════════════════
# Duty rotation
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class DutyRotator:
    """Lead/lag rotation, driven by runtime imbalance.

        (* Structured Text *)
        IF (lead_runtime - lag_runtime) > rotation_threshold THEN
            lead_lag := NOT lead_lag;
        END_IF;

    Equalising runtime is the whole point. A plant that always starts the same
    motor replaces that motor every year while its identical twin sits idle
    against a wall.
    """

    name: str
    rotation_threshold_h: float = 50.0
    lead_index: int = 0
    rotations: int = 0

    def update(self, runtimes_h: list[float]) -> None:
        if len(runtimes_h) < 2:
            return
        lead = runtimes_h[self.lead_index]
        others = [r for i, r in enumerate(runtimes_h) if i != self.lead_index]
        if lead - min(others) > self.rotation_threshold_h:
            self.lead_index = (self.lead_index + 1) % len(runtimes_h)
            self.rotations += 1

    @property
    def rotated(self) -> bool:
        return self.rotations > 0


# ═════════════════════════════════════════════════════════════════════════════
# The plant's control program
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class AerationControl:
    """DO control for the aeration basin.

    The control problem: hold a DO setpoint by moving blower output, where the
    relationship between air and DO is a *saturating* curve, the load varies
    diurnally and with every storm, and the biology responds over minutes rather
    than seconds.

    So: a slow PI loop on DO, and the plant's air-flow calculation decides what
    the blowers can physically deliver. The controller's job when the two
    disagree is to *say so* — an aeration-limited basin is an operating
    condition an operator needs to know about, and a controller that quietly
    saturates is hiding it.
    """

    setpoint_mg_l: float = 2.0
    kp: float = 12.0
    ki: float = 0.10
    output_min: float = 0.0
    output_max: float = 100.0
    controller: PIController = field(
        default_factory=lambda: PIController("do", 12.0, 0.10, 0.0, 100.0)
    )
    #: True when the blowers cannot deliver what the loop is asking for.
    aeration_limited: bool = False
    #: Duty percent actually commanded, after the plant's own limit.
    duty_pct: float = 0.0

    def update(self, do_mg_l: float, required_air_m3h: float,
               capacity_m3h: float, dt_s: float) -> float:
        demand = self.controller.update(
            self.setpoint_mg_l, do_mg_l, dt_s,
            self.output_min, self.output_max,
        )
        # What the air the loop asked for would cost in blower capacity.
        achievable = min(1.0, demand / 100.0) * max(1.0, capacity_m3h)
        self.aeration_limited = required_air_m3h > capacity_m3h
        self.duty_pct = demand
        return achievable


@dataclass(slots=True)
class LiftStationControl:
    """Wet-well level control with interlocks, trips and duty rotation."""

    level_start_m: float = 3.0
    level_stop_m: float = 1.2
    level_alarm_m: float = 5.5
    level_trip_m: float = 6.8
    level_min_pump_m: float = 0.6

    estop: LatchedTrip = field(default_factory=lambda: LatchedTrip("estop", hold_s=5.0))
    low_level: LatchedTrip = field(
        default_factory=lambda: LatchedTrip("pump_low_suction", hold_s=10.0)
    )
    motor_overload: LatchedTrip = field(
        default_factory=lambda: LatchedTrip("motor_overload", hold_s=10.0)
    )
    level_alarm: OnOffDelay = field(
        default_factory=lambda: OnOffDelay("wetwell_high", 5.0, 30.0)
    )
    rotator: DutyRotator = field(default_factory=lambda: DutyRotator("pit"))
    perms: InterlockSet = field(
        default_factory=lambda: InterlockSet("pit_start_permissives")
    )

    def start_permissives(self, *, estop: bool, overload: bool,
                          level_ok: bool, suction_ok: bool) -> InterlockSet:
        """A pump may start only if every permissive holds.

            (* Structured Text *)
            perm_start := NOT estop
                          AND NOT motor_overload
                          AND level_above_min
                          AND suction_level_ok;
        """
        self.perms.update(
            not_estop=not estop,
            not_overloaded=not overload,
            level_above_minimum=level_ok,
            suction_available=suction_ok,
        )
        return self.perms

    def can_start(self) -> bool:
        return self.perms.satisfied()

    def why_blocked(self) -> list[str]:
        """Why a start was refused.

        Kept because an interlock that blocks a start without saying which one is
        a genuinely maddening thing to diagnose from a panel, and the diagnostic
        is nearly free to carry alongside the boolean.
        """
        return self.perms.missing()

    def update_trips(self, *, estop: bool, motor_overload: bool,
                     level_m: float, now_s: float,
                     reset: bool = False) -> None:
        self.estop.update(estop, now_s, reset)
        self.motor_overload.update(motor_overload, now_s, reset)
        self.low_level.update(level_m < self.level_min_pump_m, now_s, reset)
        self.level_alarm.update(level_m > self.level_alarm_m, now_s)

    def any_tripped(self) -> bool:
        return self.estop.latched or self.motor_overload.latched or self.low_level.latched


def build_blocks(control: AerationControl,
                 lift: LiftStationControl) -> list[LogicBlock]:
    """Assemble the control program, in the order it must be evaluated.

    Order is the design, not an implementation detail:

      10  trips first, because every later block reads them
      20  interlocks next, because they gate the commands
      30  DO control
      40  level control and duty rotation
      50  outputs last, once everything that gates them is known

    Reordering these changes plant behaviour, exactly as it would on a real
    controller.
    """
    state = {"now_s": 0.0}

    def trips(img: IOImage) -> None:
        lift.update_trips(
            estop=img.get("estop_active") > 0.5,
            motor_overload=any(img.faults.values()),
            level_m=img.get("INFLUENT:LIFT:WETWELL_LEVEL"),
            now_s=state["now_s"],
            reset=img.get("reset_requested") > 0.5,
        )

    def interlocks(img: IOImage) -> None:
        level = img.get("INFLUENT:LIFT:WETWELL_LEVEL")
        lift.start_permissives(
            estop=lift.estop.latched,
            overload=lift.motor_overload.latched,
            level_ok=level > lift.level_min_pump_m,
            suction_ok=not lift.low_level.latched,
        )

    def do_control(img: IOImage) -> None:
        required = img.get("AERATION:AHU-1:AIR_FLOW")
        capacity = img.get("_blower_capacity", 26000.0)
        duty = control.update(
            img.get("AERATION:AHU-1:DO"), required, capacity, 1.0
        )
        img.set_output("_do_duty_pct", duty)
        img.set_output("_aeration_limited", 1.0 if control.aeration_limited else 0.0)

    def level_control(img: IOImage) -> None:
        level = img.get("INFLUENT:LIFT:WETWELL_LEVEL")
        if lift.can_start() and level >= lift.level_start_m:
            img.set_state("PIT-1", ScanState.RUNNING)
        elif level <= lift.level_stop_m:
            for eq in ("PIT-1", "PIT-2", "PIT-3"):
                img.set_state(eq, ScanState.STANDBY if eq == "PIT-3" else ScanState.STOPPED)
        # Annunciate rather than merely inhibit: a blocked start with no reason
        # shown is the kind of thing that costs an afternoon.
        blocked = lift.why_blocked()
        img.set_output("_start_blocked_count", float(len(blocked)))

    def clock(img: IOImage, hb: int) -> None:
        state["now_s"] += 0.02

    return [
        LogicBlock("trips", trips, order=10),
        LogicBlock("interlocks", interlocks, order=20),
        LogicBlock("dissolved_oxygen", do_control, order=30),
        LogicBlock("wet_well_level", level_control, order=40),
    ]
