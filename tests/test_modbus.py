"""Modbus register model tests.

Pure arithmetic, so these are exhaustive rather than representative. The float32
word-order handling in particular is worth testing hard, because it is the one
place where a *correct-looking* implementation produces quietly wrong data
rather than an error — and because the contract deliberately contains the trap
so the gateway will have to handle it for real.
"""

from __future__ import annotations

import struct

import pytest

from softplc.contract import contract
from softplc.servers.modbus import (
    HOLDING_BASE,
    HOLDING_SIZE,
    REGISTER_TO_SIGNAL,
    ModbusCodecError,
    RegisterModel,
    decode_float32,
    encode_float32,
    quantise_to_float32,
)

C = contract()

VALUES = {
    "INFLUENT:FLOW:FLOW": 1800.0,
    "INFLUENT:FLOW:TURBIDITY": 42.5,
    "INFLUENT:FLOW:NH4_IN": 22.0,
    "AERATION:AHU-1:DO": 2.0,
    "AERATION:AHU-1:SETPOINT_DO": 2.0,
    "AERATION:AHU-1:AIR_FLOW": 3200.0,
    "AERATION:AHU-1:MLSS": 3000.0,
    "AERATION:AHU-1:BLOWER_VALVE": 55.0,
    "AERATION:AHU-1:WASTE_RATE": 24.0,
    "SECONDARY:SEC-CL-1:BLANKET": 0.4,
    "SECONDARY:SEC-SCR-1:TORQUE": 46.0,
    "SLUDGE:DIG-1:PH": 7.1,
    "SLUDGE:DIG-1:CH4": 64.0,
    "SITE:WEATHER:STORM": 0.0,
    "INFLUENT:LIFT:RUNTIME": 12345.0,
}
#: Run state for the first five pieces of state equipment, which is what the
#: packed state word covers.
STATES = {
    "PIT-1": 1, "PIT-2": 0, "PIT-3": 3, "SCREEN-1": 1, "SCREEN-2": 0,
    "BLW-1": 1, "BLW-2": 1, "BLW-3": 3, "BLW-4": 3,
}


@pytest.fixture
def model() -> RegisterModel:
    m = RegisterModel()
    m.publish(VALUES, STATES, 42)
    return m


# ─── the float32 codec ───────────────────────────────────────────────────────


ROUND_TRIP_VALUES = [0.0, 1.0, -1.0, 2.5, -12.75, 0.1, 1800.0, 3000.0, 55.0, 1e-4, 1e4]


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
@pytest.mark.parametrize("order", ["big", "little"])
def test_float32_round_trips_exactly_in_both_word_orders(value: float, order: str) -> None:
    hi, lo = encode_float32(value, order)
    back = decode_float32(hi, lo, order)
    # Exact, because float32 is a lossy format and the comparison has to be
    # against the *quantised* value, not the original.
    assert back == quantise_to_float32(value)


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
def test_word_order_actually_matters(value: float) -> None:
    """If these agreed, the contract's traps would be pointless."""
    hi, lo = encode_float32(value, "big")
    assert encode_float32(value, "little") == (lo, hi)


def test_reading_the_wrong_word_order_gives_plausible_garbage() -> None:
    """The defining property of this bug: no error, just a wrong number."""
    hi, lo = encode_float32(2.5, "big")
    wrong = decode_float32(hi, lo, "little")
    assert wrong != 2.5
    # And it is not an obviously-invalid value either — nothing downstream can
    # tell it apart from a real reading without an out-of-range check.
    assert math_is_finite(wrong)


def math_is_finite(v: float) -> bool:
    return v == v and abs(v) != float("inf")


def test_encode_rejects_an_invalid_word_order() -> None:
    with pytest.raises(ModbusCodecError, match="word_order"):
        encode_float32(1.0, "middle")
    with pytest.raises(ModbusCodecError, match="word_order"):
        decode_float32(0, 0, "middle")


def test_encode_rejects_unrepresentable_values() -> None:
    """Overflowing to infinity would put a plausible reading on the wire."""
    with pytest.raises(ModbusCodecError, match="not representable"):
        encode_float32(1e300, "big")


