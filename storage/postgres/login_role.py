"""Create the *login* roles the long-running services authenticate as.

Separate from `storage/postgres/roles.py`, which creates **group** roles with
`NOLOGIN`. The split exists because the two have genuinely different lifetimes and
different risks:

* a group role is part of the schema's story — created, granted, checked — and
  belongs in the migration;
* a login role needs a **password**, and a password in a migration is a password
  in a git history. So the login role is created at run time from the
  environment, and nothing about it is committed.

`init-db` runs this immediately after `storage.postgres.schema`, so the gateway —
which `depends_on: init-db` — never starts without a working credential.

## Why this is the fix for a regression

The previous stack authenticated the gateway to Couchbase with a bucket-scoped
application user, verified at the time to be *unable to administer the cluster*.
The migration to Postgres replaced that with one shared password that owned the
database. That was recorded in `docs/SECURITY.md` as a deliberate, documented
regression, on the grounds that a teaching project is better off documenting a gap
than silently closing it.

That reasoning was right for the two *protocol* gaps — OPC UA's `EUInformation`
is advisory by specification and Modbus has no authentication at all, so those
are properties of the technology rather than omissions in the work. It was wrong
for this one, which is simply a grant that had not been made yet. A documented
omission is a teaching point; a documented *regression* is a debt someone agreed
to pay, and paying it is the obvious next move.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from typing import Protocol

    class Conn(Protocol):
        """The slice of a psycopg connection this module uses.

        A Protocol rather than `object`, so that the attribute access type-checks
        and so that the annotation says what is actually required. Importing
        `psycopg.Connection` for real would make the module unimportable without
        the storage extras, which is the thing the lazy imports are for.
        """

        def cursor(self) -> Any: ...
        def commit(self) -> None: ...
        def close(self) -> None: ...


log = logging.getLogger("storage.postgres.login_role")

#: Login role -> the group role it is a member of. Exactly one, deliberately.
#:
#: A login role that were a member of both `wwtp_writer` and `wwtp_reader` would
#: be a more useful default and a worse example. The gateway does not need to
#: read the rollups, and the whole point of the split is that a compromised
#: gateway cannot delete a reading.
LOGIN_ROLES: dict[str, str] = {
    "wwtp_gateway": "wwtp_writer",
    "wwtp_ui": "wwtp_reader",
}

#: Login role -> the environment variable holding **its own** password.
#:
#: Separate from `LOGIN_ROLES` on purpose. That table says which *group* a role
#: belongs to; this one says where its credential comes from, and conflating the
#: two is how `wwtp_ui` ended up with the gateway's password for a phase — see
#: `apply()`.
#:
#: A role missing from this table falls back to `POSTGRES_PASSWORD`, which is the
#: owner's password and therefore a working configuration that defeats the point.
#: `apply()` logs which source it used, so that fallback is visible in `init-db`'s
#: output rather than silent.
LOGIN_PASSWORDS: dict[str, str] = {
    "wwtp_gateway": "GATEWAY_DB_PASSWORD",
    "wwtp_ui": "WEB_DB_PASSWORD",
}

#: The owner role, which exists only to make "this is not the owner" checkable.
OWNER_ROLE = "wwtp_owner"


def apply(
    name: str,
    group: str | None = None,
    password: str | None = None,
    conn: Conn | None = None,
) -> str:
    """Create or update one login role. Idempotent.

    `ALTER ROLE ... PASSWORD` rather than `CREATE ROLE`, because the role already
    existing is the *normal* case on every run after the first and
    `CREATE ROLE` has no `IF NOT EXISTS`. Running `init-db` twice must be a no-op
    in effect and must not fail.
    """
    from storage.postgres.schema import connect

    group = group or LOGIN_ROLES.get(name)
    if group is None:
        raise ValueError(
            f"no group role declared for login role {name!r}; "
            f"known: {sorted(LOGIN_ROLES)}"
        )
    # The fallback chain matters, and it is in code rather than in the compose
    # file because **Compose cannot nest `${A:-${B}}`.** An earlier version of
    # compose.yaml tried, and the value silently became the literal string. So
    # compose passes both variables through and the precedence is decided here,
    # where it can be tested:
    #
    #     <the role's own variable>  >  POSTGRES_PASSWORD  >  ask
    #
    # Falling back to the owner's password defeats the point of the exercise, so
    # `.env.example` says plainly that a separate one is worth setting. It is not
    # *required*, because a reader arriving at this project should not be stopped
    # by a security nicety before they have seen the plant run.
    #
    # **Which variable is the role's own is a table, and it used to be the literal
    # string `GATEWAY_DB_PASSWORD`.** That was correct while there was exactly one
    # login role. Adding `wwtp_ui` to the `init-db` command generalised `--name`
    # and nothing else, so creating `wwtp_ui` set its password to **the gateway's**
    # while the `web` service was handed `WEB_DB_PASSWORD` — and the two could
    # never match. The dashboard answered
    #
    #     FATAL: password authentication failed for user "wwtp_ui"
    #
    # and `init-db` had logged "password and LOGIN refreshed" immediately before,
    # which is what made it look impossible.
    #
    # That is the same failure as everything else in this project's review: **a
    # parameter generalised in the signature and not in the behaviour.** The lesson
    # is not "add a table" — it is that `--name` implied a generalisation, and
    # nothing tested the second role. So the table is explicit, the source is
    # logged, and a test proves two roles with *different* passwords both
    # authenticate.
    env_var = LOGIN_PASSWORDS.get(name, "POSTGRES_PASSWORD")
    source = "argument"
    if not password:
        password = os.environ.get(env_var)
        source = env_var if password else "unset"
    if not password:
        password = os.environ.get("POSTGRES_PASSWORD")
        source = "POSTGRES_PASSWORD" if password else "unset"
    if not password:
        password = _prompt()
        source = "prompt"
    log.info(
        "password for %s taken from %s (set %s for a per-role credential)",
        name, source, env_var,
    )

    from psycopg import sql

    own = conn is None
    conn = conn or connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            exists = cur.fetchone() is not None

            # **DDL cannot be parameterised.** `CREATE ROLE ... PASSWORD %s` is a
            # syntax error, because a bind parameter is only meaningful where a
            # value is, and the parser has to see the literal to build the
            # statement. The first version of this did exactly that and failed
            # with ``syntax error at or near "$1"``.
            #
            # So the password is composed in as a *quoted literal* rather than
            # interpolated raw. `psycopg.sql.Literal` renders it with the
            # escaping Postgres expects, so a password containing a quote is a
            # password that works rather than a way to append SQL.
            #
            # The honest caveat: the value is now in the statement text, so it
            # can reach `log_statement`. That is inherent to `CREATE ROLE` and
            # cannot be engineered away here — the mitigations are
            # `log_statement = 'ddl'` never being set, and a password that is
            # scoped to one role in one database. It is recorded in
            # `docs/SECURITY.md` rather than left for someone to work out from a
            # stack trace.
            literal = sql.Literal(password)
            if exists:
                # LOGIN back on: an operator may have turned it off to
                # investigate, and a health check that fails forever is not a
                # diagnostic.
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                        sql.Identifier(name), literal
                    )
                )
                log.info("role %s exists; password and LOGIN refreshed", name)
            else:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(name), literal
                    )
                )
                log.info("created login role %s", name)

            cur.execute(f'GRANT "{group}" TO "{name}"')
            # Revoke anything else, so a role that was once a member of more
            # than one group converges rather than accumulating.
            # A login role that is a member of more than one group is a role
            # whose privileges are the union of the two, and the union is almost
            # never what anybody intended. So any *other* group membership is
            # revoked rather than left to accumulate — the grant list converges
            # towards the declaration instead of only ever growing.
            cur.execute(
                """
                SELECT r.rolname FROM pg_auth_members m
                JOIN pg_roles r ON r.oid = m.roleid
                JOIN pg_roles u ON u.oid = m.member
                WHERE u.rolname = %s
                """,
                (name,),
            )
            stale = [r[0] for r in cur.fetchall() if r[0] != group]
            for other in stale:
                cur.execute(f'REVOKE "{other}" FROM "{name}"')
                log.warning(
                    "revoked %s from %s: a login role should belong to exactly "
                    "one group, and the union of two is not what anybody meant",
                    other, name,
                )
        return group
    finally:
        if own:
            conn.close()


def verify(name: str, conn: Conn | None = None) -> dict[str, object]:
    """What this login role can actually do. For the health check, and the tests."""
    from storage.postgres.schema import connect

    own = conn is None
    conn = conn or connect(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.rolname, r.rolsuper, r.rolcreatedb,
                       pg_catalog.has_database_privilege(%s, current_database(),
                                                        'CREATE')
                FROM pg_roles r WHERE r.rolname = %s
                """,
                (name, name),
            )
            row = cur.fetchone()
            if row is None:
                return {"exists": False}
            # The predicate is a *function*, not a comparison, in its own
            # statement. Written inline as
            #
            #     if cur.execute(...) or cur.fetchone()[0]
            #
            # it reports every privilege on every table, because `execute`
            # returns a truthy cursor and `or` never reaches the fetch. A health
            # check that says the gateway holds DELETE on everything is better
            # than one that says nothing, which is how it was caught: the first
            # run of the fixed version reported all seven privileges and that was
            # obviously wrong.
            #
            # `has_table_privilege` rather than `role_table_grants`. The catalog
            # view only lists privileges granted *directly* to the role, so a
            # login role that inherits everything through `wwtp_writer` reported
            # `grants={}` — which is exactly the wrong answer for a function whose
            # job is to check that the role is scoped, since an empty grant list
            # looks like a very well-behaved role and means nothing.
            cur.execute(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r','p','v')"
            )
            tables = [str(r[0]) for r in cur.fetchall()]
            def _holds(table: str, privilege: str) -> bool:
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (name, table, privilege),
                )
                row = cur.fetchone()
                return row is not None and bool(row[0])

            grants: dict[str, list[str]] = {}
            for table in tables:
                held = [
                    privilege
                    for privilege in
                    ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                    if _holds(table, privilege)
                ]
                if held:
                    grants[table] = held
        assert row is not None, "the role exists but the catalog returned no row"
        return {
            "exists": True,
            "superuser": row[1],
            "createdb": row[2],
            "can_create_in_database": row[3],
            "grants": grants,
        }
    finally:
        if own:
            conn.close()


