"""The soft PLC: a runnable plant that speaks two industrial protocols.

This is the process that turns everything else into something you can point a
client at. One ``docker compose up`` and the plant is live on Modbus TCP and OPC
UA, with the control program running at a fixed scan rate and the fault engine
armed from a scenario.

Wiring, in the order the scan loop executes:

    sensors   → process models        (the plant physics)
    control   → function blocks       (interlocks, trips, DO loop)
    output    → the scan image
    publish   → Modbus registers, OPC UA variables, fault corruption

The fault engine sits *last*, deliberately. A sensor fault must corrupt only what
is published, so by the time it runs the physics has already produced a truthful
image — see ``softplc/faults/engine.py`` for why that distinction is the whole
point.

Run it::

    python -m softplc.main
    python -m softplc.main --scenario wet_weather --speed 60
    python -m softplc.main --list-scenarios
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Coroutine, TypeVar

from softplc.blocks.control import AerationControl, LiftStationControl, build_blocks
from softplc.contract import Contract
from softplc.contract import contract as get_contract
from softplc.faults.engine import FaultEngine, load_faults
from softplc.process.plant import Plant, PlantSnapshot
from softplc.scanloop import ScanLoop, ScanState
from softplc.servers.modbus_server import ModbusTcpServer
from softplc.servers.opcua import OpcUaServer

log = logging.getLogger("softplc")

#: The one signal an operator can change, by contract. Spelled out rather than
#: looked up so that `_apply_pending_writes` reads as the rule it is — one
#: writable signal, and here is where it lands — instead of a loop over
#: `contract.writable` that would silently no-op for every entry the code does
#: not yet know how to apply.
SETPOINT_SIGNAL = "AERATION:AHU-1:SETPOINT_DO"

T = TypeVar("T")


@dataclass(slots=True)
class SoftPlcConfig:
    """Everything the process needs, and nothing it does not."""

    contract: Contract = field(default_factory=get_contract)
    scan_ms: int = 20
    modbus_host: str = "0.0.0.0"
    modbus_port: int = 5020
    opcua_endpoint: str = "opc.tcp://0.0.0.0:4840/wwtp/server/"
    scenario: str | None = None
    seed: int = 0
    #: Simulated seconds per real second. 1.0 is real time; higher runs the
    #: plant faster, which is how a week of history is produced in minutes.
    speed: float = 1.0
    #: Log every scan. Off by default: at 50 scans/second it drowns everything.
    verbose: bool = False


class SoftPlc:
    """The plant, the control program, and both protocol faces in one process.

    Owns a **persistent event loop on a background thread**. That is not
    incidental: the OPC UA server holds state bound to the loop it was created
    on, so starting it with ``asyncio.run`` and then talking to it from a second
    loop produces a connection timeout that looks like a networking fault. One
    long-lived loop, driven from one thread, with the Modbus listener on a second
    daemon thread because pymodbus offers no asynchronous alternative.
    """

    def __init__(self, config: SoftPlcConfig | None = None) -> None:
        self.config = config or SoftPlcConfig()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve_loop, name="softplc-loop", daemon=True
        )
        self._thread.start()
        self.c = self.config.contract
        self.plant = Plant(c=self.c, seed=self.config.seed)
        self.faults = FaultEngine(self.plant)
        self.modbus = ModbusTcpServer(
            c=self.c, host=self.config.modbus_host, port=self.config.modbus_port
        )
        self.opcua = OpcUaServer(c=self.c, endpoint=self.config.opcua_endpoint)
        self._space: Any = None
        self._running = False
        #: Scan counter, exposed for the health endpoint and the tests.
        self.cycles = 0

        # ── the control program ──────────────────────────────────────────────
        self.aeration_control = AerationControl(
            setpoint_mg_l=self.plant.aeration.setpoint_do_mg_l
        )
        self.lift_control = LiftStationControl()
        self.blocks = build_blocks(self.aeration_control, self.lift_control)

        # ── the loop ─────────────────────────────────────────────────────────
        self.loop = ScanLoop(scan_ms=self.config.scan_ms, name="softplc")
        for block in self.blocks:
            self.loop.add_block(block)

    def _serve_loop(self) -> None:
        """Run the dedicated event loop until asked to stop."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
        """Run a coroutine on the PLC's loop from any thread."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # ─── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Blocking start. Returns once both protocols are accepting."""
        self._call(self._start())

    async def _start(self) -> None:
        await self.modbus.start()
        self._space = await self.opcua.start()
        await self.opcua.wait_ready(timeout=10.0)

        if self.config.scenario:
            self.faults.arm_scenario(self.config.scenario)
            log.info("scenario armed: %s", self.config.scenario)

        self._running = True
        log.info(
            "soft PLC up — Modbus tcp://%s:%d, OPC UA %s (%d signals, %d equipment)",
            self.config.modbus_host, self.config.modbus_port,
            self.config.opcua_endpoint, len(self.c.signals), len(self.c.equipment),
        )

    def stop(self) -> None:
        """Blocking stop. Safe to call more than once."""
        if not self._running and self._loop.is_closed():
            return
        with contextlib.suppress(Exception):
            self._call(self._stop(), timeout=10.0)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        with contextlib.suppress(Exception):
            self._loop.close()
        log.info("soft PLC stopped after %d scans", self.cycles)

    async def _stop(self) -> None:
        self._running = False
        with contextlib.suppress(Exception):
            await self.opcua.stop()
        with contextlib.suppress(Exception):
            await self.modbus.stop()

    # ─── the cycle ───────────────────────────────────────────────────────────

    def run(self, duration_s: float | None = None) -> None:
        """Blocking run for ``duration_s`` simulated seconds."""
        self._call(self._run(duration_s))

    async def _run(self, duration_s: float | None = None) -> None:
        """The scan cycle.

        ``duration_s`` is simulated time, so it is independent of ``speed`` — which
        is what lets an hour of plant time be produced in a second for a smoke
        test, and a week in a couple of minutes for a backfill.
        """
        dt = self.config.scan_ms / 1000.0
        sim_dt = dt * self.config.speed
        start = self.faults.now_s
        while self._running:
            if duration_s is not None and (self.faults.now_s - start) >= duration_s:
                return
            started = time.perf_counter()
            await self._step(sim_dt)
            # Pace through the scan loop, which owns the period. This used to be
            # `await asyncio.sleep(0)` under a comment saying the scan loop
            # already accounted for it -- and it did not, because this path calls
            # `scan_once()` rather than `run()`. The plant ran 29x too fast and
            # starved its own Modbus server into dropping the link. See
            # `ScanLoop.pace`, which divides the plant-time period by `speed`
            # so a backfill at 600x is not throttled to real time.
            remaining = self.loop.pace(started, self.config.speed)
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _step(self, dt: float) -> None:
        """One scan: apply writes, then physics, then control, then publish.

        **Writes go first, and that ordering is the whole design.** An operator's
        setpoint has to be in the plant before the physics runs, because the
        physics reads it — `AerationControl.update()` takes `self.setpoint_mg_l`,
        and `plant.snapshot()` publishes `ae.setpoint_do_mg_l` as the
        `AERATION:AHU-1:SETPOINT_DO` signal. Applying after `faults.step` would
        take one extra scan to reach the controller, and applying after
        `publish` would be undone by `_flush` in the same scan.
        """
        self._apply_pending_writes()
        snapshot = self.faults.step(dt)
        self.cycles += 1

        # Equipment state into the control program's image.
        for eq, state in snapshot.states.items():
            self.loop.image.set_state(eq, _scan_state(state))
        self.loop.image.set_output(
            "_blower_capacity", self.plant.aeration.blower_capacity_m3h
        )
        # A dead analog input does not become a zero. A PLC holds the last good
        # value in its input image and raises the fault bit beside it, so that
        # is what happens here: the image keeps the last good reading and the
        # *quality* is what travels to the protocols. Substituting 0.0 would put
        # a number into the control loop that no instrument ever measured, which
        # is the same laundering the quality channel exists to prevent.
        do_value = snapshot.get("AERATION:AHU-1:DO")
        if do_value is not None:
            self.loop.image.set_output("AERATION:AHU-1:DO", do_value)

        # Run the control program **through** the scan loop, not alongside it.
        # Calling the blocks directly would leave them out of the phase metrics,
        # and a control program that silently never executes is
        # indistinguishable from one that is working.
        self.loop.scan_once()

        # Publish. The deadband means only genuinely changed values are sent,
        # which is what makes an OPC UA subscription cheaper than Modbus polling.
        await self._publish(snapshot)

        if self.config.verbose:
            self._log_scan(snapshot)

    def _apply_pending_writes(self) -> None:
        """Apply every register write the protocol server accepted since last scan.

        **This is the seam that made the write surface real, and the trap is that
        it needs two assignments, not one.**

        `self.plant.aeration.setpoint_do_mg_l` is the value the *snapshot*
        publishes (`plant.py:422`), so setting it is what makes the register read
        back. It is **not** what the controller uses: `AerationControl` was
        constructed with `setpoint_mg_l=self.plant.aeration.setpoint_do_mg_l`
        (`main.py:104`) — a float copied by value at startup. So assigning only
        the plant attribute leaves the PI loop integrating against 2.0 forever,
        with the register showing a setpoint the loop is ignoring.

        That failure is invisible in the most expensive way: the mimic shows the
        operator's number, the audit trail records the write as accepted, the
        plant looks healthy, and the dissolved oxygen does not move. Both
        assignments are made here, next to each other, with the reason written
        down, so that adding a second writable signal does not repeat it.

        Unknown signal ids are logged and dropped rather than raising: the
        protocol server has already validated them against the contract, so an
        unknown id here means the contract changed between a client's write and
        this scan, and the right response is to refuse *that write* — not to take
        the whole scan loop down with it.
        """
        for signal_id, value in self.modbus.take_writes().items():
            if signal_id != SETPOINT_SIGNAL:
                log.warning(
                    "no write target for %s; write discarded (the contract "
                    "advertises it as writable but no code path applies it)",
                    signal_id,
                )
                continue

            # The value the snapshot publishes, so the register reads back.
            self.plant.aeration.setpoint_do_mg_l = value
            # The value the PI loop actually integrates against.
            self.aeration_control.setpoint_mg_l = value
            log.info(
                "setpoint applied: %s = %g mg/L", signal_id, value
            )

    async def _publish(self, snapshot: PlantSnapshot) -> None:
        values = snapshot.values
        states = snapshot.states
        heartbeat = self.cycles & 0x7FFF

        self.modbus.publish(values, states, heartbeat)

        for signal_id in self.c.signals:
            if signal_id not in values:
                continue
            quality = snapshot.quality.get(signal_id, 0)
            # Only publish when the value or its quality moved.
            entry = self._space.variables.get(signal_id) if self._space else None
            if entry is not None and entry.value == values[signal_id] \
                    and entry.quality == quality:
                continue
            self.opcua.set_value(signal_id, values[signal_id], quality)
        for eq, state in states.items():
            self.opcua.set_equipment_state(eq, state)

        # Stage-then-flush. ``set_value`` only records the change; without this
        # call the address space keeps whatever it was built with and a client
        # sees a frozen plant that looks perfectly healthy. The symptom is
        # invisible to unit tests of the server and obvious to a client.
        self.opcua_published = await self.opcua.publish()

    def _log_scan(self, snapshot: PlantSnapshot) -> None:
        v = snapshot.values
        log.debug(
            "scan %d  DO=%.2f air=%.0f NH4=%.2f TSS=%.1f  faults=%s",
            self.cycles,
            v.get("AERATION:AHU-1:DO", 0.0),
            v.get("AERATION:AHU-1:AIR_FLOW", 0.0),
            v.get("EFFLUENT:FLOW:NH4", 0.0),
            v.get("EFFLUENT:FLOW:TSS", 0.0),
            ",".join(self.faults.active_ids()) or "none",
        )

    # ─── introspection ───────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Health payload, for a container healthcheck and for tests."""
        h = self.loop.health()
        h.update(
            cycles=self.cycles,
            running=self._running,
            modbus=self.modbus.running,
            opcua=self.opcua.running,
            faults=list(self.faults.active_ids()),
            sim_time_s=round(self.faults.now_s, 1),
            # A plant that has never been written to and a plant whose writes are
            # all being refused look identical from the outside — both show the
            # old setpoint. These two counters separate them, so "why isn't my
            # setpoint taking" is answerable without attaching a client.
            writes=self.modbus.write_stats(),
        )
        return h


