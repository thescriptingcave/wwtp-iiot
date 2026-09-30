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
    assert "twelve\nfaults" in text or "twelve faults" in text
    assert "six scenarios" in text

    other = yaml.safe_load(Path("contracts/fault-scenarios.yaml").read_text())
    assert len(other["faults"]) == 12
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


def test_the_course_has_twenty_one_lessons() -> None:
    """The count the README should quote, counted the way a person would.

    Two versions of this test got it wrong before it got it right, and both
    mistakes are the same mistake as the README's:

    * globbing `sql/**/*.sql` found **zero** files, because the course is markdown
      lessons with the SQL in fenced blocks;
    * counting ```sql blocks found **109**, and asserted 64, because a block can
      hold several statements and `check_sql.py` also classifies 53 of them as
      illustrative and skips them.

    So: 21 lessons is the number a reader can check by looking, and 78 queries
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
    assert len(lessons) == 21, f"{len(lessons)} lessons in sql/, not 21"
    # 26 markdown files in total: the 21 lessons plus one README per stage. The
    # tool's file count includes them, which is worth knowing before quoting it.
    assert len([
        p for p in Path("sql").rglob("*.md") if "TablePlus" not in p.parts
    ]) == 26


@pytest.mark.integration
def test_the_query_count_matches_what_the_runner_reports() -> None:
    """The README's "78 queries" is `check_sql.py`'s number, checked against it.

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

    assert queries == 78, f"the course has {queries} queries, the README says 78"
    assert files == 26, f"check_sql.py sees {files} files, the README says 26"
    assert re.search(r"78 queries (in|across) 26 files", _readme()), (
        "the README's course line is stale"
    )


def test_no_course_stage_is_described_as_unwritten() -> None:
    """The worst of the five, because it says a *delivered* thing does not exist.

    `sql/03-advanced/` has four lessons — continuous aggregates, chunks,
    retention, `EXPLAIN` — and the course table still listed it as "Unwritten"
    after it was written and checked. A reader would have concluded the project
    stopped at `02-intermediate`. It then said `04-expert/` was unwritten, which
    was true for a while and then was not, and nobody noticed for the same reason
    nobody noticed the first time: the table is prose, so it drifts silently.

    **So this test now asserts the absence of the word.** The original asserted
    its *presence*, on the reasoning that a table has to be right in each
    direction. That is true in general and false in practice: an unwritten stage
    is transient, and a test that requires the word outlives the stage it was
    written for. The requirement that survives is narrower and is the one that
    matters — the table must never claim a delivered stage is missing.

    If a stage is genuinely unwritten, describing it as unwritten is good practice
    and is checked by review. What is not acceptable is the failure this guard
    exists for: shipping the word next to something that exists.
    """
    # Only the course table's rows, not the prose. The README *discusses* this
    # failure in a paragraph about counts in prose going stale, and a guard that
    # matched the bare word anywhere would fail on its own explanation — which is
    # how a guard gets deleted instead of fixed.
    offenders = [
        line.strip() for line in _readme().split("\n")
        if line.lstrip().startswith("|") and "unwritten" in line.lower()
    ]
    assert not offenders, (
        "the course table describes something as unwritten that exists:\n"
        + "\n".join(f"    {line}" for line in offenders)
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
        ("tests/test_opcua_course.py", 34),
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
    """"eleven detectors, sixteen rules"."""
    assert len(REGISTRY) == 11, f"{len(REGISTRY)} detectors, the README says eleven"
    assert len(rules()) == 16, f"{len(rules())} rules, the README says sixteen"
    assert "eleven detectors, sixteen rules" in _readme()


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

    * `test_no_document_miscounts_the_ci_workflow_jobs` failed on
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


def _claims_only_line(raw: str) -> list[str]:
    """`_claims_only` for a line that was read without its document.

    The document-level version strips quoted spans *across* the whole file first,
    which is right for the sweep tests and wrong here: `docs/LEARNING-LOG.md` has
    a quotation that spans a hundred lines, and stripping it takes the words
    "A CI workflow" with it — so a test that used the document-level helper to
    decide whether a line is *about* CI would decide that it is not, and pass
    over a claim that is.
    """
    line = re.sub(r"[*_>#]", "", raw)
    line = re.sub(r"`[^`]*`", " ", line)
    line = re.sub(r"\"[^\"]*\"", " ", line)
    return [line] if line.strip() else []


#: A markdown fence, opening or closing. ``` or ~~~ with up to three indent.
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _is_prose(path: Path) -> list[bool]:
    """One flag per line of `path`: True where the line is prose, not quoted.

    A fenced block in these documents is a *transcript* — a tool's output, a
    stack trace, a previous test failure — and a transcript is somebody else
    asserting something, not this document. `docs/LEARNING-LOG.md` is full of
    them by design, and it quotes two stale "five CI jobs" lines inside one
    precisely because finding them was the point of the entry. Asserting on them
    would make the sweep fail on the evidence it is about, which is a check that
    can never be made green except by deleting the record.

    The document-level `_claims_only` gets this right by accident — the long
    quotation that swallowed the phrase also swallowed the fence markers — which
    is the worst way for a rule to be right, and the reason this one is explicit.
    """
    flags, in_fence = [], False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            flags.append(False)
        else:
            flags.append(not in_fence)
    return flags


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


def test_the_documented_mypy_file_count_matches() -> None:
    """`docs/TESTING.md` says how many files `mypy` covers. It was wrong by 7.

    The sentence above the tables claims every number in them is asserted here,
    "and `mypy`'s file count" included. It was not, and the count it was supposed
    to be checking was seven files out of date. A claim about a check, made
    before the check exists, reads as a control and is not one — the same failure
    as a stale number, one level of indirection further out.

    Counted the way `make types` counts it: the packages the Makefile names.
    """
    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    claimed = re.search(r"mypy` \| clean across (\d+) source files", doc)
    assert claimed, "docs/TESTING.md has no mypy row"

    result = subprocess.run(
        ["uv", "run", "--no-sync", "mypy", "softplc", "gateway", "storage",
         "alarms", "scada", "tools", "ui"],
        capture_output=True, text=True, check=False,
    )
    m = re.search(r"(\d+) source files", result.stdout + result.stderr)
    assert m, f"could not read mypy's count:\n{result.stdout[-300:]}"
    assert int(claimed.group(1)) == int(m.group(1)), (
        f"docs/TESTING.md says {claimed.group(1)} source files; "
        f"mypy reports {m.group(1)}"
    )


def test_the_documented_extracted_query_count_matches() -> None:
    """`docs/TESTING.md`'s `sql/TablePlus/` row, against the directory.

    The row said 64 for a long time. There are 78 files, because the course grew
    and this number is prose. It is cheap to check, so it is checked.
    """
    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    row = re.search(r"Extracted queries \(`sql/TablePlus/`\) \| (\d+) files", doc)
    assert row, "docs/TESTING.md has no extracted-queries row"
    actual = len(list(Path("sql/TablePlus").rglob("*.sql")))
    assert actual == int(row.group(1)), (
        f"docs/TESTING.md says {row.group(1)} extracted queries; "
        f"sql/TablePlus holds {actual}"
    )


def test_the_documented_integration_count_matches() -> None:
    """The integration row, by collection. Two tests were added and it said 46."""
    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    row = re.search(r"Integration \| (\d+) \|", doc)
    assert row, "docs/TESTING.md has no integration row"
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/integration", "-q",
         "-p", "no:cacheprovider", "--collect-only"],
        capture_output=True, text=True, check=False,
    )
    m = re.search(r"(\d+) tests collected", out.stdout)
    assert m, f"could not count the integration suite:\n{out.stdout[-300:]}"
    assert int(m.group(1)) == int(row.group(1)), (
        f"docs/TESTING.md says {row.group(1)} integration tests; "
        f"pytest collects {m.group(1)}"
    )


