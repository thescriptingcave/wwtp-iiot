"""Seed a week of plant history, so the dashboards and the SQL course have
something to bite on.

An empty database teaches nothing: every query returns no rows, every chart is
flat, and the most common conclusion is that the query is wrong. It usually is
the query. Seeding separates "my SQL is broken" from "there is no data", which is
the single most useful distinction available when learning a new dialect.

## What it produces, and why it is not just noise

The seed is not random. It replays the actual process model — the same
``softplc.process.plant.Plant`` the live PLC uses — at high speed, through real
fault scenarios. So the seeded history contains the things that make the SQL
course worth doing:

- a diurnal load pattern, because a flat influent flow makes every
  time-bucketing lesson look like it does nothing;
- a storm, because the interesting questions are all about what happens when a
  signal moves faster than its deadband;
- a blower trip and recovery, because the DO sag followed by ammonia
  breakthrough hours later is the one ordering the course keeps asking about;
- deadband gaps, because a dataset with no gaps teaches nothing about ``WHERE
  time`` and nothing about why a signal can be *absent* rather than zero.

## It writes through the same tables as the live gateway

Same columns, same quality convention, same ``signal`` rows. A seeded database
that took a shortcut is a database the course cannot be trusted to describe,
because the shortcut is exactly where the interesting differences are.

The one thing it does *differently* is the write itself: ``COPY`` rather than
``INSERT``. That is not a shortcut in the sense above, it is a different path for
a different job — the seeder is loading six hundred thousand rows at once and
``COPY`` is roughly an order of magnitude faster — and it comes with a
consequence worth stating. ``COPY`` cannot upsert, so a duplicate timestamp is an
error rather than a silent overwrite. Re-running the seeder into a window it has
already written therefore fails loudly, where the gateway re-sending a spool
batch upserts and carries on. Loud is right here.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from gateway.deadband import Deadband
from softplc.contract import Contract
from softplc.contract import contract as get_contract
from softplc.faults.engine import FaultEngine
from softplc.process.plant import Plant, PlantSnapshot

from storage.postgres.schema import (
    apply_refresh_policies,
    apply_retention,
    apply_schema,
    connect,
    dsn,
    refresh_aggregates,
    seed_metadata,
)
from storage.postgres.writer import Reading, make_copy_execute

log = logging.getLogger("storage.seed")

#: ``rows -> int``. A protocol rather than a client, so the replay policy is
#: testable without a database. Returns the number of rows accepted.
ExecuteFn = Callable[[list[tuple[Any, ...]]], int]

#: One second, because that is the raw tier's resolution. Recording faster would
#: invent precision the plant does not have — and the whole point of a time-series
#: database is that it does not let you.
SAMPLE_INTERVAL_S = 1.0

#: Instrument faults armed partway through a seeded run.
#:
#: Three faults, three different shapes, deliberately spread across the week:
#:
#: * ``do_sensor_drift`` — the process is fine and the *gauge* is wrong. The
#:   value is plausible and in range, so nothing but a model-based check catches
#:   it. This is `bad_instrument`'s own claim, and without it the dataset has no
#:   example of a fault that a threshold cannot see.
#: * ``effluent_tss_stuck`` — a frozen effluent reading. Frozen values are the
#:   ones that quietly poison a time-weighted average, because the deadband stops
#:   writing and a naive `AVG` treats the gap as "nothing happened".
#: * ``sensor_dead`` — the instrument stops answering. The only source of
#:   ``value IS NULL`` with ``quality = 2`` anywhere in this project, and the
#:   reason `sql/00-foundations/00-03` and `sql/04-expert/04-01` can teach a
#:   convention instead of only describing it.
#:
#: `at_fraction` is a fraction of the run, not a timestamp, so the spread
#: follows ``--days``.
DEFAULT_INSTRUMENT_FAULTS: list[dict[str, Any]] = [
    {"fault": "do_sensor_drift", "at_fraction": 0.30, "duration_s": 5400.0},
    {"fault": "effluent_tss_stuck", "at_fraction": 0.55, "duration_s": 7200.0},
    {"fault": "sensor_dead", "at_fraction": 0.78, "duration_s": 3600.0},
]

#: Rows per COPY. 20 000 keeps any single transaction comfortably in memory while
#: still being large enough that the round trip is not the bottleneck. The old
#: limit — 3 000 — existed because line protocol capped a request body; COPY has
#: no such limit, so the number is now chosen for throughput rather than by a
#: server rule nobody remembers.
BATCH_ROWS = 20_000

#: Failed batches in a row before the seeder gives up. One is noise, three is
#: a broken connection or a broken schema.
_MAX_CONSECUTIVE_FAILURES = 3


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Seeder:
    """Replays the plant model and writes the result.

    Takes an ``execute`` callable rather than a database client, so the *policy* —
    which scenario, which sampling rate, what gets deadbanded — is testable
    without one.
    """

    def __init__(self, contract: Contract, execute: ExecuteFn, *,
                 sample_interval_s: float = SAMPLE_INTERVAL_S,
                 speed: float = 600.0, storm_after_h: float | None = 2.0,
                 instrument_faults: list[dict[str, Any]] | None = None) -> None:
        self.c = contract
        self._execute = execute
        self.sample_interval_s = sample_interval_s
        self.speed = speed
        self.storm_after_h = storm_after_h
        #: Instrument faults to arm partway through the run. See where they are
        #: armed for why the dataset is worse without them.
        self.instrument_faults = list(instrument_faults or DEFAULT_INSTRUMENT_FAULTS)
        self.deadband = Deadband.from_contract(contract)
        self.rows: list[tuple[Any, ...]] = []
        self.written = 0
        self.offered = 0
        self._consecutive_failures = 0
        self.plant = Plant(c=contract)
        self.faults = FaultEngine(self.plant)

    # ─── one sample ───────────────────────────────────────────────────────────

    def _record(self, snapshot: PlantSnapshot, ts: float) -> None:
        for signal_id, value in snapshot.values.items():
            if signal_id not in self.c.signals:
                continue
            self.offered += 1
            quality = snapshot.quality.get(signal_id, 0)
            if not self.deadband.accept(signal_id, value, quality):
                continue
            self.rows.append(
                Reading(ts=ts, signal_id=signal_id, value=value,
                        quality=quality, source="seed").as_row()
            )

    def _flush(self) -> None:
        """Write the buffer. Rows are dropped from the buffer either way.

        A seeder is not the live path, so there is no spool behind it and a failed
        batch is logged and skipped rather than retried. That is a deliberate
        asymmetry with the gateway, and worth naming: the gateway's data is the
        only copy, the seeder's data can be regenerated by running it again.
        """
        if not self.rows:
            return
        try:
            count = self._execute(self.rows)
        except Exception as exc:  # one bad batch must not end a long run
            self._consecutive_failures += 1
            log.warning("seed batch of %d rows failed (%d in a row): %s",
                        len(self.rows), self._consecutive_failures, exc)
            count = 0
        else:
            self._consecutive_failures = 0
        self.written += count
        self.rows.clear()

        # Carry on through a transient failure — a restarted database, a
        # dropped connection — but stop on a systematic one. Seeding 0.25 days
        # and logging 62 identical failures teaches nothing and takes a minute;
        # the first error in the log is the one that matters, and ploughing on
        # buries it.
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            raise SystemExit(
                f"seed: {_MAX_CONSECUTIVE_FAILURES} consecutive failed batches. "
                "The first error above is the real one; aborting rather than "
                "repeating it a few thousand times."
            )

    # ─── the run ──────────────────────────────────────────────────────────────

    def run_days(self, days: float) -> int:
        """Replay ``days`` of plant time. Returns the number of points written.

        ``days`` is *simulated*. At the default speed a week takes about a minute
        of wall clock, which is slow enough to watch and fast enough not to
        abandon.
        """
        total_s = days * 86_400.0
        # The scan period is nominal; the sampling interval is what decides how
        # many points exist. The two are deliberately different — the PLC scans
        # at 50 Hz and the historian records at 1 Hz, and conflating them is how
        # a dataset ends up claiming 1 Hz resolution it does not have.
        dt = self.sample_interval_s
        sim_now = time.time()
        start = sim_now - total_s

        if self.storm_after_h is not None:
            # Armed relative to the *end* of the run, so the storm lands in the
            # most recent data and is therefore inside the default dashboard
            # window. A storm seven days old is in no dashboard window at all.
            #
            # ``arm`` takes an offset from the engine's own clock, not a
            # timestamp, so the offset is measured from zero rather than from the
            # wall-clock start - and ``now_s`` advances as ``step`` is called, so
            # the fault fires once the replay reaches that offset.
            self.faults.arm_scenario("wet_weather")
            for fault in self.faults.active:
                delay = total_s - self.storm_after_h * 3600.0
                fault.start_s = delay
                if fault.end_s is not None:
                    fault.end_s = delay + (fault.end_s - fault.start_s)

        if self.instrument_faults:
            # Instrument faults are the *point* of this dataset being teachable.
            # Without them every row is `quality = 0` and no `value` is ever
            # NULL, so the convention three SQL lessons teach — a broken
            # instrument is stored as `value = NULL` with `quality = 2` — has
            # nothing to teach from. The reader can read the rule and never see
            # it happen.
            #
            # Spread across the week rather than clustered, because a reader
            # sampling one arbitrary window has to be able to hit a bad reading
            # by accident. The offsets are fractions of the run, so they follow
            # `--days` rather than being pinned to a date.
            for spec in self.instrument_faults:
                at = total_s * spec["at_fraction"]
                armed = self.faults.arm(spec["fault"], at)
                armed.start_s = at
                # `end_s` is set **unconditionally**, not only when the spec
                # already had one. `do_sensor_drift` declares no `duration_s`,
                # so `arm()` leaves `end_s` as None and the fault then runs from
                # its offset to the end of the week — 1 176 readings of a
                # steadily drifting probe, which is why the first version of
                # this seeded a *third* of the dataset at `quality = 1` and the
                # "only the readings I trust" query in `sql/00-foundations/00-03`
                # came back empty. The lesson was teaching a filter that had
                # nothing left to filter.
                armed.end_s = at + spec["duration_s"]

        n = int(total_s / dt)
        for i in range(n):
            ts = start + i * dt
            snapshot = self.faults.step(dt)
            self._record(snapshot, ts)
            if len(self.rows) >= BATCH_ROWS:
                self._flush()
            if i % 20_000 == 0 and i:
                log.info("seeded %.1f%% of %s days (%d points)",
                         100.0 * i / n, days, self.written)
        self._flush()
        return self.written


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seed",
        description="Generate plant history for the dashboards and lessons.",
    )
    p.add_argument("--days", type=float, default=float(_env_int("SEED_DAYS", 7)))
    p.add_argument("--storm-after", type=float,
                   default=_env_float("DEMO_STORM_AT_HOURS", 2.0),
                   help="hours before the end of the run to arm the storm; "
                        "negative to skip")
    p.add_argument("--speed", type=float, default=600.0,
                   help="simulated seconds per real second")
    p.add_argument("--no-instrument-faults", action="store_true",
                   help="seed a week where every instrument is healthy; without "
                        "them the dataset cannot teach the quality convention")
    p.add_argument("--no-deadband", action="store_true",
                              help="record every sample; a much larger dataset")
    p.add_argument("--log-level", default="INFO")
    return p


def _seed_bounds() -> tuple[datetime, datetime] | None:
    """The bounds of everything now in ``reading``, or ``None`` if it is empty.

    Takes no arguments on purpose. It could take the seeder and the day count and
    compute the range, and those parameters were in the first version; the
    database already knows, and computing it twice is how the two copies drift.

    Read from the database rather than computed, because the seeder's clock and
    the database's clock are not the same thing and the aggregates are bucketed
    by the database. A range that is right by construction in one clock and wrong
    in the other produces buckets that are one interval out, which is the kind of
    bug that looks like a rounding error and is not.
    """
    from storage.postgres.schema import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT min(ts), max(ts) FROM reading")
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    # A bucket interval of margin at each end, so the first and last partially
    # covered buckets are included rather than clipped.
    return row[0] - timedelta(minutes=90), row[1] + timedelta(minutes=90)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )

    if not os.environ.get("POSTGRES_HOST"):
        log.error("POSTGRES_HOST is not set; there is nowhere to seed to")
        return 2

    contract = get_contract()
    dsn_str = dsn()
    execute = make_copy_execute(dsn_str)

    # Schema and metadata first. The `reading` table has a foreign key onto
    # `signal`, so history cannot be written before the contract is in place —
    # the seeder now depends on the same referential integrity as everything
    # else, and would have been impossible to write against the previous engine.
    apply_schema()
    log.info("metadata: %s", seed_metadata(contract))

    seeder = Seeder(
        contract, execute, speed=args.speed,
        storm_after_h=None if args.storm_after < 0 else args.storm_after,
        instrument_faults=[] if args.no_instrument_faults else None,
    )
    if args.no_deadband:
        seeder.deadband = Deadband({})

    started = time.perf_counter()
    written = seeder.run_days(args.days)
    offered = seeder.offered
    elapsed = time.perf_counter() - started

    log.info(
        "seeded %s days into %s: %d points written of %d offered "
        "(%.1f%% filtered) in %.1fs",
        args.days, os.environ.get("POSTGRES_DB", "wwtp"), written, offered,
        (100.0 * (1 - written / offered)) if offered else 0.0, elapsed,
    )

    # The continuous aggregates, explicitly, over the range just seeded.
    #
    # **A refresh policy cannot do this.** A policy looks backwards from *now*;
    # it has no idea a week of history was just inserted behind it. Without this
    # call a seeded week of 4 290 000 readings produces a *2-row* `reading_1h`,
    # every query in `sql/03-advanced/03-01_continuous_aggregates.md` returns
    # nothing, and nothing anywhere reports an error — the tables exist, the
    # columns are right, and they are empty.
    #
    # That is the whole bug, and it lived undetected through 384 unit tests and
    # 17 integration tests, because not one of them asserted that the rollups
    # contained anything. `tests/integration/test_postgres.py` now does.
    bounds = _seed_bounds()
    if bounds is not None:
        started_at, ended_at = bounds
        refresh_aggregates(started_at, ended_at)
        log.info("aggregates refreshed over %s .. %s", started_at, ended_at)

    # Retention policies last: attaching them before the data is in place would
    # make the policy's own refresh window the thing being measured.
    apply_retention(
        raw_days=_env_int("RETENTION_RAW_DAYS", 7),
        minute_days=_env_int("RETENTION_MINUTE_DAYS", 90),
    )

    # ...and the refresh policies, which is the other half. Without these the
    # aggregates go stale the moment the seeder finishes, and a live gateway
    # writing new readings would leave the rollups frozen at the seed boundary.
    apply_refresh_policies()

    # Summary, printed rather than only logged, because the number that matters
    # is not "how many points" but "how many of the interesting events are in
    # there" — and you cannot tell that from a row count.
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE quality <> 0),
                   min(ts), max(ts),
                   count(DISTINCT signal_id)
            FROM reading
            """
        )
        row = cur.fetchone()
        if row is None or not row[0]:
            total, bad, lo, hi, signals = 0, 0, None, None, 0
        else:
            total, bad, lo, hi, signals = row
        if not total or lo is None or hi is None:
            log.warning("no readings in the database - is the deadband "
                        "filtering everything?")
            return 0
        log.info(
            "database now holds %d readings across %d signals, %d flagged "
            "non-Good, spanning %s to %s",
            total, signals, bad, lo.isoformat(), hi.isoformat(),
        )

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
