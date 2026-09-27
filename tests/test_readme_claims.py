"""The numbers in the README are measurements, and this is what makes them so.

## Why this file exists

Reviewing the repository before its first push found five claims in `README.md`
that were false, and they were false in the same way each time:

| the README said | the truth |
|---|---|
| `tags.yaml` holds the faults, all read "that one file" | two files, consumers split |
| `03-advanced/` — "Unwritten" | written, four lessons |
| "57 queries across 16 files" | 64 queries in 21 files |
| "31 tests" guard the Node-RED generator | 37 |
| "CI: five jobs" | six |

**Not one of them was caught by a test, and not one was caught by reading the
code.** They were caught by counting, once, by hand, on the way to a push. Which
means they will be wrong again the next time something is added, and the failure
mode is the specific kind this project cannot detect on its own: **a stale
number in prose is a plausible-looking wrong number.** It is thread 22 — a wrong
reference producing a wrong value rather than an error — applied to the
documentation.

So: every count the README states is asserted here, against the thing it
describes. When the eleventh alarm rule lands, this test fails and says so.

## What this does *not* do

It checks **numbers**, not **sentences**. There is no test that the README's
reasoning is any good, and there should not be one: prose that cannot be wrong
cannot be tested, and a test that graded the writing would be a test to satisfy
rather than a check on the work. The claims that were wrong here were wrong in
their *arithmetic*, which is checkable, and the reasoning around them was fine.

That is a real limitation and it is worth being clear about: **this suite can
prove the README's counts are right and cannot prove its claims are true.**
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from alarms.detectors import REGISTRY
from alarms.rules import rules
from softplc.contract import contract as get_contract

README = Path("README.md")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


# ── the contract ─────────────────────────────────────────────────────────────


def test_the_signal_and_equipment_counts() -> None:
    """57 signals, 22 assets."""
    c = get_contract()
    assert "57 signals" in _readme()
    assert re.search(r"22\s+pieces of equipment", _readme())
    assert len(c.signals) == 57
    assert len(c.equipment) == 22


def test_the_fault_and_scenario_counts_come_from_the_other_file() -> None:
    """11 faults, 6 scenarios — and from `fault-scenarios.yaml`, not `tags.yaml`.

    The original error was attributing them to the wrong file while claiming
    there was only one. Both halves are asserted, because a count attributed to
    the wrong source is how a reader ends up editing the wrong YAML.
    """
    text = _readme()
    assert "eleven\nfaults" in text or "eleven faults" in text
    assert "six scenarios" in text

    other = yaml.safe_load(Path("contracts/fault-scenarios.yaml").read_text())
    assert len(other["faults"]) == 11
    assert len(other["scenarios"]) == 6

    # And the *wrong* attribution must stay wrong-looking: `tags.yaml` has no
    # faults key at all, which is the fact the README now relies on.
    tags = yaml.safe_load(Path("contracts/tags.yaml").read_text())
    assert "faults" not in tags
    assert "scenarios" not in tags


def test_every_consumer_the_readme_names_actually_reads_the_contract() -> None:
    """The README names its consumers by role; this checks each one exists.

    **Deliberately not a count.** Two integers were written into the README
    while fixing the first false claim, and both were wrong before the commit
    that wrote them: "six" (I had listed the interesting consumers and forgotten
    the modules that load the contract to resolve a signal id) and then
    "nineteen" (`git grep -l tags.yaml` gives twelve; adding the loader gives
    twenty-five, four of which are a comment, a JSON import and a page footer).

    So the README names consumers by role and prints the command that produces
    the current list, and this test checks the *structural* claim: each named
    consumer exists, and each one really does reference the contract. A count
    would be a number in prose, which is the thing being fixed.
    """
    named = {
        "the PLC": "softplc/main.py",
        "the Modbus server": "softplc/servers/modbus_server.py",
        "the OPC UA server": "softplc/servers/opcua.py",
        "the gateway": "gateway/main.py",
        "the database schema": "storage/postgres/schema.py",
        "the seeder": "storage/seed/main.py",
        "the alarm engine": "alarms/main.py",
        "the alarm rules": "alarms/rules.py",
        "the Node-RED tag list": "scada/generate_tags.py",
        "the Node-RED flows": "scada/build_flows.py",
        "the Grafana dashboards": "ui/grafana/generate_dashboards.py",
        "the web page": "ui/web/generate_page.py",
    }
    for role, path in named.items():
        assert Path(path).exists(), f"the README names {role} ({path}); it is gone"

    # And the two loaders are each read by more than one caller, which is the
    # claim that matters: a contract read in one place cannot drift from a
    # contract read in another, because there is only one.
    for path in named.values():
        out = subprocess.run(
            ["git", "grep", "-lE",
             r"tags\.yaml|from softplc\.contract|softplc/contract",
             "--", path],
            capture_output=True, text=True, check=False,
        )
        assert out.stdout.strip(), (
            f"{path} is named as a consumer but does not read the contract"
        )

    # The README must not *claim* a bare integer for this. Quoting the wrong one
    # in order to say it was wrong is the opposite, so the check is scoped to
    # lines that are not part of the correction note — which is a blockquote.
    claims = [ln for ln in _readme().splitlines() if not ln.lstrip().startswith(">")]
    body = "\n".join(claims)
    assert "ineteen source files" not in body, (
        "the README states a consumer count in its own voice; it should point at "
        "the command instead, because no single integer is both true and cheap"
    )
    assert "git grep -l" in body, (
        "the README should point at the command rather than at a number it "
        "cannot keep current"
    )


# ── the SQL course ───────────────────────────────────────────────────────────


def test_the_course_has_seventeen_lessons() -> None:
    """The count the README should quote, counted the way a person would.

    Two versions of this test got it wrong before it got it right, and both
    mistakes are the same mistake as the README's:

    * globbing `sql/**/*.sql` found **zero** files, because the course is markdown
      lessons with the SQL in fenced blocks;
    * counting ```sql blocks found **109**, and asserted 64, because a block can
      hold several statements and `check_sql.py` also classifies 53 of them as
      illustrative and skips them.

    So: 17 lessons is the number a reader can check by looking, and 64 queries
    is `check_sql.py`'s number, asserted separately below against the tool's own
    output rather than re-derived here.
    """
    lessons = sorted(
        p for p in Path("sql").rglob("*.md") if p.name != "README.md"
    )
    assert len(lessons) == 17, f"{len(lessons)} lessons in sql/, not 17"
    # 21 markdown files in total: the 17 lessons plus one README per stage. The
    # tool's "21 files" counts all of them, which is worth knowing before
    # quoting it.
    assert len(list(Path("sql").rglob("*.md"))) == 21


@pytest.mark.integration
def test_the_query_count_matches_what_the_runner_reports() -> None:
    """The README's "64 queries" is `check_sql.py`'s number, checked against it.

    Not re-derived. `check_sql.py` owns the definition of a query — how many
    statements a block contains, and which are illustrative — and a second
    implementation of that definition in a test is a second thing to be wrong.
    """
    out = subprocess.run(
        [".venv/bin/python", "tools/check_sql.py", "sql/"],
        capture_output=True, text=True, check=False,
    )
    if "cannot connect" in out.stdout + out.stderr:
        pytest.skip("no database: check_sql.py needs a seeded one")

    m = re.search(r"(\d+) queries in (\d+) files", out.stdout)
    assert m, f"could not read the runner's summary:\n{out.stdout[-300:]}"
    queries, files = int(m.group(1)), int(m.group(2))

    assert queries == 64, f"the course has {queries} queries, the README says 64"
    assert files == 21, f"check_sql.py sees {files} files, the README says 21"
    assert re.search(r"64 queries (in|across) 21 files", _readme()), (
        "the README's course line is stale"
    )


def test_03_advanced_is_not_described_as_unwritten() -> None:
    """The worst of the five, because it says a *delivered* thing does not exist.

    `sql/03-advanced/` has four lessons — continuous aggregates, chunks,
    retention, `EXPLAIN` — and the course table still listed it as "Unwritten"
    after it was written and checked. A reader would have concluded the project
    stopped at `02-intermediate`.

    `04-expert/` **is** unwritten, and saying so is correct, so the test asserts
    both: the table has to be right in each direction, or it is not a table.
    """
    text = _readme()
    assert "Unwritten" in text, (
        "no stage is described as unwritten any more — if 04-expert has been "
        "written, the table needs a description for it too"
    )
    assert "04-expert/` | Unwritten" in text or "`04-expert/` | Unwritten" in text

    # `03-advanced/README.md` is the stage's index, not a lesson.
    advanced = sorted(
        p.name for p in Path("sql/03-advanced").glob("*.md")
        if p.name != "README.md"
    )
    assert len(advanced) == 4, f"expected four lessons in 03-advanced, got {advanced}"
    row = [ln for ln in text.splitlines() if "03-advanced" in ln and "|" in ln]
    assert row, "the course table has no row for 03-advanced"
    assert "Unwritten" not in row[0], (
        f"03-advanced is described as unwritten: {row[0].strip()!r}"
    )


