"""Modbus TCP wire adapter.

The register *model* lives in :mod:`softplc.servers.modbus` and has no pymodbus
dependency. This module is the thin layer that connects it to a socket.

Why the split
-------------
``pymodbus`` 3.15 rearranged its datastore API twice during this project's
development: ``ModbusSlaveContext`` became ``ModbusDeviceContext``, the
sequential block lost its ``getValues``/``setValues`` accessors, and
``ModbusServerContext.async_getValues`` ended up serving only
``ModbusSimulatorContext``. Every one of those changes would have forced
rewrites across the register map, the address arithmetic and the float32 codec —
all of which are pure arithmetic and deserve to be testable without a protocol
library in the way.

So the version is **pinned** to 3.6.x, which has the stable API, and the pin is
recorded in ``pyproject.toml``. If the library ever has to move, the blast
radius is this file.

Address translation
-------------------
Two conventions collide and this is where they meet. The register model indexes
its block by the contract's 4xxxx number minus 40000, so contract register
40000 is offset 0. ``pymodbus`` addresses a block starting at the address passed
to ``ModbusSequentialDataBlock``, and the standard block helper shifts by one
internally. The translation lives in :meth:`_wire_offset` so there is one place
to get it wrong, and it is covered by a test that reads a value back through a
real client.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Any

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.pdu import ExceptionResponse
from pymodbus.server import StartTcpServer

from softplc.contract import Contract, contract as get_contract
from softplc.servers.modbus import (
    COIL_SIZE,
    REGISTER_TO_SIGNAL,
    HOLDING_BASE,
    HOLDING_SIZE,
    RegisterModel,
    decode_float32,
)

#: Modbus exception codes. Spelled out rather than imported from pymodbus's own
#: constants because these are protocol values a client will read, and the number
#: in the log has to match the number on the wire.
#: `pymodbus.ModbusException` carries the code, but these are the two this file
#: raises and naming them here keeps `_accept_write` readable.
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03

log = logging.getLogger(__name__)

#: Size of the block handed to pymodbus. One larger than the model's, because
#: pymodbus shifts its base address by one internally and a client addressing
#: the last model register would otherwise read off the end.
BLOCK_SIZE = HOLDING_SIZE + 16
COIL_BLOCK_SIZE = COIL_SIZE + 16


class ModbusTcpServer:
    """Serves :class:`RegisterModel` over Modbus TCP.

    The datastore is a *live view* onto the model, not a copy. Copying it per
    scan would double the write path and leave a window in which the two
    disagreed; writing straight through means a client always sees the value the
    last scan published, which is the only definition of correct that matters
    when a client is watching a plant.
    """

    def __init__(self, c: Contract | None = None, host: str = "0.0.0.0",
                 port: int = 5020) -> None:
        self.c = c or get_contract()
        self.model = RegisterModel(c=self.c)
        self.host = host
        self.port = port
        # Base address 0. At a non-zero base this pymodbus version returns an
        # empty list from the block's own ``getValues`` rather than an error, so
        # a non-zero base silently reads as all zeroes. Every verification here
        # therefore goes through a real client, never through ``getValues``.
        self._holding = ModbusSequentialDataBlock(0x00, [0] * BLOCK_SIZE)
        self._input = ModbusSequentialDataBlock(0x00, [0] * BLOCK_SIZE)
        self._coil = ModbusSequentialDataBlock(0x00, [0] * COIL_BLOCK_SIZE)
        self._context: ModbusServerContext | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        #: Client writes land here and are applied to the plant by the scan loop.
        #:
        #: **This dict was declared and never read.** From the moment the server
        #: started to the commit that added the capture path, it stayed empty: the
        #: comment above described an intent, not a mechanism. Client writes went
        #: straight into the pymodbus block, ``_flush()`` overwrote them on the next
        #: 20 ms scan, and the only symptom was an operator's setpoint that read
        #: back as the old value.
        self.pending_writes: dict[str, float] = {}
        #: Monotonic count of accepted client writes, for the health endpoint.
        self.accepted_writes = 0
        #: Writes refused, by reason, for the same endpoint.
        self.refused_writes: dict[str, int] = {}

    # ─── address translation ─────────────────────────────────────────────────

    #: The model is written into the block one slot above index 0. Modbus
    #: reserves register 0 and pymodbus resolves ``PDU = block index - 1``, so
    #: without this the contract's first register — the heartbeat, at model index
    #: 0 — would sit at PDU -1, which is not a legal address and would be
    #: unreachable by any client.
    BLOCK_LEAD_IN = 1

    @classmethod
    def wire_offset(cls, contract_address: int) -> int:
        """Contract 4xxxx address → the PDU address a client must request.

        Three quantities compose, and getting any of them wrong returns
        plausible values from the neighbouring register rather than an error:

        1. The model's own index is ``address - 40000``.
        2. The model is written into the block with a one-slot lead-in.
        3. pymodbus resolves ``PDU = block index - 1``.

        Net: ``PDU = address - 40000``, which is the same as the model index —
        convenient, but arrived at through three shifts rather than one, and any
        of them changed independently would break the whole map.

        Established by probing a live server and locating returned values in the
        block, not by reading the source. Under base 0, the float written at model
        index 100 was returned for PDU 99, and another at model index 109 for PDU
        108 — consistent, and consistent with the lead-in.
        """
        return contract_address - HOLDING_BASE

    # ─── the write-back path ─────────────────────────────────────────────────

    def take_writes(self) -> dict[str, float]:
        """Hand the scan loop every write accepted since the last call, and clear.

        Returns and clears in one step, because a write must be applied **once**.
        Leaving it queued would re-apply it every scan, and the setpoint would
        never leave the value an operator chose — a controller whose input is
        re-driven every 20 ms is not holding a setpoint, it is being held.

        The caller — `softplc.main.SoftPlc._step` — applies these *before* the
        plant steps, so the applied value is what gets published on this same
        scan. Applying afterwards would publish the old value once more, and
        `_flush` would overwrite the block with it.
        """
        if not self.pending_writes:
            return {}
        taken, self.pending_writes = self.pending_writes, {}
        return taken

    def _accept_write(self, pdu_address: int, values: list[int]) -> None:
        """Decode a client's register write and queue it, or refuse it.

        **Where a write is validated, and why here.** This runs on the pymodbus
        listener thread, before the block is touched. Refusing here is what makes
        the refusal *visible*: the client gets a Modbus exception and an
        `isError()` response, so it knows the setpoint did not take. Validating
        after the write — in the scan loop, where the plant is reachable — would
        mean the block already holds the new value, and the next `_flush` would
        quietly restore the old one.

        So: the contract decides what may be written, the Modbus exception
        decides what the client is told, and the plant is never involved in
        refusing anything.

        Three quantities have to agree, and each one wrong yields a plausible
        wrong answer rather than an error:

        1. **PDU → contract address.** `wire_offset()` in reverse: the block is
           based at 0 with a one-slot lead-in, so the contract's `address` is
           `pdu + HOLDING_BASE`. Using the PDU address as a contract address
           looks up a register 40000 slots away — which does not exist, so the
           write would be refused as "unknown register" for a register that is
           right there.
        2. **Word order.** `AERATION_SETPOINT_DO` is `big`, but
           `AERATION_BLOWER_VALVE` is `little`, and the contract marks two
           registers "⚠ TRAP: low word first". The order is read per register from
           the contract, never assumed.
        3. **Width.** A float32 is two registers. A write of one register to a
           float32 address would leave the pair half old and half new, decoding
           to a plausible float that is neither. Refused, not merged.
        """
        reg = self._register_at_pdu(pdu_address)
        if reg is None:
            self._refuse(
                f"no register at PDU {pdu_address} "
                f"(contract {pdu_address + HOLDING_BASE})",
                ILLEGAL_DATA_ADDRESS,
            )
        if not reg.writable:
            # The address is legal and this register is the one the contract
            # declines to let a client change. Saying "no such address" would
            # point an engineer at a register-map bug that does not exist.
            self._refuse(f"{reg.name} is read-only in the contract")
        if len(values) != reg.width:
            self._refuse(
                f"{reg.name} is {reg.width} register(s) of {reg.type}; "
                f"got {len(values)}"
            )

        if reg.type == "float32":
            value = decode_float32(values[0], values[1], reg.word_order)
        else:
            value = float(values[0])

        # Range-checked here rather than in the scan loop, so the refusal reaches
        # the client. This is also the only range check on the Modbus path: the
        # base specification carries no engineering range, so without this the
        # contract's `range` would be a comment. Node-RED's permit check is a
        # *second* line of defence at the client, not this one — a client that is
        # not Node-RED still gets refused.
        signal_id = reg.signal
        if signal_id is not None and signal_id in self.c.signals:
            sig = self.c.signals[signal_id]
            if not sig.in_range(value):
                self._refuse(
                    f"{reg.name} = {value:g} is outside the engineering range "
                    f"[{sig.range_min:g}, {sig.range_max:g}]"
                )

        self.pending_writes[signal_id or reg.name] = value
        self.accepted_writes += 1
        log.info(
            "modbus write accepted: %s = %g -> %s",
            reg.name, value, signal_id or reg.name,
        )

    def _register_at_pdu(self, pdu_address: int) -> Any:
        """The contract register covering a PDU address, or None.

        **Linear scan over the register list, not a lookup table.** There are
        about forty registers and this runs once per client write — not per scan,
        not per register. A dict built once would be marginally faster and would
        also need rebuilding whenever the contract changes, and the thing this
        must get right is which register an address means, not how quickly it is
        found.

        The match is on *width*, not on the first register, because float32
        occupies two addresses: PDU 102 is the low half of the setpoint pair, and
        a lookup keyed on the register's base address alone would miss it.
        """
        for reg in self.c.registers:
            offset = self.wire_offset(reg.address)
            if offset <= pdu_address < offset + reg.width:
                return reg
        return None

    def _refuse(self, reason: str, code: int = ILLEGAL_DATA_VALUE) -> None:
        """Record the refusal and raise it as a Modbus exception for the client.

        Both halves are needed. The exception is what tells the client, and
        without it a refusal would look exactly like a successful write whose
        register did not change — which is the bug this whole path replaces. The
        counter and log line are what tell the *operator*, because the client
        that was refused is usually Node-RED and its console is not in front of
        them.
        """
        self.refused_writes[reason] = self.refused_writes.get(reason, 0) + 1
        log.warning("modbus write refused: %s", reason)
        raise _WriteRefusedError(reason, code)

    # ─── publishing ──────────────────────────────────────────────────────────

    def publish(self, values: dict[str, float | None], states: dict[str, int],
                heartbeat: int) -> None:
        """One scan's worth of plant state, into the block a client reads.

        Called every 20 ms. It is the reason a write has to be *applied* rather
        than merely stored: this overwrites the whole holding block, so anything
        a client wrote is gone one scan later unless something carried it into
        the plant first. That is what `take_writes()` exists for.
        """
        self.model.publish(values, states, heartbeat)
        self._flush()

    def _flush(self) -> None:
        """Push the model into the datastore.

        Written as one ``setValues`` per contiguous run rather than per register:
        a block write is one operation, and issuing ~40 individual writes per
        scan for a plant this size would be needlessly chatty on the wire.
        """
        self._holding.setValues(self.BLOCK_LEAD_IN, self.model.holding)
        # Coils get the same lead-in, for the same reason: coil 1 in the model
        # must not land on the reserved register 0.
        self._coil.setValues(
            self.BLOCK_LEAD_IN, [1 if v else 0 for v in self.model.coils]
        )
        # Input registers carry the indexed measurement table, so a client can
        # read a contiguous block without a per-tag request.
        self._input.setValues(self.BLOCK_LEAD_IN, self._input_words())

    def _input_words(self) -> list[int]:
        """Input registers as int16 words, in a stable contract-derived order.

        Scaled by 100 and documented rather than implicit. A client that needs
        full precision reads the holding registers, which are float32; input
        registers exist for cheap bulk scans where 2 decimal places on an
        engineering value is enough.
        """
        words = [0] * BLOCK_SIZE
        for i, sig in enumerate(sorted(self.c.signals.values(), key=lambda s: s.id)):
            idx = i + 1
            if idx >= BLOCK_SIZE:
                break
            v = self.model.values.get(_register_for_signal(sig.id))
            if v is None:
                continue
            words[idx] = int(max(-32768, min(32767, round(v * 100))))
        return words

    # ─── serving ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._flush()
        slave = _WriteAwareSlaveContext(
            server=self,
            di=self._input, co=self._coil, hr=self._holding, ir=self._input
        )
        self._context = ModbusServerContext(slaves=slave, single=True)
        # A daemon thread, deliberately. ``StartTcpServer`` blocks for the life
        # of the process and exposes no shutdown, so a non-daemon thread would
        # make the interpreter hang on exit — and an asyncio task cannot cancel
        # it either, because the work has already left the event loop. A daemon
        # is the honest mechanism: the listener lives until the process ends.
        self._thread = threading.Thread(
            target=StartTcpServer,
            kwargs={"context": self._context,
                    "address": (self.host, self.port)},
            daemon=True,
            name="modbus-tcp",
        )
        self._thread.start()
        await self._wait_until_listening()
        self._running = True
        log.info("Modbus TCP listening on %s:%d (device %d)",
                 self.host, self.port, self.device_id)

    async def _wait_until_listening(self, timeout: float = 5.0) -> None:
        """Block until the socket accepts, so a client does not race the bind.

        A client that connects the instant ``start()`` returns gets a refused
        connection, which looks like a misconfigured port rather than a race.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                fut = loop.run_in_executor(
                    None, lambda: socket.create_connection((self.host, self.port), 0.2)
                )
                conn = await fut
                conn.close()
                return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.05)
        raise TimeoutError(
            f"Modbus TCP did not start listening on {self.host}:{self.port} "
            f"within {timeout}s"
        )

    async def stop(self) -> None:
        """Mark the server down.

        The listening thread cannot actually be stopped — pymodbus offers no
        shutdown hook — so this only clears the running flag. The socket closes
        when the process exits. Stating that plainly is better than pretending a
        cancellation would work.
        """
        self._running = False
        self._context = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def device_id(self) -> int:
        return int(self.c.modbus.get("unit_id", 1))

    def write_stats(self) -> dict[str, Any]:
        """Accepted and refused write counts, for the health endpoint.

        An operator's evidence that the setpoint took is usually "the register
        changed", which cannot distinguish *the plant obeyed* from *the plant
        overwrote it*. These two counters can, and are the cheapest possible
        answer to "has anything ever written here".
        """
        return {
            "accepted": self.accepted_writes,
            "refused": dict(self.refused_writes),
            "pending": len(self.pending_writes),
        }

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"<ModbusTcpServer {self.host}:{self.port} device={self.device_id}>"