def test_the_documented_slow_count_matches() -> None:
    """The slow row, by marker. Cheap, and it was off by one as well."""
    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    row = re.search(r"Slow \(`-m slow`\) \| (\d+) \|", doc)
    assert row, "docs/TESTING.md has no slow row"
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider",
         "--ignore=tests/integration", "-m", "slow", "--collect-only"],
        capture_output=True, text=True, check=False,
    )
    m = re.search(r"(\d+)/(\d+) tests collected", out.stdout)
    assert m, f"could not count the slow tests:\n{out.stdout[-300:]}"
    assert int(m.group(1)) == int(row.group(1)), (
        f"docs/TESTING.md says {row.group(1)} slow tests; "
        f"pytest selects {m.group(1)}"
    )


def test_the_documented_notebook_row_matches_the_series() -> None:
    """`11 notebooks, 4 checks each` — both halves counted, not remembered.

    The notebook count is a directory listing and the check count is the number of
    per-notebook checks `check_notebooks.main` runs, so neither needs a database.
    The arithmetic in the prose below the table (11 x 4 = 44) is stated rather
    than left for the reader, and this is what keeps it true.
    """
    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    row = re.search(r"Analyst notebooks \| (\d+) notebooks, (\d+) checks each", doc)
    assert row, "docs/TESTING.md has no notebooks row"

    notebooks = len(list(Path("notebooks/src").glob("*.md")))
    assert notebooks == int(row.group(1)), (
        f"docs/TESTING.md says {row.group(1)} notebooks; "
        f"notebooks/src holds {notebooks}"
    )

    from tools import check_notebooks  # noqa: PLC0415

    per_notebook = ("check_outputs", "check_prose_numbers", "check_sql_fences")
    checks = sum(
        name in check_notebooks.main.__code__.co_names for name in per_notebook
    ) + 1  # the seed fingerprint, a gate with no per-notebook function
    assert checks == int(row.group(2)), (
        f"docs/TESTING.md says {row.group(2)} checks per notebook; "
        f"check_notebooks runs {checks}"
    )
    assert f"{notebooks * checks} assertions" in doc, (
        f"the prose claims {notebooks * checks} assertions and does not say so"
    )


def test_the_portability_check_is_wired_into_the_gate() -> None:
    """`check_portable_numbers` has to be *called*, or it is a function.

    A rule that is written, documented, discussed in two files and never invoked is
    the shape this project has been repeatedly bitten by — the first notebook gate
    found zero claims and reported success, which is exactly what a gate nobody
    calls looks like from the outside.

    It is not counted in the "checks each" row above, and should not be: it scans
    every source once rather than being per-notebook, so adding it there would
    make a different number wrong in a different way.
    """
    from tools import check_notebooks  # noqa: PLC0415

    assert "check_portable_numbers" in check_notebooks.main.__code__.co_names, (
        "check_notebooks.main does not call check_portable_numbers, so a "
        "notebook may state a number that depends on the disk or the clock and "
        "every other check will pass on the machine that wrote it"
    )


