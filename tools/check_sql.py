"""Check every SQL query in ``sql/`` against a live InfluxDB 3.

    INFLUX_TOKEN_TEST=… uv run python tools/check_sql.py
    INFLUX_TOKEN_TEST=… uv run python tools/check_sql.py --verbose

A course of untested SQL is not a course. This extracts every ````sql` block from
``sql/**/*.md`` and runs it.

## Why it runs each query five times

Because "this query is broken" and "this database is having a bad minute" are
different claims, and on InfluxDB 3 Core they look identical from the outside: both
are an exception with a stack trace.

So each query is attempted five times and the outcomes are classified:

* **consistent parse error** → a real dialect violation. The course is wrong and
  the lesson needs fixing. This is the only class that fails the run.
* **consistent success** → fine.
* **anything in between** → *flaky*. Reported, not failed, because a course that
  cannot be checked is a course nobody checks. Every flaky query is a candidate for
  the InfluxDB 3 Enterprise recommendation in ``docs/LEARNING-LOG.md``.

That distinction is the whole point. An earlier version of the course was written
against ``INTERVAL '1 hour'`` and ``CASE WHEN``, neither of which this dialect has,
and **every one of those queries would have looked like a database problem** if the
tool had reported the first error it saw.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ATTEMPTS = 5
SQL_BLOCK = re.compile(r"```sql\n(.*?)```", re.S)


@dataclass
class Outcome:
    path: str
    index: int
    first_line: str
    ok: int
    parse_errors: int
    planning_errors: int
    other: int
    sample_error: str

    @property
    def verdict(self) -> str:
        if self.ok == ATTEMPTS:
            return "ok"
        if self.parse_errors == ATTEMPTS:
            return "DIALECT"
        if self.planning_errors == ATTEMPTS:
            return "planning"
        if self.ok:
            return "flaky"
        return "MIXED"


def run_once(sql: str, url: str, db: str, token: str) -> tuple[str, str]:
    """Return ``(kind, message)`` where kind is ok / parse / planning / other."""
    params = urllib.parse.urlencode({"q": sql, "db": db})
    req = urllib.request.Request(
        f"{url.rstrip('/')}/query?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.loads(resp.read())
        return "ok", ""
    except urllib.error.HTTPError as exc:
        body = " ".join(exc.read().decode("utf-8", "replace").split())
        if "parsing error" in body:
            return "parse", body
        if "error while planning" in body:
            return "planning", body
        return "other", body
    except Exception as exc:  # the message is the diagnosis
        return "other", str(exc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="check_sql", description=__doc__)
    p.add_argument("--root", default="sql")
    p.add_argument("--url", default=os.environ.get("INFLUX_TEST_URL",
                                                   "http://localhost:18181"))
    p.add_argument("--db", default=os.environ.get("INFLUX_TEST_DB", "wwtp"))
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    token = os.environ.get("INFLUX_TOKEN_TEST", "")
    if not token:
        print("INFLUX_TOKEN_TEST is not set; nothing to check against.",
              file=sys.stderr)
        return 2

    results: list[Outcome] = []
    for md in sorted(Path(args.root).rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for i, block in enumerate(SQL_BLOCK.findall(text)):
            sql = block.strip().rstrip(";")
            if not sql.upper().lstrip().startswith("SELECT"):
                continue
            # Blocks marked `-- invalid:` exist to be wrong. Checking them would
            # mean the course could never document its own dialect limits.
            if "-- invalid:" in sql:
                continue
            counts = {"ok": 0, "parse": 0, "planning": 0, "other": 0}
            sample = ""
            for _ in range(ATTEMPTS):
                kind, msg = run_once(sql, args.url, args.db, token)
                counts[kind] += 1
                sample = sample or msg
            results.append(Outcome(
                path=str(md), index=i,
                first_line=" ".join(sql.split())[:64],
                ok=counts["ok"], parse_errors=counts["parse"],
                planning_errors=counts["planning"], other=counts["other"],
                sample_error=sample,
            ))

    tally: dict[str, int] = {}
    for r in results:
        tally[r.verdict] = tally.get(r.verdict, 0) + 1
        if r.verdict in ("DIALECT", "MIXED") or args.verbose:
            print(f"{r.verdict:9} {r.path}:{r.index}  {r.first_line}")
            if r.sample_error:
                print(f"          {r.sample_error[:150]}")

    print(f"\n{len(results)} query/queries: "
          + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    if tally.get("DIALECT"):
        print("\nThe course uses syntax this dialect does not have. "
              "See sql/_shared/DIALECT.md.", file=sys.stderr)
        return 1
    if tally.get("flaky") or tally.get("planning") or tally.get("MIXED"):
        print("\nNote: some queries are intermittently failing. That is the "
              "InfluxDB 3 Core planner, not the course - see DIALECT.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
