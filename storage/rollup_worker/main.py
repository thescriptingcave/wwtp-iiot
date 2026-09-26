"""Entry point for the rollup worker.

Thin. Everything worth testing — which windows are closed, how a partial window
is treated, what the weighted average is — lives in ``rollup.py`` and is tested
without a database. What is here is the wiring: read the environment, build the
execute callable, loop.

The one decision worth stating is what happens when InfluxDB is unreachable. The
worker logs and counts, and tries again next tick. It does not exit. A worker
that exits on a transient error needs a supervisor, an alert and a runbook; a
worker that retries needs none of those, and the closed windows are still closed
when it comes back — that is what the catch-up window is for.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from typing import cast

from storage.influx.rollups_sql import ROLLUP_FIELDS, ROLLUP_TAGS
from storage.rollup_worker.rollup import TIERS, RollupWorker

log = logging.getLogger("storage.rollup_worker")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_executor(database: str, url: str, token: str,
                   org: str) -> Callable[[str, int, int], int]:
    """A callable that folds one window and returns how many points it wrote.

    Closes over the InfluxDB client so the worker's ``ExecuteFn`` protocol stays
    a three-argument function, which is what makes the scheduling policy testable
    with a lambda and no database at all.

    Delete-then-insert, per window. The delete is what makes re-running a window
    safe, and re-running a window is not a hypothetical: the worker retries after
    every failure and replays a catch-up window after every restart.
    """
    from influxdb_client import InfluxDBClient, WritePrecision

    from storage.influx.rollups_sql import rollup_line, statements_for

    client = InfluxDBClient(url=url, token=token, org=org, timeout=30_000)
    query_api = client.query_api()
    write_api = client.write_api()

    def execute(tier_name: str, start: int, end: int) -> int:
        for sql, params in statements_for(tier_name, start, end):
            if sql.lstrip().upper().startswith("DELETE"):
                query_api.query(sql, org=org, params=params)
                continue
            tables = query_api.query(sql, org=org, params=params)
            # FluxRecord is subscriptable and carries a ``values`` dict; it has no
            # ``get``, which is the sort of thing that type-checks as fine and
            # fails on the first real response.
            lines = [
                rollup_line(
                    measurement=str(row.values.get("_measurement", "")),
                    tags={**{t: str(row.values.get(t, "")) for t in ROLLUP_TAGS},
                           "source": "rollup"},
                    start_ms=int(row["time"]) * 1_000_000,
                    values={k: row.values.get(k) for k in ROLLUP_FIELDS},
                )
                for table in tables
                for row in table.records
            ]
            if lines:
                write_api.write(
                    bucket=database, org=org, record="\n".join(lines) + "\n",
                    # Cast for the same reason as storage/influx/writer.py: the SDK
                    # types this as an enum and ships a str alias.
                    write_precision=cast("WritePrecision", WritePrecision.US),
                )
            return len(lines)
        return 0

    return execute


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rollup",
        description="Fold raw samples into 1-minute and 1-hour buckets.",
    )
    p.add_argument("--interval", type=int,
                   default=_env_int("ROLLUP_INTERVAL_SECONDS", 60))
    p.add_argument("--keep-partial", action="store_true",
                   default=not _env_bool("ROLLUP_DISCARD_PARTIAL", True),
                   help="also write the in-progress window, for a live dashboard")
    p.add_argument("--iterations", type=int, default=None,
                   help="stop after N ticks; for smoke tests")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )

    url = os.environ.get("INFLUX_URL", "http://influxdb:8086")
    token = os.environ.get("INFLUX_TOKEN", "")
    if not token:
        log.error("INFLUX_TOKEN is not set; nothing can be written. Exiting "
                  "rather than looping against a database we cannot reach — "
                  "this worker's whole job is writing.")
        return 2

    database = os.environ.get("INFLUX_DATABASE", "wwtp")
    org = os.environ.get("INFLUX_ORG", "wwtp")
    execute = build_executor(database, url, token, org)

    worker = RollupWorker(execute, interval_s=args.interval,
                          discard_partial=not args.keep_partial)

    # threading.Event rather than an asyncio one: the worker's loop is a plain
    # sleep, and signal handlers can only set a flag - they cannot await.
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("rollup worker starting: tiers=%s interval=%ds partial=%s",
             [t["name"] for t in TIERS], args.interval, args.keep_partial)
    n = 0
    while not stop.is_set() and (args.iterations is None or n < args.iterations):
        plan = worker.tick()
        if not plan.is_empty():
            log.info("ticked: %s", worker.stats.as_dict())
        n += 1
        if args.iterations is not None and n >= args.iterations:
            break
        # Interruptible sleep, so a container stop is immediate rather than up to
        # one interval late. Compose sends SIGTERM and then waits.
        stop.wait(args.interval)
    log.info("rollup worker stopped after %d ticks: %s", n, worker.stats.as_dict())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