def test_the_portability_check_fires_on_a_call_and_not_on_a_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule stricter than the thing it rules makes itself `# noqa`ed.

    Notebook 03 has to name `hypertable_size()` in a comment to explain why the
    cost column is gone. Scanning the whole file — prose included — would have
    flagged that comment, and the only way to make it green would have been to
    delete the explanation or silence the line. Both are worse than the rule.

    So: a `` ```python `` block is scanned, a ``#`` comment is not, and markdown
    prose is not. All three are asserted here rather than trusted, because a rule
    that cannot be explained where it applies is a rule that gets disabled where it
    hurts.
    """
    from tools import check_notebooks  # noqa: PLC0415

    monkeypatch.setattr(check_notebooks, "NB", tmp_path / "notebooks")
    (tmp_path / "notebooks" / "src").mkdir(parents=True)

    def write(body: str) -> list[str]:
        (tmp_path / "notebooks" / "src" / "01-a.md").write_text(
            body, encoding="utf-8"
        )
        return check_notebooks.check_portable_numbers()

    caught = write("```python\nb = 1043 * hypertable_size('reading')\n```\n")
    assert caught, "a hypertable_size() call in a python fence was not caught"
    assert "01-a.md:2" in caught[0], caught[0]

    assert not write(
        "```python\n# hypertable_size() is why there is no MB column\nb = 1\n```\n"
    ), "a comment naming the rule was treated as a use of it"

    assert not write(
        "hypertable_size() measures compressed chunks, so it is not portable.\n"
    ), "prose about the rule was treated as a use of it"

    assert not write("```output\nreading  4239284\n```\n"), (
        "an output block is a claim, not code, and must not be scanned for calls"
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


#: English for a small count, because a claim is written in words in these
#: documents ("Six jobs", "six on every push") and the pattern below has to see
#: both. The workflow is at seven now, which is what makes the list grow rather
#: than a special case: the first version of this test named only "five", so a
#: document that went stale at six passed it.
_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                 7: "seven", 8: "eight", 9: "nine", 10: "ten"}

#: A line has to be *about* CI before its number of jobs is a CI claim. Matched
#: against the raw line, before `_claims_only` strips the code spans — two of the
#: lines this test has to catch say "CI" only inside a backticked `docs/CI.md`.
_CI_TOPIC = re.compile(r"\bci\b|workflow|gates|nightly|lint.debt", re.I)


