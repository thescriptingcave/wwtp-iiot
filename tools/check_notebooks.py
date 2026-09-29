"""The notebook gate.

    make notebooks          # build, execute, compare
    make notebooks-check    # report drift, execute, compare

## What this gate is for, and what it deliberately is not

It does three things:

1. **Every notebook is in step with its source.** `tools/build_notebooks.py`
   generates `notebooks/*.ipynb` from `notebooks/src/*.md`, and a notebook that
   was hand-edited is drift.
2. **Every notebook executes**, top to bottom, against a live database.
3. **Every `output` block in the source matches what the notebook actually
   printed.** This is the part that matters, and it is the SQL course's rule
   applied to notebooks: *a lesson that states a number must state the number the
   database actually produced.*

That third item is why the notebooks are generated from markdown rather than
authored as `.ipynb`. An `output` block in markdown is a claim a machine can
check. A cell's `outputs` array is whatever the last run left there, which is the
opposite: it is a record, not a claim, and it goes stale silently the next time
the data is re-seeded.

## Why `assert` inside the notebook, and not a test file

Each notebook carries its own `assert` statements. They run in CI with the
notebook, they sit next to the claim they defend, and they are in the same cell as
the numbers, so a reader sees the check and the check is what they would have run.

They are **structural**, never exact-value:

    assert acf1 > 0.5          # not  assert acf1 == 0.912

The seeder uses `time.time()` for its origin, so every re-seed shifts the whole
date range. A notebook that asserted a specific float would fail on a fresh
database while describing the same data, which trains a reader to ignore the
assertions. The exceptions are counts of things the project guarantees — "thirteen
signals reported once", "exactly one row is Bad" — which are properties of the
generator, not of the moment it ran.

## The environment

Executing a notebook needs `analysis` installed and a database reachable:

    uv sync --extra protocols --extra storage --extra analysis
    docker compose up -d db
    docker compose run --rm init-db
    docker compose --profile demo run --rm seed
    docker compose stop gateway          # or the data moves under the notebook

`MPLBACKEND=Agg` is set by the gate. Notebooks must not open a window.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "notebooks"

sys.path.insert(0, str(ROOT))

#: An `output` block in a notebook source. Matched in the *source markdown*, not
#: in the generated notebook, because the source is where the claim is made.
OUTPUT_BLOCK = re.compile(r"^```output\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def _cells(notebook: dict[str, Any]) -> list[dict[str, Any]]:
    """The cells, typed.

    The `nbformat` objects are plain dicts and mypy cannot know that, so the
    return is annotated rather than cast: this is a boundary, and saying so is
    more useful than an `Any` leaking into every caller.
    """
    cells: list[dict[str, Any]] = notebook["cells"]
    return cells


def _printed(cell: dict) -> str:
    """Everything a cell printed, concatenated.

    `stream` covers `print`; `execute_result` covers a bare expression as the last
    line of a cell, which pandas does in several places. Both are output a reader
    would see, so both are compared.
    """
    parts: list[str] = []
    for out in cell.get("outputs", []):
        if out.get("output_type") == "stream":
            parts.append("".join(out.get("text", [])))
        elif out.get("output_type") == "execute_result":
            data = out.get("data", {})
            if "text/plain" in data:
                parts.append("".join(data["text/plain"]))
    return "".join(parts)


def _normalise(text: str) -> str:
    """Strip the things that vary between runs without meaning anything.

    Three things, and the justification for each:

    * **Em-dashes and curly apostrophes** — a typographic choice, not a number.
    * **Runs of whitespace, collapsed to one space.** pandas pads DataFrame
      reprs to align columns, and the padding depends on the widest cell in the
      column, which depends on the data. `signal_id` followed by thirty-one
      spaces and `signal_id` followed by twenty-four are the same row. Column
      alignment is a rendering decision, not a claim, and a checker that fails on
      it trains its reader to ignore it.
    * **Nothing else.** A digit is a digit. The first version of this function
      only stripped *trailing* whitespace, so a table whose columns shifted by two
      spaces failed a check that had nothing to do with the content.
    """
    text = text.replace("\u2014", "-").replace("\u2019", "'")
    return "\n".join(
        re.sub(r"[ \t]+", " ", line).strip() for line in text.strip().split("\n")
    )


def check_outputs(notebook_path: Path, source_path: Path, executed: dict) -> list[str]:
    """Compare each claimed `output` block against what the notebook printed.

    A block is checked only if some executed cell's output *contains* the block's
    distinctive content. Exact-position matching would be brittle — a cell prints
    a table whose width depends on the terminal — so this asks the weaker and more
    useful question: **did the notebook ever print this?**

    Which means a block that matches nothing is reported, and a reader can tell
    the difference between "the number changed" and "this claim is no longer in
    the notebook at all".
    """
    problems: list[str] = []
    source = source_path.read_text(encoding="utf-8")
    claims = OUTPUT_BLOCK.findall(source)
    if not claims:
        return problems

    printed = _normalise("\n".join(_printed(c) for c in _cells(executed)))

    # A block inside `<!-- check: skip -->` is not a claim about output — it is a
    # block that was deliberately not run, and its body is code. Skip those.
    skipped = set()
    for match in re.finditer(
        r"<!-- check: skip -->.*?^```[a-z]*\n(.*?)^```",
        source, re.MULTILINE | re.DOTALL,
    ):
        for line in _normalise(match.group(1)).split("\n"):
            if line.strip():
                skipped.add(line.strip())

    for claim in claims:
        wanted = _normalise(claim)
        lines = [ln for ln in wanted.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(ln in skipped for ln in lines):
            continue
        for line in lines:
            # An elision is not a claim: a lesson that shows the first and last
            # rows of a table writes `...` between them, and that line is not
            # something the notebook printed.
            if line.strip(".…- ") == "" or set(line) <= set(".…- |"):
                continue
            if line in skipped:
                continue
            if line not in printed:
                problems.append(
                    f"{notebook_path.name}: the notebook does not print\n"
                    f"    {line}\n"
                    f"  claimed in {source_path.name}. Re-run it and update "
                    "the block."
                )
    return problems


def main() -> int:
    from tools import build_notebooks

    # Probe the connection rather than reading an environment variable. The
    # seeder checks `POSTGRES_HOST` because it runs *in a container* where
    # compose sets it to `db`; run from a laptop the variable is legitimately
    # unset and `storage.postgres.schema.dsn()` defaults to 127.0.0.1. Copying
    # the seeder's check would have refused to run here, which is the one place
    # this gate actually gets used.
    try:
        from storage.postgres.schema import connect

        with connect() as probe:
            probe.execute("SELECT 1")
    except Exception as exc:
        print(f"  no database: {type(exc).__name__}. Seed one first:", file=sys.stderr)
        print("    docker compose up -d db && docker compose run --rm init-db",
              file=sys.stderr)
        print("    docker compose --profile demo run --rm seed", file=sys.stderr)
        return 2

    problems: list[str] = []

    built = build_notebooks.build()
    for path, notebook in built.items():
        encoded = json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"
        if not path.exists():
            problems.append(f"{path.relative_to(ROOT)} does not exist")
        elif path.read_text(encoding="utf-8") != encoded:
            problems.append(
                f"{path.relative_to(ROOT)} is out of step with its source "
                "(run: python -m tools.build_notebooks)"
            )

    if problems:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    # Deferred: importing nbformat and nbclient costs a second, and this module
    # is imported by the test suite purely for `_normalise` and the claim parser.
    import nbformat
    from nbclient import NotebookClient

    os.environ.setdefault("MPLBACKEND", "Agg")
    for path in sorted(built):
        notebook = nbformat.read(path, as_version=4)
        print(f"  running {path.name} ...", flush=True)
        try:
            NotebookClient(
                notebook, timeout=600, kernel_name="python3",
                allow_errors=False,
            ).execute()
        except Exception as exc:
            # Report and carry on to the next notebook, rather than crashing:
            # one broken notebook should not hide the state of the other ten.
            print(f"  {path.name} FAILED: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            problems.append(f"{path.name} does not execute")
            continue
        problems.extend(check_outputs(
            path, NB / "src" / (path.stem + ".md"), notebook,
        ))

    if problems:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"  {len(built)} notebook(s): built, executed, outputs agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
