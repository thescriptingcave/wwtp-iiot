"""Generate the web dashboard's data layer from the contract.

    python -m ui.web.generate_page          # write ui/web/lib/contract.json
    python -m ui.web.generate_page --check  # report drift

## Why the page is generated

Same reason the Node-RED flows and the Grafana dashboards are, and the reason is
thread 22 of the learning log: **a hand-written dashboard holds signal ids that
somebody typed**, and a wrong signal id is a *wrong number*, not an error. The
permit page in Phase 5a read pH from the TSS signal — a valid query against a
valid table, and nothing in the database objects to it, because a unit of
measure is carried by a signal's identity and by nothing on the row.

So the signal list, the areas, the units, the `normal_low`/`normal_high` bands
and the alarm rules that belong on each panel are all derived from
`contracts/tags.yaml`. There is no second place to rename a signal.

## What is generated and what is not

Generated: `ui/web/lib/contract.json` — the read model the React server
components import. One file, because a TypeScript module that reads YAML at
runtime would need a YAML parser in the browser bundle for no benefit.

**Not** generated: the JSX, the SQL, the CSS. A generator that emits code tends
to emit code nobody wants to read, and the value here is entirely in the *data*
being derived rather than the markup being derived. The page's layout is a
design decision and belongs to a person; the numbers on it do not.

## Why a JSON file and not an API call at build time

The obvious alternative is a build step that queries the database. That would
couple the image build to a running database, which means the image cannot be
built without the stack, which means the build fails for a reason that has
nothing to do with the build. The contract is in git, so the read model is in
git, and the *data* arrives at request time through a server-side pool.

## The one thing this cannot check

Nothing here verifies that a **query** is right. A generated signal list makes
the ids right; it does not make `mean(value)` the right aggregate for a table
that has `mean`, and that mistake was in the Phase 5a dashboards too. The
dashboard queries in `lib/queries.ts` are therefore pinned by
`tests/test_web_page.py::test_every_query_names_a_signal_the_contract_declares`
and by nothing else — which is the honest boundary, stated here as well as in
the test.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from softplc.contract import Contract
from softplc.contract import contract as get_contract

log = logging.getLogger("ui.web.page")

#: Where the read model is written. Inside `ui/web/lib/` so the React app imports
#: it as `./contract.json` and nothing outside the app has a reason to know.
OUT = Path("ui/web/lib/contract.json")


def _band(sig: Any) -> dict[str, float] | None:
    """The normal band, if the contract declares one that *is* one.

    Returned as a whole or not at all, and **rejected when the two ends are
    equal.** A band with only a `normal_high` is not a range, and drawing half of
    one as a shaded region draws a boundary that looks like a limit and is not.

    The equal-ends case is not hypothetical: two of the 57 signals have one.

    * `AERATION:AHU-1:SETPOINT_DO` — `normal_low: 2.0`, `normal_high: 2.0`.
      A setpoint has no normal *range*; it has a commanded value, and the band
      is a modelling artefact of reusing the same two fields for a measurement
      and a command.
    * `SITE:WEATHER:STORM` — `0.0` to `0.0`. A boolean expressed as a number.

    Shading a zero-height region draws a line, which reads as a limit the signal
    must not cross, and for the setpoint that line would sit exactly on the
    commanded value — so the panel would say "outside band" for a setpoint
    sitting exactly where it was told to sit. A zero-width band is a bug in the
    *rendering*, and the fix belongs here rather than in the contract, because the
    contract is right: a setpoint does not have a healthy range.
    """
    lo, hi = getattr(sig, "normal_low", None), getattr(sig, "normal_high", None)
    if lo is None or hi is None or float(hi) <= float(lo):
        return None
    return {"low": float(lo), "high": float(hi)}


def build_read_model(c: Contract) -> dict[str, Any]:
    """The whole `contract.json`, as a plain dict.

    Deliberately a plain dict and a plain JSON file rather than a typed object:
    the consumer is TypeScript, and a `tsconfig` path alias plus a generated
    `.d.ts` is a second thing to keep in step. A JSON file's shape is checked by
    the compiler at the import site, which is where a mistake would be made.
    """
    areas = []
    for area in c.areas:
        members = [s for s in c.signals.values() if s.area == area["id"]]
        if not members:
            # An area with no signals is a mistake in the contract, not an empty
            # section. Skipping it silently is how a renamed area survives a
            # rename of everything under it.
            log.warning("area %s has no signals; it will not appear", area["id"])
            continue
        areas.append({
            "id": area["id"],
            "name": area["name"],
            # `key=` rather than sorting the dicts. A type checker cannot
            # describe "these dicts have an `id`", so `sorted([...dicts...])`
            # needs a `SupportsRichComparisonT` the inferred union does not
            # provide — and the fix is not a cast, it is to say which field the
            # order is by, which is information the code needed anyway.
            "signals": sorted(
                (
                    {
                        "id": s.id,
                        # The label is the contract's own short name (`do_mg_l`),
                        # not the id, because `AERATION:AHU-1:DO` is a machine
                        # address and an operator should not have to read one. The
                        # contract asserts that a field name carries no metadata
                        # (`test_no_numeric_field_names`), so the label cannot
                        # drift into lying about the unit.
                        "label": s.field,
                        "eu": s.eu,
                        "precision": _precision(s.eu),
                        "writable": bool(s.writable),
                        "equipment": s.equipment,
                        "sample_ms": s.sample_ms,
                        "band": _band(s),
                        # The engineering range, which is *not* the normal band.
                        # Carried so a page can show where a value sits between
                        # "expected" and "the instrument cannot physically read
                        # this" — and, per thread 2, because OPC UA does not
                        # enforce it, so showing it is the only enforcement an
                        # operator gets.
                        "range": {"min": s.range_min, "max": s.range_max},
                    }
                    for s in members
                ),
                key=lambda d: str(d["id"]),
            ),
        })

    return {
        "$comment": (
            "GENERATED by python -m ui.web.generate_page from "
            "contracts/tags.yaml. Do not edit; run the generator."
        ),
        "areas": areas,
        "counts": {
            "areas": len(areas),
            "signals": sum(len(a["signals"]) for a in areas),
        },
    }


#: Decimal places to show, keyed by engineering unit.
#:
#: A pH shown to six decimal places is not more informative than one shown to
#: two; it is a wider column and a harder comparison. This is a display decision
#: so it lives in one table rather than in every component that renders a number.
PRECISION_BY_UNIT = {
    "mg/L": 2,
    "NTU": 1,
    "m3/h": 1,
    "%": 1,
    "kPa": 1,
    "degC": 1,
    "V": 2,
    "A": 2,
    "Hz": 2,
    "pH": 2,
    "m": 2,
    "h": 2,
}


def _precision(eu: str) -> int:
    return PRECISION_BY_UNIT.get(eu, 3)


def render(model: dict[str, Any]) -> str:
    """The exact bytes written to disk.

    Trailing newline, `indent=2`, `sort_keys=False` — key order is the order the
    dict is built in, which is the order a reader wants, and sorting would put
    `areas` after `counts` for no reason.
    """
    return json.dumps(model, indent=2, ensure_ascii=False) + "\n"


def check(c: Contract, out: Path = OUT) -> list[str]:
    problems: list[str] = []
    if not out.exists():
        return [f"{out} does not exist (run: python -m ui.web.generate_page)"]
    if out.read_text(encoding="utf-8") != render(build_read_model(c)):
        problems.append(
            f"{out} is out of step with the contract "
            "(run: python -m ui.web.generate_page)"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ui.web.generate_page",
        description="Generate ui/web/lib/contract.json from contracts/tags.yaml.",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    c = get_contract()

    if args.check:
        problems = check(c, args.out)
        for line in problems:
            log.error("%s", line)
        if problems:
            return 1
        log.info("%s is in step with the contract", args.out)
        return 0

    model = build_read_model(c)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(model), encoding="utf-8")
    log.info(
        "wrote %s: %d areas, %d signals",
        args.out, model["counts"]["areas"], model["counts"]["signals"],
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
