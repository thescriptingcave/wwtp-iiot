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
import sys
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
    # `sql/TablePlus/` is generated *from* the lessons, so it is not a lesson.
    # Excluding it here for the same reason `check_sql.py` excludes it: the course
    # must not scan its own output, and "keep the two counts equal" is a worse
    # answer than "do not look".
    lessons = sorted(
        p for p in Path("sql").rglob("*.md")
        if p.name != "README.md" and "TablePlus" not in p.parts
    )
    assert len(lessons) == 17, f"{len(lessons)} lessons in sql/, not 17"
    # 21 markdown files in total: the 17 lessons plus one README per stage. The
    # tool's "21 files" counts all of them, which is worth knowing before
    # quoting it.
    assert len([
        p for p in Path("sql").rglob("*.md") if "TablePlus" not in p.parts
    ]) == 21


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
        ("tests/test_opcua_course.py", 32),
        ("tests/test_opcua_minimal_client.py", 7),
        ("tests/test_opcua_address_space.py", 12),
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


# ── the same discipline across every other document ──────────────────────────

#: Every markdown file a reader might open, and the counts each of them states.
#:
#: The five false claims were all in `README.md` because that is what I read. The
#: same sweep then found the identical stale number — "57 queries" — in four more
#: files, and "31 tests" in a fifth. **A count is only checked in the document you
#: happened to read**, so this is the whole set.
DOCS = sorted(
    p for p in Path().rglob("*.md")
    if not any(x in p.parts for x in ("node_modules", ".next", ".git"))
)


def _claims_only(path: Path) -> list[str]:
    """The lines of a document that *assert* something, not the lines that quote.

    **Double-quoted text and markdown code spans are removed first**, so a
    document recording a mistake ("it said '57 queries' until a test caught it")
    is not treated as making the claim.

    This rule was arrived at the hard way, three times in one review, because
    three separate sweep tests each caught their own correction note:

    * `test_no_document_says_the_ci_workflow_has_five_jobs` failed on
      `docs/CI.md` saying *"It said 'five jobs' until a test caught it"*;
    * `test_no_document_quotes_a_stale_query_count` failed on this file's own
      Phase 6b entry quoting "57 queries";
    * `test_no_document_quotes_a_stale_test_count` failed on the same entry
      quoting "31 tests".

    A check that cannot tell a **claim** from a **quotation** cannot be fixed
    without deleting the explanation of what it is for, and the explanation is
    worth more than the check's convenience. So the rule is explicit and shared.

    The failure mode is real: a document *could* hide a wrong claim inside
    quotation marks. That is a price worth paying, and a document that wants to
    assert a wrong number in scare quotes deserves the failure.
    """
    text = path.read_text(encoding="utf-8")
    # Strip quoted spans from the **whole document** first, not line by line: a
    # quoted sentence wraps, and a per-line regex cannot match across the
    # newline. `docs/TESTING.md` quotes the old claim over two lines and the
    # first version of this rule reported it as a live claim.
    text = re.sub(r"\"[^\"]*\"", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text, flags=re.S)
    out = []
    for raw in text.splitlines():
        line = re.sub(r"[*_>#]", "", raw)
        if line.strip():
            out.append(line)
    return out


def test_no_document_quotes_a_stale_query_count() -> None:
    """"57 queries" appeared in five files. None of them now does.

    Not "the README is right" — *no document* is wrong, because a reader who
    finds the number in `docs/TESTING.md` has no way to know the README says
    something different. Every copy has to be right or the claim is unreliable
    wherever it is found.
    """
    offenders = [
        f"{p}: {ln.strip()}"
        for p in DOCS
        for ln in _claims_only(p)
        if re.search(r"\b57 (queries|course)", ln)
    ]
    assert not offenders, (
        "these documents quote 57 course queries; it is 64:\n  "
        + "\n  ".join(offenders)
    )


