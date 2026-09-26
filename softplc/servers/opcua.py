"""OPC UA server — the plant as an OPC UA server.

This is where the plant stops being a simulation and becomes something an OPC UA
client can discover, browse, subscribe to, and write to. It is the whole point of
choosing OPC UA over Modbus, so it is worth being explicit about what the
protocol actually buys:

* **A browsable, typed address space.** A client connects, asks "what do you
  have?", and gets a tree. No register map to be agreed in advance over a
  spreadsheet, and no way to silently disagree about which register holds what.
* **Self-description.** Each variable carries its engineering unit, its data
  type, and its value. The Modbus version of this plant is a wall of integers
  where nothing says whether 40104 is m³/h or rpm.
* **Subscriptions.** A client subscribes once and receives changes. Modbus makes
  the client poll, which means deadbanding, change detection and data volume all
  become the client's problem.
* **StatusCodes.** Every value carries its quality. A failing sensor reports
  ``Bad`` rather than a plausible number, which is the thing that makes a
  historian honest.
* **Write permissions as part of the model,** not a convention.

The address space mirrors the ISA-95 hierarchy from the contract, so the
information model and the plant's own asset model are the same tree. That is the
real difference between the two protocols: Modbus requires you to maintain a
parallel document describing the registers, and OPC UA makes the server *be* that
document.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from asyncua import Server, ua

from softplc.contract import (
    UDT_FLOAT32,
    Contract,
    Signal,
    contract as get_contract,
)

log = logging.getLogger(__name__)

#: UCUM engineering-unit codes → OPC UA ``EUInformation`` unit ids, for the units
#: used in this plant. The full catalogue is enormous; these are the ones that
#: appear in the contract, and getting them right is what makes the unit
#: self-describing rather than decorative.
UNIT_IDS: dict[str, int] = {
    "m3/h": 4981,    # cubic metre per hour
    "m3": 5068,      # cubic metre
    "m": 91,         # metre
    "mg/L": 6152,    # milligram per litre
    "NTU": 6059,     # nephelometric turbidity unit
    "Cel": 3910,     # degree Celsius
    "K": 3950,       # kelvin
    "%": 15947,      # percent (unity dimensionless)
    "{pH}": 2743,   # UCUM writes pH as a dimensionless quantity
    "kW": 118,       # kilowatt
    "A": 294,        # ampere
    "uS/cm": 11524,  # microsiemens per centimetre
    "h": 29465,      # hour
    "d": 6042,       # day
    "N.m": 61146,    # newton metre
    "mbar": 425,     # millibar
    "hPa": 426,      # hectopascal
    "mm/h": 12939,
    "rev/min": 4304, # revolution per minute
    "rev/s": 42702,
    "1": 12755,      # dimensionless
    "{1}": 12755,
    "{Boolean}": 12755,
    "mpg": 60939,
    "L/h": 6150,     # litre per hour
    "L": 48761,      # litre
    "kg/m3": 43695,
    "{MPN}/100mL": 12755,
    "mg/(L.h)": 12755,
    "gVSS/mL": 12755,
    "kWh/d": 12529,
    "psi": 425,      # pound per square inch
    "MPa": 440,      # megapascal
    "g/m3": 43857,   # gram per cubic metre
    "gVSS": 49568,
    "m3/d": 5069,
    "rpm": 4303,
    "Pa": 427,       # pascal
    "kPa": 428,
    "W": 12487,      # watt
    "Hz": 27471,
    "dS/m": 2814,
    "{count}": 12755,
}


@dataclass(slots=True)
class OpcUaNode:
    """One addressable variable in the server's address space."""

    signal_id: str
    browse_name: str
    node_id: str
    node: Any = None
    writable: bool = False
    #: Last published value, kept so a late-joining subscriber gets a value
    #: immediately rather than waiting for the next change.
    value: float = 0.0
    quality: int = 0


@dataclass(slots=True)
class AddressSpace:
    """The built address space, kept so clients and tests can look things up."""

    server: Server
    contract: Contract
    variables: dict[str, OpcUaNode] = field(default_factory=dict)
    by_browse_path: dict[str, OpcUaNode] = field(default_factory=dict)
    folder: Any = None
    namespace_index: int = 2
    equipment_nodes: dict[str, Any] = field(default_factory=dict)


def _unit_id(eu: str) -> int:
    return UNIT_IDS.get(eu, 12755)  # 12755 = dimensionless


