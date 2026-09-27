r"""The database roles, and the grants that were deliberately *not* made.

An absent grant is invisible in a diff, invisible in `\d`, and invisible until
something tries to use it. So every rule here is proved by attempting the thing
and reading the server's refusal — the same discipline as the `CHECK` constraint
tests in `test_postgres.py`, and for the same reason: a security control that
has only been asserted is a comment with a runtime cost.

The one that matters most is `wwtp_writer` having no `DELETE` on `reading`. It is
easy to add by accident — a `GRANT ALL` during an incident, a `GRANT SELECT,
INSERT, UPDATE, DELETE` written from habit — and the symptom would be a
plausible-but-truncated trend rather than an error, which is the worst possible
symptom for a historian.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from storage.postgres.roles import (
    DELIBERATELY_WITHHELD,
    GRANTS,
    apply,
    check,
)


@pytest.fixture(scope="module")
def roles(db):
    """The roles, applied. Skips with the integration suite."""
    apply()
    return {r.name: r for r in check()}


def _sql(template: str, *params: object) -> str:
    """Render bind parameters into a statement for the `_as` helper.

    `_as` runs a single statement and then resets the role, so it takes a string
    rather than a parameter list. The timestamp goes in as an ISO literal, which
    keeps the quoting question out of a test — the production path uses bind
    parameters throughout, and `psycopg.sql.Literal` where DDL makes that
    impossible.
    """
    for param in params:
        template = template.replace("%s", f"'{param}'", 1)
    return template


def _as(conn, role: str, sql: str) -> None:
    """Run one statement as ``role``, and put the role back afterwards.

    ``SET ROLE``, not ``SET LOCAL ROLE``. The shared `conn` fixture is
    **autocommit**, because the continuous-aggregate tests need
    ``CALL refresh_continuous_aggregate`` and that cannot run inside a
    transaction — so there is never a transaction for ``SET LOCAL`` to attach to,
    and Postgres silently ignores it. The first version of this helper used
    ``SET LOCAL`` and every test in the file *passed against the owner*, which
    means six tests were checking that the owner cannot do things the owner can
    obviously do. A test that passes for the wrong reason is worse than one that
    fails, because it is a green tick.

    ``RESET ROLE`` in a ``finally`` so a failure does not leave the connection
    downgraded for the next test.
    """
    with conn.cursor() as cur:
        cur.execute(f'SET ROLE "{role}"')
        try:
            cur.execute(sql)
            # Fetched *before* RESET ROLE. `RESET ROLE` returns no result, so a
            # `fetchone()` after the finally block raises "no result available" —
            # which reads like a permissions problem and is not one.
            return cur.fetchall() if cur.description else None
        finally:
            cur.execute("RESET ROLE")


# ── what the roles should have ────────────────────────────────────────────────


def test_every_declared_grant_is_present(roles) -> None:
    for name, problems in roles.items():
        assert problems.missing == [], f"{name} is missing {problems.missing}"
        assert problems.ok, name


def test_no_role_has_a_grant_it_was_declared_not_to_have(roles) -> None:
    """The assertions that cannot be made by reading the grant list.

    `DELIBERATELY_WITHHELD` exists to be *checked*, and this is the check.
    """
    for name, problems in roles.items():
        assert problems.unexpected == [], (
            f"{name} holds grants it should not: {problems.unexpected}"
        )
    # And the list is not empty, or the test above would be vacuous.
    assert DELIBERATELY_WITHHELD


# ── the writer can write and cannot erase ─────────────────────────────────────


def test_the_writer_can_insert_a_reading(conn, roles) -> None:
    _as(conn, "wwtp_writer", """
        INSERT INTO reading (ts, signal_id, value, quality, source)
        VALUES (now(), 'AERATION:AHU-1:DO', 2.1, 0, 'opcua')
    """)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reading WHERE value = 2.1")
        assert cur.fetchone()[0] == 1
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reading WHERE value = 2.1")
    conn.commit()


def test_the_writer_can_upsert_a_reading(conn, roles) -> None:
    """The gateway's re-send path, and the grant that made it possible.

    `INSERT ... ON CONFLICT DO UPDATE` needs three privileges, not one, and fails
    as a bare `permission denied` for each of the missing two:

    * **SELECT** — it reads the conflicting row to decide whether to update it;
    * **UPDATE** — it then writes that existing row, which is the one that looks
      like a mistake on a historian;
    * **USAGE on the sequence** — for `event`, whose id is `BIGSERIAL`.

    All three were found by running the stack, not by reading
    `storage/postgres/roles.py`. This test is the one that would have caught
    them, and it exists for the next person who narrows a grant.
    """
    from datetime import UTC, datetime

    ts = datetime.now(UTC).replace(microsecond=0)
    upsert = (
        "INSERT INTO reading (ts, signal_id, value, quality, source) "
        "VALUES (%s, 'AERATION:AHU-1:DO', 2.1, 0, 'opcua') "
        "ON CONFLICT (ts, signal_id, source) DO UPDATE SET value = EXCLUDED.value"
    )
    _as(conn, "wwtp_writer", _sql(upsert, ts))
    _as(conn, "wwtp_writer", _sql(upsert.replace("2.1", "2.2"), ts))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(value) FROM reading WHERE ts = %s", (ts,))
        assert cur.fetchone() == (1, 2.2)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM reading WHERE ts = %s", (ts,))
    conn.commit()


def test_the_writer_can_insert_an_event(conn, roles) -> None:
    """`event.id` is BIGSERIAL, so the sequence usage is not optional."""
    _as(conn, "wwtp_writer",
        "INSERT INTO event (ts, kind, severity, message) "
        "VALUES (now(), 'probe', 'info', 'x')")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM event WHERE message = 'x'")
    conn.commit()


def test_the_writer_cannot_delete_a_reading(conn, roles) -> None:
    """The grant this project is most likely to add by accident.

    A historian that can be made to forget is worse than one that stops. An
    operator who sees a gap investigates it; an operator looking at a
    plausible-but-truncated trend does not, because there is nothing to notice.
    """
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege) as err:
        _as(conn, "wwtp_writer", "DELETE FROM reading")
    conn.rollback()
    assert "permission denied" in str(err.value)


def test_the_writer_cannot_truncate(conn, roles) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as(conn, "wwtp_writer", "TRUNCATE reading")


def test_the_writer_cannot_change_the_contract(conn, roles) -> None:
    """A gateway that can rewrite its own signal definitions can lie about what
    the plant is measuring, and the rewrite would be invisible in the readings."""
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as(conn, "wwtp_writer",
            "UPDATE signal SET normal_high = 9999 "
            "WHERE id = 'AERATION:AHU-1:DO'")


def test_the_writer_cannot_drop_a_table(conn, roles) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as(conn, "wwtp_writer", "DROP TABLE reading")


# ── the reader can read and cannot write ──────────────────────────────────────


def test_the_reader_can_read_a_reading(conn, roles) -> None:
    assert _as(conn, "wwtp_reader", "SELECT count(*) FROM reading")
    assert _as(conn, "wwtp_reader", "SELECT count(*) FROM reading_1m")


def test_the_reader_cannot_write(conn, roles) -> None:
    """Grafana and the dashboard are the roles most likely to be handed to a
    third party, and a write grant there would do the most damage for the least
    effort."""
    import psycopg

    for sql in (
        "INSERT INTO event (kind, severity, message) VALUES ('x','info','y')",
        "INSERT INTO reading (ts, signal_id, value, quality, source) "
        "VALUES (now(), 'AERATION:AHU-1:DO', 1.0, 0, 'opcua')",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _as(conn, "wwtp_reader", sql)


# ── the declaration itself ───────────────────────────────────────────────────


def test_the_writer_role_does_not_grant_delete_anywhere() -> None:
    """Assert the *declaration*, not only the database.

    The database check above can only run when a database is available. This one
    runs always, which means a grant added to `GRANTS` by mistake is caught in the
    ordinary unit suite rather than waiting for a container.
    """
    for role, grants in GRANTS.items():
        withheld = {tuple(w) for w in DELIBERATELY_WITHHELD.get(role, ())}
        for table, privilege in grants:
            assert (table, privilege) not in withheld, (
                f"{role} is declared to have {privilege} on {table}, which it is "
                f"also declared to be without"
            )
    assert ("reading", "DELETE") in {
        tuple(w) for w in DELIBERATELY_WITHHELD["wwtp_writer"]
    }


def test_roles_are_created_nologin() -> None:
    """They are group roles. The *login* roles are environment-specific and are
    not created here — a login role needs a password, and a password in a
    migration is a password in a git history."""
    from storage.postgres.schema import connect

    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname LIKE 'wwtp%'"
        )
        rows = {r[0]: r[1] for r in cur.fetchall()}
    for name in GRANTS:
        assert name in rows, f"{name} does not exist"
        assert rows[name] is False, f"{name} can log in; it is a group role"


# ── a refusal has to be a refusal for the *right reason* ─────────────────────


#: Each entry is (SQL, a phrase the error must contain). The phrase is the whole
#: point of the test.
#:
#: **A permission-denied test that passes because the SQL was invalid proves
#: nothing.** While writing `docs/SECURITY.md` I checked "the gateway cannot
#: change the contract" with
#:
#:     UPDATE signal SET name = name
#:
#: and it was *refused* — but because `signal` has no `name` column, not because
#: of any grant. The same mistake twice more: `signal.eu` does not exist either
#: (the column is `unit`), so "cannot change a unit" and "cannot invent a signal"
#: both "passed" for the wrong reason.
#:
#: A test that cannot fail is worse than no test, because it is trusted. Every
#: refusal below is matched against the reason the server gives, so a typo in a
#: column name turns a green test red instead of leaving it green for ever.
REFUSALS: list[tuple[str, str, str]] = [
    ("wwtp_gateway", "DELETE FROM reading WHERE false", "permission denied"),
    ("wwtp_gateway", "TRUNCATE reading", "permission denied"),
    ("wwtp_gateway", "DROP TABLE reading", "must be owner"),
    ("wwtp_gateway", "CREATE ROLE probe_role", "permission denied"),
    # Deliberate, and stated in `storage/postgres/login_role.py`: the gateway does
    # not need the rollups, and a compromised gateway reading a pre-aggregated
    # trend is a smaller prize than one that can.
    ("wwtp_gateway", "SELECT 1 FROM reading_1m LIMIT 1", "permission denied"),
    ("wwtp_gateway", "SELECT 1 FROM reading_1h LIMIT 1", "permission denied"),
    # The three that were passing for the wrong reason. `unit`, not `eu`.
    ("wwtp_gateway", "UPDATE signal SET unit = 'm3/h' WHERE false",
     "permission denied"),
    ("wwtp_gateway", "DELETE FROM signal WHERE false", "permission denied"),
    ("wwtp_gateway", "INSERT INTO signal (id, unit) VALUES ('probe:x','m3/h')",
     "permission denied"),
    ("wwtp_ui", "DELETE FROM reading WHERE false", "permission denied"),
    ("wwtp_ui", "TRUNCATE reading", "permission denied"),
    ("wwtp_ui", "UPDATE signal SET unit = 'm3/h' WHERE false", "permission denied"),
    ("wwtp_ui", "CREATE TABLE probe_t (i int)", "permission denied"),
]


@pytest.mark.parametrize(("role", "sql", "reason"), REFUSALS,
                         ids=[f"{r}-{sql.split()[0]}" for r, sql, _ in REFUSALS])
def test_a_refusal_is_refused_for_the_right_reason(
    role: str, sql: str, reason: str, db, roles
) -> None:
    """Each withheld grant, refused *and* for the stated reason.

    `SET LOCAL ROLE` inside a transaction, so the check cannot leave the session
    as a different role, and `rollback()` so the `INSERT` attempts leave nothing.

    The `WHERE false` on the UPDATEs and DELETEs is not a safety measure — the
    statements are expected to fail before they execute — it is so that if the
    grant ever *is* given, this test finds out from the assertion below rather
    than from the state of the table afterwards.
    """
    from storage.postgres.schema import connect

    with connect() as conn, conn.cursor() as cur:
        with pytest.raises(Exception) as exc:
            cur.execute(f"SET LOCAL ROLE {role}")
            cur.execute(sql)
        conn.rollback()

    message = str(exc.value).lower()
    assert reason in message, (
        f"{role} was refused {sql!r}, but not for {reason!r}: {message.strip()[:200]}. "
        f"A refusal for the wrong reason means this test would pass even if the "
        f"grant were given — check the column names."
    )


def test_the_one_update_the_gateway_is_granted_is_the_one_it_needs(db, roles) -> None:
    """The mirror image: the grants that *are* made, verified by using them.

    A file of refusals proves nothing on its own — a role with no grants at all
    would pass every test above. So the allowed operations are exercised too, and
    `UPDATE reading` is included deliberately: it is the grant that looks like a
    mistake, it is required by the gateway's `ON CONFLICT DO UPDATE` re-send
    path, and a reader of `docs/SECURITY.md` deserves to see it asserted rather
    than explained.

    The first version of this test also read `reading_1m`, and it was refused. The
    gateway has no `SELECT` on the rollups on purpose, so the test was asserting
    the opposite of the design — a reminder that "the allowed set" is a decision
    and not the complement of "the refused set". Both are now asserted, and the
    refusal is in `REFUSALS` with a note saying why.
    """
    from storage.postgres.schema import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE wwtp_gateway")
        cur.execute("SELECT 1 FROM reading LIMIT 1")
        cur.execute(
            "INSERT INTO reading (ts, signal_id, value, source) "
            "VALUES (now(), 'AERATION:AHU-1:DO', 1.0, 'seed') ON CONFLICT DO NOTHING"
        )
        cur.execute("UPDATE reading SET value = value WHERE false")
        cur.execute(
            "INSERT INTO event (kind, severity, message) "
            "VALUES ('probe', 'info', 'probe')"
        )
        conn.rollback()


# ── two roles, two passwords ─────────────────────────────────────────────────


def test_every_login_role_has_its_own_password_variable() -> None:
    """The table that would have caught `wwtp_ui` getting the gateway's password.

    `login_role.apply()` reads the password from a **table keyed by role name**,
    and this asserts the table covers every login role. It used to read the
    literal string `GATEWAY_DB_PASSWORD`, which was correct while there was one
    login role and wrong the moment there were two: `init-db` created `wwtp_ui`
    with the *gateway's* password while the `web` service was handed
    `WEB_DB_PASSWORD`, and the dashboard answered

        FATAL: password authentication failed for user "wwtp_ui"

    immediately after `init-db` had logged "password and LOGIN refreshed".

    No test failed, because the only test that exercised `login_role` passed the
    password as an argument — the very thing a human never does.
    """
    from storage.postgres.login_role import LOGIN_PASSWORDS, LOGIN_ROLES

    assert set(LOGIN_PASSWORDS) == set(LOGIN_ROLES), (
        "every login role needs a password variable; missing: "
        f"{sorted(set(LOGIN_ROLES) - set(LOGIN_PASSWORDS))}"
    )
    # And the variables must be distinct, or the point of the table is lost.
    assert len(set(LOGIN_PASSWORDS.values())) == len(LOGIN_PASSWORDS), (
        f"two roles share a password variable: {LOGIN_PASSWORDS}"
    )


def test_two_login_roles_authenticate_with_different_passwords(db, roles) -> None:
    """The behavioural version: both authenticate, and neither with the other's.

    Over the network, as a separate role, and each password verified against the
    *other* role as well — because a table that is present but unused produces
    exactly the failure it was added to prevent, silently.
    """
    from storage.postgres.login_role import LOGIN_PASSWORDS, apply
    from storage.postgres.schema import dsn

    passwords = {
        "wwtp_gateway": "probe-gateway-pw",
        "wwtp_ui": "probe-ui-pw",
    }
    previous = {r: os.environ.get(LOGIN_PASSWORDS[r]) for r in passwords}
    for role, pw in passwords.items():
        os.environ[LOGIN_PASSWORDS[role]] = pw
    try:
        for role, pw in passwords.items():
            apply(role, password=pw)
    finally:
        for role, value in previous.items():
            if value is None:
                os.environ.pop(LOGIN_PASSWORDS[role], None)
            else:
                os.environ[LOGIN_PASSWORDS[role]] = value

    connection = dsn()
    for role, pw in passwords.items():
        others = [p for r, p in passwords.items() if r != role]
        with (
            psycopg.connect(connection, user=role, password=pw) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == role
        for wrong in others:
            with pytest.raises(psycopg.OperationalError), psycopg.connect(
                connection, user=role, password=wrong
            ):
                pass