def test_the_documented_unit_suite_total_matches() -> None:
    """`docs/TESTING.md`'s headline unit count, against `pytest --co`.

    The per-file table above is guarded entry by entry. The *total* was not, and
    it was 49 out of date — which is the interesting part: a number nobody checks
    rots, and it rots silently, and the document about verification is where you
    would least expect to find an unverified number.
    """
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
         "--ignore=tests/integration", "-m", "not slow and not integration",
         "--collect-only"],
        capture_output=True, text=True, check=False,
    )
    m = re.search(r"(\d+)/(\d+) tests collected", out.stdout)
    assert m, f"could not count the unit suite:\n{out.stdout[-400:]}"
    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    claimed = int(re.search(r"Unit, no database \| (\d+) \|", doc).group(1))
    assert int(m.group(1)) == claimed, (
        f"docs/TESTING.md says {claimed} unit tests; pytest collects {m.group(1)}"
    )


def test_the_documented_lesson_snippet_count_matches() -> None:
    """The OPC UA course row, against the gate's own `--list`."""
    out = subprocess.run(
        [sys.executable, "tools/check_lessons.py", "--list"],
        capture_output=True, text=True, check=False,
    )
    m = re.search(r"(\d+) to run, (\d+) skipped, across (\d+) lessons", out.stdout)
    assert m, f"could not count the lesson snippets:\n{out.stdout[-400:]}"
    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    row = re.search(r"OPC UA course \| (\d+) snippets? in (\d+) lesson", doc)
    assert row, "docs/TESTING.md has no OPC UA course row"
    assert int(row.group(1)) == int(m.group(1)), (
        f"docs/TESTING.md says {row.group(1)} snippets; the gate found {m.group(1)}"
    )
    assert int(row.group(2)) == int(m.group(3)), (
        f"docs/TESTING.md says {row.group(2)} lessons; the gate found {m.group(3)}"
    )


def test_no_document_quotes_a_stale_test_count() -> None:
    """"31 tests" on the Node-RED flows; it is 37."""
    offenders = [
        f"{p}: {ln.strip()}"
        for p in DOCS
        for ln in _claims_only(p)
        if re.search(r"\b31 tests\b", ln)
    ]
    assert not offenders, (
        "these documents quote 31 tests for the Node-RED flows; it is 37:\n  "
        + "\n  ".join(offenders)
    )


def test_no_document_says_the_ci_workflow_has_five_jobs() -> None:
    """Six, including the lint-debt ratchet added after the prose was written.

    Uses the shared `_claims_only` rule, which is where the reasoning lives: a
    number in quotation marks is somebody being cited, not this document
    asserting something.

    The pattern allows an adjective between the number and the noun, because the
    first version of this test did not — and so **passed against a README that
    said "five CI jobs" while the workflow had six.** A guard with a regex
    narrower than the prose it guards is worse than no guard, because it is
    green. Caught by reading the failure mode rather than the pass.
    """
    offenders = [
        f"{path}: {line.strip()}"
        for path in DOCS
        for line in _claims_only(path)
        if re.search(r"\b(?:five|5)\s+(?:\w+\s+){0,2}?jobs\b", line, re.I)
    ]
    assert not offenders, (
        "the workflow has six jobs; these lines claim five:\n  "
        + "\n  ".join(offenders)
    )


def test_the_lint_debt_baseline_matches_the_files() -> None:
    """`lint-debt-baseline.txt` holds the number `make lint-debt` compares to.

    The ratchet is only as good as the number in it. A baseline that has drifted
    from reality either blocks everything (too low, once findings are fixed) or
    permits an increase (too high) — and both are silent, because the ratchet
    prints a comparison rather than a verdict on the count itself.

    This is the test that keeps the ratchet honest, and it is worth noting that
    **it failed the first time it ran**, because adding this file added two
    findings and the baseline had not been moved with them. A ratchet checked by
    hand is a ratchet that will be wrong on the day somebody adds a file.
    """
    out = subprocess.run(
        ["uv", "run", "--no-sync", "ruff", "check", ".",
         "--output-format", "concise"],
        capture_output=True, text=True, check=False,
    )
    actual = len(re.findall(r":\d+:\d+:", out.stdout))
    baseline = int(Path("lint-debt-baseline.txt").read_text().strip())
    assert actual == baseline, (
        f"lint-debt-baseline.txt says {baseline} and the tree has {actual}. "
        f"Lower the baseline in the same commit as any fix."
    )