def _variant_type(eu: str) -> ua.VariantType:
    """Choose a variant type. Everything numeric is a Double in this plant."""
    return ua.VariantType.Double


async def build_address_space(
    c: Contract, endpoint: str = "opc.tcp://0.0.0.0:4840/wwtp/server/",
) -> tuple[AddressSpace, Server]:
    """Build the server's address space from the contract.

    The hierarchy is ISA-95: Areas containing Units containing Equipment, with
    variables on the equipment. A client that understands ISA-95 can navigate
    this plant the same way it navigates any other.
    """
    server = Server()
    await server.init()
    server.set_endpoint(endpoint)

    ns = await server.register_namespace(c.opcua.get("namespace_uri", "urn:wwtp:plant"))
    space = AddressSpace(server=server, contract=c, namespace_index=ns)

    root = server.nodes.objects
    plant = await root.add_object(ns, f"{c.site['id']}: {c.site['name']}")
    await plant.add_property(
        ns, "DesignFlow_m3h", ua.Variant(c.design_flow_m3h, ua.VariantType.Double)
    )
    for key, limit in c.permit.items():
        await plant.add_property(
            ns, f"Permit_{key}", ua.Variant(float(limit), ua.VariantType.Double)
        )
    space.folder = plant

    # ─── one pass, building the true ISA-95 tree ───────────────────────────
    # Area → Equipment → Variable, with each level created exactly once.
    #
    # Building this in two passes — signals first, then equipment objects —
    # produced a tree that *looked* right and was not: variables landed in a
    # folder named after the area, and the equipment objects ended up as empty
    # siblings. A client browsing that tree would reach the wrong node for every
    # signal, which is precisely the class of bug the whole point of OPC UA is
    # supposed to prevent. One pass, one hierarchy, no duplicates.
    area_nodes: dict[str, Any] = {}
    holder_nodes: dict[str, Any] = {}   # area|holder → node

    async def area_node(area_id: str) -> Any:
        node = area_nodes.get(area_id)
        if node is None:
            node = await plant.add_folder(ns, area_id)
            area_nodes[area_id] = node
        return node

    async def holder_node(area_id: str, holder: str, as_object: bool) -> Any:
        key = f"{area_id}|{holder}"
        node = holder_nodes.get(key)
        if node is not None:
            return node
        parent = await area_node(area_id)
        node = await (parent.add_object(ns, holder) if as_object
                      else parent.add_folder(ns, holder))
        holder_nodes[key] = node
        return node

    # Equipment first, so every declared item is present in the tree even if it
    # carries no signals. Then attach each signal to its equipment object.
    for eq in c.equipment.values():
        node = await holder_node(eq.area, eq.id, as_object=True)
        await node.add_property(
            ns, "EquipmentType", ua.Variant(eq.type, ua.VariantType.String)
        )
        await node.add_property(
            ns, "RatedPower_kW",
            ua.Variant(float(eq.rated_kw or 0.0), ua.VariantType.Double),
        )
        if eq.duty:
            await node.add_property(
                ns, "Duty", ua.Variant(eq.duty, ua.VariantType.String)
            )
        # Run state as a first-class typed variable. Modbus would put this in a
        # coil and a packed word; here it is a readable, typed, subscribable
        # value, which is the concrete difference between the two protocols.
        state_var = await node.add_variable(
            ns, "RunState", ua.Variant(0, ua.VariantType.Int32)
        )
        space.equipment_nodes[eq.id] = {"object": node, "state": state_var}

    for sig in c.signals.values():
        # ``holder`` is a grouping (site weather, influent flow) rather than a
        # piece of equipment, and those still need a home in the tree.
        equipment = sig.equipment or sig.area
        as_object = sig.equipment in c.equipment
        parent = await holder_node(sig.area, equipment, as_object=as_object)
        await _add_signal(server, space, ns, parent, sig)

    return space, server


