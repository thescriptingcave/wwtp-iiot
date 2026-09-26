"""Read the plant over Modbus TCP.

Modbus gives you a flat array of 16-bit registers and no idea what any of them
mean. Everything that makes it usable — which register is which signal, whether
it is one register or two, and which word comes first — lives in
``contracts/tags.yaml``. This module is the client half of the same mapping the
server half uses in ``softplc/servers/modbus.py``.

Two things make Modbus reading different from OPC UA reading, and both are worth
stating before the code.

## You must know what to ask for

OPC UA is a browsable address space: connect, walk it, read what you want. Modbus
has no discovery whatsoever. A client that does not already know the register
map cannot find anything, which is why every Modbus integration in existence
carries a hand-maintained spreadsheet of addresses. Here that spreadsheet is the
contract — the same file the server is built from, so a register cannot be
renamed on one side and not the other.

## Word order is a per-register property, not a setting

A float32 is two 16-bit registers, and the protocol does not say which half
comes first. Vendors disagree, devices disagree, and sometimes one device
disagrees with itself between registers.

The contract therefore carries ``word_order`` per register, and this client
honours it. Reading a low-word-first register as high-word-first does not
produce an error — it produces a finite, in-range, wrong number. ``2.0`` comes
back as ``2.3e-41``, which plots as a flat line near zero and looks like a
process that stopped. That is the signature bug class of this whole project, and
the only defence is to get the mapping from a file that is tested rather than
from a memory that is not.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from softplc.contract import QUALITY_BAD, QUALITY_GOOD, UDT_FLOAT32, Contract
from softplc.servers.modbus import decode_float32
from softplc.servers.modbus_server import ModbusTcpServer

log = logging.getLogger("gateway.modbus")

#: pymodbus caps a single read at 125 registers. Reading more is an error rather
#: than a silent truncation, which is at least honest, but it means a plan has to
#: be fetched in windows.
MAX_READ = 125

#: How many consecutive failed polls before the link is declared down. One missed
#: poll is normal on a plant network; a run of them is a cable.
FAILURES_BEFORE_DOWN = 3


class ModbusLinkDownError(RuntimeError):
    """The link has failed ``FAILURES_BEFORE_DOWN`` times in a row."""


@dataclass(slots=True)
class ModbusPollResult:
    """One successful poll."""

    values: dict[str, float]
    quality: dict[str, int]
    register_count: int
    elapsed_ms: float
    failed_windows: int = 0


class ModbusReader:
    """Contract-driven Modbus TCP reader.

    Reads the whole plan in as few requests as the 125-register limit allows,
    rather than one request per signal. One request per signal would be a request
    per signal per poll, and on a plant network that is enough to saturate a
    shared link and start dropping packets — a self-inflicted problem that then
    gets blamed on the network.
    """

    def __init__(self, contract: Contract, host: str = "127.0.0.1",
                 port: int = 5020, timeout: float = 3.0) -> None:
        self.c = contract
        self.unit_id = int(contract.modbus.get("unit_id", 1))
        self._client = ModbusTcpClient(host, port=port, timeout=timeout)
        self._consecutive_failures = 0
        self.connected = False
        self._plan = self._build_plan()

    # ─── the plan ────────────────────────────────────────────────────────────

    def _build_plan(self) -> list[tuple[int, int, list[tuple[str, int]]]]:
        """Group registers into contiguous read windows.

        Returns ``(start_pdu, register_count, [(name, offset_in_window), ...])``.
        Windows are grown while they stay contiguous *and* under the 125-register
        limit, so a plan with a handful of gaps costs a handful of requests
        rather than one per gap.

        **The offset is stored, not derived.** The first version kept only names
        and worked out where each one sat from its position in the list, which is
        correct exactly when every register in a window is the same width. This
        contract mixes float32 pairs with single-word integers, so the fifth
        float in a window sits at offset 8, not offset 4 — and the client read
        the wrong register and reported a confident 7.4e14. Nothing errored.

        The address translation is the server's, not a second copy of it:
        ``ModbusTcpServer.wire_offset`` composes three shifts (model index, the
        block lead-in, and pymodbus's own ``PDU = index - 1``), and a client that
        reimplemented that arithmetic would be a second, drifting, copy of the
        one thing in this project that is hardest to get right.
        """
        by_pdu: dict[int, list[tuple[str, int]]] = {}
        for reg in self.c.registers:
            pdu = ModbusTcpServer.wire_offset(reg.address)
            by_pdu.setdefault(pdu, []).append((reg.name, reg.width))

        windows: list[tuple[int, int, list[tuple[str, int]]]] = []
        for pdu in sorted(by_pdu):
            entries = by_pdu[pdu]
            span = max(width for _, width in entries)
            if (
                windows
                and windows[-1][0] + windows[-1][1] == pdu
                and windows[-1][1] + span <= MAX_READ
            ):
                start, count, acc = windows[-1]
                offset = pdu - start
                windows[-1] = (
                    start, count + span,
                    acc + [(n, offset) for n, _ in entries],
                )
            else:
                windows.append((pdu, span, [(n, 0) for n, _ in entries]))
        return windows

    @property
    def request_count(self) -> int:
        """Round trips a full poll takes. For metrics, and for tests."""
        return len(self._plan)

    @property
    def planned_registers(self) -> int:
        return sum(count for _, count, _ in self._plan)

    def describe_plan(self) -> list[str]:
        """Human-readable plan, for the startup log."""
        return [
            f"PDU {start}+{count} ({len(entries)}: "
            f"{', '.join(f'{n}@{off}' for n, off in entries)})"
            for start, count, entries in self._plan
        ]

    # ─── connection ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        self.connected = bool(self._client.connect())
        if not self.connected:
            log.warning("modbus connect failed (%s)", self._client)
        return self.connected

    def close(self) -> None:
        self._client.close()
        self.connected = False

    def __enter__(self) -> ModbusReader:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ─── polling ──────────────────────────────────────────────────────────────

    def _decode(self, name: str, raw: list[int]) -> float | None:
        """Decode one register's words, or ``None`` if it cannot be read.

        ``None`` rather than zero. Zero is a legitimate flow rate, a legitimate
        DO and a legitimate alarm state, so substituting it for "could not read"
        turns a communication fault into a process event — and the plant looks
        like it shut down rather than like the cable coming loose.
        """
        reg = self.c.register(name)
        if len(raw) < reg.width:
            return None
        if reg.type == UDT_FLOAT32:
            return decode_float32(raw[0], raw[1], reg.word_order)
        if reg.type == "uint16":
            return float(raw[0])
        if reg.type == "int16":
            return float(struct.unpack(">h", struct.pack(">H", raw[0] & 0xFFFF))[0])
        return None

    def poll(self) -> ModbusPollResult:
        """Read every planned register once.

        Anything that could not be read is marked ``QUALITY_BAD`` and left out of
        ``values`` entirely. Not defaulted to zero: zero is a real flow rate, and
        a poll that cannot reach a register must not look like a plant that has
        stopped flowing.
        """
        started = time.perf_counter()
        values: dict[str, float] = {}
        quality: dict[str, int] = {}
        registers = 0
        failed = 0

        for start, count, entries in self._plan:
            try:
                rr = self._client.read_holding_registers(
                    start, count=count, device_id=self.unit_id
                )
            except Exception as exc:  # any transport failure is a bad window
                log.warning("modbus read PDU %d+%d failed: %s", start, count, exc)
                rr = None
            if rr is None or rr.isError():
                failed += 1
                for name, _ in entries:
                    quality[name] = QUALITY_BAD
                continue

            registers += count
            for name, offset in entries:
                reg = self.c.register(name)
                decoded = self._decode(
                    name, list(rr.registers[offset: offset + reg.width])
                )
                if decoded is None:
                    quality[name] = QUALITY_BAD
                    continue
                values[name] = decoded
                quality.setdefault(name, QUALITY_GOOD)

        elapsed = (time.perf_counter() - started) * 1000.0
        if failed:
            self._consecutive_failures += 1
            if self._consecutive_failures >= FAILURES_BEFORE_DOWN:
                raise ModbusLinkDownError(
                    f"{self._consecutive_failures} consecutive polls had failed windows"
                )
        else:
            self._consecutive_failures = 0

        return ModbusPollResult(
            values=values, quality=quality, register_count=registers,
            elapsed_ms=elapsed, failed_windows=failed,
        )
