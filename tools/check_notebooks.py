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

#: Tracks that read a **database**, and so need the connectivity probe and can have
#: ```sql fences executed. A track that reads a CSV needs neither, and asserting one
#: would be worse than useless: the probe would fail with "no notebook database" on a
#: track that has no database to miss.
#:
#: This is the one place the two tracks genuinely differ in kind rather than in
#: degree, and it is why the parameterisation is a flag on the track rather than a
#: path. Passing a directory would have looked general and been wrong.
DATABACKED_TRACKS = {"notebooks"}

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
        elif out.get("output_type") in ("execute_result", "display_data"):
            data = out.get("data", {})
            for mime in ("text/markdown", "text/plain"):
                if mime in data:
                    parts.append("".join(data[mime]))
                    break
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


def _report(problems: list[str]) -> int:
    """Print the problems, and *also* raise them as GitHub annotations.

    **A CI failure nobody can read is a CI failure nobody will fix.** GitHub keeps
    a job's full log behind an authenticated endpoint, but it publishes workflow
    `::error::` annotations unauthenticated through the check-runs API. So a gate
    that prints to stderr is invisible to anything that is not a human with a
    browser and a token, and this repository's log endpoint returns 403 without
    one — which is how four consecutive runs of this job were red with nothing to
    go on but "exit code 1".

    The annotation is the same text, escaped per GitHub's rules: `%`, CR and LF
    become `%25`, `%0D` and `%0A`, because an unescaped newline ends the
    annotation and the rest of the message is silently discarded — a failure
    reported *almost* legibly, which is worse than one reported not at all.
    """
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
        flat = problem.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{flat}", file=sys.stderr)
    return 1


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


#: A number as prose writes it: `5,985`, `-21.4`, `1.78`, `64`.
NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

#: A bold span, which is how these notebooks mark a claim. Not every number in a
#: paragraph — narration says "three notebooks" and "a week" — but every number a
#: reader is meant to *take away* is bold, so that is the set worth verifying.
BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def _numbers(text: str) -> list[tuple[float, int]]:
    """`(value, decimals)` for every number in `text`, commas removed."""
    found: list[tuple[float, int]] = []
    for token in NUMBER.findall(text):
        clean = token.replace(",", "")
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        try:
            found.append((abs(float(clean)), decimals))
        except ValueError:
            continue
    return found


def _prose_bold_numbers(source: str) -> list[tuple[int, str, float, int]]:
    """`(line, span, value, decimals)` for each number inside bold prose.

    Code fences and HTML comments are removed first, and a paragraph carrying
    `<!-- num-ok -->` is skipped: some bold numbers are arithmetic on the reader's
    part ("three notebooks") and not readings from the database.
    """
    out: list[tuple[int, str, float, int]] = []
    without_code = re.sub(
        r"^```.*?^```\s*$", lambda m: "\n" * m.group(0).count("\n"),
        source, flags=re.MULTILINE | re.DOTALL,
    )
    offset = 0
    for paragraph in re.split(r"(\n\s*\n)", without_code):
        start_line = source[: source.find(paragraph, offset)].count("\n") + 1 \
            if paragraph.strip() else 0
        offset += len(paragraph)
        if not paragraph.strip() or "<!-- num-ok -->" in paragraph:
            continue
        cleaned = re.sub(r"<!--.*?-->", "", paragraph, flags=re.DOTALL)
        for span in BOLD.finditer(cleaned):
            body = span.group(1)
            line = start_line + cleaned[: span.start()].count("\n")
            for value, decimals in _numbers(body):
                out.append((line, body, value, decimals))
    return out


def check_prose_numbers(
    source_path: Path, executed: dict[str, Any],
) -> list[str]:
    """Every bold number in the prose must appear in something the notebook printed.

    The `output` fences are checked line by line, but prose is not, and prose is
    where the numbers go stale: a re-seed moved 86 claims in three notebooks and
    the *paragraphs* around them (`64 % removed`, `a 1.77x swing`, `peaking at
    08:00`) were wrong for a day before anyone looked. The pinned seed makes that
    much rarer; this makes the remainder a failure and not a surprise.

    Compared at the precision the prose states. `64 %` matches a printed `63.9`
    because that is what rounding to zero places gives, and `1.78x` matches
    `1.7791`. A percentage the notebook prints as a fraction (`0.639`) matches
    too, since prose rounds and converts freely and a checker that could not would
    be switched off within the week.
    """
    printed = [v for cell in _cells(executed) for v in _numbers(_printed(cell))]
    if not printed:
        return []

    def matches(value: float, decimals: int) -> bool:
        tolerance = 0.5 * 10 ** (-decimals) + 1e-9
        return any(
            abs(p - value) <= tolerance or abs(p * 100 - value) <= tolerance
            for p, _ in printed
        )

    source = source_path.read_text(encoding="utf-8")
    problems: list[str] = []
    for line, span, value, decimals in _prose_bold_numbers(source):
        if not matches(value, decimals):
            problems.append(
                f"{source_path.name}:{line}: the prose claims **{span.strip()}** "
                f"but no number the notebook printed rounds to {value:g}. "
                "Re-run it and correct the sentence, or mark the paragraph "
                "`<!-- num-ok -->` if it is not a reading."
            )
    return problems


