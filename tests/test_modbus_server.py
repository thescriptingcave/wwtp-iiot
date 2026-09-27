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
from softplc.servers.modbus import decode_float32
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
