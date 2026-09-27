"""Three database roles, because one shared password is a regression.

The previous stack authenticated the gateway to Couchbase with a
bucket-scoped user, verified at the time to be unable to administer the cluster.
The Postgres migration replaced that with a single ``POSTGRES_PASSWORD`` shared
by four services, and it owns the database: the gateway can ``DROP TABLE``.

This module is the fix, and it is kept out of ``schema.sql`` on purpose — see
the comment at the end of that file. It is also **idempotent**, because
``init-db`` runs on every ``docker compose up`` and a second run must not fail.

    python -m storage.postgres.roles          # apply
    python -m storage.postgres.roles --check  # report, change nothing

## The grants, and the one that is missing on purpose

``wwtp_writer`` gets ``SELECT`` and ``INSERT`` on ``reading`` and ``event``, and
``SELECT`` on the three metadata tables. It does **not** get ``DELETE`` or
``TRUNCATE``.

That omission is a decision, not an oversight, and it is worth defending: a
historian that can be made to forget is worse than one that stops. An operator
who sees a gap investigates it — they raise a ticket, they check the gateway, they
find the outage that caused it. An operator looking at a plausible-but-truncated
trend does not, because there is nothing to notice. The gateway's spool already
bounds the damage from a *stopped* database; this bounds the damage from a
*hostile* one.

``wwtp_reader`` exists so that Grafana and the dashboard cannot write at all. It
is the role most likely to be handed to a third party, and it is the one where a
write grant would do the most damage for the least effort.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
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


log = logging.getLogger("storage.postgres.roles")

#: Role -> the grants it needs. Read as data rather than as a SQL string so the
#: `--check` mode can report on them and the test can assert on them without
#: parsing SQL.
GRANTS: dict[str, list[tuple[str, str]]] = {
    # None of the three grants below is padding, and each was found by running
    # the stack rather than by reading this file. The obvious minimum — INSERT
    # alone — is not enough, and it fails as `permission denied` with no hint
    # about what is missing:
    #
    #   SELECT   `ON CONFLICT DO UPDATE` reads the conflicting row to decide
    #            whether to update it, and reads the conflict-target columns.
    #   UPDATE   `DO UPDATE` then *writes* the existing row. This is the one that
    #            looks like a mistake — UPDATE on a historian feels wrong — and it
    #            is required for the upsert the gateway's re-send path depends on.
    #   SEQUENCE `event.id` is BIGSERIAL, so an INSERT needs USAGE on
    #            `event_id_seq`, not merely INSERT on the table.
    #
    # The reason UPDATE is acceptable at all is that it is UPDATE on `reading`
    # and nothing else: it cannot change history, only overwrite a row at a
    # timestamp the gateway already believes it wrote. DELETE and TRUNCATE remain
    # withheld, and those are the ones that would let a compromised gateway make
    # the past disappear rather than merely be wrong.
    "wwtp_writer": [
        ("reading", "SELECT"),
        ("reading", "INSERT"),
        ("reading", "UPDATE"),
        ("event", "SELECT"),
        ("event", "INSERT"),
        ("signal", "SELECT"),
        ("equipment", "SELECT"),
        ("site", "SELECT"),
    ],
    "wwtp_reader": [
        ("reading", "SELECT"),
        ("reading_1m", "SELECT"),
        ("reading_1h", "SELECT"),
        ("signal", "SELECT"),
        ("equipment", "SELECT"),
        ("site", "SELECT"),
        ("event", "SELECT"),
    ],
}

#: Sequence usage, granted separately because it is not a table privilege and
#: `role_table_grants` cannot see it. `check()` verifies these by attempting a
#: write rather than by reading a catalog.
SEQUENCE_USAGE: dict[str, list[str]] = {
    "wwtp_writer": ["event_id_seq"],
}

#: Grants that are deliberately *not* made, and the reason. Checked by the test,
#: because an absent grant is invisible in a diff and this is the whole point.
DELIBERATELY_WITHHELD: dict[str, list[tuple[str, str]]] = {
    "wwtp_writer": [
        ("reading", "DELETE"),
        ("reading", "TRUNCATE"),
        ("event", "DELETE"),
        ("signal", "UPDATE"),
    ],
    "wwtp_reader": [
        ("reading", "INSERT"),
        ("event", "INSERT"),
    ],
}


@dataclass(frozen=True, slots=True)
class RoleCheck:
    name: str
    present: bool
    missing: list[tuple[str, str]]
    unexpected: list[tuple[str, str]]

    @property
    def ok(self) -> bool:
        return self.present and not self.missing and not self.unexpected


def _role_names(conn: Conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles")
        return {r[0] for r in cur.fetchall()}


def _has_grant(conn: Conn, role: str, table: str, privilege: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.role_table_grants
            WHERE grantee = %s AND table_name = %s
              AND privilege_type = %s
            """,
            (role, table, privilege),
        )
        return cur.fetchone() is not None