def test_float_bytes_match_the_ieee754_layout() -> None:
    """Check against a known encoding, so the codec cannot drift."""
    hi, lo = encode_float32(1.0, "big")
    assert (hi, lo) == (0x3F80, 0x0000)
    hi, lo = encode_float32(2.0, "big")
    assert (hi, lo) == (0x4000, 0x0000)
    assert struct.unpack(">f", struct.pack(">f", -1.0))[0] == -1.0


# ─── address arithmetic ──────────────────────────────────────────────────────


def test_offset_translation() -> None:
    """4xxxx register number → list index. 40000 is the first holding register."""
    assert RegisterModel._offset(40000) == 0, "the first holding register"
    assert RegisterModel._offset(40001) == 1
    assert RegisterModel._offset(40100) == 100
    # And every contract register lands in range.
    for reg in C.registers:
        assert RegisterModel._offset(reg.address) >= 0


def test_every_contract_register_fits_in_the_block() -> None:
    for reg in C.registers:
        off = RegisterModel._offset(reg.address)
        assert off >= 0, f"{reg.name} is below the holding block"
        assert off + reg.width <= HOLDING_SIZE, f"{reg.name} is above it"


def test_map_validation_rejects_an_overlap() -> None:
    """An overlap yields plausible wrong values, not an error, so catch it early."""
    raw = C.registers + (C.register("INFLUENT_FLOW"),)  # duplicate address
    original = C.registers
    try:
        object.__setattr__(C, "registers", raw)
        with pytest.raises(ModbusCodecError, match="claimed by both"):
            RegisterModel()
    finally:
        object.__setattr__(C, "registers", original)


# ─── publishing ──────────────────────────────────────────────────────────────


def test_heartbeat_publishes(model: RegisterModel) -> None:
    assert model.read_holding(40000)[0] == 42


def test_heartbeat_wraps_at_int16(model: RegisterModel) -> None:
    m = RegisterModel()
    m.publish(VALUES, STATES, 0x7FFF)
    assert m.read_holding(40000)[0] == 0x7FFF
    m.publish(VALUES, STATES, 0x8000)
    assert m.read_holding(40000)[0] == 0, "must wrap, never exceed 0x7FFF"


def test_storm_flag_reflects_the_signal() -> None:
    m = RegisterModel()
    m.publish({**VALUES, "SITE:WEATHER:STORM": 1.0}, STATES, 1)
    assert m.read_holding(40351)[0] == 1
    m.publish({**VALUES, "SITE:WEATHER:STORM": 0.0}, STATES, 1)
    assert m.read_holding(40351)[0] == 0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("AERATION_DO", 2.0),           # high word first
        ("AERATION_BLOWER_VALVE", 55.0),  # low word first — the trap
        ("AERATION_WASTE_RATE", 24.0),     # low word first — the trap
        ("INFLUENT_FLOW", 1800.0),
    ],
)
def test_float_registers_decode_with_their_declared_word_order(
    model: RegisterModel, name: str, expected: float
) -> None:
    assert model.read_holding_float(name) == pytest.approx(expected, rel=1e-6)


def test_contract_really_does_mix_word_orders() -> None:
    """If it stopped, the gateway's decode path would be untested."""
    orders = {r.word_order for r in C.registers if r.type == "float32"}
    assert orders == {"big", "little"}, (
        "the contract must contain both word orders so the gateway's decoding "
        "is exercised by the simulation"
    )


def test_thirty_two_bit_runtime_spans_a_register_pair(model: RegisterModel) -> None:
    hi = model.read_holding(40401)[0]
    lo = model.read_holding(40402)[0]
    assert (hi << 16 | lo) == 12345


def test_state_word_packs_three_bits_per_device(model: RegisterModel) -> None:
    """Nibbles follow contract order, which is the only thing a client can rely
    on — Modbus has no discovery, so the layout is documented and nothing more."""
    word = model.read_holding(40400)[0]
    decoded = RegisterModel.decode_state_word(word)
    first_five = list(C.state_equipment[:5])
    for i, eq in enumerate(first_five):
        assert decoded[i] == STATES.get(eq, 0), f"nibble {i} should be {eq}"
    assert decoded[0] == 1, "PIT-1 is running"
    assert decoded[2] == 3, "PIT-3 is standby"


def test_state_word_round_trips(model: RegisterModel) -> None:
    word = model.read_holding(40400)[0]
    assert RegisterModel.decode_state_word(word) == \
        RegisterModel.decode_state_word(RegisterModel()._state_word(STATES))


