"""Postgres + TimescaleDB: the schema, and the metadata that fills it.

Replaces ``storage/influx/`` (line protocol, tag columns) and
``storage/couchbase/`` (document store). The measurements behind that change are
in ``docs/DESIGN.md``; the short version is that the tag-column model was standing
in for a relation the database could have enforced, and the document store was
holding three key-sets of entirely homogeneous data.

Two things live here and they are deliberately different in kind:

* :func:`apply_schema` — runs ``schema.sql``. DDL, hand-written, reviewed.
* :func:`seed_metadata` — writes the contract's rows. Data, generated.

Keeping them apart matters because the drift risk moved. When readings lived in
InfluxDB, the danger was that nine tables' worth of generated DDL would stop
matching the encoder. Now the DDL is five stable tables, and the danger is that a
contract field has nowhere to go — which is what
``tests/test_postgres_schema.py`` checks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg import errors
from softplc.contract import Contract
from softplc.contract import contract as get_contract

log = logging.getLogger("storage.postgres")

#: How hard to try when a scheduled refresh policy holds the aggregate we want.
#: Ten attempts at 1, 2, 3 ... seconds is about a minute of waiting, which is far
#: longer than a scheduled job over a seeded week should hold the lock, and far
#: shorter than a seeder run. See `_refresh_with_retry`.
REFRESH_LOCK_ATTEMPTS = 10
REFRESH_LOCK_BACKOFF_S = 1.0

if TYPE_CHECKING:  # pragma: no cover
    from psycopg import Connection

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def dsn() -> str:
    """The connection string, from the environment.

    Assembled from parts rather than taken as one variable so that a container
    name, a port and a password can each be overridden independently — which is
    what running the tests against a throwaway database on a different port needs.
    """
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "wwtp")
    password = os.environ.get("POSTGRES_PASSWORD", "wwtp")
    db = os.environ.get("POSTGRES_DB", "wwtp")
    return f"host={host} port={port} user={user} password={password} dbname={db}"


def connect(*, autocommit: bool = False) -> Connection[Any]:
    """A connection. Thin wrapper so the rest of the package never imports psycopg.

    ``autocommit`` exists for one caller and one reason:
    ``CALL refresh_continuous_aggregate(...)`` refuses to run inside a transaction
    block, and a continuous aggregate cannot be refreshed any other way. psycopg
    opens a transaction implicitly on the first statement, so a caller that needs
    to refresh has to ask for autocommit at connect time rather than discover the
    problem later.
    """
    import psycopg

    return psycopg.connect(dsn(), autocommit=autocommit)


def apply_schema(conn: Connection | None = None) -> None:
    """Apply ``schema.sql``. Idempotent.

    Uses ``IF NOT EXISTS`` throughout rather than tracking a migration version,
    because there is exactly one schema file and the alternative would be a
    migration framework with one migration in it.
    """
    own = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        if own:
            conn.close()


def _signal_rows(c: Contract) -> list[tuple[Any, ...]]:
    """One row per signal, with its Modbus register attached if it has one.

    The register link is a ``LEFT JOIN`` in spirit and done as a dict here,
    because a register is a *view* of a signal rather than part of it, and five
    of the nineteen registers are not measurements at all.
    """
    by_signal = {r.signal: r for r in c.registers if r.signal}
    rows = []
    for sig in c.signals.values():
        reg = by_signal.get(sig.id)
        rows.append((
            sig.id,
            sig.equipment,          # nullable, and that is correct
            sig.area,
            sig.measurement,
            sig.field,
            sig.eu,
            sig.range_min,
            sig.range_max,
            sig.normal_low,
            sig.normal_high,
            sig.deadband,
            sig.deadband_mode,
            sig.sample_ms,
            sig.writable,
            reg.address if reg else None,
            reg.word_order if reg else None,
        ))
    return rows


def seed_metadata(c: Contract | None = None,
                  conn: Connection | None = None) -> dict[str, int]:
    """Write the contract into ``site``, ``equipment`` and ``signal``.

    Idempotent, and deliberately so: a seeder that appends becomes a second,
    disagreeing copy of the contract — and the copy nobody remembers to update.
    ``ON CONFLICT DO UPDATE`` rather than ``DO NOTHING`` because the contract is
    the source of truth and a changed range must reach the database.
    """
    c = c or get_contract()
    own = conn is None
    conn = conn or connect()
    counts = {"site": 0, "equipment": 0, "signal": 0}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO site (id, name, permit, design)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    permit = EXCLUDED.permit,
                    design = EXCLUDED.design
                """,
                (str(c.site.get("id", "")), str(c.site.get("name", "")),
                 _json(c.site.get("permit", {})), _json(c.site.get("design", {}))),
            )
            counts["site"] = cur.rowcount

            cur.executemany(
                """
                INSERT INTO equipment
                    (id, site_id, area, name, type, rated_kw, duty, fail_modes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    site_id = EXCLUDED.site_id, area = EXCLUDED.area,
                    name = EXCLUDED.name, type = EXCLUDED.type,
                    rated_kw = EXCLUDED.rated_kw, duty = EXCLUDED.duty,
                    fail_modes = EXCLUDED.fail_modes
                """,
                [
                    (eq.id, str(c.site.get("id", "")), eq.area, eq.name, eq.type,
                     eq.rated_kw, eq.duty, list(eq.fail_modes))
                    for eq in c.equipment.values()
                ],
            )
            counts["equipment"] = cur.rowcount

            cur.executemany(
                """
                INSERT INTO signal
                    (id, equipment_id, area, measurement, field, unit,
                     range_min, range_max, normal_low, normal_high,
                     deadband, deadband_mode, sample_ms, writable,
                     modbus_address, modbus_word_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    equipment_id = EXCLUDED.equipment_id, area = EXCLUDED.area,
                    measurement = EXCLUDED.measurement, field = EXCLUDED.field,
                    unit = EXCLUDED.unit, range_min = EXCLUDED.range_min,
                    range_max = EXCLUDED.range_max, normal_low = EXCLUDED.normal_low,
                    normal_high = EXCLUDED.normal_high, deadband = EXCLUDED.deadband,
                    deadband_mode = EXCLUDED.deadband_mode,
                    sample_ms = EXCLUDED.sample_ms, writable = EXCLUDED.writable,
                    modbus_address = EXCLUDED.modbus_address,
                    modbus_word_order = EXCLUDED.modbus_word_order
                """,
                _signal_rows(c),
            )
            counts["signal"] = cur.rowcount
        conn.commit()
    finally:
        if own:
            conn.close()
    return counts