#: The per-file counts `docs/TESTING.md` states, in the same order as its table.
#:
#: Added because the document claimed its own numbers were asserted before they
#: were. **A claim about a check, made before the check exists, is the same
#: failure as a stale number** — it reads as a control and is not one.
DOCUMENTED_SUITE_COUNTS = {
    "tests/test_contract.py": 63,
    "tests/test_alarm_detectors.py": 61,
    "tests/test_modbus.py": 63,
    "tests/test_process.py": 40,
    "tests/test_scada_contract.py": 37,
    "tests/test_scanloop.py": 31,
    "tests/test_faults.py": 28,
    "tests/test_control.py": 26,
    "tests/test_spool.py": 23,
    "tests/test_alarm_replay.py": 22,
    "tests/test_web_page.py": 22,
    "tests/test_readme_claims.py": 49,
    "tests/test_opcua_course.py": 32,
    "tests/test_opcua_minimal_client.py": 7,
    "tests/test_opcua_address_space.py": 12,
    "tests/test_alarm_engine.py": 17,
}


@pytest.mark.parametrize("path", sorted(DOCUMENTED_SUITE_COUNTS))
def test_the_documented_test_counts_match_the_suite(path: str) -> None:
    """`docs/TESTING.md`'s per-file table, against `pytest --co`.

    Collect-only, so it costs nothing and needs no database: a count is a
    property of the files, not of whether they pass.

    Note the self-reference: this file's own row says 17, and it will be wrong
    the moment a test is added here. Which is the point — the failure is
    announced rather than discovered, and a stale row in a table about staleness
    would be a poor joke.
    """
    documented = DOCUMENTED_SUITE_COUNTS[path]
    actual = _count_tests(path)
    assert actual == documented, (
        f"{path} collects {actual} tests; docs/TESTING.md says {documented}."
        f" Update the table in the same commit as the test."
    )


def test_testing_md_does_not_deny_the_gates_exist() -> None:
    """`docs/TESTING.md` said the repository had no CI and no `Makefile`.

    Both were true when written and both were false when read. A document that
    reports a control as *missing* when it is present is not being careful — it
    is wrong in the direction that makes a project look unrigorous, which is the
    mirror image of `docs/SECURITY.md` claiming the database was unprotected
    when it was the most carefully built part of the project.

    Both were the same mistake: prose written once and never revisited, describing
    a state the code had left behind.
    """
    # The shared `_claims_only` rule: the correction paragraph quotes the old
    # sentence, and a quotation is not a claim.
    body = "\n".join(_claims_only(Path("docs/TESTING.md")))
    for line in body.splitlines():
        if "no ci" in line.lower():
            pytest.fail(
                f"docs/TESTING.md still claims there is no CI: {line.strip()!r}"
            )
    assert Path(".github/workflows/gates.yml").exists()
    assert Path("Makefile").exists()
    # Against the *raw* text, not `body`: the workflow path is written in a code
    # span, and `_claims_only` strips code spans. Checking the stripped text here
    # was a second mistake in this one test — the rule that excludes quotations
    # also excludes the thing being pointed at.
    assert "gates.yml" in Path("docs/TESTING.md").read_text(encoding="utf-8"), (
        "TESTING.md should point at the workflow it describes"
    )


# ── the Modbus word order, which two documents got wrong ─────────────────────


def test_the_word_order_counts_are_right() -> None:
    """17 big, 2 little — and both documents said otherwise.

    `README.md` said "**Nineteen** of the registers deliberately use low-word-first
    ordering while their neighbours use high-word-first", and
    `docs/DATA-FLOW.md` illustrated the trap with a YAML pair at addresses
    `40101` and `40103` that **do not exist** — the real ones are `40100` and
    `40102`, and both are `big`.

    Two things are wrong with that, and the second is worse than the count:

    * the count, and
    * the **direction**. Saying most registers are low-word-first makes the
      *common* case sound like the dangerous one, so a reader goes looking for the
      trap in the wrong sixteen registers. The trap is two, and one of them is
      the fifth in a run of `big` ones rather than the neighbour of one.

    Found by writing the verification steps in `docs/VERIFYING.md` and running
    step 3.2, which printed `0 low-word-first, 19 high-word-first` — obviously
    wrong in a way that pointed straight at the sentence.
    """
    c = get_contract()
    little = [r for r in c.registers if r.word_order == "little"]
    big = [r for r in c.registers if r.word_order == "big"]
    assert len(c.registers) == 19
    assert len(little) == 2, [r.name for r in little]
    assert len(big) == 17
    assert {r.name for r in little} == {"AERATION_BLOWER_VALVE", "AERATION_WASTE_RATE"}

    assert "Seventeen of the nineteen" in _readme()
    assert "Nineteen of the registers" not in "\n".join(
        _claims_only(Path("README.md"))
    )

    flow = Path("docs/DATA-FLOW.md").read_text(encoding="utf-8")
    for address in ("40100", "40102", "40108"):
        assert f"address: {address}" in flow, (
            f"DATA-FLOW.md should show the real address {address}"
        )
    for ghost in ("40101", "40103"):
        assert f"address: {ghost}" not in flow, (
            f"DATA-FLOW.md still shows {ghost}, which is not a register address"
        )
    assert "two of the nineteen" in flow