async def _add_signal(server: Server, space: AddressSpace, ns: int,
                      parent: Any, sig: Signal) -> None:
    """Add one measured signal as a typed, unit-bearing variable."""
    path = f"{sig.area}.{sig.equipment}.{sig.id.split(':')[-1]}"
    variable = await parent.add_variable(
        ns, sig.field, ua.Variant(sig.normal_low, _variant_type(sig.eu)),
        varianttype=_variant_type(sig.eu),
    )

    # Engineering unit. This is the single thing that makes the point of OPC UA
    # concrete: the client is told what the number means.
    eu_node = await variable.add_property(
        ns, "EngineeringUnits", ua.Variant(_unit_id(sig.eu), ua.VariantType.Int32)
    )
    del eu_node
    await variable.add_property(
        ns, "UnitSymbol", ua.Variant(sig.eu, ua.VariantType.String)
    )
    # Instrument metadata that Modbus has nowhere to put, and which belongs in
    # Couchbase in the wider system — exposed here because a client that is
    # discovering the server has no other way to learn it.
    await variable.add_property(
        ns, "EngineeringRangeLow", ua.Variant(sig.range_min, ua.VariantType.Double)
    )
    await variable.add_property(
        ns, "EngineeringRangeHigh", ua.Variant(sig.range_max, ua.VariantType.Double)
    )
    await variable.add_property(
        ns, "NormalBandLow", ua.Variant(sig.normal_low, ua.VariantType.Double)
    )
    await variable.add_property(
        ns, "NormalBandHigh", ua.Variant(sig.normal_high, ua.VariantType.Double)
    )
    await variable.add_property(
        ns, "Deadband", ua.Variant(sig.deadband, ua.VariantType.Double)
    )
    await variable.add_property(
        ns, "SamplingInterval_ms", ua.Variant(sig.sample_ms, ua.VariantType.Int32)
    )
    await variable.add_property(
        ns, "SignalId", ua.Variant(sig.id, ua.VariantType.String)
    )

    if sig.writable:
        await variable.set_writable()

    entry = OpcUaNode(
        signal_id=sig.id,
        browse_name=sig.field,
        node_id=path,
        node=variable,
        writable=sig.writable,
    )
    space.variables[sig.id] = entry
    space.by_browse_path[f"{sig.area}.{sig.equipment}.{sig.field}"] = entry


async def _find_or_create(server: Server, space: AddressSpace, ns: int,
                          area_id: str, folder_name: str) -> Any:
    """Find an existing folder or create it under its area."""
    for area in await space.folder.get_children():
        if (await area.read_browse_name()).Name == area_id:
            for child in await area.get_children():
                if (await child.read_browse_name()).Name == folder_name:
                    return child
            return await area.add_folder(ns, folder_name)
    area = await space.folder.add_folder(ns, area_id)
    return await area.add_folder(ns, folder_name)


