"""Rewrite a notebook's claimed `output` blocks from a live run.

    python -m tools.update_notebook_outputs notebooks/src/01-meet-the-plant.md

## What this is for, and what it is not

The `output` fences in `notebooks/src/*.md` are claims. `tools/check_notebooks.py`
verifies them against a live run and fails when one disagrees — which is the whole
point, and it works.

**This is the other half of that:** when a re-seed moves the numbers, it proposes
what the run *actually* printed so the claims can be brought back into step.

## The rule that makes it safe or useless

It replaces **only** the contents of `output` fences. It never touches:

* a code cell, so the query that produced the number is unchanged and the number
  still means what it meant;
* the prose between blocks, which is where the argument lives and which this tool
  cannot check at all;
* a structural claim, because it does not know which claims are structural.

That last one is the danger. `thirteen signals reported once in a week` is a
property of the generator and must be re-verified by reading. `4,287,657 readings`
is a property of one seeding and can be replaced mechanically. A tool cannot tell
them apart, so **every diff this writes must be read.** It is a diff generator,
not a corrector, and it says so in its output.

The 86 failures after a clean re-seed were mostly this second kind and a few of
the first. Which is why the output is verbose: a number that moves is normal, a
*count* that moves is a finding.

## Why not `--fix` on the checker

Because the checker runs every notebook and reports every problem at once, so a
`--fix` on it would rewrite notebook 05 from notebook 01's failure. It would also
be silent. This writes a diff to stdout and changes nothing on disk without
`--write`.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _printed_blocks(notebook_path: Path) -> list[str]:
    """Each code cell's output as a string, in cell order — one entry per cell,
    empty string included so the indices line up with the markdown's fences.

    The empty entries are deliberate. Skipping them would shift every later index,
    and pairing a claim with the wrong cell's output would write a correct number
    under the wrong heading — which is worse than not writing at all, because the
    gate would then pass on a claim that has stopped meaning what it says.
    """
    # pandas warns on every `read_sql` against a DBAPI connection, and nbclient
    # routes the warning to stderr together with the offending source line. Those
    # two lines are run artefacts, not output, and `_strip_noise` was having to
    # guess which lines they were. Suppressing the warning is exact; guessing is
    # not, and a guess that puts a temp path in a teaching notebook is worse than
    # leaving the block alone.
    import warnings  # noqa: PLC0415

    import nbformat  # noqa: PLC0415
    from nbclient import NotebookClient  # noqa: PLC0415

    warnings.filterwarnings("ignore")

    notebook = nbformat.read(notebook_path, as_version=4)
    NotebookClient(
        notebook, timeout=900, kernel_name="python3", allow_errors=False,
    ).execute()

    blocks: list[str] = []
    for cell in notebook.cells:
        if cell.cell_type != "code":
            continue
        parts: list[str] = []
        for out in cell.get("outputs", []):
            if out.get("output_type") == "stream":
                parts.append("".join(out.get("text", [])))
            elif out.get("output_type") == "execute_result":
                parts.append("".join(out.get("data", {}).get("text/plain", [])))
        blocks.append(_strip_noise("".join(parts)))
    return blocks


def _strip_noise(text: str) -> str:
    """Drop warnings and the source line that triggered them.

    pandas warns on every `read_sql` against a DBAPI connection, and nbclient
    routes that warning to stderr with the cell's temp-file path in it. Both are
    noise from the run, not output the notebook produced, and pasting a temp path
    into a teaching notebook would be worse than leaving the block alone.
    """
    kept: list[str] = []
    for line in text.split("\n"):
        if "UserWarning" in line or "Warning:" in line:
            continue
        if "/ipykernel_" in line:
            continue
        if line.strip().startswith(("pd.read_sql", "c.execute")):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def _fences(source: str) -> list[tuple[str, int, str, bool]]:
    """Every fence as ``(language, line_index, body, skipped)``.

    `skipped` is true for a fence preceded by `<!-- check: skip -->`. Such a fence
    is **not a code cell** — the builder renders it as markdown — so it must not be
    counted when pairing claims with cells. The first version did count it, which
    shifted every later pairing by one: each `output` block was overwritten with the
    *previous* cell's output, the skipped block itself became an `output` fence
    containing `df.dropna()`, and `check_notebooks` passed anyway, because each
    pasted line did appear *somewhere* in the run.
    """
    from tools.build_notebooks import FENCE, SKIP_MARKER  # noqa: PLC0415

    out: list[tuple[str, int, str, bool]] = []
    lines = source.split("\n")
    i = 0
    skip_next = False
    while i < len(lines):
        if lines[i].strip() == SKIP_MARKER:
            skip_next = True
            i += 1
            continue
        match = FENCE.match(lines[i])
        if match is None:
            i += 1
            continue
        language = match.group(1).lower()
        body: list[str] = []
        start = i
        i += 1
        while i < len(lines) and FENCE.match(lines[i]) is None:
            body.append(lines[i])
            i += 1
        i += 1
        out.append((language, start, "\n".join(body).strip("\n"), skip_next))
        skip_next = False
    return out


def _claim_positions(source: str) -> list[tuple[int, int, int, str]]:
    """``(fence_line, end_line, code_cell_index, claimed_body)`` per output fence.

    Paired to a code cell by **adjacency**: an `output` fence belongs to the
    `python` fence immediately before it, allowing only blank lines between.

    The first version of this paired by best line-overlap score, which cannot
    work: a claim and the run that produced it differ *by the number*, so their
    line sets do not intersect at all and every block scored zero. It also matched
    one block confidently and to the wrong block. Adjacency cannot make that
    mistake, and where it cannot be established it reports rather than guesses.
    """
    fences = _fences(source)
    claims: list[tuple[int, int, int, str]] = []
    last_python_index: int | None = None
    code_cells = 0
    for language, line_no, body, skipped in fences:
        if language in ("python", "py", "python3"):
            if skipped:
                continue
            code_cells += 1
            last_python_index = code_cells
        elif language == "output":
            claims.append((
                line_no, line_no + len(body.split("\n")) + 2,
                last_python_index if last_python_index is not None else -1, body,
            ))
    return claims


def _claims(source: str) -> list[tuple[int, str]]:
    """Line index and body of every ```output fence."""
    from tools.check_notebooks import OUTPUT_BLOCK  # noqa: PLC0415

    return [
        (source[: m.start()].count("\n"), m.group(1).strip("\n"))
        for m in OUTPUT_BLOCK.finditer(source)
    ]





def report(source_path: Path, *, write: bool) -> int:
    from tools.check_notebooks import _normalise  # noqa: PLC0415

    notebook = ROOT / "notebooks" / f"{source_path.stem}.ipynb"
    source = source_path.read_text(encoding="utf-8")
    printed = _printed_blocks(notebook)
    claims = _claim_positions(source)

    replacements: list[tuple[int, int, str]] = []
    unpaired = 0
    for fence_line, end_line, cell_index, claimed in claims:
        if cell_index < 1 or cell_index > len(printed):
            unpaired += 1
            continue
        actual = printed[cell_index - 1]
        if not actual:
            unpaired += 1
            continue
        if _normalise(actual) == _normalise(claimed):
            continue
        replacements.append((fence_line, end_line, actual))

    for position, (fence_line, _end, actual) in enumerate(replacements, start=1):
        claimed = next(c[3] for c in claims if c[0] == fence_line)
        print(f"\n── claim {position} of {len(replacements)}, "
              f"```output fence at line {fence_line + 1}")
        for line in difflib.unified_diff(
            claimed.split("\n"), actual.split("\n"),
            fromfile="claimed", tofile="printed", lineterm="", n=0,
        ):
            print(f"   {line}")

    print(f"\n{len(claims)} claims, {len(replacements)} would change, "
          f"{unpaired} unpaired")
    print("A *count* that moves is a finding. A *mean* that moves is a re-seed.\n"
          "Read every line above before writing: this cannot tell them apart.")

    if not write:
        print("dry run — nothing written. Re-run with --write.")
        return 0

    lines = source.split("\n")
    for start, end, new in sorted(replacements, reverse=True):
        lines[start:end] = ["```output", new, "```"]
    source_path.write_text("\n".join(lines), encoding="utf-8")
    print("written. Now read the prose around each block.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="update_notebook_outputs")
    parser.add_argument("source", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    return report(args.source.resolve(), write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
