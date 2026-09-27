"""Run a SQL file against InfluxDB 3 and print the rows.

    uv run python tools/sqlrun.py sql/01-beginner/examples/01-04_hourly.sql
    uv run python tools/sqlrun.py --stdin < query.sql
    uv run python tools/sqlrun.py --url http://localhost:18181 --db wwtp file.sql

Exists because a course of untested SQL is not a course. Every query in ``sql/``
is executed against a real server by ``tools/check_sql.py``; this is the tool that
does it, and it is also the fastest way to try a query by hand.

Deliberately thin: a request, some decoding, some printing. The interesting
question when a query fails is always "what did the *server* say", so the error is
printed in full and never swallowed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def query(sql: str, url: str, db: str, token: str, timeout: float = 30.0
          ) -> list[dict[str, Any]]:
    """Run one statement. Returns rows as dicts.

    Uses the HTTP endpoint rather than ``influxdb_client`` because that SDK speaks
    InfluxDB 2.x and posts to ``/api/v2/query``, which InfluxDB 3 does not serve.
    It answers 404 with a message about the endpoint, so it looks like a wrong URL
    rather than a wrong client.
    """
    params = urllib.parse.urlencode({"q": sql.strip().rstrip(";"), "db": db})
    req = urllib.request.Request(
        f"{url.rstrip('/')}/query?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    rows: list[dict[str, Any]] = []
    for result in payload.get("results", []):
        if result.get("error"):
            raise RuntimeError(result["error"])
        for series in result.get("series", []):
            columns = series.get("columns", [])
            for values in series.get("values", []):
                rows.append(dict(zip(columns, values, strict=True)))
    return rows


def render(rows: list[dict[str, Any]], limit: int = 40) -> str:
    """Format rows as a table. Empty result says so explicitly."""
    if not rows:
        return "(0 rows)"
    shown = rows[:limit]
    columns = list(shown[0])
    widths = {
        c: min(max(len(c), *(len(_cell(r.get(c))) for r in shown)), 28)
        for c in columns
    }
    out = ["  ".join(c.ljust(widths[c]) for c in columns),
           "  ".join("-" * widths[c] for c in columns)]
    for row in shown:
        out.append("  ".join(_cell(row.get(c)).ljust(widths[c]) for c in columns))
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more row(s)")
    out.append(f"({len(rows)} row(s))")
    return "\n".join(out)


def _read(name: str) -> str:
    return Path(name).read_text(encoding="utf-8")


def _cell(value: Any) -> str:
    if value is None:
        # Not an empty string. A NULL and a zero are different facts and a table
        # that prints them the same is a table that lies.
        return "NULL"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sqlrun", description=__doc__)
    p.add_argument("file", nargs="?", help="a .sql file; omit with --stdin")
    p.add_argument("--stdin", action="store_true", help="read the query from stdin")
    p.add_argument("--url", default=os.environ.get("INFLUX_URL_TEST",
                                                   "http://localhost:18181"))
    p.add_argument("--db", default=os.environ.get("INFLUX_DB_TEST", "wwtp"))
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--token", default=os.environ.get("INFLUX_TOKEN_TEST", ""))
    args = p.parse_args(argv)

    if not args.token:
        print("no token: set INFLUX_TOKEN_TEST", file=sys.stderr)
        return 2
    if args.stdin:
        sql = sys.stdin.read()
        source = "<stdin>"
    elif args.file:
        sql = _read(args.file)
        source = args.file
    else:
        p.error("give a file or --stdin")

    try:
        rows = query(sql, args.url, args.db, args.token)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} from {args.url}", file=sys.stderr)
        print(exc.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    except Exception as exc:  # the server's message is the point
        print(f"{source}: {exc}", file=sys.stderr)
        return 1

    print(f"-- {source}")
    print(render(rows, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