# ─── coils ───────────────────────────────────────────────────────────────────


def test_coils_reflect_running_state(model: RegisterModel) -> None:
    assert model.read_coil(model.coil_index("BLW-1")) is True
    assert model.read_coil(model.coil_index("BLW-3")) is False, "standby is not running"


def test_every_state_equipment_has_a_coil(model: RegisterModel) -> None:
    for eq in C.state_equipment:
        idx = model.coil_index(eq)
        assert 0 < idx < len(model.coils)


# ─── the write surface ───────────────────────────────────────────────────────


def test_writable_register_can_be_written(model: RegisterModel) -> None:
    model.write_holding_float("AERATION_SETPOINT_DO", 3.5)
    assert model.read_holding_float("AERATION_SETPOINT_DO") == pytest.approx(3.5)


def test_read_only_registers_refuse_writes(model: RegisterModel) -> None:
    """The write surface is a security control, so it is enforced, not implied."""
    for name in ("AERATION_DO", "AERATION_MLSS", "INFLUENT_FLOW"):
        with pytest.raises(ModbusCodecError, match="not writable"):
            model.write_holding_float(name, 1.0)


def test_writable_set_is_small() -> None:
    assert len(C.writable) <= 5, "the write surface should stay deliberately small"


def test_writes_are_clamped_to_a_register(model: RegisterModel) -> None:
    off = model.read_holding(40102)
    model.write_holding(40102, [0x1FFFF])
    assert model.read_holding(40102)[0] == 0xFFFF


def test_out_of_range_access_is_refused(model: RegisterModel) -> None:
    with pytest.raises(ModbusCodecError, match="out of range"):
        model.read_holding(HOLDING_BASE + HOLDING_SIZE + 100, 1)
    with pytest.raises(ModbusCodecError, match="out of range"):
        model.write_holding(40000 - 10, [1])


# ─── the map's correspondence to the contract ────────────────────────────────


def test_every_float_register_maps_to_a_real_signal() -> None:
    """A register with no signal mapping would publish as all zeroes — a
    plausible-looking 0.0 on the wire rather than an error."""
    for reg in C.registers:
        if reg.type != "float32":
            continue
        assert reg.name in REGISTER_TO_SIGNAL, f"{reg.name} has no signal mapping"
        signal_id = REGISTER_TO_SIGNAL[reg.name]
        assert signal_id in C.signals, f"{reg.name} maps to unknown {signal_id!r}"
        assert signal_id in VALUES, f"{reg.name} is mapped but absent from the fixture"


def test_publish_is_idempotent(model: RegisterModel) -> None:
    """Re-publishing the same state must not drift — a scanner polls repeatedly."""
    before = list(model.holding)
    for _ in range(5):
        model.publish(VALUES, STATES, 42)
    assert list(model.holding) == before


def test_a_float_pair_is_written_atomically(model: RegisterModel) -> None:
    """Both halves come from one value in one call, so a client cannot read a
    value straddling a scan."""
    m = RegisterModel()
    for value in (1.0, 2.0, 3.0, 999.0):
        m.publish({**VALUES, "AERATION:AHU-1:DO": value}, STATES, 1)
        assert m.read_holding_float("AERATION_DO") == pytest.approx(value, rel=1e-6)


def test_publish_does_not_mutate_the_caller_dicts() -> None:
    values = dict(VALUES)
    states = dict(STATES)
    m = RegisterModel()
    m.publish(values, states, 1)
    assert values == VALUES
    assert states == STATES


def test_write_surface_is_enforced_by_the_contract_not_by_the_wire() -> None:
    """Modbus has no concept of a read-only register.

    Every register in the 4xxxx block is writable at the protocol level. The
    protection is the *declared* write surface in the contract, and the gateway's
    obligation to honour it. This test pins the contract side; claiming the wire
    enforces it would be false, and OPC UA is the protocol that actually does.
    """
    writable = {r.name for r in C.registers if r.writable}
    assert writable == {"AERATION_SETPOINT_DO", "FAULT_CODE", "STORM_FLAG"}, (
        f"unexpected writable set: {sorted(writable)}"
    )
    for reg in C.registers:
        if reg.name in ("AERATION_DO", "AERATION_MLSS", "INFLUENT_FLOW"):
            assert not reg.writable, f"{reg.name} must be read-only"


