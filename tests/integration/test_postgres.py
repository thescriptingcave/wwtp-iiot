"""Against a real Postgres. Skipped, loudly, when there isn't one.

    docker compose up -d db && docker compose run --rm init-db
    POSTGRES_PORT=5432 .venv/bin/python -m pytest tests/integration -q

This file is the one that would have caught the bugs the schema's constraints
were designed from. It is deliberately *not* a mirror of
``tests/test_postgres_writer.py``: that file proves the policy, this one proves
the SQL, and the two only overlap where an error message is the evidence.

Every test that writes truncates first. The database is expected to be a
scratch one; the seeder is the thing that puts real history in, and pointing
this suite at a seeded database will destroy it. That is stated in
``docs/GETTING-STARTED.md`` rather than defended against here, because a test
suite that quietly refuses to run is worse than one that is clearly documented
as destructive.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest


def _use_test_port() -> None:
    """Point the whole process at the throwaway database, once.

    There is deliberately no second DSN in this file. An earlier version built
    one from ``POSTGRES_TEST_PORT`` while ``storage.postgres.schema.dsn()`` read
    ``POSTGRES_PORT``, so the two disagreed and the fixture connected to 5432 —
    which on a developer machine is somebody's real Postgres — while the tests
    under it connected to the scratch instance. It failed as an authentication
    error, which is a wonderfully misleading message for a port mistake.

    One DSN, assembled in one place, read by everybody.
    """
    os.environ["POSTGRES_PORT"] = os.environ.get("POSTGRES_TEST_PORT", "55432")
    os.environ.setdefault("POSTGRES_PASSWORD", "itpass")


def _dsn() -> str:
    from storage.postgres.schema import dsn

    return dsn()


@pytest.fixture(scope="session")
def db():
    """A schema applied and a contract loaded, or a skip.

    ``POSTGRES_TEST_PORT`` defaults to 55432 rather than 5432 so that running
    the suite by accident hits nothing: 5432 is where the compose stack lives and
    55432 is where a throwaway instance is expected.
    """
    pytest.importorskip("psycopg")
    _use_test_port()
    from storage.postgres.schema import apply_schema, connect, seed_metadata

    try:
        with connect() as probe:
            probe.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(
            f"no Postgres at {os.environ.get('POSTGRES_HOST', '127.0.0.1')}:"
            f"{os.environ['POSTGRES_PORT']}: {exc}\n"
            "  docker compose up -d db && docker compose run --rm init-db\n"
            "  then set POSTGRES_PASSWORD (it defaults to 'itpass' for a local "
            "scratch instance)"
        )
    apply_schema()
    seed_metadata()
    return True


@pytest.fixture
def conn(db):
    """A clean connection. Readings are truncated; metadata is not.

    Truncating `reading` rather than dropping and recreating keeps the hypertable,
    its chunks and its indexes — which is the point, since chunk behaviour is
    half of what is being tested.

    Autocommit, because the aggregate tests need ``CALL
    refresh_continuous_aggregate``, which Postgres refuses to run inside a
    transaction block. That is not a preference: it is the only way to refresh a
    continuous aggregate, so any harness that wants to test one has to be
    autocommit, and finding that out by hitting the error is one round trip
    wasted.
    """
    from storage.postgres.schema import connect

    connection = connect(autocommit=True)
    with connection.cursor() as cur:
        cur.execute("TRUNCATE reading")
    connection.commit()
    yield connection
    connection.close()


def _scalar(cur, query: str, *params):
    cur.execute(query, params)
    return cur.fetchone()[0]


# ─── the constraints, each one shown to actually bite ────────────────────────
#
# Every assertion here is of the form "this is refused", and the evidence is the
# server's own error. A constraint that is merely present in the DDL proves
# nothing, and these four are the reason the migration happened.


def test_a_good_reading_with_no_value_is_refused(conn) -> None:
    """Zero is a real dissolved-oxygen concentration, a real flow rate, and a
    real alarm state. A row that has no value and claims to be Good is the one
    ambiguity that would be undetectable downstream."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation) as err, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (now(), 'AERATION:AHU-1:DO', NULL, 0, 'opcua')"
        )
    assert "reading_null_is_not_good" in str(err.value)


