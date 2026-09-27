"""Shared fixtures for the integration suite.

They live here rather than in one test file because **fixtures do not cross test
modules.** `test_postgres_roles.py` was written assuming the `db` and `conn`
fixtures from `test_postgres.py` were visible, which they are not, and it failed
with nine collection errors — a reminder that the first version of this file said
it was "the integration suite" and was in fact the integration suite's first
file.

The two destructive guards live here for the same reason and they must be applied
once: a database-name default and a refusal to truncate seeded history.
"""

from __future__ import annotations

import os

import pytest

#: Row count above which this suite considers the database to be holding
#: somebody's data rather than its own scratch space.
#:
#: 50 000 is roughly an hour of seeded plant, well below the week the seeder
#: produces, and well above the largest fixture the suite creates (3 510 rows, in
#: the deliberately-uneven aggregate test). The gap on both sides is deliberate: a
#: threshold that was merely "large" would eventually be crossed by the tests
#: themselves, and one that was merely "small" would refuse a legitimately
#: nearly-empty database.
_DESTRUCTIVE_ABOVE_READINGS = 50_000


def _db_name() -> str:
    """The database name, with the same default `storage.postgres.schema.dsn()`
    uses. Reading it from the environment directly raised a `KeyError` when
    `POSTGRES_DB` was unset — which is the *default* case, and the error arrived
    from inside the refusal message, seventeen times.
    """
    return os.environ.get("POSTGRES_DB", "wwtp")


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
    # POSTGRES_TEST_DB has to become POSTGRES_DB, not merely coexist with it.
    # An earlier version of this file accepted `POSTGRES_TEST_DB` and then asked
    # `storage.postgres.schema.dsn()` for the connection string — and that reads
    # `POSTGRES_DB`, which the test run had not set. So the suite connected to,
    # and truncated, the developer's real seeded database while appearing to use
    # a scratch one. The variable was honoured in the message and ignored in the
    # connection, which is the worst way for it to be wrong.
    if "POSTGRES_TEST_DB" in os.environ:
        os.environ["POSTGRES_DB"] = os.environ["POSTGRES_TEST_DB"]


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
            f"{os.environ.get('POSTGRES_PORT', '5432')}: {exc}\n"
            "  docker compose up -d db && docker compose run --rm init-db\n"
            "  then set POSTGRES_PASSWORD (it defaults to 'itpass' for a local "
            "scratch instance)"
        )
    apply_schema()
    seed_metadata()

    # Refuse to run against a database that has history in it. This suite
    # truncates `reading`; the seeder is what puts a week of plant history in,
    # and losing it to a test run is a genuinely bad afternoon. A docstring
    # saying "point this at a scratch database" is a promise; this is a
    # refusal. The threshold is 50 000 rows, which is roughly an hour of seeded
    # plant — well below a week, well above anything the suite itself creates
    # (its largest fixture is 3 510).
    with connect() as probe, probe.cursor() as cur:
        cur.execute("SELECT count(*) FROM reading")
        existing = cur.fetchone()[0]
    if existing > _DESTRUCTIVE_ABOVE_READINGS:
        # A skip, not a failure. The distinction matters: a failure says "this
        # suite is broken", and it would say it seventeen times because the
        # fixture is session-scoped. A skip says "not here, and here is why",
        # which is the truth.
        pytest.skip(
            f"refusing to run: {_db_name()} holds {existing} "
            f"readings. This suite truncates `reading`, so it must not be "
            "pointed at seeded history.\n"
            "    docker exec wwtp-db createdb -U wwtp wwtp_test\n"
            "    POSTGRES_TEST_DB=wwtp_test pytest tests/integration\n"
            "  To keep the seeded week, take a copy first:\n"
            "    docker compose exec db pg_dump -U wwtp wwtp > wwtp.sql"
        )
    return True


@pytest.fixture
def scalar():
    """`cur.fetchone()[0]`, as a fixture rather than a module-level helper.

    It was a module-level function in `conftest.py` first, which does not work:
    `conftest` is a pytest module rather than an importable one, so a test that
    wanted to use it had to write `from conftest import _scalar` and got
    `ModuleNotFoundError`. Putting it in a separate `helpers.py` did not help
    either, because `tests/integration` is a *package* (it has `__init__.py`) so
    the module is `tests.integration.helpers` and the directory is not on
    `sys.path`.

    A fixture is the mechanism pytest actually provides for sharing this, and it
    works regardless of package layout.
    """

    def _scalar(cur, query: str, *params: object) -> object:
        cur.execute(query, params)
        return cur.fetchone()[0]

    return _scalar


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