def apply_retention(raw_days: int = 7, minute_days: int = 90) -> None:
    """Attach Timescale retention policies.

    Retention is the reason the tiers exist. Keeping every 1 Hz reading for two
    years is expensive *and* less useful, because a query over 63 million points
    is a query nobody runs.

    Applied here rather than in ``schema.sql`` because the intervals are
    operational parameters, and a schema file that hardcodes them is a schema file
    somebody has to edit to change a retention policy.
    """
    with connect() as conn, conn.cursor() as cur:
        # add_retention_policy raises if one already exists on the relation, and
        # "already configured" is the normal case on every restart after the
        # first.
        for relation, days in (("reading", raw_days), ("reading_1m", minute_days)):
            if days <= 0:
                continue
            try:
                cur.execute(
                    "SELECT add_retention_policy(%s, INTERVAL '1 day' * %s, "
                    "if_not_exists => TRUE)",
                    (relation, days),
                )
            except Exception as exc:  # a policy that already exists
                log.debug("retention on %s: %s", relation, exc)
        conn.commit()


def apply_refresh_policies(
    start_offset_h: int = 25,
    end_offset_h: int = 1,
    schedule_minutes: int = 5,
) -> None:
    """Attach the policies that keep the continuous aggregates populated.

    **This function did not exist, and its absence was invisible for a whole
    phase.** `schema.sql` created `reading_1m` and `reading_1h`; nothing ever
    refreshed them; and a seeded week of 4 290 000 readings produced a *2-row*
    `reading_1h`. Every query in `sql/03-advanced/03-01_continuous_aggregates.md`
    — a whole stage of the course, built on the claim that the rollups make
    long-range queries cheap — returned nothing.

    Nothing failed. The tables existed, the columns were right, the retention
    policies were attached, `psql` showed two tables full of the right shape, and
    every test passed. A continuous aggregate is a table and a definition; the
    definition does not fill the table. Something has to ask.

    The two offsets are the interesting part and neither is obvious:

    * ``start_offset_h`` is how far *back* the policy looks. It has to be at
      least the bucket size plus a refresh interval, or the policy creates a
      window that moves faster than it can fill and a bucket can be missed
      entirely. 25 h for a 1 h bucket is generous on purpose.
    * ``end_offset_h`` is how far back from *now* it stops, and it must be
      **positive**. An end offset of zero asks for the current, still-forming
      bucket, which a continuous aggregate cannot produce — a partial bucket is
      not a bucket. Setting it to zero is the single most common way to end up
      with an aggregate that is always one interval behind and nobody can say
      why.

    Both aggregates get the same offsets. They are separate policies over
    separate definitions, and TimescaleDB does not compose them.
    """
    with connect() as conn, conn.cursor() as cur:
        for view in ("reading_1m", "reading_1h"):
            try:
                cur.execute(
                    "SELECT add_continuous_aggregate_policy(%s, "
                    "  start_offset => INTERVAL '1 hour' * %s, "
                    "  end_offset   => INTERVAL '1 hour' * %s, "
                    "  schedule_interval => INTERVAL '1 minute' * %s, "
                    "  if_not_exists => TRUE)",
                    (view, start_offset_h, end_offset_h, schedule_minutes),
                )
                log.info(
                    "refresh policy on %s: %d h of history every %d min",
                    view, start_offset_h, schedule_minutes,
                )
            except Exception as exc:
                # TimescaleDB raises rather than no-op'ing on some versions even
                # with if_not_exists, and "already configured" is the normal
                # case on every restart after the first.
                log.debug("refresh policy on %s: %s", view, exc)
        conn.commit()


