"""The PLC scan loop.

A PLC is not a function that computes a value. It is a *cycle* that runs at a
fixed rate, in a fixed order, with a guaranteed worst-case execution time. This
module is that cycle.

    ┌─ read inputs ──── sample sensors into the input image
    ├─ run logic ────── execute function blocks in order
    ├─ write outputs ── latch results to the output image
    └─ publish ──────── make the image visible to protocol servers
    └─ sleep ────────── until the next cycle boundary

Why the shape matters
---------------------
* **Fixed order.** Interlock logic depends on blocks running in a defined
  sequence. Reordering blocks changes plant behaviour, exactly as it does on a
  real controller.
* **Bounded execution.** A real PLC is validated against a time budget. So is
  this one: `worst_case_us` is tracked and reported, and exceeding the budget
  is surfaced rather than ignored.
* **Publish after write.** Protocol servers read a snapshot, never a
  half-updated image. Without this, a client can read a value that was written
  before the scan that produced it.
* **Sleep, don't spin.** The loop is cooperative. A 20 ms budget is honoured
  even if a block overruns — the overrun is recorded, and the next cycle starts
  on time.

What this deliberately does NOT do
----------------------------------
Python has a garbage collector with non-bounded pauses and allocates at
runtime. A real PLC forbids both. This module keeps the loop itself allocation-
free and bounded, but the *logic* running inside it is ordinary Python. That is
an honest approximation of determinism, not real hard real-time, and it is
documented as such in docs/SECURITY.md and docs/DESIGN.md.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ScanState(enum.IntEnum):
    """Mirror of the state equipment values in ``contracts/tags.yaml``.

    Equipment state is a *measurement*, not a field on a process signal — see
    design rule 3 in the contract.
    """

    STOPPED = 0
    RUNNING = 1
    FAULT = 2
    STANDBY = 3


class CyclePhase(enum.IntEnum):
    """Phases of one scan, in execution order.

    Ordering is the contract. ``LogicBlock.order`` sorts against this.
    """

    INPUTS = 0
    LOGIC = 1
    OUTPUTS = 2
    PUBLISH = 3


@dataclass(slots=True)
class CycleMetrics:
    """Timing and health of the scan loop.

    ``worst_case_us`` is the headline number. A PLC that occasionally exceeds
    its cycle budget is a PLC with a problem that shows up as a mysterious
    process fault weeks later.
    """

    cycles: int = 0
    overruns: int = 0
    worst_case_us: float = 0.0
    mean_us: float = 0.0
    last_us: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    phase_us: dict[str, float] = field(default_factory=dict)

    def record(self, phase: str, micros: float) -> None:
        prev = self.phase_us.get(phase)
        self.phase_us[phase] = micros if prev is None else max(prev, micros)

    def record_cycle(self, micros: float) -> None:
        self.cycles += 1
        self.last_us = micros
        self.worst_case_us = max(self.worst_case_us, micros)
        # Running mean without a growing sample buffer.
        n = self.cycles
        self.mean_us += (micros - self.mean_us) / n

    def as_dict(self) -> dict[str, Any]:
        # No `budget_us` local. It was assigned and never used — dead since the
        # scan-pacing fix, and the only F841 in this file — and it was invisible
        # because the ruff gate had been running over a subset of the packages.
        return {
            "cycles": self.cycles,
            "overruns": self.overruns,
            "last_us": round(self.last_us, 1),
            "mean_us": round(self.mean_us, 1),
            "worst_case_us": round(self.worst_case_us, 1),
            "phase_worst_case_us": {k: round(v, 1) for k, v in self.phase_us.items()},
            "uptime_s": round(time.monotonic() - self.started_at, 1),
        }


@dataclass(slots=True)
class IOImage:
    """The input/output image — the PLC's memory as the logic sees it.

    Modelled as two dicts rather than a typed struct because a real PLC's image
    table is a flat, sparse, fixed-size block. Discipline about which keys exist
    is the block author's job, and the contract is what enforces it.
    """

    #: Values sampled from the process on this scan.
    inputs: dict[str, float] = field(default_factory=dict)
    #: Values latched by the logic on this scan. Protocol servers publish these.
    outputs: dict[str, float] = field(default_factory=dict)
    #: Equipment run states, keyed by equipment id. Published separately.
    states: dict[str, ScanState] = field(default_factory=dict)
    #: OPC UA StatusCode per signal: 0 Good, 1 Uncertain, 2 Bad.
    quality: dict[str, int] = field(default_factory=dict)
    #: Why a sticky Bad flag was cleared. Required to clear.
    quality_reasons: dict[str, str] = field(default_factory=dict)
    #: Signal → the worst quality it has been escalated to, for auditing.
    quality_escalations: dict[str, int] = field(default_factory=dict)
    #: Fault codes, keyed by equipment id. Non-zero means faulted.
    faults: dict[str, int] = field(default_factory=dict)

    def get(self, key: str, default: float = 0.0) -> float:
        return self.inputs.get(key, self.outputs.get(key, default))

    def set_output(self, key: str, value: float) -> None:
        self.outputs[key] = value

    def set_state(self, equipment: str, state: ScanState) -> None:
        self.states[equipment] = state

    def is_faulted(self, equipment: str) -> bool:
        return self.faults.get(equipment, 0) != 0

    def clear_quality(self, key: str, reason: str) -> None:
        """Reset a signal's quality to Good. Requires a stated reason.

        Quality is sticky: a Bad reading stays Bad until the instrument is
        positively known good again — recalibrated, inspected, or replaced. An
        unrestricted clear would let a routine reset launder a genuine fault
        out of the historian, so the reason is mandatory and is expected to be
        written to the ``events`` bucket by the caller.
        """
        if not reason:
            raise ValueError(
                f"clearing quality for {key!r} requires a reason — quality flags "
                "are sticky by design (docs/DESIGN.md)"
            )
        self.quality[key] = 0
        self.quality_reasons[key] = reason

    def mark_quality(self, key: str, code: int) -> None:
        """Mark a signal's quality.

        Quality is monotonic: it may degrade (Good → Uncertain → Bad) but a
        better reading never launders a worse one. Every degradation is recorded
        in :attr:`quality_escalations` because a Good → Bad transition is an
        event in its own right, not merely a flag change.
        """
        current = self.quality.get(key, 0)
        if code > current:
            self.quality[key] = code
            self.quality_escalations[key] = code
        else:
            self.quality.setdefault(key, code)


class LogicBlock:
    """A named unit of control logic, run once per scan.

    Real controllers compile function blocks with an assigned execution order.
    ``order`` reproduces that: interlocks must be evaluated before the outputs
    they gate, and the order is part of the design, not an implementation detail.
    """

    __slots__ = ("enabled", "fn", "name", "order")

    def __init__(
        self,
        name: str,
        fn: Callable[[IOImage], None],
        order: int = 0,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.fn = fn
        self.order = order
        self.enabled = enabled

    def __repr__(self) -> str:  # pragma: no cover - display only
        state = "" if self.enabled else " (disabled)"
        return f"<LogicBlock {self.name} order={self.order}{state}>"


class ScanLoop:
    """The deterministic cycle.

    Usage::

        plc = ScanLoop(scan_ms=20, name="softplc")
        plc.add_block(LogicBlock("aeration_dissolved_oxygen", on_do, order=10))
        plc.set_input_reader(sensor_sampler)
        await plc.run()
    """

    def __init__(self, scan_ms: int = 20, name: str = "plc") -> None:
        if scan_ms <= 0:
            raise ValueError("scan_ms must be positive")
        self.scan_ms = scan_ms
        self.name = name
        self.image = IOImage()
        self.metrics = CycleMetrics()
        self.running = False
        self.blocks: list[LogicBlock] = []
        self._input_reader: Callable[[IOImage], None] | None = None
        self._listeners: list[Callable[[IOImage, int], None]] = []
        #: Cycle counter. Also the Modbus HEARTBEAT register value.
        self.heartbeat = 0
        #: Set when a block exceeds the cycle budget; latched for inspection.
        self.overrun_latched = False

    # ─── configuration ───────────────────────────────────────────────────────

    def add_block(self, block: LogicBlock) -> None:
        self.blocks.append(block)
        self.blocks.sort(key=lambda b: b.order)

    def set_input_reader(self, reader: Callable[[IOImage], None]) -> None:
        self._input_reader = reader

    def add_listener(self, fn: Callable[[IOImage, int], None]) -> None:
        """Called after every scan with the published image.

        This is how the Modbus and OPC UA servers observe values. They never
        reach into :attr:`image` mid-cycle.
        """

        self._listeners.append(fn)

    # ─── one cycle ───────────────────────────────────────────────────────────

    def scan_once(self) -> None:
        """Execute exactly one scan. Separated from :meth:`run` so tests can
        drive the loop deterministically without asyncio."""
        cycle_start = time.perf_counter()

        # ── inputs ────────────────────────────────────────────────────────────
        if self._input_reader is not None:
            t0 = time.perf_counter()
            self._input_reader(self.image)
            self.metrics.record("inputs", (time.perf_counter() - t0) * 1e6)

        # ── logic, in declared order ──────────────────────────────────────────
        for block in self.blocks:
            if not block.enabled:
                continue
            t0 = time.perf_counter()
            try:
                block.fn(self.image)
            except Exception:
                # A real PLC does not have try/except — a faulted block halts the
                # scan and raises a controller fault. Swallowing it here would
                # hide a logic bug behind a plausible-looking trace. Re-raise
                # after recording, so the loop's own error handling decides.
                self.metrics.record(
                    f"block:{block.name}",
                    (time.perf_counter() - t0) * 1e6,
                )
                self.faulted = True
                raise
            self.metrics.record(f"block:{block.name}", (time.perf_counter() - t0) * 1e6)

        # ── heartbeat ─────────────────────────────────────────────────────────
        self.heartbeat = (self.heartbeat + 1) & 0x7FFF

        # ── publish ───────────────────────────────────────────────────────────
        t0 = time.perf_counter()
        for listener in self._listeners:
            listener(self.image, self.heartbeat)
        self.metrics.record("publish", (time.perf_counter() - t0) * 1e6)

        # ── budget check ──────────────────────────────────────────────────────
        elapsed_us = (time.perf_counter() - cycle_start) * 1e6
        self.metrics.record_cycle(elapsed_us)
        if elapsed_us > self.scan_ms * 1000.0:
            self.metrics.overruns += 1
            self.overrun_latched = True

    # ─── the loop ────────────────────────────────────────────────────────────

    def pace(self, started: float, speed: float = 1.0) -> float:
        """Seconds left in this cycle, for the caller to sleep.

        The single source of truth for the scan period, so every path that steps
        the loop paces itself the same way.

        ``speed`` is simulated seconds per real second, and it divides the sleep.
        ``scan_ms`` is a period in *plant* time, so at ``speed = 600`` twenty
        milliseconds of scan is 33 microseconds of wall clock and the loop should
        run 30 000 scans a second -- or as near as the interpreter allows.

        Getting this wrong is invisible at ``speed = 1`` and catastrophic at
        ``speed = 600``: a backfill that sleeps the full plant-time period runs
        600 times slower than asked, and the test that catches it times out rather
        than failing a comparison. See :meth:`run` for the other half of this
        story.

        This method exists because of a bug worth reading about. ``softplc.main``
        steps the loop with ``scan_once()`` -- it has to, because it interleaves
        physics, control and publishing around each scan -- and so it never calls
        :meth:`run`, which is where the pacing lived. The alternative in
        ``softplc.main`` was ``await asyncio.sleep(0)``, under a comment claiming
        that "the scan loop already accounts for its own period".

        It did not. The PLC ran at **1452 scans/second against a 50 Hz target**,
        pegged a core, and starved its own Modbus server badly enough that the
        gateway's polls took 10.7 seconds and the link dropped. Every test passed,
        because every test drives ``run()`` or ``scan_once()`` and never the path
        that was broken.

        The lesson is the comment, not the code: a comment asserting that a
        responsibility lives elsewhere is a claim, and this one was plausible
        enough to survive being written. Anything that asserts *where* a
        behaviour comes from deserves the same scepticism as anything that
        asserts *what* it does.
        """
        wall_period = (self.scan_ms / 1000.0) / max(speed, 1e-9)
        return max(0.0, wall_period - (time.perf_counter() - started))

    async def run(self, max_cycles: int | None = None) -> None:
        """Run the scan loop until stopped, or for ``max_cycles`` scans."""
        self.running = True

        try:
            while self.running:
                started = time.perf_counter()
                if max_cycles is None:
                    self.scan_once()
                else:
                    if self.metrics.cycles >= max_cycles:
                        break
                    self.scan_once()

                # Sleep the *remainder* of the cycle, so a slow block shortens
                # the sleep rather than stretching the period. A PLC that drifts
                # is a PLC whose sampling interval is a lie.
                remaining = self.pace(started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            self.running = False
            raise
        except Exception:
            self.running = False
            raise
        finally:
            self.running = False

    def stop(self) -> None:
        self.running = False

    # ─── introspection ───────────────────────────────────────────────────────

    @property
    def budget_us(self) -> float:
        return self.scan_ms * 1000.0

    def health(self) -> dict[str, Any]:
        """Health payload, suitable for a container healthcheck."""
        d = self.metrics.as_dict()
        d["budget_us"] = self.budget_us
        d["budget_used_pct"] = round(
            100.0 * d["worst_case_us"] / self.budget_us, 2
        ) if self.budget_us else 0.0
        d["running"] = self.running
        d["blocks"] = len(self.blocks)
        return d

    def __repr__(self) -> str:  # pragma: no cover - display only
        return (
            f"<ScanLoop {self.name} scan_ms={self.scan_ms} "
            f"blocks={len(self.blocks)} cycles={self.metrics.cycles}>"
        )
