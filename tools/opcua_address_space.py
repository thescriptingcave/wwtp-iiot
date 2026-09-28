"""Serialise the built OPC UA address space, and check it for drift.

    python -m tools.opcua_address_space            # write the JSON
    python -m tools.opcua_address_space --check     # report drift (CI)

## Why this file exists

The address space is the largest generated artefact here — **686 addressable
nodes from 57 signals**, twelve per signal — and until now it was the only one
with no file behind it. It is built at startup, served, and discarded,
so there was nothing to diff and nothing to review. Everything else generated
here has all three:

| generated | artefact | drift gate |
|---|---|---|
| Node-RED tag list | `scada/flows/tags.json` | `generate_tags --check` |
| Node-RED flows | `scada/flows/*.json` | `build_flows --check` |
| Grafana dashboards | `ui/grafana/dashboards/*.json` | `generate_dashboards --check` |
| course queries | `sql/TablePlus/*.sql` | `extract_sql --check` |
| web read model | `ui/web/lib/*.json` | committed |
| **OPC UA address space** | **this file** | **`--check`** |

The cost of that gap was measured, not asserted. `courses/opcua/08-generated.md`
counts about fourteen decisions inside `build_address_space` and `_add_signal`, and
finds **three unambiguously wrong plus a fourth that is wrong whenever nothing
drives the plant**:

| decision | what it produced |
|---|---|
| `_variant_type` returns `Double` for any unit | the `{Boolean}` flag is a float |
| `EngineeringUnits` is a bare `Int32` | not the `EUInformation` the spec requires |
| `BaseDataVariableType`, not `AnalogItemType` | a discovery tool finds **0** |
| initial value is `sig.normal_low` | a fresh server reports DO at 1.5, `Good` |

All four survived because a pull request changing `_add_signal` shows a diff of
Python plumbing, and every one of the 686 consequences is invisible in that diff.
Serialising turns them into **rows in a table**, which is the difference between a
change you can review and a change you have to take on trust.

## What is in the file, and what is deliberately not

One record per node, in tree order, with the fields a reviewer needs to judge a
change: `path`, `browse_name`, `node_class`, `data_type`, `parent`, and for
variables the engineering unit and the value it was constructed with.

The last one is the field that makes the review worthwhile. `normal_low` is
already in `contracts/tags.yaml` and is already visible in a YAML diff — but seeing
that it is *used as the initial value* is not, and that is the decision nobody
reviewed. Writing it down as a value in a table is the whole
point of the exercise.

## What it cannot do

**It is a report, not a gate on correctness.** Nothing here asserts that the
address space is *right* — a conformant client would still find no
`AnalogItemType` after this file is committed. What it buys is that a change to
one of those fourteen decisions becomes visible as a changed row, so it can be
reviewed on its merits. Lessons 02, 03 and 08 are the argument for why that is
worth having; this file is the cheap half of the answer, and fixing the decisions
themselves is the other half and is not done here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from asyncua import ua
from softplc.servers.opcua import OpcUaServer, _unit_id

log = logging.getLogger("tools.opcua_address_space")

#: Where the serialised address space lives. Beside the server that builds it,
#: because it is that server's output — the same reason the dashboards sit in
#: `ui/grafana/dashboards/` rather than in `docs/`.
OUT = Path("contracts/address-space.json")

#: A free-ish high port, so `--check` never collides with a running plant on
#: 4840 and never needs the port to be free. Same reasoning as the lesson gate.
PORT = 48409

#: `NodeClass` values that appear in this address space, as short names. The
#: numeric values are an implementation detail of `asyncua` and must not appear
#: in a committed file, because a dependency bump would then look like drift.
NODE_CLASSES = {
    ua.NodeClass.Object: "Object",
    ua.NodeClass.Variable: "Variable",
    ua.NodeClass.Method: "Method",
    ua.NodeClass.ObjectType: "ObjectType",
    ua.NodeClass.VariableType: "VariableType",
    ua.NodeClass.ReferenceType: "ReferenceType",
    ua.NodeClass.DataType: "DataType",
    ua.NodeClass.View: "View",
}


async def _describe(node: Any) -> dict[str, Any]:
    """One node, as a reviewer needs to see it.

    `DataType` is read only for Variables. An `Object` or a `Folder` has no such
    attribute, and asking for one raises `BadAttributeIdInvalid` — which is the
    correct answer from the server and would otherwise abort the whole walk on
    the first area folder. The lesson 01 distinction between a thing you can go
    into and a fact about it is also the distinction between a node that has a
    data type and one that does not.
    """
    node_class = NODE_CLASSES.get(await node.read_node_class(), "Unknown")
    info: dict[str, Any] = {
        "browse_name": (await node.read_browse_name()).Name,
        "node_class": node_class,
    }
    if node_class == "Variable":
        info["data_type"] = (await node.read_data_type_as_variant_type()).name
    return info


async def walk(space: Any) -> dict[str, Any]:
    """Build the serialised address space.

    Walks `get_children()`, which returns both `HasComponent` and `HasProperty`
    references — the lesson 01 distinction between "a thing you can go into" and
    "a fact about this thing". Both are recorded, and the reference kind is kept
    so a reader can tell a folder from a property without inferring it from the
    name, which is the mistake lesson 01 warns about.
    """
    records: list[dict[str, Any]] = []

    async def rec(node: Any, path: str, kind: str) -> None:
        for child in await node.get_children():
            info = await _describe(child)
            name = info["browse_name"]
            here = f"{path}.{name}" if path else name
            record: dict[str, Any] = {
                "path": here,
                "browse_name": name,
                "node_class": info["node_class"],
                "reference": kind,
            }
            if info["node_class"] == "Variable":
                record["data_type"] = info["data_type"]
            records.append(record)
            await rec(child, here, "property" if kind == "property" else "component")

    await rec(space.folder, "", "component")

    # The per-signal detail that a diff of `contracts/tags.yaml` cannot show:
    # what the server actually *did* with each value. `constructed_from` is the
    # decision nobody reviewed, written down as a value rather than a line of
    # Python.
    signals: list[dict[str, Any]] = []
    for signal_id, entry in space.variables.items():
        sig = space.contract.signal(signal_id)
        signals.append({
            "signal_id": signal_id,
            "browse_name": entry.browse_name,
            "data_type": (await entry.node.read_data_type_as_variant_type()).name,
            "unit_symbol": sig.eu,
            "engineering_unit_id": _unit_id(sig.eu),
            "writable": sig.writable,
            "constructed_from": {
                # The value the node is built with, which is the plant's
                # `normal_low` and not a measurement. See lesson 03.
                "field": "normal_low",
                "value": sig.normal_low,
            },
        })
    signals.sort(key=lambda r: r["signal_id"])

    equipment = [
        {"equipment_id": eq, "run_state_constructed_as": 0}
        for eq in sorted(space.equipment_nodes)
    ]

    return {
        "note": (
            "Generated by tools/opcua_address_space.py from "
            "contracts/tags.yaml. Do not edit by hand; run "
            "`python -m tools.opcua_address_space`."
        ),
        "namespace_index": space.namespace_index,
        "plant_browse_name": (await space.folder.read_browse_name()).Name,
        "counts": {
            "nodes": len(records),
            "signals": len(signals),
            "equipment": len(equipment),
        },
        "nodes": records,
        "signals": signals,
        "equipment": equipment,
    }


def render(document: dict[str, Any]) -> str:
    """Deterministic text.

    `sort_keys` is deliberately **not** set: node order is tree order, which is
    the order a reviewer reads, and alphabetising it would destroy the one thing
    this file is for. The list order is itself derived from the contract and is
    therefore stable.
    """
    return json.dumps(document, indent=2) + "\n"


async def build() -> str:
    server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{PORT}/wwtp/server/")
    await server.start()
    await server.wait_ready()
    try:
        assert server.space is not None
        return render(await walk(server.space))
    finally:
        await server.stop()


def check(path: Path = OUT) -> list[str]:
    """Drift between the committed file and what the server actually builds."""
    wanted = asyncio.run(build())
    if not path.exists():
        return [f"{path} does not exist "
                f"(run: python -m tools.opcua_address_space)"]
    actual = path.read_text(encoding="utf-8")
    if actual == wanted:
        return []
    where = _first_difference(actual, wanted)
    return [
        f"{path} is out of step with the address space the server builds: {where}\n"
        f"  (run: python -m tools.opcua_address_space)"
    ]


def _first_difference(actual: str, wanted: str) -> str:
    """Name the first differing line, because "the files differ" is not useful.

    A 686-node JSON file has *some* difference on almost every change, and the
    whole value of this gate is that a reviewer is told which decision moved. So
    the message points at the line rather than making someone diff it by hand.
    """
    a, b = actual.splitlines(), wanted.splitlines()
    for i, (x, y) in enumerate(zip(a, b, strict=False), start=1):
        if x != y:
            return f"line {i}: committed {x.strip()[:60]!r}, built {y.strip()[:60]!r}"
    if len(a) != len(b):
        which = "committed" if len(a) > len(b) else "built"
        longer = f"{which} file is longer"
        return f"line {min(len(a), len(b)) + 1}: {longer} ({len(a)} vs {len(b)} lines)"
    return "files differ in whitespace only"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tools.opcua_address_space",
        description="Serialise the OPC UA address space, and check it for drift.",
    )
    p.add_argument("--check", action="store_true",
                   help="report drift instead of writing (used by CI)")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    if args.check:
        problems = check(args.out)
        for line in problems:
            log.error("%s", line)
        if problems:
            return 1
        log.info("%s is in step with the address space", args.out)
        return 0

    text = asyncio.run(build())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    doc = json.loads(text)
    log.info("wrote %s: %d nodes, %d signals, %d equipment",
             args.out, doc["counts"]["nodes"], doc["counts"]["signals"],
             doc["counts"]["equipment"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