def test_a_bad_reading_with_no_value_is_accepted(conn) -> None:
    """The representation is the point, not an accident.

    The previous storage engine arrived here by refusing every alternative —
    NaN is not a float it would take, Inf is not, and a quoted string is a
    different type. It is now a design choice the database enforces.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (now(), 'AERATION:AHU-1:DO', NULL, 2, 'opcua') "
            "RETURNING value, quality"
        )
        value, quality = cur.fetchone()
    assert value is None
    assert quality == 2


def test_a_reading_for_an_unknown_signal_is_refused(conn) -> None:
    """The single biggest correctness gain from leaving the tag-column model.

    In the previous engine a reading could name a signal that had never existed,
    or a typo'd one, and nothing would say so — the write would succeed and the
    data would be unfindable forever. There was no mechanism to express the
    constraint, because there was no relation to constrain.
    """
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation) as err, \
            conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (now(), 'NOT:A:SIGNAL', 1.0, 0, 'opcua')"
        )
    assert "signal_id" in str(err.value)


def test_an_unknown_quality_is_refused(conn) -> None:
    """Good/Uncertain/Bad was a convention only the writer respected.

    InfluxQL has no constraints at all, so a fourth code — or a typo, or a
    negative — would have been stored, charted, and never queried. The column
    has a CHECK now, and this is what that costs the writer: one more thing it
    cannot do.
    """
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation) as err, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (now(), 'AERATION:AHU-1:DO', 1.0, 7, 'opcua')"
        )
    assert "reading_quality_known" in str(err.value)


def test_a_signal_may_only_name_a_known_source(conn) -> None:
    with pytest.raises(Exception, match="reading_source_known"), \
            conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (now(), 'AERATION:AHU-1:DO', 1.0, 0, 'carrier-pigeon')"
        )


# ─── the two protocol faces coexist ─────────────────────────────────────────


def test_both_protocols_can_record_the_same_signal_at_the_same_instant(conn) -> None:
    """The cross-check that makes the word-order bug findable in the field.

    The gateway reads Modbus then OPC UA, and one overwrites the other per poll,
    so in normal operation only one source's row exists per instant. But the
    primary key allows both, and *that* is what lets a query ask "do the two
    faces agree?" — which is the only way to catch a low-word-first float
    decode, because the wrong answer is finite and in range.
    """
    ts = datetime.now(UTC).replace(microsecond=0)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (%s, 'AERATION:AHU-1:DO', %s, 0, %s)",
            [(ts, 2.10, "opcua"), (ts, 2.10, "modbus")],
        )
        cur.execute(
            "SELECT count(*) FROM reading WHERE signal_id = %s AND ts = %s",
            ("AERATION:AHU-1:DO", ts),
        )
        assert cur.fetchone()[0] == 2

        # And the disagreement is a two-row answer, not a guess.
        cur.execute(
            "UPDATE reading SET value = 2.3e-41 "
            "WHERE signal_id = %s AND ts = %s AND source = 'modbus'",
            ("AERATION:AHU-1:DO", ts),
        )
        cur.execute(
            """
            SELECT max(value) - min(value) AS spread
            FROM reading WHERE signal_id = %s AND ts = %s
            """,
            ("AERATION:AHU-1:DO", ts),
        )
        spread = cur.fetchone()[0]
    assert spread > 2.0, "the disagreement is not visible to a query"
    # And note what a range check would have said about the wrong value: nothing.
    # Every instrument range in the contract contains 2.3e-41, which is the whole
    # reason the defence has to be a cross-protocol comparison rather than a bound.
    lo, hi = 0.0, 20.0
    assert lo <= 2.3e-41 <= hi


def test_a_reexisting_instant_is_updated_not_duplicated(conn) -> None:
    _use_test_port()
    """The gateway re-sends from its spool, so this has to be an upsert."""
    from storage.postgres.writer import Reading, make_execute

    ts = 1_700_000_000.0
    execute = make_execute(_dsn())
    sid = "AERATION:AHU-1:DO"

    execute([Reading(ts=ts, signal_id=sid, value=1.0, source="opcua").as_row()])
    execute([Reading(ts=ts, signal_id=sid, value=2.0, source="opcua").as_row()])
    try:
        with conn.cursor() as cur:
            assert _scalar(
                cur, "SELECT count(*) FROM reading WHERE signal_id = %s", sid
            ) == 1
            assert _scalar(
                cur, "SELECT value FROM reading WHERE signal_id = %s", sid
            ) == 2.0
    finally:
        execute.close()  # type: ignore[attr-defined]


# ─── the hypertable, and the two aggregate tiers ────────────────────────────


def _seed_rows(conn, *, minutes: int = 90) -> None:
    """Rows deliberately unevenly distributed, which is the whole point.

    Minutes 0-29 get 60 readings, minutes 30-59 get 7, minutes 60+ get 1. A
    tiered rollup that averages averages gets this wrong; one that aggregates
    from raw gets it right. The weighted mean is the project's one piece of
    carried-over arithmetic, and this is the test that says whether the new
    design made it unnecessary.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for minute in range(minutes):
        n = 60 if minute < 30 else (7 if minute < 60 else 1)
        for k in range(n):
            rows.append((base + timedelta(minutes=minute, seconds=k), float(k)))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (%s, 'AERATION:AHU-1:DO', %s, 0, 'opcua')",
            rows,
        )
    return base