def test_every_address_in_data_flow_exists_in_the_contract() -> None:
    """The general rule behind the two fixed examples.

    `docs/DATA-FLOW.md` is the document that traces a scan end to end, and it
    carried four lines of hand-written YAML naming two registers that do not
    exist. Hand-written examples of generated data are the same failure as
    hand-written thresholds: correct on the day they are written and *plausibly*
    wrong afterwards, with nothing to object.

    So: every 5-digit address in that document must be a real register address.
    That is a cheap check, and it is the one that would have caught it.
    """
    addresses = {r.address for r in get_contract().registers}
    # `_claims_only`, not the raw text: the correction paragraph quotes the two
    # addresses that were wrong in order to record them, and code spans are
    # stripped. The first version of this test failed on its own correction —
    # the fourth time in this review that a check has caught the record of a
    # mistake rather than the mistake.
    flow = "\n".join(_claims_only(Path("docs/DATA-FLOW.md")))
    mentioned = {int(n) for n in re.findall(r"\b(4\d{4})\b", flow)}
    unknown = mentioned - addresses
    assert not unknown, (
        f"DATA-FLOW.md mentions register addresses that do not exist: "
        f"{sorted(unknown)}. Real addresses: {sorted(addresses)}"
    )


# ── the service table, which described a service that did not exist ──────────


def test_the_architecture_service_table_matches_compose() -> None:
    """`docs/ARCHITECTURE.md` said `web` had **"no source yet"**, for four phases.

    Not a number this time — a **description of a service**, and the same class of
    failure: prose describing a state the code had left behind. Found by asking
    whether a claim in a getting-started document was still true, which is the
    cheapest review there is and the one nobody does.

    So the table is checked against `compose.yaml` rather than trusted: every
    service compose declares appears in the table, and nothing in the table is
    absent from compose. That catches a *stale* row. It cannot catch a *wrong*
    description — "no source yet" is prose — but it can catch a row that has
    outlived its service, which is how this one went unnoticed.
    """
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    declared = set(compose["services"])

    table = [
        ln for ln in Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        .splitlines() if ln.startswith("| `")
    ]
    listed = {
        ln.split("`")[1] for ln in table if ln.split("`")[1] in declared
    }
    assert listed == declared, (
        f"compose declares {sorted(declared)} but ARCHITECTURE.md lists "
        f"{sorted(listed)}"
    )
    for ln in table:
        if "no source" in ln or "not started" in ln or "not written" in ln:
            pytest.fail(
                f"ARCHITECTURE.md still describes a service as unwritten: {ln!r}"
            )


def test_getting_started_does_not_deny_a_service_exists() -> None:
    """The sentence this whole thread started from.

    *"There is no Next.js dashboard yet. `ui/web` has a Dockerfile and no
    application, because Phase 5 has not been written."* True when written, false
    for four phases.

    A getting-started document that tells a reader a **working** page does not
    exist is worse than one that is silent, because the reader follows it and then
    concludes the project is unfinished. Matched on the raw text minus the
    correction blockquote, which is the one place the sentence legitimately
    appears.
    """
    body = "\n".join(
        ln for ln in Path("docs/GETTING-STARTED.md").read_text(encoding="utf-8")
        .splitlines() if not ln.lstrip().startswith(">")
    )
    for phrase in ("no Next.js dashboard", "has a Dockerfile and no"):
        assert phrase not in body, (
            f"GETTING-STARTED.md still says {phrase!r} — the dashboard exists "
            f"and serves 200 on three routes"
        )
    # And it should tell the reader how to start it.
    assert "--profile ui up -d web" in body


