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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from softplc.contract import Contract
from softplc.contract import contract as get_contract

log = logging.getLogger("storage.postgres")

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
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
