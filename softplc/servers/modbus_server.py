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
from pymodbus.server import StartTcpServer

from softplc.contract import Contract, contract as get_contract
from softplc.servers.modbus import (
    COIL_SIZE,
    REGISTER_TO_SIGNAL,
    HOLDING_BASE,
    HOLDING_SIZE,
    RegisterModel,
)

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
        self.pending_writes: dict[str, float] = {}

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

    # ─── publishing ──────────────────────────────────────────────────────────

    def publish(self, values: dict[str, float | None], states: dict[str, int],
                heartbeat: int) -> None:
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
        slave = ModbusSlaveContext(
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

    def __repr__(self) -> str:  # pragma: no cover - display only
        return f"<ModbusTcpServer {self.host}:{self.port} device={self.device_id}>"





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