def _prompt() -> str:  # pragma: no cover - interactive fallback
    """Ask, rather than invent one.

    A generated password nobody knows is a database nobody can read, and a
    hard-coded one is a secret in a repository. Both are worse than a prompt.
    """
    try:
        first = getpass.getpass("password for the gateway's database role: ")
    except (EOFError, OSError):
        # A container with no tty. An `EOFError` traceback is a poor way to
        # learn that, and it was the first thing this printed.
        raise SystemExit(
            "no password available: set GATEWAY_DB_PASSWORD, or POSTGRES_PASSWORD "
            "for it to fall back to, or run this interactively"
        ) from None
    if first != getpass.getpass("again: "):
        raise SystemExit("they do not match")
    if len(first) < 16:
        raise SystemExit("use at least 16 characters")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="postgres-login-role",
        description="Create the login role the gateway authenticates as.",
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--group", default=None)
    parser.add_argument("--verify", action="store_true",
                        help="report and change nothing")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    if args.verify:
        info = verify(args.name)
        if not info.get("exists"):
            log.error("role %s does not exist", args.name)
            return 1
        log.info("%s: superuser=%s createdb=%s grants=%s",
                 args.name, info["superuser"], info["createdb"], info["grants"])
        return 0

    group = apply(args.name, args.group)
    log.info("%s is a member of %s", args.name, group)
    info = verify(args.name)
    if info.get("superuser") or info.get("createdb"):
        log.error("%s has cluster-level powers it should not", args.name)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