def refresh_aggregates(start: datetime, end: datetime) -> int:
    """Materialise both aggregates over an explicit range, now.

    **A refresh policy cannot do this job.** A policy looks backwards from
    *now*; it has no idea a week of history was just inserted behind it. So the
    seeder calls this after it writes, and it is the reason a seeded database has
    rollups at all rather than two empty tables.

    Note the order and the range: the two views are refreshed independently, and
    both are refreshed *from `reading`*, never from each other. Rolling 1 h up
    from `reading_1m` gives the mean of means, which is wrong wherever the
    deadband has left minutes with different numbers of readings — which is
    everywhere. `tests/integration/test_postgres.py` asserts the two tiers agree.

    `refresh_continuous_aggregate` cannot run inside a transaction, which is why
    this takes a connection rather than a cursor and why the schema module needs
    `autocommit=True` for exactly this.
    """
    # **Bind parameters do not work in a `CALL`.** `IndeterminateDatatype:
    # could not determine data type of parameter $2` — the same class of problem
    # as `CREATE ROLE ... PASSWORD %s`, met for the second time in this project.
    # A procedure call has no parse-time type for its arguments, so the value has
    # to be a literal in the statement text. `psycopg.sql` does the quoting and
    # escaping, so this is not string interpolation by hand.
    window = (start.isoformat(), end.isoformat())
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for view in ("reading_1m", "reading_1h"):
                _refresh_with_retry(cur, view, window)
        conn.close()
    return 2