# ── the getting-started configuration reference, against compose ────────────


def _compose_variables() -> tuple[set[str], set[str]]:
    """Every variable compose interpolates, split into required and optional.

    `${VAR:?msg}` is required — compose **refuses to start** without it.
    `${VAR:-default}` and `${VAR-default}` are optional.

    Read out of `compose.yaml` as text rather than via `docker compose config`,
    because the required-ness *is* the interpolation syntax and `config` resolves
    it away. Running the `config` command in a test would also need every
    variable set, which is the very thing being tested.
    """
    text = Path("compose.yaml").read_text(encoding="utf-8")
    required: set[str] = set()
    optional: set[str] = set()
    for var, suffix in re.findall(r"\$\{(\w+)(:\?|:\?|-|:-)", text):
        (required if suffix in (":?", ":?") else optional).add(var)
    return required, optional


def test_every_required_compose_variable_is_in_the_configuration_reference() -> None:
    """`GETTING-STARTED.md` said "nothing marked REQUIRED" and listed two passwords.

    It was wrong for a phase, and wrong in the most damaging place in the
    document: the section whose entire purpose is to stop a fresh clone failing
    for a reason that has nothing to do with the project.

    `GATEWAY_DB_PASSWORD` and `WEB_DB_PASSWORD` are `${VAR:?…}` — compose refuses
    to start without them — and neither appeared in the configuration table. A
    reader who edited `.env` from the table alone would have hit

        error while interpolating services.gateway.environment.POSTGRES_PASSWORD:
        required variable GATEWAY_DB_PASSWORD is missing a value

    on their first `docker compose up`.

    To be fair to `.env.example`: it supplies all of them, so `cp .env.example .env`
    starts the stack and no reader actually hit that. **The table was still
    wrong**, and a table that omits two required variables is worse than no table,
    because it looks authoritative.
    """
    required, _ = _compose_variables()

    reference = Path("docs/GETTING-STARTED.md").read_text(encoding="utf-8")
    table = reference[reference.index("## Configuration reference"):]
    table = table[: table.index("## Troubleshooting")]

    missing = sorted(v for v in required if f"`{v}`" not in table)
    assert not missing, (
        f"compose requires {sorted(required)} but GETTING-STARTED.md's "
        f"configuration table does not mention: {missing}"
    )


def test_getting_started_does_not_claim_nothing_is_required() -> None:
    """The sentence, and the reason it matters.

    *"There is nothing marked REQUIRED"* was a deliberate design decision — the
    previous stack began with an InfluxDB licence key that a human had to fetch,
    and every fresh `docker compose up` failed because of it. Removing that was a
    real improvement and is still true: **no licence key, no account, nothing to
    obtain.**

    What became false is the absolute. Three `${VAR:?…}` variables exist, and the
    fix for the per-service credentials is precisely that they are mandatory
    rather than silently defaulting to the owner's password.

    So the test does not demand the words "nothing marked REQUIRED" stay. It
    demands the *licence key* claim stays, because that is the part that is still
    true and still worth saying, and because a test that pinned the false absolute
    would be pinning the bug.
    """
    body = "\n".join(
        ln for ln in Path("docs/GETTING-STARTED.md").read_text(encoding="utf-8")
        .splitlines() if not ln.lstrip().startswith(">")
    )
    assert "INFLUX_LICENSE_KEY" in body, (
        "the previous stack's licence-key failure is the reason this document "
        "exists; losing it loses the point of section 1"
    )
    # Whitespace-insensitive, because the sentence is wrapped by the formatter
    # and pinning the wrapping would be a test about typography. The first
    # version of this assertion looked for the literal "no licence key" and
    # failed on a line break — which is the third time in this review that a check
    # has been defeated by the shape of the text rather than its content.
    flat = re.sub(r"\s+", " ", body).lower()
    assert "no licence key" in flat
    required, _ = _compose_variables()
    assert required, "no required variables found — the regex has stopped matching"
    for var in sorted(required):
        assert var in body, f"{var} is required by compose and absent from the guide"


# ── commands in a getting-started document must be pasteable ────────────────