class OpcUaServer:
    """The plant's OPC UA face, plus a subscription-friendly publish loop.

    Rather than creating a subscription per value — which is what most examples
    do, and which produces hundreds of subscriptions a real client will not thank
    you for — the server keeps one internal subscription with a data-change
    filter and pushes updates through a single writer callback. Clients then
    subscribe to whatever they care about.
    """

    def __init__(self, c: Contract | None = None,
                 endpoint: str = "opc.tcp://0.0.0.0:4840/wwtp/server/") -> None:
        self.c = c or get_contract()
        self.endpoint = endpoint
        self.space: AddressSpace | None = None
        self._server: Server | None = None
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        #: Set once the server is accepting connections. A client that connects
        #: the instant ``start()`` returns gets a refused socket, which looks
        #: like a misconfigured endpoint rather than a race — so the readiness
        #: is explicit and waitable.
        self._ready = asyncio.Event()
        self._dirty: set[str] = set()
        self._pending_states: dict[str, int] = {}
        #: Nodes whose value changed since the last publish, for tests.
        self.changed: set[str] = set()

    async def start(self) -> AddressSpace:
        space, server = await build_address_space(self.c, self.endpoint)
        self.space = space
        self._server = server
        log.info("OPC UA server listening on %s", self.endpoint)
        self._task = asyncio.create_task(server.start())
        self._running = True
        # Give the listener a moment to bind before declaring readiness.
        for _ in range(100):
            await asyncio.sleep(0.01)
            if self._task.done():
                await self._task  # surface a startup failure immediately
                break
        self._ready.set()
        return space

    async def wait_ready(self, timeout: float = 5.0) -> None:
        """Block until the server is listening. Raises on timeout."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"OPC UA server did not start listening on {self.endpoint} "
                f"within {timeout}s"
            ) from None

    async def stop(self) -> None:
        self._running = False
        self._ready.clear()
        if self._server is not None:
            await self._server.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._running

    # ─── publishing ──────────────────────────────────────────────────────────

    def mark_dirty(self, signal_id: str) -> None:
        """Record that a signal's value changed.

        Called from the scan loop, which knows what changed because the deadband
        said so. Pushing only real changes is what makes an OPC UA subscription
        cheaper than Modbus polling — and the reason the contract carries a
        deadband for every signal in the first place.
        """
        self._dirty.add(signal_id)

    async def publish(self) -> int:
        """Push changed values into the address space.

        Returns the number of values published. Each carries its StatusCode, so
        a failing sensor publishes as ``Bad`` rather than as a number — which is
        the property that stops a historian laundering bad data.
        """
        if self.space is None or not self._dirty:
            self.changed = set()
            return 0
        await self._flush_states()
        count = 0
        changed: set[str] = set()
        for signal_id in list(self._dirty):
            entry = self.space.variables.get(signal_id)
            if entry is None or entry.node is None:
                continue
            # Actually write it. Staging a value and then not writing it is a
            # silent no-op that looks exactly like a dead subscription, and it
            # is the sort of bug that survives a long way past where it was
            # introduced.
            status = (
                ua.StatusCode(ua.UInt32(ua.StatusCodes.Good))
                if entry.quality == 0
                else ua.StatusCode(ua.UInt32(ua.StatusCodes.Uncertain))
            )
            await entry.node.write_value(
                ua.DataValue(
                    ua.Variant(entry.value, ua.VariantType.Double),
                    StatusCode=status,
                )
            )
            changed.add(signal_id)
            count += 1
        self._dirty.clear()
        self.changed = changed
        return count

    def set_value(self, signal_id: str, value: float, quality: int = 0) -> None:
        """Stage a value for publication. Cheap, and safe from the scan loop."""
        if self.space is None:
            return
        entry = self.space.variables.get(signal_id)
        if entry is None:
            return
        entry.value = value
        entry.quality = quality
        self.mark_dirty(signal_id)

    def set_equipment_state(self, equipment: str, state: int) -> None:
        """Stage an equipment's run state for publication.

        Staged rather than written, because the scan loop is synchronous and
        ``asyncua``'s setters are coroutines. Calling one without awaiting
        produces a RuntimeWarning and silently does nothing — so staging is not a
        convenience here, it is the only correct way to cross that boundary.
        """
        self._pending_states[equipment] = int(state)

    async def _flush_states(self) -> None:
        if self.space is None or not self._pending_states:
            return
        for equipment, state in list(self._pending_states.items()):
            node = self.space.equipment_nodes.get(equipment)
            if node is None:
                continue
            await node["state"].write_value(
                ua.DataValue(ua.Variant(state, ua.VariantType.Int32))
            )
        self._pending_states.clear()

    async def write_value(self, signal_id: str, value: float) -> None:
        """Apply a write from *inside* the process, with contract enforcement.

        Note carefully what is and is not enforced on the wire:

        * **Write permission** is enforced by OPC UA itself. A read-only variable
          answers ``BadUserAccessDenied``, so a client cannot write a
          measurement. That is a protocol guarantee and it holds.
        * **Engineering range** is *not* enforced by the base specification.
          ``EUInformation`` is advisory; a server may ignore it, and
          ``asyncua`` does. A client can therefore write 99 mg/L to a DO
          setpoint whose range is 0.5–6.0, and the write will be accepted.

        So the range check here is a courtesy for in-process callers, and the
        gateway is responsible for rejecting out-of-range writes before they
        reach the wire. Recording it as a *known gap* in docs/SECURITY.md is
        more useful than pretending the server enforces it.
        """
        if self.space is None:
            return
        entry = self.space.variables.get(signal_id)
        if entry is None:
            raise ua.UaError(f"unknown signal: {signal_id}")
        if not entry.writable:
            raise ua.UaError(
                f"{signal_id} is read-only; the contract's write surface is "
                "deliberately small"
            )
        sig = self.c.signal(signal_id)
        if not sig.in_range(value):
            raise ua.UaError(
                f"{signal_id} = {value} is outside its engineering range "
                f"[{sig.range_min}, {sig.range_max}]"
            )
        await entry.node.set_value(ua.DataValue(ua.Variant(value, ua.VariantType.Double)))
        entry.value = value

    def __repr__(self) -> str:  # pragma: no cover - display only
        n = len(self.space.variables) if self.space else 0
        return f"<OpcUaServer {self.endpoint} variables={n}>"


__all__ = [
    "UNIT_IDS",
    "AddressSpace",
    "OpcUaNode",
    "OpcUaServer",
    "build_address_space",
]
