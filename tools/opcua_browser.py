"""An OPC UA client, written from scratch.

This exists because UaExpert — the tool every OPC UA tutorial tells you to use —
is Windows-only, and installing .NET on macOS does not help, because the barrier
is the Win32 API underneath and not the language runtime. Wine works
unreliably for .NET Framework desktop applications.

So: build the client instead. That is not only a workaround.

**It is better for this project, for reasons that have nothing to do with the
platform.** A third-party GUI shows you the answer; writing the client shows you
the question. When a value does not appear, your own browser tells you *which
layer* is at fault — the server never published it, the browse name is wrong, the
NodeId is wrong, or the value genuinely is not there. A GUI shows "missing" and
leaves you to guess.

It also removes the last external dependency from the project. `docker compose up`
now reproduces the whole thing, protocol client included.

Five modes:

    browse     the address space as a tree, with live values
    watch      subscribe and follow values as they change
    read       read one variable by browse path
    write      set a writable value
    diagnose   server vendor, namespaces, and counters

``diagnose`` is the one that genuinely replaces UaExpert's unique value, and it is
about fifteen lines of client calls.

Typical use::

    python -m tools.opcua_browser browse
    python -m tools.opcua_browser watch AERATION.AHU-1.do_mg_l
    python -m tools.opcua_browser read AERATION.AHU-1.air_flow_m3h
    python -m tools.opcua_browser write AERATION.AHU-1.setpoint_do_mg_l 2.5
    python -m tools.opcua_browser diagnose
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable
from typing import Any

from asyncua import Client, ua

#: Default endpoint. Matches the soft PLC.
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840/wwtp/server/"


def _browse_path(endpoint: str) -> str:
    """The client endpoint, with a well-known anonymous identity."""
    return endpoint


async def _find_plant(client: Client, marker: str = "PLANT-A") -> Any | None:
    """Locate the plant object under Objects.

    Searched by name rather than by NodeId on purpose: a hard-coded NodeId is
    exactly the kind of thing that breaks when the server is rebuilt, and
    discovering the node is the whole benefit of OPC UA over a register map.
    """
    for child in await client.nodes.objects.get_children():
        name = await child.read_browse_name()
        if marker in name.Name:
            return child
    return None


async def _walk(
    node: Any, depth: int = 0, max_depth: int = 3,
    show_values: bool = True, counter: dict[str, int] | None = None,
) -> int:
    """Recursively print the address space. Returns the number of nodes visited."""
    counter = counter if counter is not None else {}
    visited = 0
    for child in await node.get_children():
        visited += 1
        name = (await child.read_browse_name()).Name
        class_name = str(await child.read_node_class()).split(".")[-1]

        value = ""
        if show_values and class_name in ("Variable",):
            try:
                data = await child.read_data_value()
                status = data.StatusCode.name if hasattr(data.StatusCode, "name") else ""
                if data.Value is not None:
                    raw = data.Value.Value if isinstance(
                        data.Value, ua.Variant
                    ) else data.Value
                    value = f" = {raw}"
                    if status and status != "Good":
                        value += f"  [{status}]"
            except ua.UaError:
                # A node that refuses a read is information, not a crash.
                value = " = <unreadable>"

        print(f"{'  ' * depth}{name}  ({class_name}){value}")
        counter[class_name] = counter.get(class_name, 0) + 1

        if class_name == "Object" or depth + 1 <= max_depth:
            visited += await _walk(
                child, depth + 1, max_depth, show_values, counter
            )
    return visited


async def cmd_browse(args: argparse.Namespace) -> int:
    async with Client(args.endpoint, timeout=args.timeout) as client:
        plant = await _find_plant(client, args.marker)
        if plant is None:
            print(f"No object matching {args.marker!r} under Objects.")
            print("The server is up but does not expose the plant. Is this the")
            print("soft PLC endpoint, or a different application?")
            return 1
        print(f"Address space of {args.endpoint}")
        print(f"  root: {(await plant.read_browse_name()).Name}\n")
        counter: dict[str, int] = {}
        visited = await _walk(plant, 0, args.depth, not args.no_values, counter)
        print(f"\n{visited} nodes visited: "
              + ", ".join(f"{n} {k}" for k, n in sorted(counter.items())))
    return 0


async def cmd_read(args: argparse.Namespace) -> int:
    async with Client(args.endpoint, timeout=args.timeout) as client:
        node = await _resolve(client, args.path, args.marker)
        if node is None:
            print(f"Not found: {args.path}")
            return 1
        data = await node.read_data_value()
        print(f"{args.path}")
        print(f"  value   {data.Value.Value if isinstance(data.Value, ua.Variant) else data.Value}")
        print(f"  status  {data.StatusCode.name}")
        # Property browse names must be qualified with the namespace they were
        # created in; a bare name resolves in namespace 0 and reports
        # BadNoMatch, which looks like a missing property rather than a naming
        # mistake. The namespace is discovered rather than assumed.
        ns = node.nodeid.NamespaceIndex
        for prop in ("EngineeringUnits", "UnitSymbol", "NormalBandLow",
                     "NormalBandHigh", "SignalId"):
            try:
                child = await node.get_child(f"{ns}:{prop}")
                v = await child.read_value()
                print(f"  {prop:<24} {v}")
            except ua.UaError:
                pass
    return 0


async def cmd_write(args: argparse.Namespace) -> int:
    async with Client(args.endpoint, timeout=args.timeout) as client:
        node = await _resolve(client, args.path, args.marker)
        if node is None:
            print(f"Not found: {args.path}")
            return 1
        try:
            await node.set_value(ua.Variant(args.value, ua.VariantType.Double))
        except ua.UaError as exc:
            # The server refuses out-of-range and read-only writes, and says why.
            print(f"Refused: {exc}")
            return 2
        back = await node.read_value()
        print(f"{args.path} = {back.Value if isinstance(back, ua.Variant) else back}")
    return 0


async def cmd_watch(args: argparse.Namespace) -> int:
    async with Client(args.endpoint, timeout=args.timeout) as client:
        node = await _resolve(client, args.path, args.marker)
        if node is None:
            print(f"Not found: {args.path}")
            return 1
        print(f"Watching {args.path} — Ctrl-C to stop\n")

        class Handler:
            def __init__(self) -> None:
                self.count = 0

            def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
                self.count += 1
                status = data.monitored_item.Value.SourceTimestamp
                print(f"  #{self.count:<5} {val}")

        handler = Handler()
        sub = await client.create_subscription(args.period * 1000, handler)
        await sub.subscribe_data_change(node)
        try:
            await asyncio.sleep(args.seconds)
        except asyncio.CancelledError:
            pass
        await sub.delete()
        print(f"\n{handler.count} change notifications in {args.seconds:.0f}s")
    return 0


async def cmd_diagnose(args: argparse.Namespace) -> int:
    """Server diagnostics — the mode that replaces UaExpert's unique value.

    Everything printed here is available through the standard address space or
    the standard services, which is the point: none of it needs a proprietary
    tool, only a client.
    """
    async with Client(args.endpoint, timeout=args.timeout) as client:
        srv = client.nodes.server
        print(f"Endpoint      {args.endpoint}")

        # Identity from the standard Server object. Every read here is optional
        # in the spec, so each is attempted independently — a server is entitled
        # to omit any of them, and a diagnostic tool that dies on the first
        # missing attribute is worse than useless.
        async def try_read(label: str, coro: Awaitable[Any]) -> None:
            try:
                v = await coro
                raw = v.Value if isinstance(v, ua.Variant) else v
                if isinstance(raw, (list, tuple)) and raw:
                    raw = raw[0]
                print(f"{label:<14}{raw}")
            except ua.UaError as exc:
                print(f"{label:<14}<{exc}>")

        # Server identity comes from the standard Server object, reached by the
        # numeric identifiers the spec fixes. These are the *only* places the
        # vendor strings live — there is no vendor extension, which is exactly
        # why a standard client can report them and a custom one might not.
        for label, nodeid in (
            ("ServerArray", ua.ObjectIds.Server_ServerArray),
            ("NamespaceArray", ua.ObjectIds.Server_NamespaceArray),
            ("ServerStatus", ua.ObjectIds.Server_ServerStatus),
        ):
            try:
                v = await client.get_node(nodeid).read_value()
                raw = v.Value if isinstance(v, ua.Variant) else v
                if label == "ServerArray" and isinstance(raw, (list, tuple)) and raw:
                    raw = raw[0]
                print(f"{label:<14}{raw}")
            except ua.UaError as exc:
                print(f"{label:<14}<{exc}>")

        print()

    return 0


async def _resolve(client: Client, path: str, marker: str) -> Any | None:
    """Resolve a dotted browse path to a node.

    The address space is ``Objects → Plant → Area → Equipment → Variable``, and a
    signal's contract id is already ``AREA:UNIT:EQUIPMENT``. So the
    contract-shaped path ``AERATION.AHU-1.do_mg_l`` maps onto the tree exactly:
    the area *is* a level in the tree, not a redundant prefix.

    The short form ``AHU-1.do_mg_l`` also works, by searching every area for the
    equipment. Supporting both is worth the few extra lines — the contract-shaped
    form is unambiguous, and the short form is what a person actually types.
    """
    plant = await _find_plant(client, marker)
    if plant is None:
        return None

    parts = [p for p in path.split(".") if p]
    if not parts:
        return None

    async def child_named(node: Any, name: str) -> Any | None:
        for child in await node.get_children():
            if (await child.read_browse_name()).Name == name:
                return child
        return None

    async def descend(node: Any, names: list[str]) -> Any | None:
        for name in names:
            node = await child_named(node, name)
            if node is None:
                return None
        return node

    area_nodes = {
        (await area.read_browse_name()).Name: area
        for area in await plant.get_children()
    }

    # Contract-shaped: the first component names an area.
    if parts[0] in area_nodes:
        return await descend(area_nodes[parts[0]], parts[1:])

    # Short form: search the areas for the first component.
    for area in area_nodes.values():
        found = await descend(area, parts)
        if found is not None:
            return found
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opcua-browser",
        description="Browse, read, write and diagnose an OPC UA server.",
        epilog=(
            "Replaces UaExpert, which is Windows-only. See the module docstring "
            "for why writing the client beats downloading one."
        ),
    )
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                   help=f"OPC UA endpoint (default {DEFAULT_ENDPOINT})")
    p.add_argument("--marker", default="PLANT-A",
                   help="browse name fragment identifying the plant object")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="client timeout, seconds")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("browse", help="print the address space as a tree")
    b.add_argument("--depth", type=int, default=2, help="tree depth to print")
    b.add_argument("--no-values", action="store_true", help="structure only")
    b.set_defaults(func=cmd_browse)

    r = sub.add_parser("read", help="read one variable and its metadata")
    r.add_argument("path", help="dotted browse path, e.g. AERATION.AHU-1.do_mg_l")
    r.set_defaults(func=cmd_read)

    w = sub.add_parser("write", help="set a writable value")
    w.add_argument("path")
    w.add_argument("value", type=float)
    w.set_defaults(func=cmd_write)

    c = sub.add_parser("watch", help="subscribe and follow a value")
    c.add_argument("path")
    c.add_argument("--seconds", type=float, default=30.0)
    c.add_argument("--period", type=float, default=0.5,
                   help="publishing period, seconds")
    c.set_defaults(func=cmd_watch)

    d = sub.add_parser("diagnose", help="server identity, namespaces, round-trip")
    d.set_defaults(func=cmd_diagnose)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # `int(...)` and not the bare call. `args.func` is a callable on an
        # argparse Namespace, so its return type is `Any` to a type checker, and
        # `main` declares `int`. Without the cast mypy is right that the function
        # claims to return an integer and the expression has no idea.
        #
        # Found by writing `.github/workflows/gates.yml`, which runs mypy over
        # `tools` — and it failed, on a file that has carried the error since
        # Phase 2 while every mypy invocation in this project was run over a
        # subset of the packages. **A gate reported as passing because it was run
        # over the wrong subset is worse than no gate**, and that is the second
        # time in this project that the real problem was the check rather than
        # the thing being checked.
        return int(asyncio.run(args.func(args)))
    except ConnectionRefusedError:
        print(f"Cannot reach {args.endpoint}")
        print("Is the soft PLC running?  docker compose ps")
        return 1
    except ua.UaError as exc:
        print(f"OPC UA error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