#: The documents a reader follows *while a terminal is open*, as opposed to the
#: ones they read. Only these are held to the pasteability rule, because only
#: these are copied line by line.
FOLLOW_ALONG = ["docs/GETTING-STARTED.md", "docs/VERIFYING.md", "README.md"]


def _fenced_blocks(path: Path) -> list[tuple[str, list[str]]]:
    """(fence language, lines) for every closed block. Unbalanced fences raise."""
    out: list[tuple[str, list[str]]] = []
    language: str | None = None
    body: list[str] = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if language is None:
            if line.startswith("```"):
                language = line[3:].strip()
                body = []
            continue
        if line.startswith("```"):
            out.append((language, body))
            language = None
            continue
        body.append(f"{lineno}: {line}")
    assert language is None, f"{path} has an unclosed code fence"
    return out


def test_no_command_line_carries_a_trailing_comment() -> None:
    """`docker compose --profile ui up -d web     # or: make web` reached the shell.

    A reader pasted that line from this file and got

        no such service: #

    **I could not reproduce the precise shell mechanism**, and that is the reason
    for this rule rather than a diagnosis of it. A `#` starts a comment only at
    the start of a word, and only when the shell is parsing comments at all, so a
    trailing comment is *unreliable in exactly the way that matters* — it worked
    when I tested it, in bash and in zsh, and it failed for the reader.

    So this is not an argument about which shell did what. **A command in a
    getting-started document is not a snippet, it is something to paste**, and a
    line that parses differently in two shells is a defect even when it works most
    of the time. The note belongs on its own line, where it cannot be mistaken for
    part of the command.

    25 of these were fixed. Note the corollary in the docstring of the block below:
    **log output and expected values are not commands**, which an automated pass
    over this file got wrong by turning two log lines into shell comments.
    """
    offenders: list[str] = []
    for name in FOLLOW_ALONG:
        path = Path(name)
        if not path.exists():
            continue
        for language, body in _fenced_blocks(path):
            if language not in ("bash", "sh", "make", "console"):
                continue
            for line in body:
                content = line.split(": ", 1)[1] if ": " in line else line
                stripped = content.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if re.search(r"\S\s+#", content):
                    offenders.append(f"{name}: {line}")

    assert not offenders, (
        "these command lines have a trailing # comment, which reaches the shell "
        "as an argument in some contexts:\n  " + "\n  ".join(offenders)
    )


#: What a line has to look like before this file treats it as *something to run*.
#: Deliberately narrow. A block of expected output is allowed to go unnamed,
#: because naming ten psql tables and log excerpts `text` is churn that buys
#: nothing — the rule exists to stop a **command** escaping the checks, not to
#: enforce tidiness.
_COMMAND_SHAPED = re.compile(
    r"^\s*(?:"
    r"docker|make|uv|git|npm|node|\.venv/bin/python|python3?|psql|curl|cat|ls|cd|"
    r"for |while |if |export |source |grep |awk |sed |rm |mv |cp "
    r")"
)


def test_a_block_containing_a_command_is_named() -> None:
    """A fence with no language escapes the pasteability rules — and silently.

    The failure mode of a regex over documentation is silence: a pattern that
    stops matching reports "no problems found", indistinguishable from a clean
    file. So this test is about **commands going unnamed**, not about tidiness.

    The first version demanded a language on *every* block and immediately
    objected to ten unnamed blocks that were all psql tables, log excerpts and
    the architecture diagram. Those are output, and naming them `text` is churn
    that buys nothing. The rule is now the precise one: a block must be named
    **iff it contains something a reader might run**.

    An unclosed fence is the other half, and it is worse: the page still renders,
    it just renders everything after the stray ``` as code. `_fenced_blocks`
    asserts on that, so this test cannot pass against a malformed file.
    """
    for name in FOLLOW_ALONG:
        path = Path(name)
        if not path.exists():
            continue
        for language, body in _fenced_blocks(path):
            if language:
                continue
            for line in body:
                content = line.split(": ", 1)[-1]
                if content.strip().startswith("#"):
                    continue
                if _COMMAND_SHAPED.match(content):
                    pytest.fail(
                        f"{name}:{line.split(':')[0]} — a block containing a "
                        f"command has no language, so it escapes the "
                        f"pasteability rules: {content.strip()[:60]!r}"
                    )


