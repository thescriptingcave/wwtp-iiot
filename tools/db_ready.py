"""Is the database up, and is it *usable*? One answer, and the reason if not.

    uv run python -m tools.db_ready          # prints yes, or the reason
    uv run python -m tools.db_ready --data   # also requires readings to exist

## Why this is a module and not a shell one-liner

It was one, for a long time:

    $(PY) -c "from storage.postgres.schema import connect; \
    c = connect(); c.execute('SELECT 1'); c.close()" >/dev/null 2>&1 \
    && echo yes || echo no

which answers `yes` or `no` and discards the reason. That is a reasonable thing to
poll sixty times in a row and a **useless** thing to be told after sixty seconds.
Three separate setup failures have presented as `the database did not become
reachable in 60s`:

- a seeder writing to the wrong database, so the right one stayed empty;
- a compose service missing an argument, so nothing was ever created;
- a volume initialised with a different `POSTGRES_PASSWORD` than `.env` now has.

None of them is diagnosable from a boolean, and in each case the real error was
available at the first attempt and thrown away. The attempt that failed first is
almost never the attempt somebody reads.

## What "usable" means, and why it is not `SELECT 1`

`SELECT 1` succeeds against a Postgres with no `reading` table, which is exactly
what `docker compose up -d db` gives you: `init-db` is the service that applies the
schema, and nothing makes the bare `db` service run it, because `db` is the one
service everything else depends on. So a target that asks only "is it reachable"
proceeds, and the failure surfaces sixty queries later as
`relation "reading" does not exist` — a symptom of a decision taken four steps
earlier.

This checks the table, and with `--data` the row count, so "no" means "not yet" and
the reason says which.
"""

from __future__ import annotations

import argparse
import sys

#: Human-readable causes, most likely first. Not exhaustive and not a FAQ: the point
#: is that the reader is told something they could act on rather than that they are
#: told "no" sixty times.
HINTS = (
    "the volume was created with a different POSTGRES_PASSWORD than .env now has.\n"
    "    'docker compose down -v' then 'make up' fixes it, and deletes the data.",
    "another database already holds the configured POSTGRES_PORT.",
    "POSTGRES_HOST or POSTGRES_PORT in .env do not match the mapping compose used.",
    "the schema has not been applied. 'docker compose up -d db init-db' does it.",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="db-ready",
        description="Exit 0 if the database is usable, and explain itself if not.",
    )
    parser.add_argument("--data", action="store_true",
                        help="also require at least one reading")
    args = parser.parse_args(argv)

    from storage.postgres.schema import connect  # noqa: PLC0415

    try:
        with connect() as conn:
            conn.execute("SELECT 1")
            rows = None
            if args.data:
                # The table's existence is the check, and the row count is the
                # course's requirement. One query, because a target that probes in
                # two steps can report the first step's answer after the second has
                # already failed.
                found = conn.execute("SELECT count(*) FROM reading").fetchone()
                rows = int(found[0]) if found else 0
    except Exception as exc:  # the point is to report *any* failure
        print(f"  postgres said: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  things worth checking, in order:", file=sys.stderr)
        for hint in HINTS:
            print(f"    - {hint}", file=sys.stderr)
        return 1

    if rows == 0:
        print("  the database is up and the schema is there, but it has no readings.",
              file=sys.stderr)
        print("  the SQL course is written against a seeded week. Run 'make up'",
              file=sys.stderr)
        return 1
    if rows:
        print(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
