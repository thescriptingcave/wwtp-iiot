"""The alarm engine's two entry points: audit the rules, or run them.

    # The coverage matrix. No database; about eight minutes for eleven faults.
    python -m alarms.main coverage

    # Run against a live historian, writing events. About one query per watched
    # signal per tick.
    python -m alarms.main serve --interval 30

## Two modes, and why they are in one program

They share the rule set, the engine and the detectors, and they answer the same
question from different directions: *is this alarm system any good?* One does it
by simulation (`coverage`), the other by observation (`serve`).

Keeping them together is what makes the first one trustworthy. A rule set that
has only ever run against seeded data is a claim; a rule set that also runs in
the stack is a measurement, and the difference is eleven minutes of work.

The reverse is also true and worth stating: **a coverage report with no running
engine behind it is a document, not a system.** An alarm rule that nothing
evaluates is configuration. That is why `serve` is not behind a feature flag.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from typing import Any

from alarms.coverage import audit
from alarms.engine import AlarmEngine
from alarms.rules import rules as all_rules
from alarms.scenarios import DEFAULT_HOURS, run_fault

log = logging.getLogger("alarms.main")


# ─── coverage ────────────────────────────────────────────────────────────────


def do_coverage(args: argparse.Namespace) -> int:
    """Run every fault through the plant and report what the rules did."""
    from softplc.contract import contract as get_contract
    from softplc.faults.engine import FaultEngine
    from softplc.process.plant import Plant

    c = get_contract()
    rule_set = all_rules(c)
    if args.only:
        wanted = set(args.only)
        rule_set = tuple(r for r in rule_set if r.id in wanted)
        if not rule_set:
            log.error("no rules match %s", sorted(wanted))
            return 2

    fault_ids = args.faults or sorted(FaultEngine(Plant(c=c)).faults)
    runs: dict[str, Any] = {}
    for fid in fault_ids:
        started = time.perf_counter()
        run = run_fault(fid, contract=c, hours=args.hours, rule_set=rule_set)
        runs[fid] = run
        raised = sorted({t.rule.id for t in run.transitions if t.kind == "raised"})
        log.info("%-26s %5.1fs  %d transitions  raised: %s",
                 fid, time.perf_counter() - started, len(run.transitions),
                 ", ".join(raised) or "nothing")

    engine = AlarmEngine(rule_set)
    report = audit(
        engine,
        {fid: run.transitions for fid, run in runs.items()},
    )

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print()
        print(report.render())

    # A blind spot is not a failure of the run — it is a finding about the alarm
    # system, and several are known and documented. An *unimplemented detector* is
    # a failure, because the contract names it and nothing is looking.
    if report.blind_spots and args.strict:
        log.error("blind spots: %s", ", ".join(report.blind_spots))
        return 1
    return 0


# ─── serve ───────────────────────────────────────────────────────────────────


def do_serve(args: argparse.Namespace) -> int:
    """Evaluate the rules against the live historian and write events."""
    from storage.postgres.schema import connect, dsn

    from alarms.base import Window  # noqa: F401  (documented import for clarity)
    from alarms.store import PostgresEventSink, PostgresWindowSource

    rule_set = all_rules()
    conn = connect(autocommit=True)
    source = PostgresWindowSource(conn, rule_set)
    sink = PostgresEventSink(conn)
    engine = AlarmEngine(rule_set, sink)

    running = True

    def stop(*_: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info(
        "alarm engine up: %d rules over %d signals, reading %s",
        len(rule_set), len(source.signals),
        dsn().split("dbname=")[-1],
    )
    tick = 0
    while running:
        now = time.time()
        try:
            windows = source.windows(now)
            transitions = engine.evaluate_all(windows)
            tick += 1
            if tick % max(1, int(args.report_every / max(args.interval, 1))) == 0:
                stale = source.coverage(now)
                quiet = sorted(
                    f"{s}={d / 3600:.1f}h"
                    for s, d in stale.items() if d > args.stale_seconds
                )
                log.info(
                    "tick=%d rules=%d active=%d events=%d %s",
                    tick, len(rule_set), len(engine.active_alarms()),
                    sink.written,
                    f"silent: {', '.join(quiet)}" if quiet else "all signals talking",
                )
            for t in transitions:
                if t.kind != "reaffirmed":
                    log.info("%s %s observed=%s", t.kind, t.rule.id,
                             t.verdict.observed)
        except Exception:  # the loop outlives any one tick
            log.exception("evaluation failed")
        time.sleep(args.interval)

    log.info("stopping: %d events written", sink.written)
    conn.close()
    return 0


# ─── cli ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alarms", description="Alarm rules, coverage, and the engine."
    )
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)

    cov = sub.add_parser(
        "coverage",
        help="run every fault through the plant and report what the rules did",
    )
    cov.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                     help="simulated hours per fault (default 8)")
    cov.add_argument("--faults", nargs="*", help="a subset of fault ids")
    cov.add_argument("--only", nargs="*", help="a subset of rule ids")
    cov.add_argument("--json", action="store_true", help="machine-readable")
    cov.add_argument("--strict", action="store_true",
                     help="exit non-zero if any fault is a blind spot")
    cov.set_defaults(func=do_coverage)

    srv = sub.add_parser("serve", help="evaluate against a live historian")
    srv.add_argument("--interval", type=float, default=30.0,
                     help="seconds between evaluations")
    srv.add_argument("--report-every", type=float, default=300.0)
    srv.add_argument("--stale-seconds", type=float, default=900.0,
                     help="a signal silent for longer than this is reported")
    srv.set_defaults(func=do_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
    )
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