def _refresh_with_retry(cur: Any, view: str, window: tuple[str, str]) -> None:
    """One `CALL`, retried if a scheduled policy is holding the same view.

    TimescaleDB runs a **background job** for each policy — `reading_1m` and
    `reading_1h` each have one, every five minutes. A manual `CALL` for the same
    aggregate over an overlapping window collides with it, and the collision is
    reported as::

        LockNotAvailable: could not refresh continuous aggregate "reading_1h"
        due to a concurrent refresh

    The seeder lost a whole run to this: it finished writing 4.3 million
    readings, then raised on the refresh, and the *data* was left in place with
    both rollups stale. So the reader saw a populated `reading` and empty
    `reading_1h` and had no way to tell that apart from a fresh database.

    The job holds the lock for the length of its own refresh, which is seconds
    rather than minutes, so a short bounded retry is the right shape. A timeout
    that is generous enough for the longest legitimate job and no longer keeps
    the failure loud for the case that is genuinely wrong.
    """
    from psycopg import sql

    attempts = 0
    while True:
        try:
            cur.execute(
                sql.SQL("CALL refresh_continuous_aggregate({}, {}, {})")
                .format(
                    sql.Literal(view),
                    sql.Literal(window[0]),
                    sql.Literal(window[1]),
                )
            )
            log.info("refreshed %s over %s .. %s", view, *window)
            return
        except errors.LockNotAvailable:
            attempts += 1
            if attempts > REFRESH_LOCK_ATTEMPTS:
                raise
            # Linear backoff, small. The competing job is already running and
            # will finish; hammering it just makes both slower.
            time.sleep(attempts * REFRESH_LOCK_BACKOFF_S)
            log.warning(
                "refresh of %s collided with the scheduled policy "
                "(attempt %d of %d); waiting %.1fs",
                view, attempts, REFRESH_LOCK_ATTEMPTS,
                attempts * REFRESH_LOCK_BACKOFF_S,
            )


def record_event(conn: Connection, *, kind: str, severity: str,
                  message: str, ts: datetime | None = None,
                  signal_id: str | None = None,
                  equipment_id: str | None = None, **detail: Any) -> None:
    """Write one event.

    ``severity`` is a word, not a number, and the column has a CHECK on it. It is
    queried by meaning — "everything critical this week" — and a numeric scale
    nobody remembers the order of gets queried backwards.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event (ts, kind, severity, message, signal_id,
                               equipment_id, detail)
            VALUES (COALESCE(%s, now()), %s, %s, %s, %s, %s, %s)
            """,
            (ts, kind, severity, message, signal_id, equipment_id, _json(detail)),
        )
    conn.commit()


def _json(value: Any) -> str:
    """JSONB parameters take a string; psycopg would rather not guess at jsonb."""
    return json.dumps(value, default=str)


__all__ = [
    "apply_retention",
    "apply_schema",
    "connect",
    "dsn",
    "record_event",
    "seed_metadata",
]


def main(argv: list[str] | None = None) -> int:
    """``python -m storage.postgres.schema`` — make the database match the contract.

    Three steps, in this order, and the order is the whole job:

    1. ``schema.sql`` — the tables, the hypertable, the continuous aggregates.
    2. ``seed_metadata`` — the contract's 1 site, 22 equipment and 57 signals.
    3. ``apply_retention`` — the policies, from the environment.

    Metadata before retention because retention policies are attached to
    relations, and attaching a policy to a relation that does not exist is an
    error. Metadata before readings because ``reading.signal_id`` is a foreign
    key, so history cannot be written until the contract is in place — a
    constraint the previous storage engine had no way to express.
    """
    import logging

    parser = argparse.ArgumentParser(
        prog="postgres-schema",
        description="Apply the schema and load the contract into Postgres.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )

    apply_schema()
    log.info("schema applied")
    log.info("metadata: %s", seed_metadata())
    apply_retention(
        raw_days=int(os.environ.get("RETENTION_RAW_DAYS", "7")),
        minute_days=int(os.environ.get("RETENTION_MINUTE_DAYS", "90")),
    )
    log.info("retention policies attached")

    # Roles last, and separately from the DDL. They are cluster state rather than
    # schema state, they are not restored by a schema-only dump, and a migration
    # that creates a table and forgets to grant on it produces a runtime
    # permission error rather than a schema error. See `storage/postgres/roles.py`
    # and `docs/SECURITY.md` for why they exist at all.
    from storage.postgres.roles import apply as apply_roles
    from storage.postgres.roles import check as check_roles

    apply_roles()
    for role in check_roles():
        if not role.ok:
            log.error(
                "role %s is not as declared: missing=%s unexpected=%s",
                role.name, role.missing, role.unexpected,
            )
        else:
            log.info("role %s ok", role.name)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
