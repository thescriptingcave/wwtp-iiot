"""Read the plant over OPC UA.

The mirror image of ``modbus_client.py``, and the contrast is the lesson.

| | Modbus TCP | OPC UA |
|---|---|---|
| Discovery | none | browsable address space |
| Identity | register address | typed node with a browse path |
| Validity | none | StatusCode on every value |
| Change delivery | you poll | subscriptions with deadband filters |
| Range enforcement | none | advisory only (see docs/SECURITY.md) |

So this client can *find* things rather than being told where they are, and it
gets validity for free. The thing it has to work hardest at is the same thing
Modbus does: knowing which of several nodes it actually wants, so it does not
read 400 nodes to store 57 signals.

## StatusCodes are the point

A value arrives with a StatusCode. ``Good`` means the value is valid.
``Uncertain`` means the server is telling you it does not fully believe it —
which for a fouled probe is exactly the truth. ``Bad`` means do not use it.

The temptation is to read the number and ignore the code, because the code is
noise and the number is what you want on the chart. That is how a fouled DO
probe becomes a week of confident, wrong, flat-lined data. So this reader returns
quality alongside every value, from the same place, and there is no code path
that produces a value without one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from asyncua import Client, ua
from softplc.contract import QUALITY_BAD, QUALITY_GOOD, QUALITY_UNCERTAIN, Contract

log = logging.getLogger("gateway.opcua")


def quality_from_status(status: ua.StatusCode | None) -> int:
    """Map an OPC UA StatusCode onto this project's quality scale.

    Lossy in one direction only: ``Good`` and ``Uncertain`` map cleanly, and
    **everything else** maps to ``Bad`` — including a ``None`` status, and
    including any code this function has never heard of. A status it does not
    recognise is not a status to be optimistic about.

    The subtlety is that ``Bad`` here means "a specific well-known bad code",
    while the fallback means "not good and not uncertain", which covers the whole
    upper half of the 32-bit code space. Conflating them would be fine, and
    deliberately is: the two cases want the same response, which is to not use
    the number.
    """
    if status is None:
        return QUALITY_BAD
    if status.name == "Good":
        return QUALITY_GOOD
    if status.name == "Uncertain":
        return QUALITY_UNCERTAIN
    return QUALITY_BAD


@dataclass(slots=True)
class OpcUaPollResult:
    values: dict[str, float] = field(default_factory=dict)
    quality: dict[str, int] = field(default_factory=dict)
    read: int = 0
    failed: int = 0
    elapsed_ms: float = 0.0


class OpcUaReader:
    """Contract-driven OPC UA reader.

    Resolves each contract signal to a node once, at connect time, and reads them
    in one batched request per poll. Re-resolving by browse path on every poll
    would be the intuitive implementation and it is the wrong one: a browse is a
    multi-request round trip, and 57 of them per second is a self-inflicted
    denial of service against your own server.
    """

    def __init__(self, contract: Contract, endpoint: str | None = None,
                 timeout: float = 5.0) -> None:
        self.c = contract
        self.endpoint = endpoint or contract.modbus.get(
            "opcua_endpoint", "opc.tcp://127.0.0.1:4840/wwtp/server/"
        )
        self.timeout = timeout
        self._client: Client | None = None
        self._nodes: dict[str, Any] = {}
        self.connected = False

    # ─── connection ───────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        self._client = Client(url=self.endpoint, timeout=self.timeout)
        try:
            await self._client.connect()
        except Exception as exc:  # surfaced as False, not a crash
            log.warning("opcua connect to %s failed: %s", self.endpoint, exc)
            self._client = None
            return False
        self.connected = True
        await self._resolve()
        return True

    async def close(self) -> None:
        if self._client is not None:
            # Closing must not raise: a disconnect failure during shutdown would
            # mask whatever error is actually being handled.
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._client = None
        self.connected = False

    async def __aenter__(self) -> OpcUaReader:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ─── resolution ──────────────────────────────────────────────────────────

    async def _resolve(self) -> None:
        """Walk the address space once and bind every contract signal to a node.

        Resolved by *browse path* rather than by a generated NodeId string. The
        contract's NodeId scheme is deterministic, but hard-coding
        ``ns=2;s=AERATION.AHU-1.DO`` in a client is a second copy of the mapping
        that lives in the server — and the one place where a mistake produces
        numbers rather than an error.
        """
        assert self._client is not None  # connect() guarantees it
        # The plant object is *found*, not addressed.
        #
        # Its browse name is "<site id>: <site name>" — "PLANT-A: Northgate Water
        # Reclamation Facility" — so a client that hardcodes either half holds a
        # second copy of a fact the contract already owns, and renaming the site
        # breaks it silently. The site id comes from the contract; the text after
        # the colon is free to change. Guessing "PLANT-A" and getting
        # ``BadNoMatch`` on every lookup is the version that does not work.
        site_id = str(self.c.site.get("id", ""))
        root = None
        for child in await self._client.nodes.objects.get_children():
            name = (await child.read_browse_name()).Name
            if name == site_id or name.startswith(f"{site_id}:"):
                root = child
                break
        if root is None:
            log.error("no plant object for site %r under Objects", site_id)
            return
        for sig in self.c.signals.values():
            parts = [f"2:{sig.area}"]
            # The holder, not the asset: INFLUENT:FLOW:FLOW lives under a folder
            # called FLOW, and `sig.equipment` is None for grouping signals.
            parts.append(f"2:{sig.holder}")
            # The node's browse name is the signal's ``field`` verbatim, which is
            # what softplc/servers/opcua.py creates. Lower-casing it here looked
            # right and resolved nothing.
            parts.append(f"2:{sig.field}")
            try:
                self._nodes[sig.id] = await root.get_child(parts)
            except Exception as exc:  # a missing node is a real gap, not a crash
                log.warning("opcua node not found for %s: %s", sig.id, exc)
        log.info("opcua resolved %d of %d signals",
                 len(self._nodes), len(self.c.signals))

    @property
    def resolved_count(self) -> int:
        return len(self._nodes)

    def unresolved(self) -> list[str]:
        return [s for s in self.c.signals if s not in self._nodes]

    # ─── polling ──────────────────────────────────────────────────────────────

    async def poll(self) -> OpcUaPollResult:
        """Read every resolved node once, with its StatusCode."""
        result = OpcUaPollResult()
        if self._client is None or not self._nodes:
            return result
        started = time.perf_counter()
        params = ua.ReadParameters()
        for node in self._nodes.values():
            params.NodesToRead.append(
                ua.ReadValueId(
                    NodeId=node.nodeid,
                    AttributeId=ua.AttributeIds.Value,
                )
            )
        try:
            response = await self._client.uaclient.read(params)
        except Exception as exc:  # one bad batch is a bad poll, not a crash
            log.warning("opcua read failed: %s", exc)
            result.failed = len(self._nodes)
            result.elapsed_ms = (time.perf_counter() - started) * 1000.0
            return result

        for (signal_id, _), rv in zip(self._nodes.items(), response, strict=True):
            status = rv.StatusCode
            quality = quality_from_status(status)
            result.quality[signal_id] = quality
            if quality == QUALITY_BAD:
                result.failed += 1
                continue
            if rv.Value is None:
                # Good status with no value. Theoretically impossible and
                # practically occasional; treat it as bad rather than crashing
                # the poll, because a historian that stops is worse than one
                # that records a gap.
                result.quality[signal_id] = QUALITY_BAD
                result.failed += 1
                continue
            result.values[signal_id] = float(rv.Value.Value)
            result.read += 1
        result.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return result

    async def poll_for(self, seconds: float,
                       interval: float = 1.0) -> list[OpcUaPollResult]:
        """Poll repeatedly. Used by tests and by the diagnostic mode."""
        out = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            out.append(await self.poll())
            await asyncio.sleep(interval)
        return out
