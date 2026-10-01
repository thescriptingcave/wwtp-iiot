"""`tools/db_ready.py` — the check that can say why.

Three separate setup failures presented as `the database did not become reachable
in 60s`: a seeder writing to the wrong database, a compose service missing an
argument, and a volume initialised with a different password than `.env` had. None
of them was diagnosable, because the loop polled a boolean and threw the reason
away on every one of its sixty attempts.

These tests are about the two things that fix that: it distinguishes *unreachable*
from *reachable but unusable*, and it says which.
"""

from __future__ import annotations

import tools.db_ready as mod
from storage.postgres import schema


def test_it_prints_the_row_count_when_there_is_data(monkeypatch, capsys) -> None:
    """A row count, because `db-has-data` needs one and a bare `yes` would not do."""
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql):
            return self if "count(*)" in sql else None
        def fetchone(self): return (4_239_284,)


    monkeypatch.setattr(schema, "connect", Conn)
    # `--data`, because **without it the count is never computed** -- the bare check
    # that `db-live` polls only needs the exit code, and the first version of this
    # test asserted a count after a call that could not produce one.
    assert mod.main(["--data"]) == 0
    assert "4239284" in capsys.readouterr().out


def test_a_missing_table_is_not_reported_as_ready(monkeypatch) -> None:
    """`SELECT 1` succeeds against a database with no schema, which is the trap.

    `docker compose up -d db` gives you exactly that: a reachable Postgres with no
    `reading` table, because `init-db` is the service that applies the schema and
    nothing makes the bare `db` service run it. A readiness check that asks only
    "is it there" says yes, and the failure appears sixty queries later as
    `relation "reading" does not exist` — a symptom of a decision four steps back.
    """
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): raise RuntimeError(
            'relation "reading" does not exist')
        def fetchone(self): return (0,)

    monkeypatch.setattr(schema, "connect", Conn)
    assert mod.main(["--data"]) == 1


def test_an_empty_seeded_database_is_distinguished_from_an_unreachable_one(
    monkeypatch, capsys,
) -> None:
    """Both are "not ready", and they need different commands.

    This is the distinction that matters for a reader: an unreachable database wants
    `make up` started, an empty one wants a *seed*, and telling somebody to go and
    re-run the same thing is the advice that makes a problem look unfixable.
    """
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): return self
        def fetchone(self): return (0,)

    monkeypatch.setattr(schema, "connect", Conn)
    assert mod.main(["--data"]) == 1
    err = capsys.readouterr().err
    assert "no readings" in err
    assert "make up" in err
    assert "not reachable" not in err


def test_a_connection_failure_lists_what_to_check(monkeypatch, capsys) -> None:
    """Every failure names something a reader could act on.

    The first version of this module was a shell one-liner that answered yes or no,
    and the complaint it exists to fix is precisely that a boolean leaves the reader
    with nothing. So this asserts there is something: a non-empty error, a
    connection-shaped message, and more than one suggestion.
    """

    def boom():
        raise RuntimeError("connection to server at 127.0.0.1, port 5432 failed")

    monkeypatch.setattr(schema, "connect", boom)
    assert mod.main([]) == 1
    err = capsys.readouterr().err
    assert "postgres said" in err
    assert "5432" in err, "the actual error must be shown, not summarised"
    assert err.count("    - ") >= 3, (
        "three separate causes have presented this way; one suggestion is not enough"
    )


def test_the_hints_are_the_causes_that_have_actually_happened() -> None:
    """Kept honest deliberately.

    Each of these is a failure this repository has had, reported by somebody running
    a setup step on a machine nobody had run it on before. A hint that was only
    plausible would make the list longer and the advice worse, so the assertions
    here are that the real ones are still present.
    """
    joined = " ".join(mod.HINTS)
    for cause in ("POSTGRES_PASSWORD", "POSTGRES_PORT", "schema"):
        assert cause in joined, f"lost a cause that has actually bitten: {cause}"
