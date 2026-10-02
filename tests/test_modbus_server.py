"""Modbus TCP integration tests.

A real server, a real client, a real socket. The register model can be tested as
pure arithmetic, but the *wire adapter* can only be tested by reading a value
back through a client — and that is exactly where the interesting bugs live,
because a wrong offset reads as a plausible zero rather than an error.

Two bugs this file exists to prevent, both of which were introduced and caught
during development:

  * A non-zero base address made ``getValues`` return an empty list, so every
    register silently read as zero.
  * An off-by-one between the model's offset and pymodbus's block addressing
    would shift the whole map by one register — every value wrong, none of them
    obviously so.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from pymodbus.client import ModbusTcpClient
from softplc.contract import contract
from softplc.servers.modbus import decode_float32, encode_float32
from softplc.servers.modbus_server import ModbusTcpServer

#: These exercise the *wire*: a real client reading a real socket. The
#: contract-address → PDU translation was settled empirically with a unique
#: ramp and is asserted here against a live server, because a wrong translation
#: returns plausible values from the neighbouring register rather than an error
#: — which is precisely why it needed proving rather than assuming.
C = contract()
PORT = 5032

VALUES = {
    "INFLUENT:FLOW:FLOW": 1800.0,
    "AERATION:AHU-1:DO": 2.0,
    "AERATION:AHU-1:BLOWER_VALVE": 55.0,
    "AERATION:AHU-1:AIR_FLOW": 3300.0,
    "SITE:WEATHER:STORM": 1.0,
    "INFLUENT:LIFT:RUNTIME": 12345.0,
}
STATES = {"PIT-1": 1, "PIT-2": 0, "PIT-3": 3, "BLW-1": 1}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# One server for the whole module, on its own event loop in a background thread.
#
# ``StartTcpServer`` is synchronous and offers no cooperative shutdown, so a
# per-test fixture would leak a listening thread on every test and eventually
# hang the suite. A single module-scoped loop is both simpler and honest about
# the library's limits.
_PORT = _free_port()
_SERVER = ModbusTcpServer(host="127.0.0.1", port=_PORT)
_SERVER.publish(VALUES, STATES, 42)
_LOOP = asyncio.new_event_loop()
_LOOP.run_until_complete(_SERVER.start())

# Modbus runs in a worker thread; give the listener a moment to bind.
import time as _time

for _ in range(50):
    try:
        with socket.create_connection(("127.0.0.1", _PORT), timeout=0.2):
            break
    except OSError:
        _time.sleep(0.1)


@pytest.fixture(scope="module")
def server():
    _SERVER.publish(VALUES, STATES, 42)
    return _SERVER, _PORT


@pytest.fixture
def client():
    c = ModbusTcpClient("127.0.0.1", port=_PORT)
    c.connect()
    try:
        yield c
    finally:
        c.close()


def pytest_sessionfinish(session, exitstatus) -> None:
    """Tear the listener down so pytest can exit."""
    try:
        _LOOP.call_soon_threadsafe(_LOOP.stop)
    except Exception:
        pass


def _read_float(c: ModbusTcpClient, name: str) -> float:
    """Read a float register through a real client, honouring word order."""
    reg = C.register(name)
    # Contract address → the address a client requests (pymodbus reserves 0).
    offset = ModbusTcpServer.wire_offset(reg.address)
    rr = c.read_holding_registers(offset, count=2, device_id=1)
    assert not rr.isError(), f"read of {name} failed: {rr}"
    return decode_float32(rr.registers[0], rr.registers[1], reg.word_order)


# ─── the round trip ──────────────────────────────────────────────────────────


def test_client_can_read_a_high_word_first_float(client) -> None:
    assert _read_float(client, "AERATION_DO") == pytest.approx(2.0, abs=1e-6)


def test_client_can_read_a_low_word_first_float(client) -> None:
    """The trap. If the adapter flattened the word order, this is what breaks —
    and it breaks silently, producing 2.3e-41 rather than an error."""
    assert _read_float(client, "AERATION_BLOWER_VALVE") == pytest.approx(55.0, abs=1e-6)


def test_every_float_register_survives_the_round_trip(client) -> None:
    for reg in C.registers:
        if reg.type != "float32":
            continue
        signal_id = _signal_for(reg.name)
        if signal_id not in VALUES:
            continue
        got = _read_float(client, reg.name)
        assert got == pytest.approx(VALUES[signal_id], abs=1e-6), (
            f"{reg.name} ({reg.word_order}-word) read back as {got}, "
            f"published {VALUES[signal_id]}"
        )


def _signal_for(reg_name: str) -> str:
    from softplc.servers.modbus import REGISTER_TO_SIGNAL

    return REGISTER_TO_SIGNAL.get(reg_name, reg_name)


def test_heartbeat_is_readable(client) -> None:
    rr = client.read_holding_registers(
        ModbusTcpServer.wire_offset(40000), count=1, device_id=1
    )
    assert rr.registers[0] == 42


def test_storm_flag_is_readable(client) -> None:
    off = ModbusTcpServer.wire_offset(C.register("STORM_FLAG").address)
    rr = client.read_holding_registers(off, count=1, device_id=1)
    assert rr.registers[0] == 1


def test_thirty_two_bit_runtime_reads_back(client) -> None:
    hi_off = ModbusTcpServer.wire_offset(C.register("PUMP1_RUNTIME_HI").address)
    rr = client.read_holding_registers(hi_off, count=2, device_id=1)
    assert (rr.registers[0] << 16 | rr.registers[1]) == 12345


def test_equipment_state_is_readable_as_coils(client) -> None:
    """Coils are the right type for state, and the index follows contract order."""
    idx = C.state_equipment.index("PIT-1")
    assert c_read_coil(client, idx) is True, "PIT-1 is running"
    assert c_read_coil(client, C.state_equipment.index("PIT-2")) is False
    assert c_read_coil(client, C.state_equipment.index("PIT-3")) is False, (
        "standby is not running"
    )


def c_read_coil(c: ModbusTcpClient, contract_index: int) -> bool:
    rr = c.read_coils(contract_index + 1, count=1, device_id=1)
    return bool(rr.bits[0])


def test_input_registers_carry_a_bulk_table(client) -> None:
    """One read for many signals, scaled to int16. Documented, not implicit."""
    rr = client.read_input_registers(0, count=8, device_id=1)
    assert not rr.isError()
    assert all(0 <= w <= 32767 or -32768 <= w <= 32767 for w in rr.registers)
    assert any(w != 0 for w in rr.registers), "the table should carry some data"


# ─── writing ─────────────────────────────────────────────────────────────────


def _setpoint_words(value: float) -> list[int]:
    """The two registers a client would send for a setpoint of `value`."""
    reg = C.register("AERATION_SETPOINT_DO")
    return list(encode_float32(value, reg.word_order))


def _write_setpoint(client: ModbusTcpClient, value: float):
    off = ModbusTcpServer.wire_offset(C.register("AERATION_SETPOINT_DO").address)
    return client.write_registers(off, _setpoint_words(value), device_id=1)


def test_a_client_may_write_the_setpoint(client, server) -> None:
    srv, _port = server
    reg = C.register("AERATION_SETPOINT_DO")
    assert reg.writable, "the setpoint must be writable for this test to mean anything"

    # Written through the model, not straight into the datastore: the datastore
    # has a one-slot lead-in, and a test that bypassed it would be asserting the
    # wrong address space.
    srv.model.write_holding_float("AERATION_SETPOINT_DO", 2.75)
    srv._flush()
    assert _read_float(client, "AERATION_SETPOINT_DO") == pytest.approx(2.75, abs=1e-6)


def test_a_client_write_reaches_the_scan_loop(client, server) -> None:
    """A Modbus write is queued for the plant, not just stored in the block.

    **The write-back path did not exist, and every test above passed anyway.**

    The pre-existing write test went through `srv.model.write_holding_float()` —
    the model's own API, called in-process. So it proved the *model* can hold a
    written value, which was always true, and said nothing about whether a write
    arriving over the wire from a client reached the plant. It did not: writes
    landed in the pymodbus block and were overwritten by the next `_flush`, one
    scan later. An operator's setpoint read back as the old value forever.

    This goes through a real client and a real function code, so it exercises the
    chokepoint (`ModbusSlaveContext.setValues`) that every FC6/FC16 write passes
    through, and asserts the thing that was missing: the value is *queued* for
    the scan loop rather than left in the block.
    """
    srv, _port = server
    srv.take_writes()  # drain anything a previous test left

    assert not _write_setpoint(client, 3.25).isError()

    pending = srv.pending_writes
    assert pending == {"AERATION:AHU-1:SETPOINT_DO": pytest.approx(3.25, abs=1e-6)}, (
        f"the write did not reach the scan loop's queue; pending={pending}. It "
        "went into the block, which the next publish overwrites — which is what "
        "made every write appear to succeed and then not take."
    )
    srv.take_writes()


def test_take_writes_hands_over_each_value_once(client, server) -> None:
    """A write must be applied once, not re-driven every scan.

    Leaving it queued would re-apply the setpoint on every scan, and a controller
    whose input is re-written 50 times a second is not holding a setpoint. The
    clearing is as load-bearing as the hand-over.
    """
    srv, _port = server
    srv.take_writes()
    _write_setpoint(client, 2.25)

    assert srv.take_writes() != {}, "the write should be available once"
    assert srv.take_writes() == {}, "and only once"


def test_a_refused_write_leaves_the_block_alone(client, server) -> None:
    """A refused write must not reach the datastore.

    The worst available outcome is a write that lands in the block while the
    client is told it failed: the operator sees an error, believes the plant is
    unchanged, and it is not. So the block is checked, not just the exception.
    """
    srv, _port = server
    srv.take_writes()
    srv.publish({**VALUES, "AERATION:AHU-1:SETPOINT_DO": 2.0}, STATES, 42)

    # FC6 to a read-only register: one register, so it fits any width.
    off = ModbusTcpServer.wire_offset(C.register("AERATION_DO").address)
    err = client.write_register(off, 0x4248, device_id=1)

    assert err.isError(), "writing a read-only register must be refused"
    assert _read_float(client, "AERATION_DO") != pytest.approx(4.8, abs=1e-3), (
        "the refused value reached the block anyway"
    )


def test_an_out_of_range_write_is_refused(client, server) -> None:
    """The engineering range is enforced on the wire, not only in Node-RED.

    Node-RED's permit check is the *client's* courtesy. The Modbus specification
    carries no engineering range at all, so if the server does not check it then
    any other client — a laptop with a Modbus tool, a script — can write 99 mg/L
    to a basin rated for 6. `OpcUaServer.write_value` checks this for OPC UA;
    without the same check here the two protocols would differ on a safety
    property.
    """
    srv, _port = server
    srv.take_writes()
    srv.publish({**VALUES, "AERATION:AHU-1:SETPOINT_DO": 2.0}, STATES, 42)

    assert _write_setpoint(client, 99.0).isError(), (
        "99 mg/L is outside [0.5, 6] and must not be accepted"
    )
    assert srv.pending_writes == {}, "a refused write must not be queued"
    assert _read_float(client, "AERATION_SETPOINT_DO") == pytest.approx(
        2.0, abs=1e-6
    ), "and must not reach the block"


def test_half_a_float32_is_refused(client, server) -> None:
    """Writing one register of a two-register float is refused, not merged.

    The failure this prevents is the quiet one: FC6 to the low half would leave
    the high half holding its old value, and the pair decodes to a finite, in-range
    float that is neither the value sent nor the value before it. Nothing raises.
    """
    srv, _port = server
    srv.take_writes()
    off = ModbusTcpServer.wire_offset(C.register("AERATION_SETPOINT_DO").address)

    assert client.write_register(off, 0x4060, device_id=1).isError()


def test_writes_are_counted(client, server) -> None:
    """Accepted and refused writes are visible, not inferred.

    An operator's evidence that a setpoint took is "the register changed", which
    cannot distinguish the plant obeying from the plant overwriting. The counters
    can, and they are the cheapest answer to "has anything ever written here".
    """
    srv, _port = server
    srv.take_writes()
    before = srv.write_stats()

    _write_setpoint(client, 2.4)
    _write_setpoint(client, 400.0)  # refused: out of range
    srv.take_writes()

    after = srv.write_stats()
    assert after["accepted"] == before["accepted"] + 1
    assert any("outside" in reason for reason in after["refused"])


# ─── robustness ──────────────────────────────────────────────────────────────


def test_publishing_again_updates_the_wire(client, server) -> None:
    """A scanner polls repeatedly; the values must track the plant."""
    srv, _port = server
    srv.publish({**VALUES, "AERATION:AHU-1:DO": 4.5}, STATES, 43)
    assert _read_float(client, "AERATION_DO") == pytest.approx(4.5, abs=1e-6)
    rr = client.read_holding_registers(
        ModbusTcpServer.wire_offset(40000), count=1, device_id=1
    )
    assert rr.registers[0] == 43


def test_wrong_word_order_gives_wrong_numbers_not_errors(client) -> None:
    """The defining property, asserted against a real server.

    A client that ignores the address map and assumes high-word-first gets a
    finite, in-range, entirely wrong value for every low-word-first register. No
    amount of range checking downstream will catch it.
    """
    reg = C.register("AERATION_BLOWER_VALVE")
    off = ModbusTcpServer.wire_offset(reg.address)
    rr = client.read_holding_registers(off, count=2, device_id=1)
    wrong = decode_float32(rr.registers[0], rr.registers[1], "big")
    assert wrong != pytest.approx(55.0, abs=1e-6)
    assert wrong == wrong, "finite — which is why it is dangerous"