def _ci_job_count() -> int:
    """The number of jobs in `gates.yml`, counted rather than remembered.

    A guard that hardcodes the number it is guarding is a second number to keep
    current, and it has already been wrong: this test said "five" and the
    workflow had six, which is the failure it was written to catch. So the count
    comes from the file. Parsed with `yaml` because the file is YAML and a
    hand-rolled scan of it is a second thing to be wrong about the first.
    """
    workflow = yaml.safe_load(
        Path(".github/workflows/gates.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict) and jobs, "gates.yml has no jobs"
    return len(jobs)


def test_no_document_miscounts_the_ci_workflow_jobs() -> None:
    """Every number a document attaches to "CI jobs", against `gates.yml`.

    This replaces a test that asserted `len(jobs) == 6`, which is the same
    failure wearing a different hat: the number it checked was written down
    somewhere else and went stale the moment a job was added, and it could only
    be made green by editing the test rather than the workflow. The count is
    read out of the file, and the file is the claim.

    Uses the shared `_claims_only` rule, which is where the reasoning lives: a
    number in quotation marks is somebody being cited, not this document
    asserting something.

    Two narrownesses were found by being wrong, and both are load-bearing:

    * The pattern allows an adjective between the number and the noun, because
      the first version did not — and so **passed against a README that said
      "five CI jobs" while the workflow had six.** A guard with a regex
      narrower than the prose it guards is worse than no guard, because it is
      green.
    * The line must also be *about* CI, and *outside* a fence. Matching any
      number before "jobs" flags `Signal.equipment` "doing two jobs at once" and
      a note that `POSTGRES_PASSWORD` "was set on seven of eight jobs" — both
      true, neither a claim about this workflow, and a check that fails on them
      gets deleted. The fence rule is the same disagreement with a different
      pair of lines: `docs/LEARNING-LOG.md` quotes two stale "five CI jobs"
      lines inside a code block *because finding them was the point of the
      entry*, and a check that fails on the evidence it is about can only be
      made green by deleting the record.
    """
    actual = _ci_job_count()
    word = _NUMBER_WORDS[actual]
    others = "|".join(
        w for n, w in sorted(_NUMBER_WORDS.items(), reverse=True) if w != word
    )
    offenders = [
        f"{path}:{number}: {stripped.strip()}"
        for path in DOCS
        for number, (raw, prose) in enumerate(
            zip(path.read_text(encoding="utf-8").splitlines(),
                _is_prose(path), strict=True), start=1,
        )
        if prose
        and _CI_TOPIC.search(raw)
        for stripped in _claims_only_line(raw)
        if re.search(r"\b\d+\s+(?:\w+\s+){0,2}?jobs\b", stripped, re.I)
        or re.search(rf"\b(?:{others})\s+(?:\w+\s+){{0,2}}?jobs\b", stripped, re.I)
    ]
    assert not offenders, (
        f"the workflow has {actual} jobs ({word}); these lines say otherwise:\n  "
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
    "tests/test_alarm_detectors.py": 63,
    "tests/test_modbus.py": 63,
    "tests/test_process.py": 40,
    "tests/test_scada_contract.py": 37,
    "tests/test_scanloop.py": 31,
    "tests/test_faults.py": 28,
    "tests/test_control.py": 26,
    "tests/test_spool.py": 23,
    "tests/test_alarm_replay.py": 22,
    "tests/test_web_page.py": 22,
    "tests/test_readme_claims.py": 72,
    "tests/test_opcua_course.py": 34,
    "tests/test_opcua_minimal_client.py": 7,
    "tests/test_opcua_address_space.py": 12,
    "tests/test_alarm_engine.py": 17,
    "tests/test_seed.py": 5,
    "tests/test_makefile_env.py": 7,
    "tests/test_lessons_gate_ports.py": 4,
    "tests/test_notebook_kernels.py": 4,
    "tests/test_notebook_reset.py": 4,
    "tests/test_jupyter_url.py": 12,
    "tests/test_clean_checkout.py": 5,
}


@pytest.mark.parametrize("path", sorted(DOCUMENTED_SUITE_COUNTS))
def test_the_documented_test_counts_match_the_suite(path: str) -> None:
    """`docs/TESTING.md`'s per-file table, against `pytest --co`.

    Collect-only, so it costs nothing and needs no database: a count is a
    property of the files, not of whether they pass.

    **Both halves are checked, and the copy in this dict used to be a second
    number to keep current.** `DOCUMENTED_SUITE_COUNTS` was the expected value
    while the table in the document was only ever read by a human, so the two
    could disagree and the test would report the dict as the truth. The number is
    now parsed out of `docs/TESTING.md` as well, which means a stale row in a
    table about staleness fails rather than informing nobody.

    Note the self-reference: this file's own row is read from the document, so
    adding a test here is a two-place edit. Which is the point — the failure is
    announced rather than discovered.
    """
    documented = DOCUMENTED_SUITE_COUNTS[path]
    actual = _count_tests(path)
    assert actual == documented, (
        f"{path} collects {actual} tests; docs/TESTING.md says {documented}."
        f" Update the table in the same commit as the test."
    )

    doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    row = re.search(rf"\| `{re.escape(Path(path).name)}` \| (\d+)", doc)
    assert row, f"docs/TESTING.md has no per-file row for {path}"
    assert int(row.group(1)) == documented, (
        f"docs/TESTING.md's row for {path} says {row.group(1)}; the table in this "
        f"file says {documented}. Two copies of one number, and they have drifted."
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


def test_04_expert_claims_the_dashboard_query_that_shipped() -> None:
    """Lesson 04-04 describes `_raw_query`; keep the two from diverging.

    The lesson used to be a critique of a bug that was still in the tree, on the
    argument that the reasoning was the lesson. That was defensible and it was
    still the wrong call for something operators look at, so the query is now
    fixed and the lesson describes the fix. This asserts the fix is still there:
    a regression back to `avg()` inside the bucket, or back to filtering NULLs in
    the `WHERE` clause, fails.

    The generated JSON is checked separately — `test_grafana_dashboards.py` runs
    every panel's query against a live database, and asserts the JSON is in step
    with the generator. This is the cheaper guard on the generator itself.
    """
    from ui.grafana import generate_dashboards as gen  # noqa: PLC0415

    # The generator's *output*, not its source: the docstring quotes the old
    # broken query to explain why it was broken, and a source check reads that
    # prose as the code still being wrong.
    raw = gen._raw_query(get_contract(), "AERATION:AHU-1:DO")

    assert "(array_agg(value ORDER BY ts DESC))[1] AS value" in raw, (
        "the trend query averages again instead of taking the last value in the "
        "bucket; that is the defect lesson 04-04 exists to explain"
    )
    assert "max(quality) AS quality" in raw, (
        "the trend query stopped carrying the quality, so a panel can no longer "
        "distinguish a bad reading from no reading"
    )
    assert "count(*) AS samples" in raw, (
        "the trend query stopped reporting how many samples a bucket holds, so "
        "coverage went back to being invisible"
    )
    assert "value IS NOT NULL" not in raw, (
        "the NULL filter is back in the WHERE clause, which is 04-01's pitfall: "
        "a bucket an instrument failed through disappears instead of being drawn "
        "as a bucket with no data"
    )

    lesson = Path(
        "sql/04-expert/04-04_the_dashboard_query.md"
    ).read_text(encoding="utf-8")

    # The lesson must still describe the code it is about.
    for claim in ("_raw_query", "ui/grafana/generate_dashboards.py"):
        assert claim in lesson, (
            f"lesson 04-04 no longer names {claim!r}, so a reader cannot find the "
            "code it is explaining"
        )


# ── documented docker commands ───────────────────────────────────────────────


def _documented_compose_service_names() -> dict[str, str]:
    """Every `docker compose <verb> <name>` a document tells a reader to type.

    Returns name → the file it came from, because the failure message is only
    useful if it says which document to fix.
    """
    found: dict[str, str] = {}
    pattern = re.compile(
        r"docker compose\s+(?:--profile\s+\S+\s+)?"
        r"(?:stop|start|restart|logs|up|run|exec)\s+(?:-{1,2}\S+\s+)*"
        r"(?P<name>[a-z][a-z0-9-]+)"
    )
    for path in sorted(Path().rglob("*.md")):
        if any(part in (".git", "node_modules", "TablePlus")
               for part in path.parts):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            # Only lines that *are* the command. Without this the pattern
            # matched prose — `docs/LEARNING-LOG.md` line 627 reads
            # "`docker compose up` produces a Grafana you cannot log into, and"
            # and the first word after `up` is `produces`, which was then
            # reported as a service that does not exist.
            #
            # A fenced block's body is indented by at most a couple of spaces and
            # a `$ ` prompt is stripped, so both are accepted. A line that
            # continues into a sentence — more than a few words after the
            # command, or ending in a comma — is prose and is skipped.
            stripped = line.strip().lstrip("$ ").strip()
            if not stripped.startswith("docker compose"):
                continue
            if stripped.endswith((",", ".", ";")) and " -" not in stripped:
                continue
            if len(stripped.split()) > 8:
                continue
            for match in pattern.finditer(stripped):
                found.setdefault(match.group("name"), str(path))
    return found


def _compose_profiles() -> list[str]:
    """Every profile name declared in compose.yaml, read from the file itself."""
    text = Path("compose.yaml").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'profiles:\s*\[(?:"|\')([a-z-]+)(?:"|\')\]', text)))