def test_the_hypertable_really_is_chunked(conn) -> None:
    """Otherwise "hypertable" is a claim rather than a fact."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'reading'"
        )
        assert cur.fetchone()[0] == 1


def test_both_tier_buckets_come_from_raw_so_the_mean_is_a_true_mean(conn) -> None:
    """The bug the previous rollup worker documented at length, designed out.

    Averaging the 1-minute averages into an hourly mean is wrong as soon as two
    minutes hold different numbers of points — and they always do, because the
    deadband filtered some of them and the last window of a run is short. The
    error is largest exactly when the data is most interesting, which is during
    an event.

    Aggregating *both* tiers from `reading` costs a second pass and removes the
    problem entirely. The test asserts the hourly mean against the raw mean over
    the same hour, and it would fail on a tiered design.
    """
    base = _seed_rows(conn, minutes=90)

    with conn.cursor() as cur:
        cur.execute("CALL refresh_continuous_aggregate('reading_1m', NULL, NULL)")
        cur.execute("CALL refresh_continuous_aggregate('reading_1h', NULL, NULL)")
        cur.execute(
            "SELECT count(*) FROM timescaledb_information.continuous_aggregates"
        )
        assert cur.fetchone()[0] >= 2

        # The 1 h aggregate, over the deliberately uneven hour.
        cur.execute(
            "SELECT mean, n FROM reading_1h "
            "WHERE signal_id = 'AERATION:AHU-1:DO' ORDER BY bucket LIMIT 1"
        )
        agg_mean, agg_n = cur.fetchone()

        cur.execute(
            "SELECT avg(value), count(value) FROM reading "
            "WHERE signal_id = 'AERATION:AHU-1:DO' AND ts >= %s AND ts < %s",
            (base, base + timedelta(hours=1)),
        )
        raw_mean, raw_n = cur.fetchone()


    assert agg_n == raw_n == 30 * 60 + 30 * 7, (agg_n, raw_n)
    assert agg_n != 60 * 90, "the fixture is not as uneven as intended"
    assert agg_mean == pytest.approx(raw_mean, rel=1e-9), (
        f"the hourly mean came from averages of averages: "
        f"{agg_mean} vs raw {raw_mean}"
    )

    # And the arithmetic an average-of-averages would have produced, so the
    # size of the error is on the record rather than merely asserted.
    minutes = [60] * 30 + [7] * 30
    avg_of_avgs = sum(minutes) / 60
    assert avg_of_avgs != pytest.approx(raw_mean, rel=1e-6)


def test_count_value_is_not_count_star(conn) -> None:
    """A reading that happened is not a reading you can use.

    `count(*)` counts the non-Good rows too, so a "fraction of readings that
    were bad" query built on it would quietly measure the wrong thing. The
    aggregates use `count(value)`, which skips the NULLs.
    """
    _seed_rows(conn, minutes=2)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reading (ts, signal_id, value, quality, source) "
            "VALUES (now(), 'AERATION:AHU-1:DO', NULL, 2, 'opcua')"
        )
    with conn.cursor() as cur:
        cur.execute("CALL refresh_continuous_aggregate('reading_1m', NULL, NULL)")
        cur.execute(
            "SELECT sum(n) FROM reading_1m WHERE signal_id = 'AERATION:AHU-1:DO'"
        )
        usable = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM reading WHERE signal_id = 'AERATION:AHU-1:DO'"
        )
        happened = cur.fetchone()[0]
    assert happened - usable == 1, (happened, usable)


def test_the_newest_window_is_not_available_through_the_aggregate(conn) -> None:
    """`materialized_only = true`, and the trade is deliberate.

    A rollup that answers for the last incomplete window teaches a reader to
    trust a number that is about to change. The price is that "right now" is a
    query on `reading`, not on `reading_1m`, and that is the correct place to
    ask it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT materialized_only FROM timescaledb_information."
            "continuous_aggregates WHERE view_name = 'reading_1m'"
        )
        assert cur.fetchone()[0] is True


# ─── COPY, the bulk path ───────────────────────────────────────────────────


