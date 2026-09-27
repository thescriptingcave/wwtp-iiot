"""Generate the Node-RED tag list from the contract.

    python -m scada.generate_tags          # write scada/flows/tags.json
    python -m scada.generate_tags --check  # report drift, change nothing

## Why this file is generated rather than written

`contracts/tags.yaml` says, in its own header, that every consumer is generated
from or validated against it — and it lists **"Node-RED tag list"** among them.
That is a promise, and a hand-written tag list is how it would be broken.

A tag list is 57 entries of an id, a label, a unit, an area and an equipment id.
Write it by hand and it is correct on the day you write it. Rename a signal,
change a unit, or add an equipment id, and it is wrong — and *plausibly* wrong,
because a stale tag list still resolves, still renders, and still shows the last
value it knew about. The failure is a mimic diagram that looks fine and is
quietly describing a plant that no longer exists.

`tests/test_scada_contract.py` closes that loop, the same way
`tests/test_contract.py` closes it for Modbus and OPC UA. And as with the other
consumers, **the generated file is committed**, because a build step that can
only run inside one container is a build step that will not run.

## What a tag needs, and what it deliberately does not

The three fields a SCADA tag normally carries beyond identity — `min`, `max` and
`unit` — are all here, and so are `normal_low`/`normal_high`.

The `min`/`max` pair is the *engineering range*, which is the physical bound of
the instrument. It is **not** the alarm threshold, and the two being confused is
how a SCADA system ends up alarming on a value that was only ever out of the
instrument's calibration range. A mimic needs the range to draw its bar; it must
not draw the alarm line, because the alarm line lives in `alarms/rules.py` and
is measured against a healthy plant's actual distribution.

`area` and `equipment` are here because a mimic is organised by area and an alarm
is attributed to a machine, and because the contract is the only place that
knows which is which.

`writable` carries the `range` a control flow must check before writing. That is
the *permit* surface — the small explicit list of things an operator may change —
and it is the third thing in this project that has been declared and unused.
`scada/flows/03-control.json` is its first consumer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from softplc.contract import Contract, contract as get_contract

log = logging.getLogger("scada.generate_tags")

#: Where the generated file goes. Inside `flows/` because a Node-RED import
#: directory is the natural home for it and someone who drops a flow into
#: `scada/flows/` gets the tag list with it.
TAGS_PATH = Path("scada/flows/tags.json")

#: The schema version, so a Node-RED flow can check it has the shape it expects
#: rather than failing on a missing field four nodes later.
SCHEMA = "wwtp.node-red.tags/1"


def _label(signal_id: str, field: str) -> str:
    """A human label, derived rather than stored.

    Taken from the signal's `field` — the contract's own short name like
    ``do_mg_l`` — rather than from the id, because ``AERATION:AHU-1:DO`` is a
    machine address and ``AERATION:AHU-1:do_mg_l`` is worse. The contract already
    asserts that a field name carries no metadata (see
    `tests/test_contract.py::test_no_numeric_field_names`), so this cannot drift
    into lying about the unit.
    """
    return field


def build_tags(c: Contract) -> dict[str, Any]:
    """The whole tag list, as a dict ready to serialise.

    Sorted by signal id so the file is byte-stable: a regenerated file that
    differs only in ordering produces a diff that says nothing, and a diff that
    says nothing trains people to ignore diffs.
    """
    writable = c.writable
    tags: list[dict[str, Any]] = []

    for signal_id in sorted(c.signals):
        s = c.signals[signal_id]
        entry: dict[str, Any] = {
            "id": s.id,
            "label": _label(s.id, s.field),
            "area": s.area,
            "field": s.field,
            "unit": s.unit or "",
            "min": s.range_min,
            "max": s.range_max,
            "normal_low": s.normal_low,
            "normal_high": s.normal_high,
            "deadband": s.deadband,
            "sample_ms": s.sample_ms,
            "equipment": s.equipment,
            "holder": s.holder,
            "writable": signal_id in writable,
        }
        if signal_id in writable:
            # `Contract.writable` is a plain dict, not a dataclass -- the spec
            # is only ever read out of the YAML and three fields are carried
            # forward, so a typed object would be ceremony. `range` is a
            # two-element list, and `range_low`/`range_high` do not exist.
            spec = writable[signal_id]
            low, high = spec["range"]
            entry["write_range"] = [low, high]
            entry["write_reason"] = spec.get("reason", "")
        tags.append(entry)

    areas: dict[str, list[str]] = {}
    for tag in tags:
        areas.setdefault(tag["area"], []).append(tag["id"])

    return {
        "schema": SCHEMA,
        "source": "contracts/tags.yaml",
        "generated_by": "python -m scada.generate_tags",
        "site": c.site.get("id", "wwtp"),
        "permit": dict(sorted(c.permit.items())),
        "counts": {
            "tags": len(tags),
            "areas": len(areas),
            "writable": sum(1 for t in tags if t["writable"]),
        },
        "areas": {k: sorted(v) for k, v in sorted(areas.items())},
        "tags": tags,
    }


def render(payload: dict[str, Any]) -> str:
    """The file, as Node-RED wants to read it.

    ``indent=2`` and a trailing newline: a generated file is a file people read
    in a diff, and a single-line JSON blob is unreadable exactly when you need it.
    """
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def check(c: Contract | None = None, path: Path = TAGS_PATH) -> list[str]:
    """Differences between the contract and the committed file.

    Returns human-readable lines rather than a boolean, because the useful
    failure message names the tag and the field, and a bare ``False`` makes the
    reader go and diff it themselves.
    """
    expected = render(build_tags(c or get_contract()))
    if not path.exists():
        return [f"{path} does not exist; run: python -m scada.generate_tags"]
    actual = path.read_text(encoding="utf-8")
    if actual == expected:
        return []
    return _diff(expected, actual, path)


def _diff(expected: str, actual: str, path: Path) -> list[str]:
    """A short, specific description of the drift."""
    import difflib

    out: list[str] = [f"{path} is out of step with the contract:"]
    diff = list(
        difflib.unified_diff(
            actual.splitlines(), expected.splitlines(),
            fromfile="committed", tofile="from contract", lineterm="", n=0,
        )
    )
    out.extend(diff[:40])
    if len(diff) > 40:
        out.append(f"... and {len(diff) - 40} more lines")
    out.append("run: python -m scada.generate_tags")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scada.generate_tags",
        description="Generate the Node-RED tag list from contracts/tags.yaml.",
    )
    parser.add_argument("--check", action="store_true",
                        help="report drift without writing")
    parser.add_argument("--path", type=Path, default=TAGS_PATH)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    c = get_contract()

    if args.check:
        problems = check(c, args.path)
        for line in problems:
            log.error("%s", line)
        if problems:
            return 1
        log.info("%s is in step with the contract", args.path)
        return 0

    payload = build_tags(c)
    args.path.parent.mkdir(parents=True, exist_ok=True)
    args.path.write_text(render(payload), encoding="utf-8")
    log.info(
        "wrote %s: %d tags over %d areas, %d writable",
        args.path, payload["counts"]["tags"], payload["counts"]["areas"],
        payload["counts"]["writable"],
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