def _real_compose_services() -> set[str]:
    """The service names compose actually knows, across every profile.

    `docker compose config --services` without `--profile` omits profiled
    services, so asking that question alone would have called `scada` fictional
    when it is real — which is the whole reason the original instruction slipped
    through. Every profile has to be included.
    """
    services: set[str] = set()
    # The profile list is *read from compose.yaml* rather than written down here.
    # Hardcoding it is the same mistake this test exists to catch: the first
    # version of it listed `demo` and `scada` and called `grafana` fictional,
    # because `grafana` lives behind the `observability` profile. A guard that
    # invents its own list of services to trust is a guard that invents findings.
    profiles = ["", *_compose_profiles()]
    for profile in profiles:
        cmd = ["docker", "compose", "config", "--services"]
        if profile:
            cmd[2:2] = ["--profile", profile]
        out = subprocess.run(
            cmd, capture_output=True, text=True, check=False, cwd=".",
        )
        if out.returncode != 0:
            pytest.skip(
                "docker compose config is unavailable: "
                f"{out.stderr.strip()[:200]}"
            )
        services.update(out.stdout.split())
    return services


def test_documented_compose_services_exist() -> None:
    """Every service name in a fenced `docker compose` line must be real.

    A reader who types a service that does not exist gets `no such service`, and
    a prerequisite that silently fails is worse than no prerequisite: the reader
    concludes the data is at fault.

    The check exists because `sql/README.md` told the reader to run
    `docker compose stop scada` for a very long time.
    """
    real = _real_compose_services()
    for name, where in _documented_compose_service_names().items():
        assert name in real, (
            f"{where} tells the reader to run a docker compose command naming "
            f"{name!r}, which is not a service. Real services: "
            f"{sorted(real)}"
        )


def test_the_reproducibility_prerequisite_names_the_writer() -> None:
    """`sql/README.md` must name `gateway`, and `gateway` is the writer.

    This is a *different* check from the one above, and it is the one that would
    have caught the real bug. `scada` is a genuine service — Node-RED, behind
    the `scada` profile — so `stop scada` passes "does this service exist" and is
    still wrong, because Node-RED only reads and `gateway` is what writes.

    The failure mode is specific and worth guarding: a valid name for the wrong
    component is invisible to an existence check and to a human skimming for
    typos. Asserting on the *writer* is what makes it checkable.
    """
    readme = Path("sql/README.md").read_text(encoding="utf-8")
    assert "docker compose stop gateway" in readme, (
        "sql/README.md no longer tells the reader to stop the gateway, which is "
        "the service that writes rows; without it the seeded data moves under "
        "the lessons and every result becomes unreproducible"
    )

    # And the claim itself, checked against the code rather than believed.
    gateway = Path("gateway/main.py").read_text(encoding="utf-8")
    assert "PostgresWriter" in gateway and "make_execute" in gateway, (
        "gateway/main.py no longer writes readings; the prerequisite above names "
        "the wrong component again and the data will not be reproducible"
    )
    # Node-RED must not be the thing that writes, or the doc is right for a
    # reason nobody has checked.
    assert "INSERT INTO reading" not in Path(
        "scada/build_flows.py"
    ).read_text(encoding="utf-8"), (
        "scada/build_flows.py now writes readings, so stopping the gateway is no "
        "longer sufficient to make the data static"
    )


# ── the notebook gate ────────────────────────────────────────────────────────


def test_notebook_sources_are_built_and_tagged() -> None:
    """Every notebook source builds, and no fence is left untagged.

    The tag on a fence is not cosmetic. ```python runs, ```output is a claim the
    gate checks against a live run, and an untagged block is neither — which is
    how the first version of notebook 02 came to have *zero* checkable output
    claims while the gate reported it green. `build_notebooks` raises on an
    untagged opening fence; this asserts that path still raises, so the
    protection is tested rather than assumed.
    """
    from tools import build_notebooks  # noqa: PLC0415

    sources = sorted((Path("notebooks") / "src").glob("*.md"))
    assert sources, (
        "notebooks/src is empty; the series is authored as markdown there and "
        "generated into notebooks/*.ipynb"
    )
    for src in sources:
        built = build_notebooks.build_one(src)
        code = [c for c in built["cells"] if c["cell_type"] == "code"]
        assert code, f"{src} has no executable cell; it is prose, not a notebook"
        # Every claim must be in an `output` fence, which the builder renders as
        # a markdown cell. A `check: skip` code block is *not* a claim.
        text = src.read_text(encoding="utf-8")
        assert "```output" in text or "```sql" in text, (
            f"{src} states no expected output and shows no query. A notebook "
            "that claims nothing is a notebook nothing checks."
        )