class _WriteRefusedError(Exception):
    """A client write the contract does not allow, with its Modbus exception code.

    Carries the code so the decision is made once, in `_accept_write`, where the
    contract is available. The alternative — returning a status and translating
    it in the context — would put "is this allowed?" and "what does the client
    hear?" in two places, and they would eventually disagree.

    * **2 Illegal Data Address** — the address is not a register at all.
    * **3 Illegal Data Value** — the address is real and this value must not be
      written: not writable per the contract, wrong width for the type, or out of
      engineering range. 3 rather than 2 because the address *is* legal; the
      client aimed at the right place and asked for something forbidden, and
      telling it "that address doesn't exist" would send an engineer looking for
      a register-map bug that is not there.
    """

    def __init__(self, reason: str, code: int = ILLEGAL_DATA_VALUE) -> None:
        super().__init__(reason)
        self.code = code


class _WriteAwareSlaveContext(ModbusSlaveContext):
    """A slave context that routes register writes through the server's policy.

    **Why a subclass, and why `setValues` specifically.** pymodbus has no write
    callback in 3.6, but every register write funnels through exactly one method
    on the slave context: `setValues`. Confirmed in the installed package rather
    than assumed —

    * `register_write_message.py:71`  FC6  write single register
    * `register_write_message.py:216` FC16 write multiple registers
    * `bit_write_message.py:93,229`   FC5/FC15 coils
    * `context.py:102`                where each of those lands

    Overriding `setValues` therefore sees every write on the wire, whatever
    function code it arrived under, without patching pymodbus.

    Two details that are easy to get wrong and produce silent corruption rather
    than an error:

    * **The address is the raw wire address.** An override sees its arguments
      before the parent's body runs, so the `address += 1` in
      `ModbusSlaveContext.setValues` has not been applied yet. That shift
      compensates for pymodbus resolving `PDU = block index - 1`, and applying
      it *here* would move a write onto the previous register — see the note at
      the unshift below.

    * **A refusal is returned, not raised.** Every request handler does
      `result = context.setValues(...); if isinstance(result,
      ExceptionResponse): return result`. Raising reaches the listener's generic
      handler and comes back as SlaveFailure (4) whatever the real cause.

    * **The parent's `setValues` returns `None`, not a status.** It cannot refuse
      anything, so a policy that could only return would be unable to say no.
    """

    def __init__(self, server: ModbusTcpServer, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._server = server

    def setValues(self, fc_as_hex: int, address: int,
                  values: list[int]) -> Any:
        # **The parameter name lies; the value is an int.** Despite being called
        # `fc_as_hex`, pymodbus passes `self.function_code` — 6 or 16 — and only
        # reaches the `"h"` form inside the parent's `decode()` lookup
        # (`register_write_message.py:216`, `:71`). Comparing against `"h"`
        # therefore matches nothing at all, and the write path silently never
        # runs while every "it must be refused" test still passes, because a
        # write that goes nowhere looks exactly like a refused one. It is fixed
        # by *using* the same `decode()` rather than re-deriving which function
        # codes mean "holding" — that mapping is pymodbus's table, not ours, and
        # duplicating it is how the two drift apart.
        # The wire address, unshifted.
        #
        # **An override sees the argument before the parent body runs**, so
        # `address` here is the PDU address exactly as the client sent it. The
        # `+= 1` in `ModbusSlaveContext.setValues` has not happened yet — it is
        # the first statement *after* this override returns. So no correction is
        # needed, and undoing one lands a write on the previous register: 102
        # becomes 101, and PDU 102 is the setpoint while PDU 100-101 is
        # AERATION_DO. The write is then refused as "read-only", which is a
        # truthful answer to the wrong question and an alarming one to debug —
        # it looks like the writable register is not writable.
        #
        # `zero_mode` describes the parent's shift, not the wire address, so it
        # has no bearing on this.
        pdu_address = address

        # `h` is the holding block. Coils (`c`) are equipment run state, which is
        # the PLC's to decide, so they pass straight through — the same reasoning
        # as the writable surface being one register rather than forty.
        if self.decode(fc_as_hex) == "h":
            try:
                self._server._accept_write(pdu_address, list(values))
            except _WriteRefusedError as refusal:
                # **Returned, not raised.** Every request handler does
                #     result = context.setValues(...); if isinstance(result,
                #     ExceptionResponse): return result
                # (`register_write_message.py:216-218`, `:71-73`), so a returned
                # exception PDU is the sanctioned channel and carries the exact
                # exception code. Raising instead reaches the listener's generic
                # handler and comes back as **SlaveFailure (4)** regardless of
                # what was wrong — so a client would be told "the server broke"
                # when the truth was "you may not write that register".
                #
                # Returning early is also what keeps a refused write out of the
                # block. Calling the parent anyway would leave the value sitting
                # in the datastore for the client to read back as though it had
                # taken, while the operator was told it had failed.
                return ExceptionResponse(fc_as_hex, refusal.code)
        return super().setValues(fc_as_hex, address, values)





__all__ = ["ModbusTcpServer"]


def _register_for_signal(signal_id: str) -> str:
    """Contract signal id → Modbus register name, for the input-register table.

    Only signals that appear in the register map have a Modbus presence at all;
    the other 40-odd signals are reachable over OPC UA only. That asymmetry is
    real and deliberate — a Modbus register map is a negotiated artefact, while
    an OPC UA address space is whatever the server chooses to expose.
    """
    for reg_name, sid in REGISTER_TO_SIGNAL.items():
        if sid == signal_id:
            return reg_name
    return signal_id
