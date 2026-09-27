"""Every ```sql block in the course, executed against a real database.

    uv run python tools/check_sql.py
    uv run python tools/check_sql.py sql/01-beginner

## Why a checker at all

A course that queries tables which do not exist is worse than no course, because
the student cannot tell whether they misunderstood the query or the data. Every
block in every lesson is therefore run, and a block that fails fails the run.

## It cannot change your data

Every block runs inside a transaction that is rolled back afterwards.

This is not a nicety and it is not theoretical. The lessons contain `INSERT`
statements — [00-03](../00-foundations/00-03_quality_is_data.md) has you write
some bad-instrument readings, and they are *supposed* to be written — and the first
version of this tool ran each block three times in autocommit. So a lesson's
`INSERT` executed on run 1, collided with its own primary key on run 2, and was
reported as a failing query. Worse, the rows stayed: a tool whose job is to check
the course was quietly growing the database by one lesson's worth of readings on
every run.

The same hazard had already bitten the integration test suite, which truncated a
week of seeded history because two environment variables disagreed about which
database to use. Two different tools, the same failure mode: *a program that runs
SQL from a directory needs a rollback, not a promise.*

## The repetition, and what it was for

The previous version of this checker ran each query **five times**, because
"this query is broken" and "this database is having a bad minute" look identical
from the outside. That was a real problem, but it belonged to the previous
storage engine: InfluxDB 3's Core planner is non-deterministic, and nine of
twenty-three course queries failed intermittently depending on which plan it
picked. The tool was distinguishing parse errors from planning errors, which
turned out to be a distinction only that database needed.

Postgres is deterministic for a fixed data set, so three runs is now a
*regression* check rather than a flakiness classifier, and the distinction is gone.

Keeping the repeat anyway is deliberate, because the property it verifies is the
one that actually matters for a lesson:

> **A query whose answer changes between runs is not a lesson.**

A reader who runs a query twice and gets two different numbers has learned
something about the query and nothing about the plant, and they cannot tell which.
Time-dependent queries — anything using `now()` — are expected to vary in *row
count* as new data arrives, so the checker compares results only for queries
without a wall-clock dependency, and reports the rest as informational.

## What counts as a failure

* any error, on any run — a course query that cannot run is a course bug;
* a non-deterministic answer to a query that has no reason to be
  non-deterministic;
* a block that returns zero rows. This one is a judgement call and it is
  configurable, because a lesson *about* an empty result is legitimate. It is on
  by default: an empty result in a seeded database almost always means the lesson
  is querying a signal id that does not exist, and that is exactly the kind of
  error a student would otherwise spend an hour on.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

SQL_BLOCK = re.compile(r"```sql\n(.*?)```", re.S)

#: A block preceded by this comment is illustrative, not runnable.
#:
#: The course is full of fragments that are *meant* to be wrong or *meant* to be
#: partial: "this is the query that does not work", "the shape of a CTE", a DDL
#: excerpt. Checking those is not merely useless, it is actively harmful — it
#: turns the checker into a list of failures that are all expected, and a reader
#: who learns to ignore the output learns to ignore real failures too.
#:
#: ```sql
#: <!-- check: skip -->
#: WHERE value = NULL          -- always returns no rows
#: ```
SKIP_MARKER = "<!-- check: skip -->"

#: The generated directory, excluded from the scan.
#:
#: `sql/TablePlus/` is *produced from* these lessons by `tools/extract_sql.py`, so
#: scanning it makes the course scan its own output. Two things went wrong before
#: this exclusion existed, and both were found by a test rather than by reading:
#:
#: * `sql/TablePlus/README.md` became the course's 22nd file, so the count in the
#:   README and in `docs/TESTING.md` was wrong — and the README needed a
#:   `check: skip` marker on its own example fence so it would not be extracted
#:   as a **65th runnable query**, which is the more embarrassing version.
#: * A generator whose output is scanned by the thing it generates from is a cycle,
#:   and the fix is always to exclude the output, never to keep the two in step.
GENERATED_DIRS = {"TablePlus"}

#: A query containing any of these is wall-clock dependent, so its row count may
#: legitimately change between runs as new data arrives — or, for `EXPLAIN`, its
#: output text does, because it contains the execution time.
_VOLATILE = ("now()", "current_timestamp", "localtimestamp",
             "clock_timestamp", "explain")

#: Relative tolerance when comparing floats between runs.
#:
#: This number is not a fudge factor; it is a measured fact. `sum()` over
#: `DOUBLE PRECISION` is not associative — floating-point addition is not — so
#: the order rows are added in changes the last few bits of the result. Postgres
#: aggregates in parallel by default, chooses how many workers based on the table
#: size, and does not promise a fixed plan. So the same query on the same data
#: returns:
#:
#:     6391.15525417062
#:     6391.155254170616
#:     6391.155254170615
#:
#: — a relative difference of about 1e-15, sixteen significant digits in, on a
#: 59 000-row average.
#:
#: An earlier version of this tool compared results with `==` and reported that
#: as "non-deterministic", which is a true statement about the bits and a
#: useless thing to tell somebody learning SQL. It is reported now as float
#: jitter, separately, and only a difference *larger* than this is a failure.
JITTER_RTOL = 1e-9


#: How many blocks each file opted out of, for the summary line.
_skipped: dict[Path, int] = {}


def _blocks(path: Path) -> list[tuple[int, str]]:
    """Every runnable ```sql block, with the line its fence starts on.

    The line number is what makes a failure report useful. "One lesson has a bad
    query" is not actionable; "01-04 line 61" is.
    """
    text = path.read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    skipped = 0
    for match in SQL_BLOCK.finditer(text):
        line = text[: match.start()].count("\n") + 1
        # Look at the four lines above the fence: enough to allow a blank line
        # and a sentence between the marker and the block.
        preamble = "\n".join(text.splitlines()[max(0, line - 5):line - 1])
        if SKIP_MARKER in preamble:
            skipped += 1
            continue
        out.append((line, match.group(1).strip()))
    _skipped[path] = skipped
    return out


def _same_value(x: object, y: object) -> bool:
    """Whether two column values agree, up to float summation order."""
    if isinstance(x, float) or isinstance(y, float):
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            # A float against a string, a date, a Decimal. Different types are
            # never "the same up to rounding".
            return type(x) is type(y) and x == y
        return math.isclose(x, y, rel_tol=JITTER_RTOL, abs_tol=0.0)
    return x == y


def _same(a: list, b: list) -> bool:
    """Whether two result sets agree exactly, allowing only float jitter."""
    if len(a) != len(b):
        return False
    return all(
        len(ra) == len(rb)
        and all(_same_value(x, y) for x, y in zip(ra, rb, strict=True))
        for ra, rb in zip(a, b, strict=True)
    )


def _jittered(a: list, b: list) -> bool:
    """Whether `b` could be `a` re-ordered, with only float jitter.

    This is subtler than comparing rows in place, and the subtlety is the point.

    Floating-point jitter does not only change a value — it changes the *order* of
    a result. If two rows' means differ by 1e-12 and the query says
    `ORDER BY mean DESC`, which of them comes first is decided by a bit that
    parallel summation re-rolls on every run. Measured on this project: the
    `HAVING` query in 01-03 produced two distinct orderings across fifteen runs,
    with the same 31 rows in both.

    So the comparison has to be: do these two results contain the same rows, up to
    float jitter, in *some* order? If yes, the instability is float jitter and the
    answer is a note. If no, something real changed.
    """
    if len(a) != len(b):
        return False

    def key(row: tuple) -> tuple:
        return tuple(
            None if v is None else round(v, 9) if isinstance(v, float) else v
            for v in row
        )

    return sorted(map(key, a)) == sorted(map(key, b)) and not _same(a, b)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_sql", description="Run every sql block in the course."
    )
    parser.add_argument("paths", nargs="*", default=["sql"],
                        help="files or directories (default: sql)")
    parser.add_argument("--runs", type=int, default=3,
                        help="how many times to run each query (default 3)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="do not fail a query that returns no rows")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    try:
        import psycopg
    except ImportError:  # pragma: no cover
        sys.exit("psycopg is not installed: uv sync --all-extras")

    from storage.postgres.schema import dsn

    try:
        # Autocommit, so that the explicit BEGIN/ROLLBACK around each block is
        # the *only* transaction and there is nothing to commit by accident.
        conn = psycopg.connect(dsn(), autocommit=True)
    except Exception as exc:
        sys.exit(
            f"cannot connect: {exc}\n"
            "  docker compose up -d db && docker compose run --rm init-db\n"
            "  docker compose --profile demo run --rm seed"
        )

    files: list[Path] = []
    for target in args.paths:
        p = Path(target)
        found = sorted(p.rglob("*.md")) if p.is_dir() else [p]
        files.extend(
            f for f in found
            if not (set(f.relative_to(p).parts[:-1]) & GENERATED_DIRS)
        )

    total = failures = empty = skipped = 0
    with conn, conn.cursor() as cur:
        for path in files:
            rel = path.relative_to(Path.cwd()) if path.is_absolute() else path
            blocks = _blocks(path)
            skipped += _skipped.get(path, 0)
            for line, query in blocks:
                total += 1
                label = f"{rel}:{line}"
                volatile = any(v in query.lower() for v in _VOLATILE)
                seen: list[tuple] | None = None
                err = ""
                rows = 0
                jittered = False

                for run in range(args.runs):
                    cur.execute("BEGIN")
                    try:
                        cur.execute(query)
                    except Exception as exc:
                        err = str(exc).strip().splitlines()[0]
                        cur.execute("ROLLBACK")
                        break
                    if cur.description is None:
                        # INSERT/UPDATE/CREATE: a statement that returns no rows
                        # because it does not *have* rows, which is not the same
                        # as a SELECT that matched nothing.
                        result, rows = [], -1
                    else:
                        result = cur.fetchall()
                        rows = len(result)
                    cur.execute("ROLLBACK")
                    if not volatile:
                        if seen is None:
                            seen = result
                        elif not _same(result, seen):
                            if _jittered(result, seen):
                                jittered = True
                            else:
                                err = (f"non-deterministic: run {run + 1} "
                                       f"differs from run 1 "
                                       f"({rows} vs {len(seen)} rows)")
                                break

                if err:
                    failures += 1
                    first = err.splitlines()[0]
                    print(f"FAIL  {label}\n      {first}")
                    if args.verbose:
                        print("      " + query.replace("\n", "\n      ")[:600])
                elif jittered and args.verbose:
                    print(f"ok    {label}  ({rows} rows, float jitter "
                          f"<{JITTER_RTOL:g} — parallel summation order)")
                elif rows == 0 and not args.allow_empty:
                    empty += 1
                    print(f"EMPTY {label}")
                    if args.verbose:
                        print("      " + query.replace("\n", "\n      ")[:600])
                elif args.verbose:
                    print(f"ok    {label}  ({rows} rows"
                          f"{', volatile' if volatile else ''})")

    conn.close()
    print(f"\n{total} queries in {len(files)} files: "
          f"{total - failures - empty} ok, {failures} failed, {empty} empty"
          f"{f', {skipped} illustrative (skipped)' if skipped else ''}")
    if empty and not args.allow_empty:
        print("  (an empty result in a seeded database is usually a wrong "
              "signal id;\n   pass --allow-empty if the lesson means it)")
    return 1 if failures or empty else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
