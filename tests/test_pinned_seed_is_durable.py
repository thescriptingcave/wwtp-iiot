"""The pinned week must still be there next week.

`notebooks/_data.py` pins its database to a fixed instant — `SEED_END`,
2026-09-29 — and eleven notebooks state numbers that a gate compares against a real
run of it. That is the arrangement the whole series rests on.

## What broke it

`storage.seed.main` ends by calling `apply_retention`, which attaches
`add_retention_policy(..., drop_after => '7 days')`. TimescaleDB evaluates that
against **`now()`**. The seed is pinned and the policy is not, so a week after the
seed the oldest chunks of a *fixed* window fall outside `now() - 7 days` and a
background job — which runs every day, logs nothing, and reports no error —
deletes them.

Found on 2026-10-01, a day and a half after the seed: `wwtp_notebooks` held
2,998,009 rows instead of 4,239,284, `min(ts)` was 2026-09-24 instead of
2026-09-22, and `timescaledb_information.chunks` had **one** chunk where the seed
writes one per day. Three notebooks failed their claimed numbers and nothing said
why.

## Why a test, when the symptom is a number changing

Because the symptom is *not* a number changing. The numbers were correct when they
were written, correct when they were last checked, and the fixture was decaying on a
schedule in between. Every existing gate compares prose to a run *at the moment it
runs*, so a database that has quietly lost two days still passes anything that
checks a number it happens to have kept.

So these tests are about the mechanism, not the value: a fixture must have no
retention policy, and `notebook_data` must remove one if a previous version left it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from notebooks._data import NOTEBOOK_DB
from storage.postgres.schema import dsn as base_dsn


def _names(path: Path) -> set[str]:
    return {n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef)}


def test_a_fixture_database_has_no_retention_policy() -> None:
    """The direct check, against the database if there is one.

    Skipped without a database, like the other database tests here, because the
    claim is about a TimescaleDB background job and a unit test cannot observe it.
    """
    psycopg = pytest.importorskip("psycopg")

    target = re.sub(r"dbname=\S+", f"dbname={NOTEBOOK_DB}", base_dsn())
    try:
        conn = psycopg.connect(target)
    except Exception as exc:  # any failure at all means "not available here"
        pytest.skip(f"no {NOTEBOOK_DB}: {type(exc).__name__}")
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT hypertable_name FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention'"
        )
        offenders = [row[0] for row in cur.fetchall()]
    assert not offenders, (
        f"{NOTEBOOK_DB} has retention policies on {offenders}. It is a pinned "
        f"fixture, not an operational store: the policy is evaluated against "
        f"now(), so it will delete the oldest days of a fixed window with no error "
        f"and no re-seed. `make notebooks-data` removes them."
    )


def test_seeding_the_fixture_declines_and_then_removes_retention() -> None:
    """Both halves, and the second is the one that matters.

    Setting `RETENTION_RAW_DAYS=0` stops a *new* policy being attached. It does
    nothing about one attached before the fix, because `apply_retention` uses
    `if_not_exists => TRUE` and the old policy is still there, still on a schedule.
    The first version of the fix did only the first half and the fixture kept
    rotting.
    """
    source = Path("tools/notebook_data.py").read_text(encoding="utf-8")
    assert '"RETENTION_RAW_DAYS"] = "0"' in source, (
        "the fixture seed does not disable retention, so a re-seed re-attaches the "
        "policy and the pinned week starts decaying again"
    )
    assert "remove_retention" in source, (
        "the fixture seed does not remove an existing retention policy. Declining to "
        "add one is not enough: `apply_retention` uses if_not_exists, so a policy "
        "attached by an earlier version outlives the code that added it."
    )


def test_the_plant_itself_keeps_its_retention() -> None:
    """The other half of the rule, so the fix cannot be over-applied.

    `wwtp` is an operational store that grows without bound, and dropping old
    readings is the correct behaviour for it. A fix that removed retention
    everywhere would be trading a slow fixture leak for an unbounded database.
    """
    seed = Path("storage/seed/main.py").read_text(encoding="utf-8")
    assert "_env_int(\"RETENTION_RAW_DAYS\", 7)" in seed, (
        "the seeder no longer attaches retention by default. That default is what "
        "keeps the plant's own database bounded, and it is the reason the fixture "
        "has to opt out explicitly rather than the behaviour being removed."
    )
    assert "remove_retention" not in seed, (
        "the seeder now removes retention policies. It is called for operational "
        "databases as well as fixtures, and it would leave the plant's database "
        "growing forever."
    )


def test_the_helper_filters_on_the_proc_and_not_the_hypertable() -> None:
    """`reading` has both a retention policy and a refresh policy.

    Removing by hypertable would take the refresh policy with it, and the continuous
    aggregates would go stale -- which is the failure `apply_refresh_policies` was
    written to prevent, and which `03-01_continuous_aggregates.md` is built on.

    **The assertions are over the string literals in the AST, not over the source
    text.** The first version grepped the function body and failed, because the
    function's own comment explains that refresh policies share these hypertables
    and so names `policy_refresh_continuous_aggregate` -- the comment was the
    evidence that the distinction is understood, and the test read it as a bug.

    That is this repository's recurring lesson arriving for the fourth time in a
    day, in a test written specifically to prevent a fifth.
    """
    assert "remove_retention" in _names(Path("storage/postgres/schema.py"))
    tree = ast.parse(Path("storage/postgres/schema.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "remove_retention")
    literals = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    joined = " ".join(literals)
    assert "proc_name = 'policy_retention'" in joined, (
        "the removal is not filtered on the procedure name, so it will also remove "
        "the continuous-aggregate refresh policies that share these hypertables"
    )
    assert "policy_refresh_continuous_aggregate" not in joined, (
        "the removal queries the refresh policies by name too, which means it can "
        "find and drop them"
    )