def test_the_fence_helper_would_notice_a_command_it_cannot_see() -> None:
    """Self-check on the extractor, so the test above is not vacuous.

    The failure mode for a regex over documentation is silence: a pattern that
    stops matching reports "no problems found", which is indistinguishable from a
    clean file. This asserts the extractor sees blocks, sees commands, and would
    flag one if it were there.
    """
    path = Path("docs/VERIFYING.md")
    bash_blocks = [b for lang, b in _fenced_blocks(path) if lang == "bash"]
    assert len(bash_blocks) > 20, (
        f"only {len(bash_blocks)} bash blocks found in VERIFYING.md — the fence "
        f"parser has stopped matching"
    )
    commands = [
        ln for body in bash_blocks for ln in body
        if ln.split(": ", 1)[-1].strip()
        and not ln.split(": ", 1)[-1].strip().startswith("#")
    ]
    assert len(commands) > 50, f"only {len(commands)} command lines seen"


def test_the_guide_never_hardcodes_a_port_a_reader_must_visit() -> None:
    """A hardcoded `localhost:3000` sent a reader into a different application.

    Verified by hand for a phase against `http://localhost:3000`, which on this
    machine is an unrelated Next.js dev server from another repository. It
    returned **HTTP 200**, it had a **login page**, and it had no datasource and
    no dashboards — which is indistinguishable from "Grafana is broken" unless you
    already know what Grafana should look like.

    The port is `GRAFANA_PORT` and defaults to 3000, so the default is *correct*
    and still wrong often enough to matter, because a busy machine has 3000 taken
    and whatever grabs it will be a plausible-looking web application.

    So the guide must name the variable and tell the reader how to find the real
    port (`docker compose port grafana 3000`), rather than printing a number that
    is only right on an idle machine.
    """
    guide = Path("docs/GETTING-STARTED.md").read_text(encoding="utf-8")

    assert "docker compose port grafana 3000" in guide, (
        "the guide must tell the reader how to find the real port, not print one"
    )
    assert "Do not assume 3000" in guide

    # Both follow-along documents, not just the guide: `docs/VERIFYING.md` is the
    # one a reader runs *while checking a live system*, so a wrong port there is
    # worse — it produces a failing check that looks like a product fault.
    documents = ["docs/GETTING-STARTED.md", "docs/VERIFYING.md"]

    # The rule is about **instructions to visit**, not every mention of a port.
    # The paragraph above has to be able to say "I was wrong, here is the URL I
    # used" — and a test that forbids that would force the correction to be
    # deleted, which is the trade this project has refused four times now.
    #
    # So only *instruction-shaped* lines count: "On <http…>", or a line that
    # starts with `curl` / `open`. Prose that mentions a URL is not an
    # instruction, and the second version of this test failed on exactly that.
    instruction = re.compile(r"^\s*(?:curl\b|open\b|wget\b)|On\s*<https?://")
    offenders: list[str] = []
    seen: list[tuple[str, int, str]] = []
    for name in documents:
        path = Path(name)
        if path.exists():
            seen.extend(
                (name, n, ln)
                for n, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            )
    for name, lineno, line in seen:
        if not instruction.search(line):
            continue
        if "${" in line or "docker compose exec" in line:
            continue  # a variable, or inside the container where 3000 is right
        if "docker compose port" in line:
            continue  # the command that *finds* the port
        if re.search(r"`\w*PORT`", line):
            # The default value plus the variable that governs it, on the same
            # line — "On <http://127.0.0.1:3001> (`WEB_PORT`)". That is the shape
            # a reader can act on, because markdown cannot interpolate a shell
            # variable and the alternative is not printing a URL at all.
            #
            # So the defect this test exists for is narrower than "a hardcoded
            # port": it is **a hardcoded port with nothing saying which variable
            # moves it**, which is exactly what sent a reader to another
            # application's login page.
            continue
        for match in re.finditer(r"(?:localhost|127\.0\.0\.1):(\d+)", line):
            if match.group(1) in ("5432", "4840"):
                continue  # container-internal protocol ports
            offenders.append(f"{name}:{lineno}: {line.strip()[:70]}")
    assert not offenders, (
        "these lines tell a reader to visit a hardcoded host port:\n  "
        + "\n  ".join(offenders)
    )