def _scan_state(state: int) -> ScanState:
    return ScanState(state)


async def _run(args: argparse.Namespace) -> int:
    config = SoftPlcConfig(
        scan_ms=args.scan_ms,
        modbus_host=args.modbus_host,
        modbus_port=args.modbus_port,
        opcua_endpoint=args.opcua_endpoint,
        scenario=args.scenario,
        seed=args.seed,
        speed=args.speed,
        verbose=args.verbose,
    )
    plc = SoftPlc(config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    plc.start()

    # ``run`` and ``stop`` are blocking on purpose: they marshal onto the PLC's
    # own loop, so they may be called from a thread that has no loop of its own.
    # Here there *is* a loop, so they are pushed onto a worker thread rather than
    # awaited. (Handing the blocking ``run`` straight to ``create_task`` type
    # checks as ``None``, and the failure mode is a task that never runs — the
    # CLI would report success having simulated nothing.)
    runner: asyncio.Task[None] = asyncio.create_task(
        asyncio.to_thread(plc.run, args.duration)
    )
    reporter: asyncio.Task[None] = asyncio.create_task(
        _report(plc, args.report_every)
    )

    # Wait for whichever comes first: a signal, or the requested duration.
    # Waiting only on the signal made ``--duration`` hang forever, which turns a
    # bounded smoke test into something that has to be killed — and a test you
    # have to kill is a test nobody runs.
    waiters: list[asyncio.Task[Any]] = [asyncio.create_task(stop.wait())]
    if args.duration is not None:
        waiters.append(runner)
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await w
        for task in (runner, reporter):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Teardown last, and only here. Scheduling the stop alongside the run —
        # which is what a stopper task looks like it should do — runs it
        # immediately instead, so the plant exits after a single scan and the
        # command reports success having simulated nothing.
        await asyncio.to_thread(plc.stop)
    return 0


async def _report(plc: SoftPlc, every: float) -> None:
    """Periodic health line, so a container log shows the plant is alive."""
    while True:
        await asyncio.sleep(every)
        h = plc.health()
        log.info(
            "scans=%d worst=%.0fus (%.1f%% of budget) overruns=%d faults=%s sim=%.0fs",
            h["cycles"], h["worst_case_us"], h["budget_used_pct"],
            h["overruns"], ",".join(h["faults"]) or "none", h["sim_time_s"],
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="softplc",
        description="Simulated wastewater plant speaking Modbus TCP and OPC UA.",
    )
    p.add_argument("--scan-ms", type=int, default=20, help="PLC scan period")
    p.add_argument("--modbus-host", default="0.0.0.0")
    p.add_argument("--modbus-port", type=int, default=5020)
    p.add_argument("--opcua-endpoint", default="opc.tcp://0.0.0.0:4840/wwtp/server/")
    p.add_argument("--scenario", default=None, help="fault scenario to arm")
    p.add_argument("--list-scenarios", action="store_true")
    p.add_argument("--duration", type=float, default=None,
                   help="simulated seconds to run, then exit")
    p.add_argument("--speed", type=float, default=1.0,
                   help="simulated seconds per real second")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--verbose", action="store_true", help="log every scan")
    p.add_argument("--report-every", type=float, default=30.0)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    # asyncua is noisy about the absent encryption policy, which is expected: the
    # simulation is LAN-only and says so in docs/SECURITY.md. Silenced here so it
    # does not look like a warning worth investigating.
    logging.getLogger("asyncua").setLevel(logging.ERROR)

    if args.list_scenarios:
        _faults, scenarios = load_faults()
        for sc in scenarios.values():
            print(f"{sc.id:<26} {sc.title}")
            print(f"{'':<26} {sc.expect}")
        return 0

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