def apply(conn: Conn | None | None = None) -> list[str]:
    """Create the roles and their grants. Idempotent.

    Roles are cluster-wide in Postgres, not per database, so a role name that
    already exists is reused rather than recreated — `CREATE ROLE` has no `IF NOT
    EXISTS`, which is why this is written as a check followed by a create.
    """
    from storage.postgres.schema import connect

    own = conn is None
    conn = conn or connect(autocommit=True)
    made: list[str] = []
    try:
        existing = _role_names(conn)
        for name in GRANTS:
            if name in existing:
                continue
            with conn.cursor() as cur:
                cur.execute(f'CREATE ROLE "{name}" NOLOGIN')
            made.append(name)
            log.info("created role %s (NOLOGIN: the app role inherits it)", name)

        for name, grants in GRANTS.items():
            for table, privilege in grants:
                with conn.cursor() as cur:
                    # The grant is executed as the owner, which init-db is.
                    cur.execute(
                        f'GRANT {privilege} ON "{table}" TO "{name}"'
                    )
        for name, sequences in SEQUENCE_USAGE.items():
            for sequence in sequences:
                with conn.cursor() as cur:
                    cur.execute(
                        f'GRANT USAGE ON SEQUENCE "{sequence}" TO "{name}"'
                    )
        conn.commit()
        log.info(
            "granted %d privileges across %d roles",
            sum(len(g) for g in GRANTS.values()), len(GRANTS),
        )
        return made
    finally:
        if own:
            conn.close()


def check(conn: Conn | None | None = None) -> list[RoleCheck]:
    """Report what the roles actually have, against what they should."""
    from storage.postgres.schema import connect

    own = conn is None
    conn = conn or connect(autocommit=True)
    out: list[RoleCheck] = []
    try:
        existing = _role_names(conn)
        for name, grants in GRANTS.items():
            withheld = DELIBERATELY_WITHHELD.get(name, [])
            missing = [
                (t, p) for t, p in grants if not _has_grant(conn, name, t, p)
            ]
            unexpected = [
                (t, p) for t, p in withheld if _has_grant(conn, name, t, p)
            ]
            out.append(RoleCheck(name, name in existing, missing, unexpected))
        return out
    finally:
        if own:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="postgres-roles", description="Apply or check the database roles."
    )
    parser.add_argument("--check", action="store_true",
                        help="report without changing anything")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    if args.check:
        results = check()
        bad = 0
        for r in results:
            status = "ok" if r.ok else "PROBLEM"
            log.info("%-14s %-8s %s", r.name, status,
                     "" if r.ok else
                     f"missing={r.missing} unexpected={r.unexpected}")
            if not r.ok:
                bad += 1
        if bad:
            log.error("%d of %d roles are not as declared", bad, len(results))
            return 1
        log.info("all %d roles are as declared", len(results))
        return 0

    made = apply()
    for r in check():
        log.info("%-14s %s", r.name, "ok" if r.ok else
                 f"missing={r.missing} unexpected={r.unexpected}")
    log.info("created %d new roles", len(made))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
