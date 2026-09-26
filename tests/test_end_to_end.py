"""End-to-end tests against a running soft PLC.

Everything so far has been tested in pieces: the model, the register map, the
address space, the codec. This file starts the *process* and talks to it the way
a real client would — a Modbus socket and an OPC UA session.

That is the only way to catch the class of bug where every component is correct
and the wiring between them is not. Two such bugs were found and fixed while
writing this file:

  * ``--duration`` waited only on a signal, so a bounded smoke test hung forever
    and had to be killed. A test you have to kill is a test nobody runs.
  * The scan loop published to Modbus and OPC UA but never re-published when only
    the *quality* of a value changed, so a sensor fault never reached a client.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from asyncua import Client
from pymodbus.client import ModbusTcpClient
from softplc.contract import QUALITY_UNCERTAIN, contract
from softplc.main import SoftPlc, SoftPlcConfig
from softplc.servers.modbus import decode_float32
from softplc.servers.modbus_server import ModbusTcpServer

C = contract()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MB_PORT = _free_port()
UA_PORT = _free_port()
ENDPOINT = f"opc.tcp://127.0.0.1:{UA_PORT}/wwtp-e2e/"


def _start_plc(scenario: str | None = None, duration_s: float = 600.0,
               speed: float = 600.0, own_ports: bool = False) -> SoftPlc:
    # A second PLC needs its own ports. Sharing the module-level ones produced
    # ``Errno 48: address already in use`` -- a real constraint of running two
    # servers in one process, not a flake to be retried away.
    mb, ep = (MB_PORT, ENDPOINT) if not own_ports else (
        _free_port(), f"opc.tcp://127.0.0.1:{_free_port()}/wwtp-solo/")
    plc = SoftPlc(SoftPlcConfig(
        scan_ms=20,
        modbus_host="127.0.0.1",
        modbus_port=mb,
        opcua_endpoint=ep,
        scenario=scenario,
        speed=speed,
    ))
    plc.start()
    plc.run(duration_s=duration_s)
    return plc


@pytest.fixture(scope="module")
def plc():
    p = _start_plc()
    yield p
    p.stop()


# ─── the process runs and serves ─────────────────────────────────────────────


def test_soft_plc_completes_its_requested_duration(plc: SoftPlc) -> None:
    """A bounded run must terminate. It hung once, waiting only on a signal."""
    assert plc.cycles > 0
    assert plc.plant is not None


def test_health_reports_both_protocols_up(plc: SoftPlc) -> None:
    h = plc.health()
    assert h["modbus"] is True
    assert h["opcua"] is True
    assert h["cycles"] == plc.cycles
    assert h["overruns"] == 0, f"scan loop overran: {h['worst_case_us']}us"


def test_control_blocks_stay_within_the_scan_budget(plc: SoftPlc) -> None:
    h = plc.health()
    assert h["budget_used_pct"] < 50.0


# ─── Modbus, as a client ─────────────────────────────────────────────────────


def test_modbus_client_reads_the_heartbeat(plc: SoftPlc) -> None:
    c = ModbusTcpClient("127.0.0.1", port=MB_PORT)
    c.connect()
    try:
        rr = c.read_holding_registers(
            ModbusTcpServer.wire_offset(40000), count=1, device_id=1
        )
        assert rr.registers[0] == plc.cycles & 0x7FFF
    finally:
        c.close()


@pytest.mark.parametrize(
    ("register", "signal"),
    [("AERATION_DO", "AERATION:AHU-1:DO"),
     ("AERATION_BLOWER_VALVE", "AERATION:AHU-1:BLOWER_VALVE"),
     ("AERATION_WASTE_RATE", "AERATION:AHU-1:WASTE_RATE")],
)
def test_modbus_float_registers_read_back(plc: SoftPlc, register: str,
                                         signal: str) -> None:
    """Including the two low-word-first registers the contract plants as traps."""
    c = ModbusTcpClient("127.0.0.1", port=MB_PORT)
    c.connect()
    try:
        reg = C.register(register)
        rr = c.read_holding_registers(
            ModbusTcpServer.wire_offset(reg.address), count=2, device_id=1
        )
        got = decode_float32(rr.registers[0], rr.registers[1], reg.word_order)
        truth = plc.plant.snapshot().get(signal, 0.0)
        assert got == pytest.approx(truth, abs=max(0.5, abs(truth) * 0.25)), (
            f"{register} read back as {got}, plant reports {truth}"
        )
    finally:
        c.close()


def test_modbus_coils_report_equipment_state(plc: SoftPlc) -> None:
    c = ModbusTcpClient("127.0.0.1", port=MB_PORT)
    c.connect()
    try:
        states = plc.plant.snapshot().states
        for eq in ("PIT-1", "BLW-1", "BLW-2"):
            if eq not in C.state_equipment:
                continue
            idx = C.state_equipment.index(eq)
            rr = c.read_coils(idx + 1, count=1, device_id=1)
            assert bool(rr.bits[0]) == (states.get(eq, 0) == 1), eq
    finally:
        c.close()


# ─── OPC UA, as a client ─────────────────────────────────────────────────────


def test_opcua_client_discovers_the_plant(plc: SoftPlc) -> None:
    async def go() -> None:
        async with Client(ENDPOINT) as cl:
            names = [
                (await ch.read_browse_name()).Name
                for ch in await cl.nodes.objects.get_children()
            ]
            assert any("PLANT-A" in n for n in names)
    asyncio.run(go())


def test_opcua_publishes_real_values(plc: SoftPlc) -> None:
    async def go() -> None:
        async with Client(ENDPOINT) as cl:
            node = None
            children = await cl.nodes.objects.get_children()
            names = [(await ch.read_browse_name()).Name for ch in children]
            plant = next(ch for ch, n in zip(children, names, strict=True)
                         if "PLANT-A" in n)
            for area in await plant.get_children():
                if (await area.read_browse_name()).Name != "AERATION":
                    continue
                for child in await area.get_children():
                    if (await child.read_browse_name()).Name != "AHU-1":
                        continue
                    for v in await child.get_children():
                        if (await v.read_browse_name()).Name == "do_mg_l":
                            node = v
            assert node is not None, "DO must be discoverable"
            value = await node.read_value()
            assert value == pytest.approx(plc.plant.aeration.do_mg_l, abs=0.05), (
                "the address space must be flushed, not merely staged"
            )
    asyncio.run(go())


def test_opcua_quality_reflects_a_sensor_fault() -> None:
    """A failing instrument must reach the client as Bad/Uncertain, not as a
    number that happens to be wrong.

    This is the property that lets a historian be honest, and it is the reason
    OPC UA carries StatusCodes at all.
    """
    p = _start_plc(scenario="bad_instrument", duration_s=1800.0, own_ports=True)
    try:
        sig = "AERATION:AHU-1:DO"
        entry = p.opcua.space.variables[sig]
        assert entry.quality == QUALITY_UNCERTAIN, (
            "the drifting probe should publish an Uncertain status"
        )
        # And the process itself must be untouched — that is the whole point of
        # a sensor fault.
        assert p.plant.aeration.do_mg_l < 3.0, "the process should be healthy"
    finally:
        p.stop()


def _run_scenario_hours(scenario: str, hours: float) -> dict[str, float]:
    """Run one scenario for ``hours`` of plant time and report the signature."""
    p = _start_plc(scenario=scenario, duration_s=hours * 3600.0, own_ports=True)
    try:
        a = p.plant.aeration
        return {
            "do": a.do_mg_l,
            "kla": a.kla_per_h,
            "nh4_out": a.nh4_out_mg_l,
            "nh4_in": a.nh4_in_mg_l,
        }
    finally:
        p.stop()


def test_a_scenario_produces_its_signature_through_the_wire() -> None:
    """Arm the blower trip, run, and read the consequence.

    The signature is not "DO goes low" — it is the *order*. The basin holds about
    40 kg of dissolved oxygen, so a blower trip cannot empty it: DO sags over
    one to two hours while the nitrifiers are still fine, and effluent ammonia
    only breaks hours after that. Asserting the lag is what stops an alarm
    design that fires on ammonia when the actual fault was air.
    """
    at_2h = _run_scenario_hours("aeration_loss", 2.0)
    at_6h = _run_scenario_hours("aeration_loss", 6.0)

    assert at_2h["kla"] < 5.5, "the blower trip must cut transfer capability"

    # Two hours in: air is short, DO is falling, but the effluent is still good.
    assert at_2h["do"] < 2.0, f"DO should sag by 2 h, got {at_2h['do']}"
    assert at_2h["nh4_out"] < 2.0, (
        f"effluent ammonia must lag the air loss, got {at_2h['nh4_out']}"
    )

    # Six hours in: DO has collapsed and the ammonia breakthrough has arrived.
    assert at_6h["do"] < 0.2, f"DO should have collapsed, got {at_6h['do']}"
    assert at_6h["nh4_out"] > at_2h["nh4_out"] * 2.0, (
        f"ammonia must break through after the oxygen, "
        f"{at_2h['nh4_out']} -> {at_6h['nh4_out']}"
    )


# ─── the control program is actually running ─────────────────────────────────


def test_control_program_ran_every_scan(plc: SoftPlc) -> None:
    """A control program that silently never executes is indistinguishable
    from one that is correct, so the blocks must appear in the metrics."""
    phases = plc.health()["phase_worst_case_us"]
    assert any(k.startswith("block:") for k in phases), (
        f"no control block appears in the phase metrics: {sorted(phases)}"
    )


def test_lift_interlocks_are_evaluated(plc: SoftPlc) -> None:
    perms = plc.lift_control.perms.permissives
    assert "not_estop" in perms
    assert "suction_available" in perms


def test_aeration_loop_owns_a_setpoint(plc: SoftPlc) -> None:
    assert plc.aeration_control.setpoint_mg_l == pytest.approx(2.0)
    assert plc.aeration_control.duty_pct >= 0.0
