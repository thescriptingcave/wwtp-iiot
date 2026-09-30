"""The fault injection engine.

A fault library is only worth building if each fault is *injected honestly* and
produces the signature it would in reality. Two rules govern everything here.

**Process faults mutate real plant state.** Nothing is faked at the reporting
layer. A blower trip reduces the aeration system's actual transfer capability
and the physics does the rest — DO falls because less oxygen is transferred, not
because a number was overwritten. This is what makes the resulting trends
trustworthy enough to design an alarm system against.

**Sensor faults corrupt only the published values.** The plant carries on
perfectly; the instrument lies. This asymmetry is the whole point. A sensor
fault applied to the process would be undetectable by definition — the process
*is* wrong — whereas corrupting only what is reported produces exactly the
situation a historian has to cope with: every number looks plausible and the
plant is quietly misrepresented.

That distinction is also why the fault library is worth more than a list of
alarming numbers. A simulator that only produces clean data teaches nothing
about detection, and threshold-based alerting is the thing most likely to fail
in a real plant.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from softplc.process.plant import Plant, PlantSnapshot
from softplc.scanloop import ScanState

# softplc/faults/engine.py → parents[0]=faults, parents[1]=softplc, parents[2]=root
FAULTS_PATH = Path(__file__).resolve().parents[2] / "contracts" / "fault-scenarios.yaml"

#: Quality StatusCode used for instrument faults. A failing transmitter reports
#: a quality the SCADA layer can act on — this is exactly why OPC UA carries
#: StatusCodes and why design rule 4 exists.
QUALITY_UNCERTAIN = 1

#: ...and an instrument that has stopped answering altogether reports ``Bad`` with
#: no value at all. This is the *only* way a ``None`` value reaches the historian,
#: and it is the case three SQL lessons teach: a broken instrument is stored as
#: ``value = NULL`` with ``quality = 2``, never as a plausible number.
#:
#: Every other sensor injector above it corrupts the *value* and reports Uncertain,
#: which is the more insidious failure — the reading looks real. This one is the
#: honest one, and without it the "keep the value, keep the status" rule in
#: ``sql/00-foundations/00-03`` has nothing to teach from.
QUALITY_BAD = 2


class FaultError(RuntimeError):
    """Raised when a fault cannot be applied — a typo, or a target that does
    not exist. Failing loudly matters more here than anywhere else, because a
    silently-missing fault is indistinguishable from a fault with no effect."""


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """One declared fault."""

    id: str
    kind: str  # "process" | "sensor"
    title: str
    description: str
    start_at_s: float
    duration_s: float | None
    ramp_s: float
    targets: tuple[str, ...]
    inject: str
    params: dict[str, Any]
    expects: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    expect: str
    faults: tuple[tuple[str, float], ...]


@dataclass(slots=True)
class ActiveFault:
    """A fault currently being applied."""

    spec: FaultSpec
    start_s: float
    end_s: float | None

    #: Targets this *instance* has frozen, and the value each was frozen at.
    #:
    #: **It belongs here rather than on the engine, and that is the whole fix.**
    #: A frozen value is a property of one fault occurrence — "the reading when
    #: *this* fault began" — and the engine used to hold it in a single
    #: `self._frozen: dict[str, ...]` keyed by target alone. One dict, one capture
    #: per target per *engine lifetime*, cleared only by `clear()` and never by
    #: `_retire()`. So a second flatline on the same target reported a value
    #: frozen at the **first** one's onset: a transmitter that stuck in the morning
    #: went on reporting the morning's reading for the rest of the run, and the
    #: fault appeared never to end.
    #:
    #: Nothing observed it, because every seeded week arms each sensor fault exactly
    #: once. It is here on the instance so that retiring the fault discards the
    #: freeze automatically — no clearing logic to get wrong, and two overlapping
    #: instances each keep their own.
    frozen: dict[str, float | None] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.spec.id

    def intensity(self, now_s: float) -> float:
        """0..1 — how far through its onset the fault is.

        Mechanical and biological faults are not instantaneous. A step change
        in a DO reading is a fault of the *model*, not of the plant, and it
        would let a naive detector pass by accident.
        """
        elapsed = now_s - self.start_s
        if elapsed < 0:
            return 0.0
        ramp = max(1e-6, self.spec.ramp_s)
        return min(1.0, elapsed / ramp)

    def is_active(self, now_s: float) -> bool:
        if now_s < self.start_s:
            return False
        if self.end_s is not None and now_s > self.end_s:
            return False
        return True

    def remaining_s(self, now_s: float) -> float | None:
        if self.end_s is None:
            return None
        return max(0.0, self.end_s - now_s)


@functools.lru_cache(maxsize=1)
def load_faults(path: Path | str | None = None) -> tuple[dict[str, FaultSpec], dict[str, Scenario]]:
    """Parse and validate the fault library."""
    p = Path(path) if path else FAULTS_PATH
    if not p.exists():
        raise FaultError(f"fault library not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))

    faults: dict[str, FaultSpec] = {}
    for f in raw.get("faults", []):
        for key in ("id", "kind", "title", "inject", "targets", "params"):
            if key not in f:
                raise FaultError(f"fault entry missing {key!r}: {f.get('id')}")
        if f["kind"] not in ("process", "sensor"):
            raise FaultError(
                f"{f['id']}: kind must be 'process' or 'sensor', got {f['kind']!r}"
            )
        if f["id"] in faults:
            raise FaultError(f"duplicate fault id: {f['id']}")
        faults[f["id"]] = FaultSpec(
            id=f["id"],
            kind=f["kind"],
            title=f["title"],
            description=" ".join(str(f.get("description", "")).split()),
            start_at_s=float(f.get("start_at_s", 0.0)),
            duration_s=(
                float(f["duration_s"]) if f.get("duration_s") is not None else None
            ),
            ramp_s=float(f.get("ramp_s", 0.0)),
            targets=tuple(f["targets"]),
            inject=f["inject"],
            params=dict(f["params"]),
            expects=dict(f.get("expects", {})),
        )

    scenarios: dict[str, Scenario] = {}
    for s in raw.get("scenarios", []):
        pairs = tuple((fid, float(off)) for fid, off in s.get("faults", []))
        for fid, _ in pairs:
            if fid not in faults:
                raise FaultError(f"scenario {s['id']!r} references unknown fault {fid!r}")
        scenarios[s["id"]] = Scenario(
            id=s["id"],
            title=s.get("title", s["id"]),
            expect=" ".join(str(s.get("expect", "")).split()),
            faults=pairs,
        )

    if not faults:
        raise FaultError("fault library is empty")
    return faults, scenarios


def _signal_range(plant: Plant, signal_id: str) -> tuple[float, float]:
    """Engineering range for a signal, from the contract."""
    sig = plant.c.signal(signal_id)
    return sig.range_min, sig.range_max


class FaultEngine:
    """Applies faults to a running plant.

    Usage::

        plant = Plant()
        engine = FaultEngine(plant)
        engine.arm("do_sensor_drift")
        while running:
            snap = engine.step(20.0)
    """

    def __init__(self, plant: Plant) -> None:
        self.plant = plant
        self.faults, self.scenarios = load_faults()
        self.active: list[ActiveFault] = []
        self.now_s: float = 0.0
        #: Equipment states forced by process faults, and the states to restore.
        self._forced_states: dict[str, int] = {}
        self._original_states: dict[str, int] = {}
        self._digester_original: dict[str, dict[str, float]] = {}
        self._clarifier_originals: dict[str, dict[str, float]] = {}
        self._original_kla: dict[str, tuple[float, float]] = {}
        self.log: list[dict[str, Any]] = []

    # ─── arming ──────────────────────────────────────────────────────────────

    def arm(self, fault_id: str, start_offset_s: float = 0.0) -> ActiveFault:
        """Arm a fault, optionally delayed relative to scenario start."""
        if fault_id not in self.faults:
            raise FaultError(
                f"unknown fault {fault_id!r}; known: {sorted(self.faults)}"
            )
        spec = self.faults[fault_id]
        start = self.now_s + start_offset_s
        end = start + spec.duration_s if spec.duration_s is not None else None
        af = ActiveFault(spec=spec, start_s=start, end_s=end)
        self.active.append(af)
        return af

    def arm_scenario(self, scenario_id: str) -> list[ActiveFault]:
        if scenario_id not in self.scenarios:
            raise FaultError(
                f"unknown scenario {scenario_id!r}; known: {sorted(self.scenarios)}"
            )
        return [self.arm(fid, off) for fid, off in self.scenarios[scenario_id].faults]

    def clear(self) -> None:
        """Remove every fault and undo its effects.

        Effects are reverted rather than merely stopped, because a sensor fault
        that has been frozen into a reported value must not leave that value
        wrong after the fault clears.

        The frozen values themselves need no reverting here: they live on
        ``ActiveFault.frozen``, so emptying ``self.active`` discards them. That is
        the point of putting them there — the guarantee this docstring promises is
        now structural rather than a line somebody has to remember to clear.
        """
        for eq, state in self._forced_states.items():
            self.plant.snapshot  # no-op: states are rebuilt each snapshot
            self._restore_state(eq, state)
        self._forced_states.clear()
        self._original_states.clear()
        for dig_id, saved in self._digester_original.items():
            if dig_id == "DIG-1":
                self.plant.digester.souring = saved["souring"]
        self._digester_original.clear()
        for cl_id, saved in self._clarifier_originals.items():
            if cl_id == "SEC-CL-1":
                self.plant.secondary.health = saved["health"]
        self._clarifier_originals.clear()
        self.active.clear()

    def _restore_state(self, equipment: str, state: int) -> None:
        """Put a piece of equipment back into its pre-fault state."""
        eq = self.plant.c.equipment.get(equipment)
        if eq is None:
            return
        if eq.type == "pump":
            for p in self.plant.lift.pumps:
                if p.id == equipment:
                    p.running = state == int(ScanState.RUNNING)
                    p.fault = 0 if state != int(ScanState.FAULT) else 1
                    p.cavitating = False
                    p.speed = p.speed if p.running else 0.0

    # ─── the step ─────────────────────────────────────────────────────────────

    def step(self, dt: float) -> PlantSnapshot:
        """Advance the plant one step, applying active faults around it.

        Order matters: process faults are applied *before* the physics runs, so
        the consequence propagates through the model honestly. Sensor faults are
        applied *after*, to the published values only.
        """
        for af in list(self.active):
            if not af.is_active(self.now_s) and self.now_s >= (af.end_s or math.inf):
                self._retire(af)

        for af in self.active:
            # Sensor faults are applied to the *published* values after the
            # physics runs. Dispatching one here would mutate the process, which
            # is precisely the thing a sensor fault must never do.
            if af.is_active(self.now_s) and af.spec.kind == "process":
                self._apply_process_fault(af)

        snapshot = self.plant.step(dt)
        self.now_s += dt

        # Sensor faults corrupt only what is reported.
        for af in self.active:
            if af.is_active(self.now_s) and af.spec.kind == "sensor":
                self._apply_sensor_fault(af, snapshot)

        return snapshot

    def _retire(self, af: ActiveFault) -> None:
        """Undo a fault whose duration has expired."""
        if af.id == "lift_pump_failure":
            for t in af.spec.targets:
                for p in self.plant.lift.pumps:
                    if p.id == t:
                        p.fault = 0
        elif af.id == "digester_souring":
            self.plant.digester.souring = 0.0
        elif af.id == "sludge_blanket_thickening":
            self.plant.secondary.health = 1.0
        elif af.id == "blower_failure":
            if "aeration" in self._original_kla:
                kla, cap = self._original_kla.pop("aeration")
                self.plant.aeration.kla_per_h = kla
                self.plant.aeration.blower_capacity_m3h = cap
        self.log.append({"t": self.now_s, "event": "retired", "fault": af.id})
        if af in self.active:
            self.active.remove(af)

    # ─── process fault injection ──────────────────────────────────────────────

    def _apply_process_fault(self, af: ActiveFault) -> None:
        kind = af.spec.inject
        i = af.intensity(self.now_s)
        plant = self.plant

        if kind == "storm":
            if not plant.influent.storm_active:
                plant.influent.start_storm(
                    duration_s=float(af.spec.params.get("duration_s", 7200.0)),
                    intensity=float(af.spec.params.get("intensity", 1.0)),
                )
                self.log.append({"t": self.now_s, "event": "armed", "fault": af.id})

        elif kind == "aeration_capacity":
            # Remove transfer capability, not the reported air flow. The
            # controller keeps trying to deliver the air it thinks it needs and
            # simply cannot move the oxygen — which is what a blower trip is.
            #
            # The baseline is captured **once**. Re-reading it every scan makes
            # the degradation compound against itself, and the fault silently
            # annihilates the aeration system within a few minutes instead of
            # removing the blowers.
            frac = float(af.spec.params.get("fraction_remaining", 0.5))
            if "aeration" not in self._original_kla:
                self._original_kla["aeration"] = (
                    plant.aeration.kla_per_h,
                    plant.aeration.blower_capacity_m3h,
                )
            base_kla, base_cap = self._original_kla["aeration"]
            plant.aeration.kla_per_h = base_kla * max(0.0, frac)
            plant.aeration.blower_capacity_m3h = base_cap * max(0.0, frac)

        elif kind == "clarifier_degrade":
            # Degrade the *scraper*, not the capture efficiency.
            #
            # This is the subtle part of blanket physics: reducing capture
            # actually makes the blanket shallower, because less sludge arrives
            # to accumulate. A blanket deepens when solids are captured faster
            # than the scraper can remove them. So the injected degradation is
            # impaired raking, with capture left alone — which is also what
            # actually happens in a plant: a failed or fouled scraper drive.
            target = af.spec.targets[0]
            if target == "SEC-CL-1":
                if "SEC-CL-1" not in self._clarifier_originals:
                    self._clarifier_originals["SEC-CL-1"] = {
                        "health": plant.secondary.health
                    }
                impaired = float(af.spec.params.get("raking_difficulty", 0.25))
                # The model recomputes raking_difficulty from blanket depth each
                # scan, so the impairment is expressed as a *cap* the scraper
                # cannot beat rather than a one-off assignment.
                plant.secondary.scraper_impairment = 1.0 + (impaired - 1.0) * i

        elif kind == "digester_overload":
            if "DIG-1" not in self._digester_original:
                self._digester_original["DIG-1"] = {
                    "souring": plant.digester.souring
                }
            plant.digester.souring = float(af.spec.params.get("souring", 1.0)) * i
            # Overloading means more feed. Without this the "overload" only
            # sets a flag and the chemistry barely moves.
            plant.digester_feed_multiplier = (
                1.0 + (float(af.spec.params.get("overload", 2.5)) - 1.0) * i
            )

        elif kind == "pump_cavitation":
            # A standing cause, not a momentary flag: the lift station re-derives
            # low-level cavitation every scan and would clear an injected flag
            # immediately, making the fault indistinguishable from a no-op.
            for t in af.spec.targets:
                for p in plant.lift.pumps:
                    if p.id == t:
                        p.degraded_suction = i > 0.0

        elif kind == "pump_fault":
            code = int(af.spec.params.get("fault_code", 1))
            for t in af.spec.targets:
                for p in plant.lift.pumps:
                    if p.id == t:
                        p.fault = code
                        p.running = False
                        p.speed = 0.0
            self.log.append(
                {"t": self.now_s, "event": "faulted", "fault": af.id, "targets": af.spec.targets}
            )

        elif kind == "load_shock":
            mult = float(af.spec.params.get("nh4_multiplier", 1.0))
            cod = float(af.spec.params.get("cod_multiplier", 1.0))
            # Scale the *base* characteristics, not the instantaneous value, so
            # the load stays elevated for the fault's duration.
            plant.influent.base_nh4_mg_l *= 1.0 + (mult - 1.0) * i
            plant.influent.base_cod_mg_l *= 1.0 + (cod - 1.0) * i

        else:
            raise FaultError(
                f"{af.id}: no injector named {kind!r}. Add one rather than "
                "letting a declared fault do nothing."
            )

    # ─── sensor fault injection ───────────────────────────────────────────────

    def _apply_sensor_fault(self, af: ActiveFault, snap: PlantSnapshot) -> None:
        for target in af.spec.targets:
            if target not in snap.values:
                raise FaultError(
                    f"{af.id}: target {target!r} is not in the plant snapshot, so "
                    "this sensor fault would silently do nothing"
                )

            # Handled before the corruption chain because it is the one injector
            # with no value to corrupt. Every other one *rewrites* what the
            # instrument would have said; this one says nothing at all, and
            # inventing a number for it — even the last good reading, even a
            # zero — is precisely the laundering design rule 4 forbids. ``None``
            # is a fact about the instrument; a number is a claim about the
            # process.
            if af.spec.inject == "sensor_dead":
                snap.values[target] = None
                snap.quality[target] = QUALITY_BAD
                continue

            self._corrupt(af, target, snap)

            # A failing instrument still reports a quality the SCADA layer can
            # act on. Overwriting the value without marking the quality is how a
            # historian ends up laundering bad data — see design rule 4.
            snap.quality[target] = QUALITY_UNCERTAIN

    def _corrupt(self, af: ActiveFault, target: str,
                 snap: PlantSnapshot) -> None:
        """Rewrite one reported value the way a misbehaving instrument would.

        Split out from :meth:`_apply_sensor_fault` so that "what does this
        injector do to the number" and "what does a failing instrument report
        about its own trustworthiness" are separate questions. Every branch here
        produces a *plausible* value, which is exactly why the caller has to
        decide the quality separately — the number cannot be trusted to say
        anything about its own reliability.
        """
        kind = af.spec.inject
        i = af.intensity(self.now_s)
        plant = self.plant
        true_value = snap.values[target]

        if kind == "sensor_drift":
            if true_value is None:
                # Already dead. There is nothing to bias — a drifting instrument
                # is a *reporting* fault, and an instrument that is not
                # reporting cannot also be drifting.
                snap.values[target] = None
                return
            bias = float(af.spec.params.get("bias_max", 1.0)) * i
            lo, hi = _signal_range(plant, target)
            snap.values[target] = min(hi, max(lo, true_value + bias))

        elif kind == "sensor_flatline":
            # Freeze at the value captured when **this instance** began, and keep
            # it there. A flatline that still wanders is not a flatline.
            #
            # A frozen ``None`` is legitimate: an instrument that had already
            # stopped answering freezes at "not answering", and a dead
            # instrument is exactly as flat as a stuck one.
            #
            # Keyed on the fault, not on the engine — see ``ActiveFault.frozen`` for
            # why that is the whole difference between a fault that ends when it
            # says it does and one that reports its first reading forever.
            if target not in af.frozen:
                af.frozen[target] = true_value
            frozen = af.frozen[target]
            if frozen is None:
                snap.values[target] = None
            else:
                lo, hi = _signal_range(plant, target)
                snap.values[target] = min(hi, max(lo, frozen))

        elif kind == "sensor_stuck_high":
            frac = float(af.spec.params.get("fraction", 0.97))
            lo, hi = _signal_range(plant, target)
            snap.values[target] = lo + (hi - lo) * frac

        elif kind == "sensor_stuck_low":
            val = float(af.spec.params.get("value", 0.0))
            lo, hi = _signal_range(plant, target)
            snap.values[target] = min(hi, max(lo, val))

        else:
            raise FaultError(f"{af.id}: no sensor injector named {kind!r}")

    # ─── introspection ───────────────────────────────────────────────────────

    def active_ids(self) -> tuple[str, ...]:
        return tuple(af.id for af in self.active if af.is_active(self.now_s))

    def truth(self) -> dict[str, float | None]:
        """The plant's *real* values, before any sensor fault.

        This is what a fault test compares the reported values against. It is
        the difference between asserting "the reported DO rose" — which a
        drifting sensor satisfies trivially — and "the reported DO rose *while
        the real DO did not*", which is the actual detection problem.
        """
        return dict(self.plant.snapshot().values)

    def report(self) -> str:
        lines = [f"FaultEngine at t={self.now_s:.0f}s"]
        for af in self.active:
            state = "active" if af.is_active(self.now_s) else "pending/expired"
            lines.append(
                f"  {af.id:<26} {af.spec.kind:<8} {state:<15} "
                f"intensity={af.intensity(self.now_s):.2f}"
            )
        for entry in self.log[-10:]:
            lines.append(f"  · t={entry['t']:.0f}s {entry['event']}: {entry['fault']}")
        return "\n".join(lines)


def run_scenario(
    scenario_id: str,
    hours: float,
    dt: float = 20.0,
    sample_every_s: float = 300.0,
    seed: int = 0,
) -> Iterator[tuple[float, PlantSnapshot]]:
    """Run a named scenario, yielding ``(elapsed_s, snapshot)`` samples.

    The snapshot is the *reported* view — sensor faults included — which is
    exactly what a historian would have recorded.
    """
    plant = Plant(seed=seed)
    engine = FaultEngine(plant)
    engine.arm_scenario(scenario_id)
    steps = int(hours * 3600 / dt)
    every = max(1, int(sample_every_s / dt))
    for i in range(steps):
        snap = engine.step(dt)
        if i % every == 0:
            yield engine.now_s, snap
