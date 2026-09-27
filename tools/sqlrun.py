"""Run one SQL file or query against the project's database.

    uv run python tools/sqlrun.py "SELECT 1"
    uv run python tools/sqlrun.py sql/01-beginner/01-04_time_buckets.sql
    uv run python tools/sqlrun.py --tsv "SELECT count(*) FROM reading"

Reads the same ``POSTGRES_*`` environment variables as everything else, so if
the seeder worked then this works.

## Why this exists at all

``psql`` is the better tool and you should use it when you have it. This is for
the case where you do not: a fresh checkout, a machine without Postgres
installed, or a container where ``psql`` is on the server and not on your side of
the wire. It is also the tool ``tools/check_sql.py`` uses, so anything this can
run, the course checker can run.

Table output rather than CSV, because the failure mode of learning SQL is
misreading a result and a table makes a wrong answer *look* wrong. A CSV of
`2.3e-41` and a CSV of `2.31` are very easy to tell apart at a glance and very
easy to tell apart wrongly in a spreadsheet.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from psycopg import Connection


def _connect() -> Connection:
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        sys.exit(
            "psycopg is not installed. It is in the `storage` extra:\n"
            "  uv sync --all-extras"
        )

    from storage.postgres.schema import dsn

    try:
        return psycopg.connect(dsn(), autocommit=True)
    except Exception as exc:
        host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
        port = os.environ.get("POSTGRES_PORT", "5432")
        sys.exit(
            f"cannot connect to Postgres at {host}:{port}\n  {exc}\n\n"
            "Start one, and give it a week of history:\n"
            "  docker compose up -d db\n"
            "  docker compose run --rm init-db\n"
            "  docker compose --profile demo run --rm seed"
        )


def _read(source: str) -> str:
    """A file's contents, or the string itself.

    The order of the checks is not incidental. `Path(x).exists()` raises
    `OSError: File name too long` rather than returning False, and a query longer
    than the filesystem's name limit is the *common* case for this tool — the
    awkward case is the path. So: cheap length check, then a guarded stat, then
    fall through to treating the argument as SQL.
    """
    if "\n" in source or len(source) > 4096:
        return source
    try:
        path = Path(source)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqlrun", description="Run SQL against the project's database."
    )
    parser.add_argument("sql", help="a query, or the path to a .sql file")
    parser.add_argument("--tsv", action="store_true", help="tabs, no grid")
    parser.add_argument("--timing", action="store_true",
                        help="print how long the statement took")
    args = parser.parse_args(argv)

    query = _read(args.sql).strip().rstrip(";")
    if not query:
        sys.exit("nothing to run")

    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            if args.timing:
                started = time.perf_counter()
                cur.execute(query)
                elapsed = (time.perf_counter() - started) * 1000
            else:
                cur.execute(query)
                elapsed = None

            if cur.description is None:
                print(f"OK{'' if elapsed is None else f'  ({elapsed:.1f} ms)'}")
                return 0

            rows = cur.fetchall()
            columns = [d.name for d in cur.description]

            if args.tsv:
                print("\t".join(columns))
                for row in rows:
                    print("\t".join("" if v is None else str(v) for v in row))
            else:
                widths = [
                    max(len(c), *(len(_fmt(r[i])) for r in rows))
                    if rows else len(c)
                    for i, c in enumerate(columns)
                ]
                print("  ".join(
                    c.ljust(w) for c, w in zip(columns, widths, strict=True)
                ))
                print("  ".join("-" * w for w in widths))
                for row in rows:
                    print("  ".join(
                        _fmt(v).ljust(w)
                        for v, w in zip(row, widths, strict=True)
                    ))
            print(f"\n{len(rows)} row{'s' if len(rows) != 1 else ''}"
                  f"{'' if elapsed is None else f'  ({elapsed:.1f} ms)'}")
    except Exception as exc:
        # A traceback is the wrong output for a teaching tool. `psql` prints the
        # server's message and a caret pointing at the offending character, and
        # the whole point of this tool is that someone learning SQL can read the
        # error without first learning to read a Python stack trace.
        diag = getattr(exc, "diag", None)
        message = getattr(diag, "message_primary", None) or str(exc)
        detail = getattr(diag, "message_detail", None)
        hint = getattr(diag, "message_hint", None)

        where = ""
        position = int(getattr(diag, "statement_position", 0) or 0) if diag else 0
        if position:
            caret = " " * (4 + _visual_width(query, position)) + "^"
            where = f"\n    {query}\n{caret}"

        print(f"ERROR: {message}", file=sys.stderr)
        if detail:
            print(f"DETAIL: {detail}", file=sys.stderr)
        if hint:
            print(f"HINT: {hint}", file=sys.stderr)
        if where:
            print(where, file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _visual_width(text: str, position: int) -> int:
    """Display columns up to ``position``, counting a tab as 4.

    Only used to line a caret up under a query, and a query with a tab in it
    would otherwise put the caret in the wrong place — which in a tool whose
    purpose is showing you *where* something is wrong is not a small mistake.
    """
    # `statement_position` arrives from psycopg as a str in some builds and an
    # int in others, and `text[:position]` raises TypeError on the former — which
    # turned a useful error message into a crash in the error handler, the worst
    # possible place for a bug.
    position = int(position)
    return sum(4 if ch == "\t" else 1 for ch in text[:position])


def _fmt(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        # Python's default `str` for a float is the shortest string that reads
        # back as the same float, which is exactly the right thing here.
        #
        # An earlier version used `%.10g`, chosen so that `2.3e-41` and `2.31`
        # would be distinguishable. It does distinguish those — and it also hides
        # that `avg()` over 59 000 rows returns 6391.15525417062 on one run and
        # 6391.155254170616 on the next, which is a fact the course now has a
        # lesson about. A tool that formats numbers for you is making a decision
        # about precision, and the right decision is to make none.
        return str(value)
    return str(value)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