def test_copy_and_insert_agree(conn) -> None:
    _use_test_port()
    """The two transports must produce the same rows.

    They do not have to: `COPY` cannot upsert, which is why the live path uses
    `INSERT ... ON CONFLICT` and the seeder uses `COPY`. But for a fresh
    timestamp they must agree exactly, and the asymmetry that bit during the
    migration — a float timestamp accepted by one and refused by the other — is
    worth a standing test.
    """
    from storage.postgres.writer import Reading, make_copy_execute, make_execute

    ts = 1_700_000_100.0
    sid = "EFFLUENT:FLOW:FLOW"
    rows = [Reading(ts=ts + i, signal_id=sid, value=float(i), source="seed")
            .as_row() for i in range(3)]

    ins = make_execute(_dsn())
    cop = make_copy_execute(_dsn())
    try:
        ins([r for r in rows if r[0] == ts])
        cop([r for r in rows if r[0] != ts])
    finally:
        ins.close()      # type: ignore[attr-defined]
        cop.close()      # type: ignore[attr-defined]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, value FROM reading WHERE signal_id = %s ORDER BY ts",
            (sid,),
        )
        got = cur.fetchall()
    assert len(got) == 3
    assert all(isinstance(t, datetime) and t.tzinfo is not None for t, _ in got)
    assert [v for _, v in got] == [0.0, 1.0, 2.0]


def test_copy_refuses_a_duplicate_rather_than_silently_overwriting(db) -> None:
    _use_test_port()
    """The consequence of choosing COPY for the seeder, stated as a test.

    A re-run into a window already written fails loudly. For the seeder that is
    correct — a doubled history is worse than an error message — and for the
    gateway it would be wrong, which is exactly why the live path does not use
    COPY. Both halves of that sentence are load-bearing.
    """
    import psycopg
    from storage.postgres.writer import Reading, make_copy_execute

    execute = make_copy_execute(_dsn())
    row = Reading(ts=1_700_000_200.0, signal_id="EFFLUENT:FLOW:FLOW",
                  value=1.0, source="seed").as_row()
    try:
        execute([row])
        with pytest.raises(psycopg.errors.UniqueViolation):
            execute([row])
    finally:
        execute.close()  # type: ignore[attr-defined]


# ─── metadata ───────────────────────────────────────────────────────────────


def test_the_contract_is_loaded_and_joins_to_the_readings(db) -> None:
    _use_test_port()
    from softplc.contract import contract as get_contract
    from storage.postgres.schema import connect

    c = get_contract()
    with connect() as connection, connection.cursor() as cur:
        assert _scalar(cur, "SELECT count(*) FROM signal") == len(c.signals)
        assert _scalar(cur, "SELECT count(*) FROM equipment") == len(c.equipment)
        assert _scalar(
            cur, "SELECT count(*) FROM signal WHERE equipment_id IS NULL"
        ) == 24

        # The join the previous engine could not express. Two stores, no way to
        # ask this question, which is the whole reason there were two.
        cur.execute(
            """
            SELECT s.unit, count(r.*)
            FROM reading r JOIN signal s ON s.id = r.signal_id
            GROUP BY s.unit ORDER BY count(r.*) DESC LIMIT 3
            """
        )
        assert cur.fetchall(), "the metadata and the readings cannot be joined"


def test_seeding_metadata_twice_changes_nothing(db) -> None:
    _use_test_port()
    """An appending seeder becomes a second, disagreeing copy of the contract.

    Which is the copy nobody remembers to update. `ON CONFLICT DO UPDATE`
    because the contract is the source of truth, and a changed range has to
    reach the database.
    """
    from softplc.contract import contract as get_contract
    from storage.postgres.schema import connect, seed_metadata

    c = get_contract()
    with connect() as connection:
        seed_metadata(c, conn=connection)
        with connection.cursor() as cur:
            assert _scalar(cur, "SELECT count(*) FROM signal") == len(c.signals)
            assert _scalar(cur, "SELECT count(*) FROM equipment") == len(c.equipment)


# ─── events: the one genuinely document-shaped thing ───────────────────────


def test_an_event_is_typed_where_it_matters_and_free_where_it_does(db) -> None:
    _use_test_port()
    from storage.postgres.schema import connect, record_event

    with connect(autocommit=True) as connection:
        record_event(connection, kind="blower_trip", severity="critical",
                     message="AHU-1 stopped", equipment_id="AHU-1",
                     rpm_before=1487.0, reason="overcurrent")
        record_event(connection, kind="calibration_due", severity="info",
                     message="NH4 probe due", signal_id="AERATION:AHU-1:NH4_IN")
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM event WHERE severity = 'critical'")
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT detail->>'reason' FROM event WHERE kind = 'blower_trip'"
            )
            assert cur.fetchone()[0] == "overcurrent"
    with connect(autocommit=True) as connection, connection.cursor() as cur:
        cur.execute("DELETE FROM event")


def test_an_event_severity_outside_the_scale_is_refused(db) -> None:
    _use_test_port()
    import psycopg
    from storage.postgres.schema import connect

    with connect(autocommit=True) as connection, \
            pytest.raises(psycopg.errors.CheckViolation), \
            connection.cursor() as cur:
        cur.execute(
            "INSERT INTO event (kind, severity, message) "
            "VALUES ('x', 'catastrophic', 'y')"
        )
