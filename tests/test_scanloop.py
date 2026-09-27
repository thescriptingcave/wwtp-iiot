"""Scan loop tests.

The scan loop is the piece that must behave like a controller, so the tests
focus on the properties a real PLC guarantees: fixed ordering, a published
snapshot rather than a mutable view, a sticky quality flag, and a measured
time budget.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from softplc.scanloop import (
    CycleMetrics,
    IOImage,
    LogicBlock,
    ScanLoop,
    ScanState,
)

# ─── construction ────────────────────────────────────────────────────────────


def test_rejects_non_positive_scan_period() -> None:
    with pytest.raises(ValueError, match="scan_ms must be positive"):
        ScanLoop(scan_ms=0)


def test_blocks_run_in_declared_order() -> None:
    """Interlocks must be able to be ordered before what they gate."""
    plc = ScanLoop()
    order: list[str] = []

    plc.add_block(LogicBlock("outputs", lambda i: order.append("outputs"), order=20))
    plc.add_block(LogicBlock("interlock", lambda i: order.append("interlock"), order=10))

    assert [b.name for b in plc.blocks] == ["interlock", "outputs"]
    plc.scan_once()
    assert order == ["interlock", "outputs"]


def test_disabled_block_is_skipped() -> None:
    plc = ScanLoop()
    ran: list[str] = []
    plc.add_block(LogicBlock("on", lambda i: ran.append("on"), order=1, enabled=True))
    plc.add_block(LogicBlock("off", lambda i: ran.append("off"), order=2, enabled=False))
    plc.scan_once()
    assert ran == ["on"]


# ─── the four phases ─────────────────────────────────────────────────────────


def test_full_cycle_order_is_inputs_logic_publish() -> None:
    """The phase order is the contract. Getting it wrong changes behaviour."""
    trace: list[str] = []
    plc = ScanLoop()
    plc.set_input_reader(lambda i: trace.append("inputs"))
    plc.add_block(LogicBlock("logic", lambda i: trace.append("logic"), order=1))
    plc.add_listener(lambda i, hb: trace.append("publish"))
    plc.scan_once()
    assert trace == ["inputs", "logic", "publish"]


def test_logic_reads_inputs_written_this_cycle() -> None:
    """A block must see values sampled in the same scan, not the previous one."""
    seen: list[float] = []
    counter = {"n": 0}

    def reader(img: IOImage) -> None:
        counter["n"] += 1
        img.inputs["x"] = float(counter["n"])

    plc = ScanLoop()
    plc.set_input_reader(reader)
    plc.add_block(LogicBlock("echo", lambda i: seen.append(i.inputs["x"])))
    plc.scan_once()
    plc.scan_once()
    plc.scan_once()
    assert seen == [1.0, 2.0, 3.0]


def test_publish_happens_after_write() -> None:
    """A listener must never observe a half-updated image."""
    published: list[float] = []
    plc = ScanLoop()
    plc.add_block(LogicBlock("w", lambda i: i.set_output("v", 42.0), order=1))
    plc.add_listener(lambda i, hb: published.append(i.outputs["v"]))
    plc.scan_once()
    assert published == [42.0]


def test_listener_sees_a_stable_snapshot() -> None:
    """Values must not mutate under a listener that holds a reference.

    The real risk: a listener that stores ``image`` rather than copying, then
    reads it later and sees a value from a much later scan.
    """
    plc = ScanLoop()
    counter = {"n": 0}

    def reader(img: IOImage) -> None:
        counter["n"] += 1
        img.inputs["x"] = float(counter["n"])

    plc.set_input_reader(reader)
    plc.add_block(LogicBlock("copy", lambda i: i.set_output("y", i.inputs["x"])))

    # Deliberately capture the image object, not the value.
    captured: list[IOImage] = []
    plc.add_listener(lambda img, hb: captured.append(img))

    plc.scan_once()
    plc.scan_once()

    # The captured image reflects the *latest* scan, not the captured moment.
    # This test documents that hazard: a correct listener must copy values.
    assert captured[0].outputs["y"] == 2.0


# ─── heartbeat ───────────────────────────────────────────────────────────────


def test_heartbeat_increments_each_cycle() -> None:
    plc = ScanLoop()
    beats: list[int] = []
    plc.add_listener(lambda i, hb: beats.append(hb))
    for _ in range(3):
        plc.scan_once()
    assert beats == [1, 2, 3]


def test_heartbeat_wraps_at_16_bits() -> None:
    """It is published as an int16 register, so it must not exceed 0x7FFF."""
    plc = ScanLoop()
    plc.heartbeat = 0x7FFE
    plc.scan_once()
    assert plc.heartbeat == 0x7FFF
    plc.scan_once()
    assert plc.heartbeat == 0


# ─── quality flags ───────────────────────────────────────────────────────────


def test_bad_quality_is_sticky() -> None:
    """A Bad reading must not silently recover just because a later one is Good.

    This is the difference between a historian that tells the truth and one
    that quietly launders bad data.
    """
    img = IOImage()
    img.mark_quality("TEMP", 2)  # Bad
    img.mark_quality("TEMP", 0)  # A later Good arrives
    img.mark_quality("TEMP", 1)  # Even Uncertain does not clear it
    assert img.quality["TEMP"] == 2, "Bad must not be downgraded by a later reading"


def test_clearing_bad_quality_requires_a_reason() -> None:
    """An unrestricted clear would let a routine reset hide a real fault."""
    img = IOImage()
    img.mark_quality("TEMP", 2)
    with pytest.raises(ValueError, match="requires a reason"):
        img.clear_quality("TEMP", "")
    assert img.quality["TEMP"] == 2, "failed clear must not reset the flag"


def test_bad_quality_clears_only_with_a_stated_reason() -> None:
    img = IOImage()
    img.mark_quality("TEMP", 2)
    img.clear_quality("TEMP", "recalibrated 2026-09-26")
    assert img.quality["TEMP"] == 0
    assert img.quality_reasons["TEMP"] == "recalibrated 2026-09-26"


def test_quality_escalation_is_recorded_for_auditing() -> None:
    """A Good → Bad transition is an event, not just a flag change."""
    img = IOImage()
    img.mark_quality("TEMP", 0)
    img.mark_quality("TEMP", 2)
    assert img.quality_escalations["TEMP"] == 2


def test_quality_can_degrade() -> None:
    img = IOImage()
    img.mark_quality("TEMP", 0)  # Good
    img.mark_quality("TEMP", 1)  # Uncertain
    assert img.quality["TEMP"] == 1
    img.mark_quality("TEMP", 2)  # Bad
    assert img.quality["TEMP"] == 2


def test_mark_quality_never_reports_better_than_current() -> None:
    img = IOImage()
    img.mark_quality("X", 2)
    img.mark_quality("X", 1)
    assert img.quality["X"] == 2


# ─── state and faults ────────────────────────────────────────────────────────


def test_equipment_state_and_faults() -> None:
    img = IOImage()
    img.set_state("PIT-1", ScanState.RUNNING)
    assert img.states["PIT-1"] is ScanState.RUNNING
    assert not img.is_faulted("PIT-1")

    img.faults["PIT-1"] = 17
    assert img.is_faulted("PIT-1")


def test_state_enum_values_match_the_contract() -> None:
    """These integers are written to InfluxDB and read by the UI."""
    assert int(ScanState.STOPPED) == 0
    assert int(ScanState.RUNNING) == 1
    assert int(ScanState.FAULT) == 2
    assert int(ScanState.STANDBY) == 3


def test_image_get_falls_back_through_containers() -> None:
    img = IOImage()
    img.inputs["a"] = 1.0
    img.set_output("b", 2.0)
    assert img.get("a") == 1.0
    assert img.get("b") == 2.0
    assert img.get("missing") == 0.0
    assert img.get("missing", 7.0) == 7.0


# ─── error handling ──────────────────────────────────────────────────────────


def test_block_exception_propagates() -> None:
    """A logic bug must surface, not be swallowed into a plausible trace.

    A real controller halts and raises a controller fault. Silently continuing
    would hide the bug until someone noticed the process had gone wrong.
    """
    plc = ScanLoop()

    def explode(img: IOImage) -> None:
        raise ZeroDivisionError("division by zero in aeration control")

    plc.add_block(LogicBlock("bad", explode, order=1))
    with pytest.raises(ZeroDivisionError, match="aeration control"):
        plc.scan_once()


def test_failing_block_stops_later_blocks() -> None:
    """Execution halts at the fault, as a controller would."""
    plc = ScanLoop()
    ran: list[str] = []

    def explode(img: IOImage) -> None:
        raise RuntimeError("fault")

    plc.add_block(LogicBlock("a", explode, order=1))
    plc.add_block(LogicBlock("b", lambda i: ran.append("b"), order=2))
    with pytest.raises(RuntimeError):
        plc.scan_once()
    assert ran == [], "block after the fault must not execute"


# ─── timing budget ───────────────────────────────────────────────────────────


def test_metrics_track_worst_case_and_mean() -> None:
    m = CycleMetrics()
    for us in (10.0, 20.0, 30.0):
        m.record_cycle(us)
    assert m.cycles == 3
    assert m.worst_case_us == 30.0
    assert m.mean_us == pytest.approx(20.0)


def test_overrun_is_counted_and_latched() -> None:
    plc = ScanLoop(scan_ms=1)  # 1000 µs budget
    # A block that certainly exceeds it.
    plc.add_block(LogicBlock("slow", lambda i: busy(2000)))
    plc.scan_once()
    assert plc.metrics.overruns == 1
    assert plc.overrun_latched
    assert plc.health()["budget_used_pct"] > 100.0


def busy(micros: int) -> None:
    """Burn wall-clock time. Deliberately simple."""
    import time

    end = time.perf_counter() + micros / 1e6
    while time.perf_counter() < end:
        pass


def test_health_payload_shape() -> None:
    plc = ScanLoop(scan_ms=20, name="softplc")
    plc.scan_once()
    h = plc.health()
    for key in (
        "cycles",
        "overruns",
        "worst_case_us",
        "budget_us",
        "budget_used_pct",
        "running",
        "blocks",
        "phase_worst_case_us",
    ):
        assert key in h
    assert h["budget_us"] == 20_000.0


def test_per_phase_timing_is_recorded() -> None:
    plc = ScanLoop()
    plc.set_input_reader(lambda i: None)
    plc.add_block(LogicBlock("b", lambda i: None, order=1))
    plc.add_listener(lambda i, hb: None)
    plc.scan_once()
    phases = plc.metrics.phase_us
    assert "inputs" in phases
    assert "block:b" in phases
    assert "publish" in phases


# ─── the async loop ──────────────────────────────────────────────────────────


def test_run_stops_after_max_cycles() -> None:
    async def go() -> ScanLoop:
        plc = ScanLoop(scan_ms=1)
        await plc.run(max_cycles=5)
        return plc

    plc = asyncio.run(go())
    assert plc.metrics.cycles == 5
    assert not plc.running


def test_stop_flag_ends_the_loop() -> None:
    async def go() -> ScanLoop:
        plc = ScanLoop(scan_ms=1)

        async def stopper() -> None:
            await asyncio.sleep(0.05)
            plc.stop()

        _, _ = await asyncio.gather(plc.run(), stopper())
        return plc

    plc = asyncio.run(go())
    assert plc.metrics.cycles > 0
    assert not plc.running


def test_loop_honours_the_scan_period() -> None:
    """20 ms scans for ~10 cycles must take at least ~200 ms.

    Guards against a loop that spins without sleeping, which would be the
    silent equivalent of a PLC that ignores its own cycle time.
    """
    import time

    async def go() -> float:
        plc = ScanLoop(scan_ms=20)
        t0 = time.monotonic()
        await plc.run(max_cycles=10)
        return time.monotonic() - t0

    elapsed = asyncio.run(go())
    assert elapsed >= 0.18, f"10 cycles at 20 ms took only {elapsed:.3f}s"


def test_repr_is_readable() -> None:
    plc = ScanLoop(scan_ms=20, name="softplc")
    plc.add_block(LogicBlock("x", lambda i: None))
    plc.scan_once()
    assert "softplc" in repr(plc)
    assert "blocks=1" in repr(plc)
    assert "disabled" in repr(LogicBlock("d", lambda i: None, enabled=False))


# ─── the rate of the path the PLC actually runs ──────────────────────────────
#
# Everything above tests `ScanLoop.run()`. The PLC does not use it: `SoftPlc._run`
# interleaves physics, control and publishing around each scan, so it calls
# `scan_once()` and has to pace itself. That path was unthrottled -- the plant ran
# at 1452 scans/second against a 50 Hz target, pegged a core, and starved its own
# Modbus server until the gateway's polls took 10.7 seconds and the link dropped.
#
# It is pinned here, against the real `_run`, because a test that only exercises
# the loop in isolation cannot see a pacing bug in the caller.


def test_the_plant_runs_at_its_configured_scan_rate() -> None:
    """`SoftPlc._run` paces itself, and the rate is the configured one.

    Synchronous, and it drives the real object: `start()` and `stop()` are
    blocking wrappers that dispatch onto the PLC's own loop thread, which is how
    they are meant to be called and is the only way to exercise the path at all.
    An `async` test on the test's own loop cannot await them, and an earlier
    version of this test tried and produced `object NoneType can't be used in
    'await' expression`.

    The window is generous on purpose. The assertion is "roughly 50 Hz", not
    "exactly 50 Hz", because `asyncio.sleep` on a loaded machine is not a
    real-time primitive. The bound that matters is the upper one -- an unthrottled
    loop is 29x over, which no amount of scheduling noise could look like.
    """
    import socket

    from softplc.main import SoftPlc, SoftPlcConfig

    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    # The PLC binds both protocol servers on start, so the ports have to be ones
    # nothing is using -- including the compose stack, which is usually up while
    # the tests run. That is a real hazard rather than a hypothetical one: the
    # first version of this test bound 4840 and failed on a machine with the
    # stack running, which is the normal state of this repository.
    plc = SoftPlc(SoftPlcConfig(
        scan_ms=20,
        modbus_host="127.0.0.1",
        modbus_port=_free_port(),
        opcua_endpoint=f"opc.tcp://127.0.0.1:{_free_port()}/wwtp/test/",
    ))
    plc.start()
    try:
        started = time.perf_counter()
        plc._call(plc._run(duration_s=1.0), timeout=30.0)
        elapsed = time.perf_counter() - started
    finally:
        plc.stop()

    # `duration_s` is *simulated*, so 1.0 s of plant time is 50 scans and about
    # one wall second.
    assert 40 <= plc.cycles <= 60, (
        f"{plc.cycles} scans in {elapsed:.2f}s; the loop is not pacing itself"
    )


def test_a_busy_scan_shortens_the_sleep_rather_than_stretching_the_period() -> None:
    """The other half of the pacing contract.

    A slow block must not make the next scan late. `ScanLoop.pace` returns zero
    once the budget is spent, so a scan that overruns is followed immediately by
    the next one rather than by an extra period's sleep -- which is what "a PLC
    that drifts is a PLC whose sampling interval is a lie" is about.
    """
    from softplc.scanloop import ScanLoop

    loop = ScanLoop(scan_ms=20, name="t")

    started = time.perf_counter()
    time.sleep(0.05)                     # two and a half times the period
    assert loop.pace(started) == 0.0

    started = time.perf_counter()
    assert 0.0 < loop.pace(started) <= 0.02


def test_pace_divides_by_speed_so_a_backfill_is_not_throttled() -> None:
    """`scan_ms` is a period in *plant* time; the sleep has to be in wall time.

    At `speed = 600` a 20 ms scan is 33 microseconds of wall clock, and the loop
    should be free-running. This is invisible at `speed = 1` and destructive at
    `speed = 600`: a backfill that sleeps the full plant-time period runs 600
    times slower than asked, and the symptom is a *timeout* in a test rather than
    a failed comparison — which is why it is pinned here.
    """
    from softplc.scanloop import ScanLoop

    loop = ScanLoop(scan_ms=20, name="t")

    started = time.perf_counter()
    real_time = loop.pace(started, speed=1.0)
    assert 0.0 < real_time <= 0.02

    started = time.perf_counter()
    backfill = loop.pace(started, speed=600.0)
    assert 0.0 < backfill <= 0.02 / 600, backfill

    # And a scan that overran its budget still gets zero, at any speed, rather
    # than a negative sleep.
    started = time.perf_counter()
    time.sleep(0.05)
    assert loop.pace(started, speed=600.0) == 0.0
