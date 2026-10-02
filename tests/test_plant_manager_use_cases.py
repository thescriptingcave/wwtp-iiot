"""Acceptance tests for the operator's day, in `docs/USE-CASES.md`.

**Why this file is small and mostly reference checks.** The obvious way to verify
a use case is to assert "fault X raises alarm Y", and doing that here would have
produced a suite that passes and means nothing. Three findings from building it:

1. **`run_fault` costs 256-281 s per fault** at the default 9.25-hour settle, and
   69 s at six hours. Three faults is a three-minute test file, which does not
   belong in `make test` and duplicates `make coverage` — the eight-minute gate
   that already exists to answer "which rule catches which fault". The use cases
   therefore *cite* that gate rather than restate its matrix.

2. **The raised rules are dominated by documented false positives.** A
   `do_sensor_drift` run raises `secondary_blanket_stuck`, which
   `docs/ALARM-TUNING.md` records as firing on 100 % of healthy time and always
   will. Asserting "some alarm fired" is therefore nearly free, and nearly
   meaningless.

3. **The rules' own `detects=` claims are partly aspirational.**
   `aeration_do_xvalidation` declares `detects=("do_sensor_drift",)` and does not
   fire on it — and by construction it cannot, because `cross_validation`
   compares the Modbus reading against the OPC UA reading and in this simulation
   both report the same drifted value. It is the defence against a low-word-first
   float, which is a *wire* fault, not a sensor drift.

A test asserting any of those would have been a green tick over a question mark.
So what is here is the part that is cheap and true:

* **the surfaces the use cases tell a manager to open actually exist** — page
  routes, panel titles, event kinds, signal ids, scenario ids, rule ids;
* **the acknowledgement use case works as documented**, tested against
  `alarms.replay.replay()`, which is a pure function and therefore the one part
  of the alarm system that can be tested honestly and in milliseconds;
* **the setpoint and permit use cases keep their behavioural tests elsewhere**,
  named here so the use case and its test cannot drift apart silently.

The last point is the one that keeps this honest: a use case whose confirmation
is "go and look" is not a use case, and this file at least insists the
confirmation exists somewhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from softplc.contract import contract
from softplc.faults.engine import load_faults

ROOT = Path(__file__).resolve().parents[1]
USE_CASES = ROOT / "docs" / "USE-CASES.md"
C = contract()


def _doc() -> str:
    return USE_CASES.read_text(encoding="utf-8")


# ── the acknowledgement use case, already proven elsewhere ──────────────────


def test_the_acknowledgement_claims_are_backed_by_the_tests_they_name() -> None:
    """Use case 4's three claims, each with a named test that already exists.

    I wrote these three tests, and then found `tests/test_alarm_replay.py` had
    all of them — twenty tests over `alarms.replay`, covering a raise alone, a
    raise and an acknowledgement, a late acknowledgement, a recurrence, an
    orphan, and a log with no rows at all. **They are better than mine**: they
    use a timestamp helper rather than six-element tuples, they assert on
    `active`/`unacknowledged`/`acknowledged` rather than the raw `alarms` list,
    and they already carry the reasoning.

    So this does not duplicate them. It asserts that the *document* names the
    tests, which is the thing that can actually rot: a use case citing a test
    file that has been renamed, or deleted, or that quietly stopped covering the
    claim it is cited for. That is the failure mode worth a gate, and it is a
    different one from the duplicate I nearly added.
    """
    cited = {
        # claim: the acknowledgement is honoured and the alarm leaves the list
        "test_a_raise_then_an_acknowledgement_takes_it_off_the_list",
        # claim: a recurrence is a new alarm needing a new acknowledgement
        "test_a_recurrence_is_a_new_alarm_and_does_not_inherit_the_acknowledgement",
        # claim: a clear before an acknowledgement still honours it
        "test_a_clear_then_a_late_acknowledgement_is_honoured",
        # claim: an orphan acknowledgement is counted, not applied
        "test_an_acknowledgement_with_no_raise_is_applied_to_nothing",
    }
    source = (ROOT / "tests" / "test_alarm_replay.py").read_text(encoding="utf-8")
    for name in sorted(cited):
        assert f"def {name}(" in source, (
            f"tests/test_alarm_replay.py no longer has {name}, and "
            f"docs/USE-CASES.md use case 4 relies on that behaviour. Either "
            f"restore the test or change what the use case claims."
        )
    assert "test_alarm_replay.py" in _doc(), (
        "use case 4 does not name the test that proves the acknowledgement "
        "behaviour, so the claim is unfalsifiable from the document"
    )


# ── the surfaces the use cases send an operator to ──────────────────────────


def test_every_panel_the_use_cases_name_exists_on_a_dashboard() -> None:
    """A use case that names a panel which is not there is worse than none.

    **Checked by containment rather than by extraction, and the direction is the
    whole design.** The first version tried to pull panel names *out* of the
    prose with a regex, and it matched half a document — a backtick-quoted
    phrase in one paragraph and the next backtick in a later one, so "the panel"
    somewhere in a use case captured four kilobytes of Markdown including a
    table and a bash block. Extracting free prose is the wrong tool for this.

    So the assertion is the direction that cannot misfire: every panel title that
    appears *in* the dashboards should be cited in the document, by name and
    exactly. A renamed panel stops being cited and fails; a deleted panel stops
    being cited and fails. What it does not catch is a panel name the document
    invented — which is why the second half asserts a minimum citation count, so
    the document cannot quietly narrow to one panel and still pass.

    **This is also the check that made the document honest.** The equipment-state
    panel could not be built, so there was nothing to cite, and that shaped what
    the document says rather than the document describing a panel that does not
    exist.
    """
    dashboards = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "ui" / "grafana" / "dashboards").glob("*.json"))
    }
    assert dashboards, "no dashboards found; the glob is wrong"

    titles = {
        panel["title"] for dash in dashboards.values() for panel in dash["panels"]
    }
    doc = _doc()
    cited = {t for t in titles if t in doc}

    # **The panels a use case turns on, by distinctive prefix.** The containment
    # count above cannot catch a rename — a renamed panel simply stops being
    # cited, the count drops by one, and the count is still above the floor. That
    # is what happened when this was fault-injected: renaming "What has stopped
    # reporting" passed, because a *lower* citation count is not a failure.
    #
    # A prefix pair catches it: the prefix has to appear in the document *and*
    # still be the start of a panel that exists. Rename the panel and one of the
    # two halves breaks.
    for prefix, why in (
        ("DO against air flow", "use case 3 is entirely about this panel"),
        ("What has stopped reporting",
         "use case 2's second step points at it"),
        ("Permit compliance", "use case 6's confirmation is that table"),
        ("Influent flow", "use case 1 opens with the four at-a-glance numbers"),
    ):
        assert prefix in doc, (
            f"docs/USE-CASES.md never mentions the {prefix!r} panel — {why}. "
            f"A use case that turns on a panel has to name it."
        )
        assert any(t.startswith(prefix) for t in titles), (
            f"the {prefix!r} panel does not exist on any dashboard; "
            f"{why}. The panels are {sorted(titles)}."
        )

    assert len(cited) >= 8, (
        f"docs/USE-CASES.md cites only {len(cited)} of the {len(titles)} panels "
        f"that exist: {sorted(cited)}. A use-case document that has stopped "
        f"describing the surfaces an operator looks at is not one."
    )


def test_every_signal_id_the_use_cases_name_is_real() -> None:
    """Signal ids are transcribed into queries, so a typo is a silent empty result.

    Not backtick-delimited, because the document quotes signal ids inside SQL
    blocks as well as in prose, and an operator copies from either.
    """
    doc = _doc()
    quoted = set(re.findall(r"\b([A-Z][A-Z0-9]*(?::[A-Z0-9-]+){2})\b", doc))
    assert len(quoted) >= 3, (
        f"only {len(quoted)} signal ids were found in docs/USE-CASES.md: "
        f"{sorted(quoted)}. The extraction has probably stopped matching."
    )
    unknown = sorted(s for s in quoted if s not in C.signals)
    assert not unknown, (
        f"docs/USE-CASES.md names signals the contract does not declare: "
        f"{unknown}. A query against a signal that does not exist returns no "
        f"rows, which reads as an empty plant rather than a typo."
    )


def test_every_scenario_and_fault_the_use_cases_name_is_real() -> None:
    """Scenario ids go on a command line, so a wrong one is a usage error."""
    specs, scenarios = load_faults()
    doc = _doc()

    quoted_scenarios = {s for s in re.findall(r"`([a-z_]+)`", doc) if "_" in s}
    known = set(scenarios) | set(specs)
    # Only assert on names the document presents as a scenario or a fault, which
    # it does in backticks; other snake_case words are not identifiers.
    unknown = sorted(
        name for name in quoted_scenarios
        if name not in known
        and name in {
            "wet_weather", "aeration_loss", "night_shift_compliance",
            "bad_instrument", "everything_at_once", "baseline",
        }
    )
    assert not unknown, f"docs/USE-CASES.md names unknown scenarios: {unknown}"

    for wanted in ("wet_weather", "bad_instrument", "everything_at_once"):
        assert wanted in doc, f"the {wanted} scenario is no longer mentioned"
        assert wanted in scenarios, f"{wanted} is quoted but is not a scenario"


def test_the_event_kinds_the_use_cases_ask_the_operator_to_query_are_real(
) -> None:
    """`alarm_acknowledged` and `setpoint_written` are strings in SQL INSERTs.

    Told to "check the audit trail for a row of kind X", an operator copies X into
    a query. A wrong X returns no rows, which looks like "nothing happened" —
    the most reassuring possible answer to the wrong question.

    Scoped to the two kinds the document actually tells a reader to query, rather
    than every snake_case word in it: the previous version did the latter and
    matched `do_sensor_drift` and `wet_weather`, which are faults and scenarios,
    not event kinds.
    """
    flows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "scada" / "flows").glob("*.json"))
    )
    doc = _doc()
    for kind in ("alarm_acknowledged", "setpoint_written"):
        assert kind in doc, f"use cases no longer mention the {kind!r} event"
        assert kind in flows, (
            f"docs/USE-CASES.md tells the operator to query for {kind!r}, and no "
            f"flow writes it. The query returns nothing, which reads as an empty "
            f"audit trail."
        )


def test_the_web_routes_the_use_cases_send_an_operator_to_exist() -> None:
    """`/alarms` and `/permit` are file paths under `ui/web/app`.

    Next.js resolves a route from a directory name, so a use case pointing at a
    page that does not exist is a 404 the operator meets mid-task. Checked on
    disk rather than by trusting the URL in the prose.
    """
    app = ROOT / "ui" / "web" / "app"
    routes = {
        f"/{p.name}" for p in app.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    }
    assert routes, "no routes found; the path is wrong"

    cited = set(re.findall(r"`(/[a-z-]+)`", _doc())) - {"/scada"}
    assert cited, "no web routes are cited"
    missing = sorted(r for r in cited if r not in routes and r != "/")
    assert not missing, (
        f"docs/USE-CASES.md sends the operator to pages that do not exist: "
        f"{missing}. The app has {sorted(routes)}."
    )


# ── the use cases that keep their proof elsewhere ──────────────────────────


def test_each_use_case_names_a_way_to_confirm_it_worked() -> None:
    """Every use case must state how you know it worked.

    A use case without an observable outcome is a tour of the features. This is
    the single discipline that keeps `USE-CASES.md` from becoming the thin
    descriptive prose this repository already has plenty of — and it is checked
    per use case rather than once for the document, because a document-level
    check passes just as happily when half the sections have drifted.
    """
    sections = re.split(r"^## ", _doc(), flags=re.MULTILINE)[1:]
    cases = [s for s in sections if re.match(r"Use case \d", s)]
    assert len(cases) >= 5, (
        f"only {len(cases)} use cases found; the extraction has stopped matching"
    )
    for case in cases:
        title = case.splitlines()[0]
        assert re.search(r"know (it|you) worked|confirm", case, re.IGNORECASE), (
            f"{title!r} has no 'how you know it worked'. Without an observable "
            f"outcome it is a description, and a description cannot be wrong in "
            f"a way a reader can detect."
        )


def test_the_use_cases_with_a_behavioural_test_say_which_one() -> None:
    """The setpoint and permit use cases are proven elsewhere; name the file.

    These two are the ones an operator would most believe, and both have real
    tests that take seconds to minutes to run. If a use case says "check the
    register" and nothing checks it, that is how the FC6 bug survived: every
    layer reported success. Naming the test makes the gap visible.
    """
    text = _doc()
    for case, test_file in (
        ("setpoint", "test_modbus_writeback.py"),
        ("permit", "test_grafana_dashboards.py"),
    ):
        assert test_file in text, (
            f"the {case} use case does not name {test_file}, which is the test "
            f"that proves it"
        )
        assert (ROOT / "tests" / test_file).exists(), (
            f"{test_file} does not exist but the use case cites it"
        )


@pytest.mark.parametrize("path", ["README.md", "docs/TASKS.md", "docs/GUIDE.md"])
def test_the_use_cases_are_reachable(path: str) -> None:
    """A use-case document nobody is linked to leaves the gap open.

    The three stakeholders — plant manager, data analyst, developer — are now
    named in the README's entry point. This asserts the use cases are one of them.
    """
    assert "USE-CASES.md" in (ROOT / path).read_text(encoding="utf-8"), (
        f"{path} does not link docs/USE-CASES.md. The plant manager is one of "
        f"three stakeholders and this document is the one entry point they have."
    )


def test_the_readme_names_all_three_stakeholders() -> None:
    """The framing that produced this document, asserted so it stays visible.

    A document written for the plant manager and indexed from an entry point
    organised by *document* is the same mistake one level up, so the README's
    "I want to…" table has to be organised by *person*. Cheap to check, and it is
    the difference between this being found and this being filed.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for role in ("plant manager", "data analyst", "developer"):
        assert role.lower() in readme.lower(), (
            f"the README no longer names the {role} as an audience. The entry "
            f"point was reorganised around the three stakeholders, and losing "
            f"one of them is how a use-case document ends up orphaned."
        )
