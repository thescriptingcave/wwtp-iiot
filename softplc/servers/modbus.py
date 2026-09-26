"""Modbus register model and float32 codec.

Deliberately independent of ``pymodbus``.

The register map, the address arithmetic, and the float32 word-order handling are
the parts worth owning and testing. They are pure arithmetic, and pure
arithmetic can be tested exhaustively. Binding them to a particular version of a
protocol library's datastore API would make them untestable in isolation — and
``pymodbus`` 3.x has rearranged that API twice, which is exactly the cost.

``softplc/servers/modbus_server.py`` adapts this model to the wire.

What Modbus actually gives you, and why it is worth learning
------------------------------------------------------------
* **The register map is the entire interface.** A client knows that register
  40104 holds two registers which, read as a high-word-first float, are the
  aeration air flow. There is no schema, no unit, no way to ask, and no
  discovery.
* **32-bit values are the trap.** A float spans two consecutive registers and the
  word order is device-specific: ABCD (high word first) or CDAB (low word
  first). Read the wrong one and you get a plausible, entirely wrong number
  rather than an error — ``2.5`` read with the wrong order decodes to 2.3e-41,
  which looks like noise and gets averaged away.
* **No subscriptions.** The client polls, so deadbanding, change detection and
  data volume are all the client's problem.
* **No quality, timestamp, or units.** Everything a historian needs beyond the
  raw number has to come from somewhere else — which is precisely why the tag
  contract exists.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from softplc.contract import UDT_FLOAT32, Contract, contract as get_contract
from softplc.scanloop import ScanState

#: The 4xxxx convention, and the single most common source of off-by-one errors
#: in Modbus code: libraries disagree about whether the first holding register is
#: addressed as 0, 1, or 40001.
#:
#: This model resolves it once. Contract addresses are register numbers offset
#: by 40000, so the wire offset is a plain subtraction:
#:
#:     40000 → 0      40001 → 1      40100 → 100
#:
#: Every translation goes through :meth:`RegisterModel._offset` so there is
#: exactly one place to get wrong. Converting to a library's own convention
#: belongs in the wire adapter, not smeared through the register model.
HOLDING_BASE = 40000
COIL_BASE = 0
#: Block sizes. Comfortably larger than the contract needs, so a client reading
#: a whole block gets zeroes rather than an error for anything unmapped.
HOLDING_SIZE = 4096
COIL_SIZE = 256

_FLOAT = struct.Struct(">f")


class ModbusCodecError(ValueError):
    pass


def encode_float32(value: float, word_order: str = "big") -> tuple[int, int]:
    """Encode a float as two 16-bit registers.

    ``big``    → high word first (ABCD). The common convention.
    ``little`` → low word first  (CDAB). Also common, and a classic source of
                quiet data corruption.
    """
    if word_order not in ("big", "little"):
        raise ModbusCodecError(f"word_order must be 'big' or 'little', got {word_order!r}")
    try:
        raw = _FLOAT.pack(value)
    except (OverflowError, struct.error) as exc:
        # Encoding out-of-range as infinity would put a plausible reading on the
        # wire; failing here is the honest behaviour.
        raise ModbusCodecError(f"value {value!r} is not representable as float32") from exc
    first, second = struct.unpack(">HH", raw)
    if word_order == "big":
        return int(first), int(second)
    return int(second), int(first)


def decode_float32(first: int, second: int, word_order: str = "big") -> float:
    """Decode two registers to a float, honouring the word order."""
    if word_order not in ("big", "little"):
        raise ModbusCodecError(f"word_order must be 'big' or 'little', got {word_order!r}")
    if word_order == "big":
        raw = struct.pack(">HH", first & 0xFFFF, second & 0xFFFF)
    else:
        raw = struct.pack(">HH", second & 0xFFFF, first & 0xFFFF)
    return float(_FLOAT.unpack(raw)[0])


def quantise_to_float32(value: float) -> float:
    """Round a value to what a float32 can actually represent.

    Needed in tests and in the decoder: comparing a round-tripped value against
    the original with a fixed tolerance fails for large magnitudes, because
    float32 carries about seven significant digits and nothing more.
    """
    return float(_FLOAT.unpack(_FLOAT.pack(value))[0])


#: Modbus register name → contract signal id.
#:
#: Derived from the contract's ``signal:`` field rather than written out here.
#: This mapping used to be a literal dict in this file, which meant the register
#: map existed in two places: the YAML the client read, and this copy the server
#: read. They could not disagree loudly — the server would simply publish
#: whatever its own dict said, and a register renamed in the contract would keep
#: working here while breaking every client. One source, validated at load.
REGISTER_TO_SIGNAL: dict[str, str] = {
    r.name: r.signal for r in get_contract().registers if r.signal
}


@dataclass(slots=True)
class RegisterModel:
    """The plant's Modbus register image.

    Three blocks, chosen to reflect what each is *for* rather than lumping
    everything into one:

    ``holding``  read/write. Writable setpoints and the fault-injection
                 register. A client's ability to change something should be a
                 deliberate decision, visible in the address map.
    ``coil``     read/write single bits. Equipment run state, because state
                 genuinely *is* one bit. Modelling it as a 16-bit number in a
                 holding register is a common and pointless misrepresentation.
    """

    c: Contract = field(default_factory=get_contract)
    holding: list[int] = field(default_factory=lambda: [0] * HOLDING_SIZE)
    coils: list[bool] = field(default_factory=lambda: [False] * COIL_SIZE)
    #: Latest decoded value per register name, for the gateway and for tests.
    values: dict[str, float] = field(default_factory=dict)
    states: dict[str, int] = field(default_factory=dict)
    fault_code: int = 0
    heartbeat: int = 0

    def __post_init__(self) -> None:
        self._validate_map()

    # ─── map validation ──────────────────────────────────────────────────────

    def _validate_map(self) -> None:
        """Reject a register map that would silently corrupt data.

        An overlap here produces *plausible wrong values* rather than an error,
        which is the worst class of configuration bug there is.
        """
        occupied: dict[int, str] = {}
        for reg in self.c.registers:
            for addr in range(reg.address, reg.address + reg.width):
                if addr in occupied:
                    raise ModbusCodecError(
                        f"Modbus address {addr} claimed by both "
                        f"{occupied[addr]!r} and {reg.name!r}"
                    )
                occupied[addr] = reg.name
            offset = self._offset(reg.address)
            if offset < 0 or offset + reg.width > HOLDING_SIZE:
                raise ModbusCodecError(
                    f"register {reg.name!r} at {reg.address} falls outside the "
                    f"holding block of {HOLDING_SIZE}"
                )

    @staticmethod
    def _offset(address: int) -> int:
        """4xxxx register number → index into the ``holding`` list.

        40000 is the first holding register, so this is a plain subtraction.
        Having it in one method means the convention can be changed for a whole
        file at once.
        """
        return address - HOLDING_BASE

    # ─── publishing plant state ──────────────────────────────────────────────

    def publish(self, values: dict[str, float], states: dict[str, int],
                heartbeat: int) -> None:
        """Copy current plant state into the register image.

        Called after every scan. A client's read of two consecutive registers
        must never straddle a scan, so each value is written whole — the two
        halves of a float32 are produced from one value in one call rather than
        accumulated across scans.
        """
        self.heartbeat = heartbeat & 0x7FFF
        self.states = dict(states)

        for reg in self.c.registers:
            off = self._offset(reg.address)
            if reg.name == "HEARTBEAT":
                self.holding[off] = self.heartbeat
                continue
            if reg.name == "FAULT_CODE":
                self.holding[off] = self.fault_code & 0x7FFF
                continue
            if reg.name == "EQUIP_STATE_WORD":
                self.holding[off] = self._state_word(states)
                continue
            if reg.name in ("PUMP1_RUNTIME_HI", "PUMP1_RUNTIME_LO"):
                continue  # written as a pair below

            # Unlinked registers are not measurements, so there is nothing to
            # publish for them. ``STORM_FLAG`` used to need a special case here
            # to threshold its value to 0/1; now that it declares a ``signal:``
            # the generic path below handles it, and the flag's own field is
            # already 0 or 1.
            if reg.signal is None:
                continue

            value = values.get(reg.signal)
            if value is None:
                continue
            self.values[reg.name] = value
            if reg.type == UDT_FLOAT32:
                hi, lo = encode_float32(value, reg.word_order)
                self.holding[off] = hi
                self.holding[off + 1] = lo
            else:
                self.holding[off] = int(value) & 0x7FFF

        # 32-bit runtime across a register pair. Modbus has no 32-bit type, so
        # this is the usual convention: high word first, unsigned.
        runtime_h = int(values.get("INFLUENT:LIFT:RUNTIME", 0.0)) & 0xFFFFFFFF
        hi_reg = self.c.register("PUMP1_RUNTIME_HI")
        lo_reg = self.c.register("PUMP1_RUNTIME_LO")
        self.holding[self._offset(hi_reg.address)] = (runtime_h >> 16) & 0xFFFF
        self.holding[self._offset(lo_reg.address)] = runtime_h & 0xFFFF

        # Coils: one bit per piece of state equipment, in contract order.
        for i, eq in enumerate(self.c.state_equipment):
            if i + 1 < COIL_SIZE:
                self.coils[i + 1] = states.get(eq, 0) == int(ScanState.RUNNING)

    def _state_word(self, states: dict[str, int]) -> int:
        """Pack five devices' state into one register, 3 bits each.

        A convenience a real vendor offers so a client needs one read instead of
        twenty. The bit layout is documented only here, because Modbus has no
        way to discover it — which is the whole difficulty of the protocol.
        """
        word = 0
        for i, eq in enumerate(self.c.state_equipment[:5]):
            word |= (int(states.get(eq, 0)) & 0x07) << (i * 3)
        return word & 0xFFFF

    @staticmethod
    def decode_state_word(word: int, count: int = 5) -> list[int]:
        """Inverse of :meth:`_state_word`, so a client has something to copy."""
        return [(word >> (i * 3)) & 0x07 for i in range(count)]

    # ─── read access, for clients and tests ──────────────────────────────────

    def read_holding(self, address: int, count: int = 1) -> list[int]:
        off = self._offset(address)
        if off < 0 or off + count > HOLDING_SIZE:
            raise ModbusCodecError(
                f"holding read at {address} count {count} is out of range"
            )
        return list(self.holding[off:off + count])

    def read_holding_float(self, name: str) -> float:
        """Read a float register by its contract name.

        This is what a correct client does — it looks the register up in the
        address map and honours the declared word order. Getting it wrong is the
        bug this project deliberately contains.
        """
        reg = self.c.register(name)
        if reg.type != UDT_FLOAT32:
            raise ModbusCodecError(f"{name} is {reg.type}, not a float32")
        off = self._offset(reg.address)
        return decode_float32(self.holding[off], self.holding[off + 1], reg.word_order)

    def write_holding(self, address: int, values: list[int]) -> None:
        off = self._offset(address)
        if off < 0 or off + len(values) > HOLDING_SIZE:
            raise ModbusCodecError("holding write out of range")
        self.holding[off:off + len(values)] = [v & 0xFFFF for v in values]

    def write_holding_float(self, name: str, value: float) -> None:
        reg = self.c.register(name)
        if not reg.writable:
            raise ModbusCodecError(
                f"{name} is not writable in the contract; writing it would "
                "defeat the point of declaring a write surface"
            )
        if reg.type != UDT_FLOAT32:
            raise ModbusCodecError(f"{name} is {reg.type}, not a float32")
        hi, lo = encode_float32(value, reg.word_order)
        off = self._offset(reg.address)
        self.holding[off] = hi
        self.holding[off + 1] = lo

    def read_coil(self, index: int) -> bool:
        return self.coils[index]

    def write_coil(self, index: int, value: bool) -> None:
        self.coils[index] = bool(value)

    def coil_index(self, equipment: str) -> int:
        return self.c.state_equipment.index(equipment) + 1


__all__ = [
    "COIL_BASE",
    "COIL_SIZE",
    "HOLDING_BASE",
    "HOLDING_SIZE",
    "REGISTER_TO_SIGNAL",
    "ModbusCodecError",
    "RegisterModel",
    "decode_float32",
    "encode_float32",
    "quantise_to_float32",
]