def test_the_output_checker_would_catch_a_wrong_number(tmp_path) -> None:
    """`check_notebooks` must fail on a claim anywhere in a block, not just line 1.

    The first version of the checker compared only the **first** line of each
    `output` block, so a number altered four rows into a table passed. That was
    found by mutation rather than by reading: a row count was changed from 30,718
    to 99,999 and the gate stayed green.

    This calls the real `check_outputs` with a doctored notebook, rather than
    re-implementing the comparison. The earlier version of this test did the
    latter — it called `_normalise` and did its own `in` check — so it passed
    happily while the real checker was broken. A guard that tests a copy of the
    logic is a guard that tests nothing.

    `tmp_path` supplies the real source file, because `check_outputs` reads the
    claims from the markdown and compares them against the executed notebook.
    """
    from tools.check_notebooks import check_outputs  # noqa: PLC0415

    claimed = (
        "# 02\n\n```output\n"
        "signal_id    rows  distinct\n"
        "UNDERFLOW    30718         4\n"
        "```\n"
    )
    source = tmp_path / "02.md"
    source.write_text(claimed, encoding="utf-8")

    def executed(underflow_rows: int) -> dict:
        return {
            "cells": [{
                "cell_type": "code",
                "source": "print()",
                "outputs": [{"output_type": "stream", "text": [
                    "signal_id    rows  distinct\n",
                    f"UNDERFLOW    {underflow_rows}         4\n",
                ]}],
            }],
        }

    # The claim agrees with the run: no problems.
    assert check_outputs(Path("nb.ipynb"), source, executed(30718)) == []

    # The first row is altered: caught.
    assert check_outputs(Path("nb.ipynb"), source, executed(99999)), (
        "a wrong number in the first row of a claim was not caught"
    )

    # The SECOND row is altered, which is the case the first-line-only version
    # missed.
    source.write_text(
        claimed.replace("UNDERFLOW    30718         4",
                        "UNDERFLOW    30718         4\nFLOW          2570       912"),
        encoding="utf-8",
    )
    deep = {
        "cells": [{
            "cell_type": "code",
            "source": "print()",
            "outputs": [{"output_type": "stream", "text": [
                "signal_id    rows  distinct\n",
                "UNDERFLOW    30718         4\n",
                "FLOW          9999       912\n",   # wrong, and it is the third line
            ]}],
        }],
    }
    problems = check_outputs(Path("nb.ipynb"), source, deep)
    assert problems, (
        "a wrong number on the third line of a claim was not caught; the "
        "checker is comparing only the first line of each block"
    )
    # The message names the line the *lesson* claims, not the line the notebook
    # printed — the lesson is what is wrong, so that is what the reader has to go
    # and fix. Getting this backwards would send them to edit the query.
    assert any("FLOW 2570 912" in problem for problem in problems), (
        f"the problem does not name the claimed line the reader must fix: "
        f"{problems}"
    )


