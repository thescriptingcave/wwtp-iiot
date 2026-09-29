"""Build the analyst notebooks from their markdown sources.

    python -m tools.build_notebooks          # write the .ipynb files
    python -m tools.build_notebooks --check   # report drift

## Why notebooks are generated rather than authored in place

Because the prose is the thing that gets reviewed, and a `.ipynb` is a JSON file
with four hundred lines of escaped markdown in it. A reviewer wants to read the
argument; a git diff on a notebook shows one enormous line changed because a
comma moved in an output cell.

So the source of truth is `notebooks/src/*.md` — ordinary markdown with fenced
python — and this module turns it into a real `.ipynb`. **The same pattern the
SQL course uses**, where `sql/**/*.md` is authored and `sql/TablePlus/` is
generated, and for the same reason: the readable thing is the source and the
derived thing is checked.

## The fence that is a query, not code

Some fenced blocks in a lesson are SQL to show, or output to compare against, or
a shell command — none of which should be executed. They carry an info string:

    ```sql              -- shown, not run
    ```output           -- expected output, compared, not run
    ```bash             -- shown, not run
    ```python           -- run

An `output` block is **asserted against the live run**, which is the discipline
the SQL course already follows: a lesson that states a number must state the
number the database actually produced. `sql/00-foundations/00-03` drifted for
months because its expected output described a dataset that no longer existed.

Fence languages and what happens to each:

| fence | becomes | executed |
|---|---|---|
| `python` | code cell | yes |
| `sql`, `bash`, `output` | markdown cell, verbatim | no |
| anything else | markdown cell, verbatim | no |

## `check: skip`

A code cell preceded by `<!-- check: skip -->` becomes a markdown cell instead.
It is kept, visible, and not run — for a block that is deliberately wrong (the
lesson is about the mistake), or that needs a state the notebook does not have.
`sql/` uses the same marker for the same reason.

## ruff reads the notebook, not the source — so the source must already be clean

`ruff` lints `notebooks/*.ipynb` and not the markdown, because that is the file it
understands. The consequence is a loop worth naming: `ruff check --fix notebooks/`
edits the *generated* file, and the next `build_notebooks` overwrites the fix with
the source, and `check_notebooks` then reports the notebook as out of step.

So the source has to be written in the order ruff wants in the first place.
`storage` is classified third-party and `notebooks` first-party by ruff's own
detection, which is why the import block in notebook 02 reads:

    import psycopg
    from storage.postgres.schema import dsn

    from notebooks._style import apply_style

Fixing it in the notebook and not the source works until the next build. This is
the price of generating, and it is smaller than the price of a JSON diff.

**Cell `id` fields** are deliberately not emitted. `nbformat` warns about their
absence and offers `normalize()` to add them; adding them would put generated
identifiers in a file the gate byte-compares, so the warning is noise from a tool
being told something it does not need to know.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "notebooks" / "src"
OUT = ROOT / "notebooks"

#: Fences that are a code cell and get executed.
EXECUTED = {"python", "py", "python3"}

#: Fences shown verbatim. Everything not in `EXECUTED` is treated the same way,
#: so an unrecognised fence degrades to "shown" rather than to "executed", which
#: is the safe direction: the worst case is a block that does not run, not one
#: that runs something the reader did not intend.
VERBATIM = {"sql", "bash", "sh", "shell", "console", "output", "text", "yaml"}

FENCE = re.compile(r"^```([a-zA-Z0-9_+-]*)\s*$")
SKIP_MARKER = "<!-- check: skip -->"


def cell_id(kind: str, text: str) -> str:
    """A stable cell identifier, from the cell's own content.

    **This exists because Jupyter rewrites notebooks that have no `id`, and a
    notebook that Jupyter rewrites no longer matches its source.**

    `nbformat` warns about the missing field and hands you `normalize()`, which
    adds one. JupyterLab calls it on load, marks the document dirty, and writes
    the result back on save — so merely *opening* `01-meet-the-plant.ipynb` in
    JupyterLab was enough to make `make notebooks` report it out of step. A gate
    that fails because someone followed the instructions is worse than no gate.

    So the id is derived from the cell's text rather than generated at random:
    rebuilding from an unchanged source produces an unchanged file, and editing a
    cell changes its id, which is what a notebook diff wants anyway.

    nbformat requires `[a-zA-Z0-9-_]+`, 1-64 characters. The `c`-prefix keeps it
    from starting with a digit, and eight hex characters is far short of 64.
    """
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=4).hexdigest()
    return f"c-{kind}-{digest}"


def as_lines(text: str) -> list[str]:
    """`source` as nbformat writes it: a list of lines, each keeping its newline.

    A single joined string is valid nbformat and reads the same, but it is not
    what Jupyter saves, so it is drift the moment the file is opened.
    """
    return text.splitlines(keepends=True)

#: A fenced block with no language tag. In a notebook source this is ambiguous
#: and it is the one authoring mistake the gate cannot otherwise catch: the
#: builder cannot know whether the block is code to run or output being claimed,
#: and an output block that is not tagged is not a claim the checker can check.
#:
#: The first version of 02 wrote all of its expected output untagged, so
#: `tools/check_notebooks.py` found **zero** output blocks and passed. The gate was
#: green and checking nothing — which is worse than having no gate, because it
#: looked like verification.
#: An *opening* fenced block with no language tag. Opening means "not currently
#: inside a block" — a closing fence is also a bare ```` ``` ````, and matching
#: those was the second version of this bug: the checker reported every closing
#: fence in the file as an untagged opening, so a correctly tagged lesson still
#: failed the build.
UNTAGGED = re.compile(r"^```\s*$", re.MULTILINE)


def untagged_openings(text: str) -> list[int]:
    """Line numbers of opening fences with no language tag.

    Walks the fences in order and alternates open/closed, which is what a
    markdown parser does and what a bare regex cannot. Returns 1-based line
    numbers so the error message can point at one.
    """
    inside = False
    out: list[int] = []
    for number, line in enumerate(text.split("\n"), start=1):
        match = FENCE.match(line)
        if match is None:
            continue
        if inside:
            inside = False
            continue
        inside = True
        if not match.group(1):
            out.append(number)
    return out

#: The YAML front matter that becomes notebook metadata.
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _cells(markdown: str) -> list[dict[str, Any]]:
    """Split markdown into cells.

    Headings, prose, tables and lists outside a fence become markdown cells, so
    a notebook reads as a document rather than as a wall of markdown with code
    dropped in. Fenced blocks become code or markdown per the table above.
    """
    lines = markdown.split("\n")
    cells: list[dict[str, Any]] = []
    buffer: list[str] = []
    i = 0
    #: Set by a `check: skip` marker and consumed by the *next* fence.
    skip_next = False

    def flush() -> None:
        text = "\n".join(buffer).strip("\n")
        if text.strip():
            cells.append({
                "cell_type": "markdown",
                "id": cell_id("md", text),
                "metadata": {},
                "source": as_lines(text),
            })
        buffer.clear()

    while i < len(lines):
        line = lines[i]
        match = FENCE.match(line)
        if match is None:
            # The marker applies to the next fence, so record it and drop the
            # line rather than leaking it into a markdown cell.
            #
            # The first version consumed the marker and carried on without
            # recording anything, so the fence that followed was still treated as
            # a code cell and still ran. The marker was decorative. It has to be
            # *state*, or "skip" means "delete the comment".
            if line.strip() == SKIP_MARKER:
                skip_next = True
                i += 1
                continue
            buffer.append(line)
            i += 1
            continue

        language = match.group(1).lower()
        body: list[str] = []
        i += 1
        while i < len(lines) and not FENCE.match(lines[i]):
            body.append(lines[i])
            i += 1
        i += 1  # the closing fence
        flush()
        skipped, skip_next = skip_next, False
        text = "\n".join(body).strip("\n")
        if language in EXECUTED and not skipped:
            cells.append({
                "cell_type": "code",
                "id": cell_id("py", text),
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": as_lines(text),
            })
        else:
            label = f"```{language}\n{text}\n```"
            if skipped:
                label = "<!-- not executed: `check: skip` -->\n\n" + label
            cells.append({
                "cell_type": "markdown",
                "id": cell_id("md", label),
                "metadata": {},
                "source": as_lines(label),
            })
    flush()
    return cells


def _metadata() -> dict[str, Any]:
    """The kernel block.

    Takes nothing: the title and subtitle from the front matter belong in the
    notebook's *own* metadata, not in the kernel specification, and the first
    version threaded the front matter in here and then used none of it.
    """
    return {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3.13"},
    }


def build_one(source: Path) -> dict[str, Any]:
    """One notebook, as nbformat-shaped JSON.

    Raises on an untagged fence. The language tag is not decoration here: it is
    how a reader, the builder and the output checker all agree on whether a block
    is code or a claim, and a block that omits it is a block three of them have to
    guess about.
    """
    text = source.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    match = FRONT_MATTER.match(text)
    if match:
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip('"')
        text = text[match.end():]

    for line_no in untagged_openings(text):
        raise SystemExit(
            f"{source}:{line_no}: a fenced block with no language tag.\n"
            "  Use ```python to run it, or ```output to claim what it "
            "printed.\n"
            "  An untagged block cannot be told apart from the other, and an "
            "untagged `output` block is a claim nothing checks."
        )

    return {
        "cells": _cells(text),
        "metadata": {
            **_metadata(),
            "title": meta.get("title", source.stem),
            "subtitle": meta.get("subtitle", ""),
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def build() -> dict[Path, dict[str, Any]]:
    """Every source markdown, paired with the notebook it generates.

    The output is `notebooks/<name>.ipynb`, **not** alongside the source. The
    first version used `src.with_suffix(".ipynb")`, which keeps the directory
    and wrote the notebook into `notebooks/src/` — where it would have sat next
    to its own source and been picked up by the next `SRC.glob`, generating a
    notebook from a notebook.
    """
    return {
        OUT / (src.stem + ".ipynb"): build_one(src)
        for src in sorted(SRC.glob("*.md"))
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_notebooks")
    parser.add_argument("--check", action="store_true",
                        help="report drift instead of writing")
    args = parser.parse_args(argv)

    problems: list[str] = []
    for path, notebook in build().items():
        encoded = json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"
        if args.check:
            if not path.exists():
                problems.append(f"{path.relative_to(ROOT)} does not exist")
            elif path.read_text(encoding="utf-8") != encoded:
                problems.append(
                    f"{path.relative_to(ROOT)} is out of step with its source "
                    "(run: python -m tools.build_notebooks)"
                )
        else:
            path.write_text(encoded, encoding="utf-8")
            code_cells = sum(
                1 for c in notebook["cells"] if c["cell_type"] == "code"
            )
            print(f"  {path.name}: {len(notebook['cells'])} cells, "
                  f"{code_cells} executable")

    if problems:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if not args.check:
        print("  notebooks are in step with notebooks/src")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