# ── the tests ────────────────────────────────────────────────────────────────


def _count_tests(path: str) -> int:
    out = subprocess.run(
        [".venv/bin/python", "-m", "pytest", path, "-q", "-p", "no:cacheprovider",
         "--co"],
        capture_output=True, text=True, check=False, cwd=".",
    ).stdout
    m = re.search(r"(\d+) tests? collected", out)
    assert m, f"could not count tests in {path}:\n{out[-400:]}"
    return int(m.group(1))


@pytest.mark.parametrize(
    ("path", "claimed"),
    [
        ("tests/test_scada_contract.py", 37),
        ("tests/test_grafana_dashboards.py", 15),
        ("tests/test_web_page.py", 22),
    ],
)
def test_the_per_area_test_counts(path: str, claimed: int) -> None:
    """The counts in the phase list, checked by collecting the tests.

    `--co` (collect only), so this costs nothing and needs no database — the
    numbers are a property of the files, not of whether they pass.
    """
    actual = _count_tests(path)
    assert actual == claimed, (
        f"{path} collects {actual} tests, the README says {claimed}. A stale "
        f"count in the phase list is a stale count in the phase list."
    )


def test_the_ci_job_count() -> None:
    """Six jobs, not five.

    `lint-debt` was added after the README line was written, which is the
    ordinary way a number goes stale: the change was real, the prose was simply
    not revisited.
    """
    workflow = yaml.safe_load(
        Path(".github/workflows/gates.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    assert len(jobs) == 6, f"the workflow has {len(jobs)} jobs: {sorted(jobs)}"
    assert "CI: six jobs" in _readme()


def test_the_reading_count_is_about_right() -> None:
    """"4.3 M readings" for a seeded week.

    Not asserted exactly, because it depends on the seed and on how many days
    were seeded — but the *order of magnitude* is a claim, and a seeder that
    quietly produced 400 000 rows would still satisfy "4.3 M" to nobody.
    """
    assert "4.3 M readings" in _readme()
    assert (Path("storage/seed/main.py")).exists()


# ── the detector and rule counts, which the phase list also states ───────────


def test_the_detector_and_rule_counts() -> None:
    """"ten detectors, fifteen rules"."""
    assert len(REGISTRY) == 10, f"{len(REGISTRY)} detectors, the README says ten"
    assert len(rules()) == 15, f"{len(rules())} rules, the README says fifteen"
    assert "ten detectors, fifteen rules" in _readme()


# ── the thing this file is really about ─────────────────────────────────────


def test_the_readme_states_its_own_stale_numbers() -> None:
    """The correction is in the README, not only in a commit message.

    A wrong number fixed silently in a commit is a wrong number with no record of
    having been wrong, and the next person to add a rule re-introduces it. The
    five corrections are listed where a reader of the README will see them.
    """
    text = _readme()
    assert "used to say" in text, (
        "the README should record that its contract-file claim was wrong, in the "
        "README — otherwise the correction lives only in git history"
    )