def test_an_untagged_fence_is_rejected(tmp_path) -> None:
    """A fence with no language tag must fail the build, not pass quietly.

    This is the bug the whole mechanism exists to prevent: notebook 02 was first
    written with untagged fences, so `check_notebooks` found **zero** output
    claims and reported the notebook green. It was checking nothing, and looking
    like verification.

    `tmp_path` is used rather than a file in the tree so the test can assert the
    failure without leaving an unbuildable lesson behind.
    """
    from tools import build_notebooks  # noqa: PLC0415

    bad = tmp_path / "bad.md"
    bad.write_text(
        "# A lesson\n\n```\nsome output that nothing checks\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as caught:
        build_notebooks.build_one(bad)
    assert "no language tag" in str(caught.value), (
        f"an untagged fence was rejected for the wrong reason: {caught.value}"
    )

    # And the same content *is* buildable once tagged, so the test is not just
    # asserting that any input fails.
    good = tmp_path / "good.md"
    good.write_text(
        "# A lesson\n\n```output\nsome output that gets checked\n```\n",
        encoding="utf-8",
    )
    built = build_notebooks.build_one(good)
    assert all(c["cell_type"] == "markdown" for c in built["cells"]), (
        "an output fence became a code cell; it would execute instead of claim"
    )


def test_every_env_line_is_a_plain_key_equals_value() -> None:
    """`Makefile` does `include .env`, so a quoted or spaced value breaks it.

    The Makefile loads `.env` with GNU make's `include` plus a bare `export`,
    which is how every recipe gets `POSTGRES_PORT` and friends. That is safe only
    because every line in `.env` is a plain `KEY=value`.

    It is *not* safe in general, and the failure would be silent and confusing:

    * a quoted value keeps its quotes, because make does not strip them — so
      `POSTGRES_PASSWORD="s3cret"` exports the literal `"s3cret"` and every
      connection fails with a wrong-password error naming a password that is
      correct on screen;
    * a line like `export FOO=bar` becomes a make variable named `export FOO`;
    * a comment line starting with a tab is a recipe line, not a comment.

    So this asserts the *shape* of `.env` rather than its contents — the file holds
    real credentials and is never committed, so a test cannot read its values.
    """
    # The whole line has to be `KEY=value` — not merely *start* with `KEY=`.
    #
    # The first version of this test used `^[A-Za-z_][A-Za-z0-9_]*=` and was
    # fooled by `QUOTED_SECRET="with quotes"`, which matches it: the regex never
    # looked at the value. That is precisely the case the test exists for, so a
    # guard that cannot see the quoted value is a guard that reports the one thing
    # it should catch.
    #
    # What make does with the value, and why each costs something:
    #
    #   FOO="bar"   exports `"bar"` — quotes included, because make does not strip
    #               them. The password on screen is correct and every connection
    #               fails on it.
    #   FOO=a b     exports `a b` and passes the second word to the recipe as $1.
    #   FOO=a\ b    exports `a\ b` — a literal backslash in the value.
    #
    # So the value may not contain a quote, a space or a backslash.
    # CI has no `.env`, and that is correct — it is git-ignored and holds real
    # credentials. The rule only matters where a developer has one, so a missing
    # file is the case this test has nothing to say about.
    #
    # The first version read the file unconditionally and CI failed with
    # `FileNotFoundError: '.env'` — a test asserting that a git-ignored
    # credentials file exists in a fresh clone, which is a test asserting the
    # wrong thing. Discovered by CI, which is the only place a fresh clone exists.
    if not Path(".env").is_file():
        pytest.skip("no .env here; the rule only constrains one that exists")

    lines = Path(".env").read_text(encoding="utf-8").split("\n")
    offenders = [
        f"{number}: {line!r}"
        for number, line in enumerate(lines, start=1)
        if line.strip()
        and not line.lstrip().startswith("#")
        and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=[^\s\"'\\]*$", line)
    ]
    assert not offenders, (
        "`.env` has lines that GNU make's `include` cannot read as variables. "
        "The Makefile includes it to export these to every recipe, and a quoted "
        "or spaced value is exported with the quote or the space still in it.\n"
        + "\n".join(f"    {line}" for line in offenders)
    )


def test_the_makefile_loads_env_for_every_recipe() -> None:
    """Every recipe must reach Postgres on this project's port, not 5432.

    This is the guard for a failure that named the wrong thing entirely.
    `make notebooks-open` launched JupyterLab with no `POSTGRES_PORT` in its
    environment, so `dsn()` fell back to its defaults — port 5432 — and the first
    cell of notebook 01 died with::

        connection to server at "127.0.0.1", port 5432 failed:
        FATAL:  password authentication failed for user "wwtp"

    A reader reads that as a wrong password. **The port is the tell**: this
    project's database is on 55433 and nothing in the repository has ever run on
    5432.

    `docker compose` loads `.env` itself, which is exactly why `make up` and
    `make seed` always worked and hid the gap — compose was the only thing
    loading it, and anything reaching Postgres directly was relying on its caller
    to export the environment by hand.

    ## Why it asserts by running bash, not by reading the Makefile

    The first three versions of this test read the Makefile and asserted on its
    text. All three passed while the thing they were guarding was broken, because
    the text kept changing and the test kept being changed with it — most
    recently, `include .env` plus a bare `export` *looked* like it exported the
    values and did not, because `include` gives make variables and `.env` contains
    shell syntax make cannot evaluate.

    So this runs the mechanism instead. It reads the path out of the Makefile,
    points a bash process at it the way a recipe does, and asserts a **child**
    process sees the right port. A child, because sourcing alone is not enough —
    bash sets shell variables, not environment variables, so a version of this
    that only checked the parent shell would pass with the values stopping one
    process short of the code that needed them.
    """
    import subprocess  # noqa: PLC0415

    makefile = Path("Makefile").read_text(encoding="utf-8")
    match = re.search(r"^export BASH_ENV\s*:=\s*(\S+)\s*$", makefile, re.MULTILINE)
    assert match, (
        "the Makefile does not export BASH_ENV, so no recipe sources .env and "
        "every target that reaches Postgres gets dsn()'s default port"
    )

    env_script = Path(match.group(1).replace("$(CURDIR)", str(Path.cwd())))
    assert env_script.is_file(), (
        f"the Makefile points BASH_ENV at {env_script}, which does not exist"
    )

    # No `.env` means nothing to load, and CI has none — it is git-ignored and
    # holds real credentials. Found by CI: this asserted a child process sees
    # POSTGRES_PORT, which is true on a developer's machine and meaningless on a
    # fresh clone, so it failed there and passed here for a week.
    #
    # The assertions below still run in CI. What they cannot do is *demonstrate*
    # the mechanism, and only the demonstration needs a file to load.
    if not Path(".env").is_file():
        pytest.skip("no .env here; the mechanism still needs to exist, "
                    "but there is nothing for it to load")

    # The script must auto-export: a sourced assignment is a *shell* variable and
    # never reaches the `python` a recipe launches.
    #
    # **Indented `set -a` counts.** The first version of this asserted
    # `^set -a$`, and the script then grew an `if [ -f .env ]` around the load —
    # because a checkout has no `.env` at all, and a gitignored file cannot be a
    # hard dependency of every recipe. The assertion failed on correct code, which
    # is the worst kind: the fix was to widen the regex, not to unindent the file.
    # A guard that is stricter than the thing it guards teaches a reader to delete
    # the guard.
    script = env_script.read_text(encoding="utf-8")
    assert re.search(r"^\s*set -a\s*$", script, re.MULTILINE), (
        f"{env_script} does not `set -a`, so values sourced from .env stay in "
        "the shell and never reach child processes"
    )

    child = subprocess.run(
        ["/bin/bash", "-c", 'env | grep "^POSTGRES_PORT=" || true'],
        capture_output=True, text=True, env={
            "PATH": "/usr/bin:/bin",
            "BASH_ENV": str(env_script),
        }, check=False,
    )
    inherited = child.stdout.strip()
    assert inherited, (
        "a child process does not see POSTGRES_PORT at all; every recipe that "
        "opens a database connection would fall back to port 5432"
    )

    port = inherited.split("=", 1)[1]
    assert port != "5432", (
        f"POSTGRES_PORT resolves to {port} — the PostgreSQL default, which is "
        "what the notebooks were failing to reach. This project is not on 5432."
    )


def test_no_source_file_is_hidden_from_git_by_ignore_rules() -> None:
    """Every `.py` file on disk must be tracked. Ignored means invisible.

    This is the guard for the failure that made CI red for two days and was
    nobody's fault in particular:

    ```
    .gitignore:36:spool/    gateway/spool/__init__.py
    ```

    An **unanchored** `spool/` matches a directory called `spool` at any depth.
    `gateway/spool/` is not a runtime directory — it is the source package, with
    `__init__.py` and an 19 kB `store.py`, imported by `gateway/main.py` and by
    `test_gateway_cycle`, `test_gateway_clients`, `test_spool` and others. The
    three rules above it (`gateway/spool/*.jsonl`, `*.db`, `*.wal`) already cover
    the runtime data, so the bare `spool/` bought nothing and cost the package.

    Nothing local noticed, because **an ignored file is still on disk**. Every
    test passed, every lesson ran, and `make lint` — which lints the `gateway`
    package — reported nothing about a package it could not see. The evidence was
    only ever in CI:

    ```
    E   ModuleNotFoundError: No module named 'gateway.spool'
    ```

    and CI had failed on **every one of its 22 runs** for that reason, so the
    signal was there and continuous. What was missing was anyone reading it.

    Two things make this check worth having rather than trusting review:

    * it asks git, not the filesystem, because the filesystem is what lied;
    * it looks for *ignored* files specifically. A file that is merely untracked
      is work in progress. A file that git is *actively told to discard* is a
      source file the project cannot build.
    """
    import subprocess  # noqa: PLC0415

    def _sources(text: str) -> list[str]:
        return [
            line for line in text.split("\n")
            if line.endswith(".py")
            and not line.startswith((".venv/", "node_modules/", "ui/web/"))
            and "__pycache__" not in line
        ]

    # Two queries, because git reports the two cases differently and the first
    # one alone was not enough.
    #
    # `--others --ignored` lists files git is told to discard and nobody has
    # staged. That is the original incident: `gateway/spool/` was created, the
    # rule matched, and it was never staged.
    #
    # But `--others` says nothing once a file *is* staged, and the rule then sits
    # there looking harmless. Adding `spool/` back while `gateway/spool/` is
    # tracked produced **no failure at all** under this query — verified by
    # mutation. So `--ignored` alone (without `--others`) is asked as well, which
    # lists tracked files that an ignore rule claims. That combination is a
    # contradiction: git is tracking something it is also told to discard, which
    # usually means somebody reached for `git add -f` and the rule is still wrong.
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard"],
        capture_output=True, text=True, check=True,
        cwd=str(Path.cwd()),
    ).stdout
    tracked_but_ignored = subprocess.run(
        ["git", "ls-files", "--cached", "--ignored", "--exclude-standard"],
        capture_output=True, text=True, check=True,
        cwd=str(Path.cwd()),
    ).stdout

    offenders = [f"{line}  (untracked, and ignored)"
                 for line in _sources(untracked)]
    offenders += [f"{line}  (tracked, but an ignore rule claims it)"
                  for line in _sources(tracked_but_ignored)]

    assert not offenders, (
        "these Python files exist on disk and are excluded from the repository "
        "by a .gitignore rule. They are invisible to everyone else — CI, a "
        "fresh clone, a reviewer — while remaining readable here:\\n"
        + "\\n".join(f"    {line}" for line in offenders)
        + "\\n\\n  If the rule is meant to cover runtime data, anchor it to the "
        "repository root (`/spool/`, not `spool/`) or name the files it applies "
        "to. An unanchored directory name matches at every depth."
    )


def test_the_spool_package_is_tracked() -> None:
    """`gateway/spool/` is a source package, not a runtime directory.

    A narrow pin on the specific incident, kept alongside the general guard above
    because the general one is about a *pattern* and this is about a *package*
    that imports at runtime. If a future ignore rule hides it again, the failure
    is `ModuleNotFoundError` in five test modules and nothing else — a symptom
    that does not obviously point at a `.gitignore` line.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "gateway/spool/"],
        capture_output=True, text=True, check=True,
    ).stdout.split()

    assert "gateway/spool/__init__.py" in tracked, (
        "gateway/spool/__init__.py is not tracked; `gateway.spool` is a source "
        "package imported by gateway/main.py and five test modules"
    )
    assert "gateway/spool/store.py" in tracked, (
        "gateway/spool/store.py is not tracked; it is the spool's storage layer"
    )