SQL_FENCE = re.compile(
    r"(<!-- check: skip -->\s*\n)?^```sql\s*\n(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


def check_sql_fences(source_path: Path) -> list[str]:
    """Every ```sql block must run, against the notebook database, and return rows.

    A `sql` fence is shown and never executed by the notebook, so before this it was
    the one kind of claim in the series nothing checked: a query could rot when a
    column was renamed and the notebook would go on recommending it. They are run
    here in a read-only transaction that is always rolled back, so a block cannot
    change the data the other notebooks describe.

    Zero rows fails, as in the SQL course: a query that returns nothing is
    indistinguishable from a query that is wrong, and "nothing" reads as success.
    """
    from notebooks._data import connect

    source = source_path.read_text(encoding="utf-8")
    problems: list[str] = []
    with connect() as conn:
        for match in SQL_FENCE.finditer(source):
            if match.group(1):
                continue
            body = match.group(2)
            line = source[: match.start(2)].count("\n") + 1
            try:
                with conn.transaction(force_rollback=True):
                    conn.execute("SET TRANSACTION READ ONLY")
                    rows = conn.execute(body).fetchall()
            except Exception as exc:
                problems.append(
                    f"{source_path.name}:{line}: the sql block does not run: "
                    f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                )
                continue
            if not rows:
                problems.append(
                    f"{source_path.name}:{line}: the sql block returns no rows"
                )
    return problems


#: Things whose *value* is a property of the machine, the disk or the clock rather
#: than of the seed. See `check_portable_numbers` for why this list exists and for
#: the two ways the first version of it was too narrow.
NOT_PORTABLE = re.compile(
    r"""
      \bhypertable_size\b          # compressed chunks; write-order dependent
    | \bpg_total_relation_size\b
    | \bpg_relation_size\b
    | \bpg_size_pretty\b
    | \bpg_database_size\b
    | \btime\.(?:time|time_ns|monotonic|perf_counter)\b
    | \bdatetime\.datetime\.(?:now|today|utcnow)\b
    | \bpd\.Timestamp\.now\b
    | \bpd\.Timestamp\.utcnow\b
    | \bpd\.to_datetime\(['\"]now
    | \bdatetime\.date\.today\b
    | \bos\.getpid\b
    | \bplatform\.(?:uname|node|platform|machine)\b
    | \bsocket\.gethostname\b
    | \bsecrets\.
    | \bgetpass\.
    """,
    re.VERBOSE | re.IGNORECASE,
)


def check_portable_numbers(src_dir: Path | None = None) -> list[str]:
    """No notebook may state a number that is a fact about this machine.

    **The existing checks cannot catch this, and that is not a gap in them.** Every
    other gate compares a number against a run *on the same machine*: the seed
    fingerprint, the `output` blocks, the prose. A number derived from the disk or
    the clock passes all of them and is still wrong somewhere else.

    It was found the hard way, by CI. Notebook 03 printed a cost table beside its
    row counts:

    .. code-block:: text

        reading     4239284  1043  ...
        reading_1m   185455    57  ...

    and the row counts matched on the runner to the digit while the megabytes did
    not — `hypertable_size()` measures *compressed chunks*, and compression
    depends on the order the rows were written. The gate's own message said
    `the notebook does not print … Re-run it and update the block`, and following
    that instruction would have committed a number that is wrong on the laptop it
    was written on.

    So this is a rule about the *source*, checked before anything runs: a name that
    cannot produce a portable number cannot appear in a notebook that has to be
    true on a laptop and on a runner.

    ## What it does and does not catch

    It catches the call, not the effect. A notebook that hardcodes
    ``measured = 1043`` with no call in it passes this and fails the output check
    on a runner, which is the honest limit of a source-level rule. What it buys is
    that the *next* one of these is caught on a Mac, by the person who wrote it,
    rather than a week later on a runner.

    Two things that looked like portable numbers and are not, added after a second
    look:

    * ``pd.to_timedelta`` of a *duration the data spans* is fine — that is a
      property of the rows. This list is about the machine, not about the data, so
      nothing derived from a column is caught, and that is intended.
    * `uuid`, and anything else that is unique per run, is not in the list because
      nothing in the series generates one. Adding it speculatively would be a rule
      with no example behind it.

    The list is checked against the **python fences in the source markdown**, not
    the generated notebook, so a finding names a line an author can edit.

    Only code is scanned, and a `#` comment is not code. Both because that is
    where the rule belongs — a call is what produces the number — and because
    notebook 03 has to *name* `hypertable_size()` in a comment to explain why the
    column is gone. A rule that cannot be explained at the site it applies to
    produces a `# noqa` comment, and from there every line is one.
    """
    problems: list[str] = []
    for source in sorted((NB / "src" if src_dir is None else src_dir).glob("*.md")):
        in_python = False
        for number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.startswith("```python"):
                in_python = True
                continue
            if line.startswith("```"):
                in_python = False
                continue
            if not in_python or line.lstrip().startswith("#"):
                continue
            found = NOT_PORTABLE.search(line)
            if found:
                problems.append(
                    f"{os.path.relpath(source, ROOT)}:{number}: "
                    f"{found.group(0)} is a "
                    "property of the disk, the clock or the host, not of the "
                    "pinned seed. A number this series states has to be the same "
                    "on a laptop and on a runner."
                )
    return problems


def _database_is_the_pinned_one() -> bool:
    """Is the notebook database reachable, and is it the seed the prose describes?

    The seed fingerprint is the one gate here that is about *this* data rather than
    about the prose: the analyst notebooks' numbers are written for a pinned week, so
    a database that is merely reachable is not enough. A CSV-backed track has no
    fingerprint to check, and asking for one would be asking a question with no
    answer available — which is why the caller guards on `DATABACKED_TRACKS` rather
    than this returning something unhelpful.

    Extracted from `main` because the two checks are a preamble with a contract
    between them (print, explain, return the exit code) and inlining it twice in
    different shapes is how a gate ends up reporting a different problem on the way
    out than on the way in.
    """
    # Probe the connection rather than reading an environment variable. The seeder
    # checks `POSTGRES_HOST` because it runs *in a container* where compose sets it
    # to `db`; run from a laptop the variable is legitimately unset and
    # `storage.postgres.schema.dsn()` defaults to 127.0.0.1. Copying the seeder's
    # check would have refused to run here, which is the one place this gate is used.
    try:
        from notebooks._data import connect

        with connect() as probe:
            probe.execute("SELECT 1")
    except Exception as exc:
        print(f"  no notebook database: {type(exc).__name__}. Create and seed it:",
              file=sys.stderr)
        print("    python -m tools.notebook_data    (or: make notebooks-data)",
              file=sys.stderr)
        return False

    from tools.notebook_data import status

    current = status()
    print(f"  data: {current}")
    if "the pinned seed" not in current:
        print("  the notebooks' prose was written for the pinned seed and this is "
              "not it.\n  Re-seed: python -m tools.notebook_data", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse

    from tools import build_notebooks

    parser = argparse.ArgumentParser(prog="check_notebooks")
    parser.add_argument("--track", default="notebooks",
                        choices=sorted(build_notebooks.TRACKS),
                        help="which authored-notebook track to check "
                             "(default: notebooks, the analyst series)")
    parser.add_argument("notebooks", nargs="*",
                        help="name fragments selecting notebooks, e.g. `05 06`")
    args = parser.parse_args(argv)
    only = list(args.notebooks)
    src_dir, out_dir = build_notebooks.track_paths(args.track)

    if args.track in DATABACKED_TRACKS and not _database_is_the_pinned_one():
        return 2

    problems: list[str] = []

    built = build_notebooks.build(src_dir, out_dir)
    # A name fragment selects notebooks (`... 05 06`); drift is still checked for all.
    selected = {path: nb for path, nb in built.items()
                if not only or any(o in path.name for o in only)}
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
        return _report(problems)

    # Deferred: importing nbformat and nbclient costs a second, and this module
    # is imported by the test suite purely for `_normalise` and the claim parser.
    import nbformat
    from nbclient import NotebookClient

    os.environ.setdefault("MPLBACKEND", "Agg")
    for path in sorted(selected):
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
        source = src_dir / (path.stem + ".md")
        problems.extend(check_outputs(path, source, notebook))
        problems.extend(check_prose_numbers(source, notebook))
        if args.track in DATABACKED_TRACKS:
            problems.extend(check_sql_fences(source))

    problems.extend(check_portable_numbers(src_dir))

    if problems:
        return _report(problems)
    print(
        f"  {len(selected)} notebook(s): built, executed, "
        "outputs and prose numbers agree"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
