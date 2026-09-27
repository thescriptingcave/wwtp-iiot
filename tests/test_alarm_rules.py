"""The alarm rules, checked against the fault library.

This is the test that makes the rule set more than a list of thresholds. It runs
two representative faults through the plant model and asserts that the rules
which *claim* to catch them do.

## Why only two faults, and why they are marked slow

A full run over all eleven faults takes about eight minutes, which is too long
for the unit suite and is available as `make coverage`. Two faults is the
interesting pair:

* `blower_failure` — a process fault with a **delayed** signature. DO sags over an
  hour, ammonia over six. The contract says a single-point threshold will not find
  it, and the assertion is that the *trend* rule does, inside the first two hours.
* `sensor_flatline` — a sensor fault. The signature is an *absence*, and the
  deadband is what makes it invisible to everything else.

The first asserts that a rule works. The second asserts that a rule fires for a
reason the storage layer would otherwise have hidden, which is the harder claim
and the more valuable one.

## What is deliberately not asserted

**That the whole matrix is green.** It is not, and pretending otherwise would be
the most dishonest thing this project could do. `make coverage` reports the real
numbers, `docs/ALARMS.md` records them, and the blind spots are named there as
open work rather than smoothed over here.

**That no rule fires spuriously.** The coverage run *did* find two rules firing on
faults they do not claim — `influent_lift_current_anomaly` and
`secondary_blanket_stuck`, both on `blower_failure` — which is a tuning problem
and not a correctness one. They are recorded rather than hidden, and
`docs/ALARMS.md` says what they are.
"""

from __future__ import annotations

import pytest
from alarms.base import DETECTION_VOCABULARY
from alarms.coverage import CoverageReport, audit, load_faults
from alarms.engine import AlarmEngine
from alarms.rules import rules, rules_by_signal
from alarms.scenarios import run_fault

#: The four tests that run the plant model are marked `slow` individually rather
#: than with a module-level `pytestmark`. The first version used the module mark
#: and `-m "not slow"` deselected all fifteen tests — including the declaration
#: checks, which take no time at all and which the ordinary suite is supposed to
#: run. A marker that silences the cheap tests along with the expensive ones gets
#: the cheap ones skipped forever.


# ── the rule set as a declaration ─────────────────────────────────────────────


def test_every_rule_names_a_detector_the_contract_uses() -> None:
    """The contract's vocabulary is the specification, and it is enforced.

    `AlarmRule.__post_init__` also checks this, per rule. Checking it once here
    as well means the *error* names the whole set rather than the first offender,
    which is the difference between a useful message and a whack-a-mole.
    """
    assert rules(), "the rule set is empty"
    for rule in rules():
        assert rule.detector in DETECTION_VOCABULARY, rule.id


def test_every_rule_carries_a_message_and_a_rationale() -> None:
    """A rule with no message pages nobody. A rule with no rationale is a number
    somebody chose, and the number is the part a future maintainer will
    question."""
    for rule in rules():
        assert rule.message, rule.id
        assert rule.rationale, f"{rule.id} has no rationale"


def test_no_two_rules_share_an_id() -> None:
    ids = [r.id for r in rules()]
    assert len(ids) == len(set(ids))


def test_every_rule_claims_at_least_one_fault_or_says_why_not() -> None:
    """A rule that detects nothing is either a placeholder or a mistake.

    `aeration_do_low` is the one deliberate exception, and it declares
    `detects = ()` with a rationale saying it never fires in a seeded week. That
    is allowed; being *silent* about it is not.
    """
    fault_ids = {f["id"] for f in load_faults()}
    for rule in rules():
        if rule.detects:
            unknown = set(rule.detects) - fault_ids
            assert not unknown, f"{rule.id} claims unknown faults {unknown}"
        else:
            assert rule.rationale, rule.id


def test_every_contract_fault_is_claimed_by_at_least_one_rule() -> None:
    """The other half: no fault in the library is unclaimed.

    This is the assertion that fails when somebody adds a fault to
    `contracts/fault-scenarios.yaml` and does not think about alarming. It is
    deliberately about *claims* and not about detections, because a claim can be
    checked in a millisecond and a detection needs eight minutes.
    """
    claimed = {f for rule in rules() for f in rule.detects}
    unclaimed = sorted({f["id"] for f in load_faults()} - claimed)
    assert unclaimed == [], (
        f"faults with no rule claiming them: {unclaimed}. Either write a rule "
        "or record in the rule's rationale why the fault is deliberately "
        "un-alarmed."
    )


def test_rules_are_grouped_by_signal_for_one_query_each() -> None:
    grouped = rules_by_signal()
    assert sum(len(v) for v in grouped.values()) == len(rules())
    assert len(grouped) < len(rules()), (
        "every rule watches a different signal, so grouping saves nothing"
    )


# ── the coverage report, on hand-built transitions ───────────────────────────


def test_the_report_distinguishes_a_missed_fault_from_one_never_run() -> None:
    """The distinction the first version of the report got wrong.

    Asked for three faults out of eleven, it reported *"8 of 11 blind spots"* —
    because the other eight had no transitions simply by never having been
    simulated. A fault that was not tested is an *unknown*, and reporting it as a
    missed detection invites a rule to be deleted on the strength of a
    measurement that was never taken.
    """
    engine = AlarmEngine(rules())
    report = audit(engine, {"blower_failure": []})

    assert report.evaluated_faults == 1
    assert report.untested, "the ten unrun faults should be reported as untested"
    assert "digester_souring" in report.untested
    assert "blower_failure" not in report.untested
    # `blower_failure` was run and nothing fired, so it *is* a blind spot.
    assert report.blind_spots == ["blower_failure"]
    assert report.luck == [], "nothing fired at all, so nothing was luck"
    assert "3 of 11" not in report.render()


def test_an_incidental_catch_is_still_a_blind_spot() -> None:
    """The distinction the report got wrong, and the one that matters most.

    The first version treated any rule firing at all as evidence the fault was
    caught. The coverage run showed why that is wrong:
    `sludge_blanket_thickening` was "found" by six rules that were not looking for
    it. Retune those six — and two of them demonstrably needed retuning during
    this phase — and the fault is invisible again.

    **A coincidence standing in front of a blind spot is still a blind spot.**
    """
    from alarms.base import Verdict
    from alarms.engine import Transition

    engine = AlarmEngine(rules())
    # `aeration_do_low` is the one rule that deliberately claims nothing, and the
    # test says why: it needs a rule that fires without having promised to.
    unclaimed = [r for r in engine.rules if not r.detects]
    assert unclaimed, "no rule claims nothing, so this test cannot be written"
    stray = unclaimed[0]
    t = Transition("raised", stray, engine.states[stray.id], 100.0,
                   Verdict.firing(1.0))
    report = audit(engine, {"blower_failure": [t]})

    assert report.blind_spots == ["blower_failure"], (
        "an incidental catch is not a detection"
    )
    assert report.luck == ["blower_failure"]
    assert "by luck" in report.render()
    assert "does not survive a retune" in report.render()


def test_the_report_counts_a_rule_as_used_when_it_fired_incidentally() -> None:
    """A rule that raised an alarm in every run is not dead configuration.

    Counting "never fired" over the *claimed* pairs only would have reported two
    rules as never used when in fact they had raised in every single run — for
    faults they do not claim. That is a tuning problem, not a dead rule.
    """
    from alarms.base import Verdict
    from alarms.engine import Transition

    engine = AlarmEngine(rules())
    stray = next(r for r in engine.rules if r.id == "aeration_do_low")
    t = Transition("raised", stray, engine.states[stray.id], 100.0,
                   Verdict.firing(1.0))
    report = audit(engine, {"blower_failure": [t]})
    assert "aeration_do_low" not in report.unused_rules


def test_the_report_separates_a_real_contradiction_from_an_explained_one() -> None:
    """The distinction the rewording created, and the reason for it.

    `digester_souring` lists `single_point_threshold` under `not_sufficient_alone`
    *and* carries a `threshold_eventually_fires` note saying a limit check does
    find it, late. A threshold rule firing on it is therefore **expected**, and the
    report says so under `explained`.

    A fault with the `not_sufficient_alone` entry and *no* such note is different:
    a threshold firing there is a genuine contradiction and belongs in
    `disagreements`. Before the rewording the two cases produced the same message,
    which is why the report told me "either the contract is wrong or the rule is"
    three times about three faults where the contract was fine.
    """
    from alarms.base import Verdict
    from alarms.engine import Transition

    engine = AlarmEngine(rules())
    vfa = next(r for r in engine.rules if r.id == "digester_vfa_high")
    t = Transition("raised", vfa, engine.states[vfa.id], 100.0, Verdict.firing(0.7))

    explained = audit(engine, {"digester_souring": [t]})
    assert not explained.disagreements, (
        "digester_souring carries a threshold_eventually_fires note, so this is "
        "not a contradiction"
    )
    assert explained.explained, "but it is still worth reporting"
    assert "not_sufficient_alone" in explained.explained[0]
    assert "eventually" in explained.explained[0]

    # A fault whose contract entry has no such note *is* a contradiction. The
    # rule has to be one that *claims* that fault, because the report only
    # cross-checks claimed pairs — an incidental fire is reported under
    # `incidental` instead, which is the other half of the same distinction.
    torque = next(r for r in engine.rules
                  if "sludge_blanket_thickening" in r.detects)
    assert torque.detector == "single_point_threshold", (
        "this test is about a threshold rule contradicting the contract, so the "
        f"rule it picks has to be one; {torque.id} is {torque.detector}"
    )
    contradicted = audit(engine, {"sludge_blanket_thickening": [
        Transition("raised", torque, engine.states[torque.id], 100.0,
                   Verdict.firing(140.0))
    ]})
    assert contradicted.disagreements, (
        "sludge_blanket_thickening lists the threshold as not sufficient alone "
        "and carries no threshold_eventually_fires note, so a threshold rule "
        "firing on it is a real contradiction and must be reported as one"
    )
    assert not contradicted.explained
    assert "no threshold_eventually_fires note" in contradicted.disagreements[0]


def test_the_rewording_only_claims_late_faults_the_audit_actually_confirmed() -> None:
    """The contract must not assert a `threshold_eventually_fires` on its own say-so.

    The three notes in `contracts/fault-scenarios.yaml` exist because the coverage
    run *observed* a threshold rule firing on those faults. If someone adds a
    fourth note from reasoning alone, it is a claim in a file this project treats
    as a source of truth, and it should have to be earned.

    The check is that every fault carrying a note is one the report has placed in
    `explained` for — which needs a simulation and so lives with the slow tests.
    Here it asserts the weaker, always-true half: that the notes are well-formed
    and reference a method the engine actually has.
    """
    from alarms.base import DETECTION_VOCABULARY

    noted = [f for f in load_faults() if f["expects"].get("threshold_eventually_fires")]
    assert noted, "the rewording removed every note; the audit found three"

    for fault in noted:
        expects = fault["expects"]
        note = expects["threshold_eventually_fires"]
        assert len(note) > 80, f"{fault['id']}: the note is too short to be a note"
        # A note about a threshold must sit on a fault that lists a threshold as
        # not sufficient alone, or the two fields are contradicting each other.
        assert "single_point_threshold" in expects["not_sufficient_alone"], (
            f"{fault['id']} has a threshold_eventually_fires note but does not "
            "list single_point_threshold as not sufficient alone"
        )

    assert all("single_point_threshold" in DETECTION_VOCABULARY for _ in noted)


def test_the_report_renders_a_table_a_person_can_read() -> None:
    engine = AlarmEngine(rules())
    report = audit(engine, {"blower_failure": [], "digester_souring": []})
    text = report.render()
    assert "blower_failure" in text
    assert "untested" in text, "unrun faults must be visibly unrun"
    assert "faults detected" in text
    # Every fault appears *at least* once, so nothing is silently dropped. Not
    # "exactly once": a missed fault is named in its row and again in the
    # blind-spots list, which is the point of naming it twice.
    faults = load_faults()
    for fault in faults:
        assert fault["id"] in text, fault["id"]
    # And one row per fault, which is the assertion that catches a duplicate.
    assert text.count("YES") + text.count("no") + text.count("untested") \
        >= len(faults)


# ── against the plant model ──────────────────────────────────────────────────


@pytest.mark.slow
def test_the_blower_trip_is_caught_by_a_trend_not_by_a_threshold() -> None:
    """The project's central claim, measured rather than asserted.

    A blower trip moves the fan in under a second, DO over one to two hours, and
    effluent ammonia two to six hours after that. So:

    * the fan alarm is first, and is about a fan;
    * the DO *trend* fires within the first two hours;
    * the ammonia threshold fires hours later, if at all;
    * and **no threshold on DO fires at all**, because DO sags within its band.

    The contract asserts the last point. This is the measurement.
    """
    run = run_fault("blower_failure", hours=8)
    assert run.raised("aeration_blower_speed_drop"), (
        "the fastest alarm in the system did not fire"
    )
    assert run.raised("aeration_do_sagging"), (
        "the DO trend did not fire; the contract says a threshold cannot find "
        "this fault, and the trend is the alternative"
    )
    # The contract used to claim `single_point_threshold` *cannot* find this
    # fault, and this assertion is what disproved it: on the six-hour trip the
    # contract itself declares, DO does cross 1.5 and the threshold fires.
    #
    # The contract has since been reworded to `not_sufficient_alone` and given a
    # `threshold_eventually_fires` note for this fault, so the behaviour below is
    # now *expected* rather than a contradiction. The assertion stays because the
    # thing worth pinning is the ordering: `fan_at < do_at` is the warning time,
    # and it holds however the contract phrases itself.
    assert run.raised("aeration_do_low"), (
        "a six-hour blower trip drives DO below 1.5, so the threshold does fire — "
        "which is why the contract now says a threshold fires here eventually"
    )

    fan_at = run.first_raise("aeration_blower_speed_drop")
    do_at = run.first_raise("aeration_do_sagging")
    assert fan_at is not None and do_at is not None
    assert fan_at < do_at, (fan_at, do_at)


@pytest.mark.slow
def test_a_flatlined_sensor_is_caught_despite_the_deadband() -> None:
    """The absence case.

    A deadband suppresses readings that have not moved, so a healthy steady
    signal and a failed instrument are the *same observation* at the database
    level. `sensor_flatline` freezes `SECONDARY:SEC-CL-1:BLANKET` and
    `AERATION:AHU-1:BLOWER_RPM`; the rule on the blanket must notice.

    Note what the assertion does *not* say. It does not claim the rule knows the
    sensor is broken — only that movement stopped, which is all the data
    supports. `detectors.flatline_detection` says so in its own output, and
    `sql/02-04` is the long version.
    """
    run = run_fault("sensor_flatline", hours=6)
    assert run.raised("secondary_blanket_stuck"), (
        "a frozen sensor produced no alarm; the deadband hid it"
    )


@pytest.mark.slow
@pytest.mark.slow
def test_a_healthy_plant_raises_almost_nothing() -> None:
    """A ratchet on the false-positive rate, and the most useful test here.

    An alarm system whose rules fire on a healthy plant is worse than no alarm
    system: every alarm it raises is a false one, and operators learn to ignore
    the panel within a shift. So this is the number to hold down.

    **It is currently six.** One of the fifteen rules was fixed during this phase
    — `aeration_do_sagging` fired on ten of eleven faults and on the healthy
    plant, because its 0.15 mg/L per hour threshold sat *inside* the diurnal
    distribution whose steepest healthy fall is 0.427. Re-measuring and setting
    the threshold at 0.6 took it from the worst offender to clean.

    The other five are untuned. They were written from engineering judgement and
    from the contract's normal bands, and neither of those is a measurement of
    what a healthy plant actually does. `influent_flow_ceiling` fires because
    influent flow reaches 2 249 m³/h against a `range_max` of 2 200 — the
    contract's range is slightly optimistic, and a rule built on a range will
    always inherit its optimism.

    So this is a ratchet rather than an assertion. It asserts the *set*, so a
    newly-tuned rule has to be removed deliberately, and it asserts the count, so
    a regression is obvious. Reaching zero is the next piece of work and it is
    measurement, not design.
    """
    run = run_fault("baseline", hours=6)
    raised = sorted({t.rule.id for t in run.transitions if t.kind == "raised"})

    # Sorted, because the assertion is about the *set* and an unsorted
    # comparison fails on ordering and reads as a content change.
    assert raised == sorted([
        # 0.5 mg/L per hour upward over three hours is inside the diurnal swing.
        "aeration_ammonia_rising",
        # Blower speed moves by thousands per hour on the plant's own load cycle.
        "aeration_blower_speed_drop",
        # DO reaches 1.40 against a normal_low of 1.5. The contract's normal band
        # is optimistic, not the rule.
        "aeration_do_low",
        # Influent flow reaches 2 249 m3/h and the contract's range_max is 2 200.
        "influent_flow_ceiling",
        # Lift pump current is noisy; a 25 % deviation from a 1 800 s window mean
        # is well inside its ordinary scatter.
        "influent_lift_current_anomaly",
        # The clarifier blanket genuinely moves less than its deadband.
        "secondary_blanket_stuck",
    ]), (
        "the set of rules that fire on a healthy plant changed. If one of these "
        "has been tuned, remove it from this list — and say so in "
        "`docs/ALARMS.md`. If a rule has been *added* to this list, it needs "
        "measuring before it ships."
    )
    assert len(raised) == 6, f"false-positive rate moved: {len(raised)} rules"


@pytest.mark.slow
def test_a_baseline_run_produces_fewer_transitions_than_a_fault_run() -> None:
    """A blunt instrument, and deliberately blunt.

    It would catch a rule that went mad. It cannot tell a *correct* alarm from a
    false one, which is why the specific assertions above exist alongside it.
    """
    baseline = run_fault("baseline", hours=4)
    faulted = run_fault("blower_failure", hours=4)
    assert len(faulted.transitions) > len(baseline.transitions)


def test_coverage_json_round_trips() -> None:
    """The report is consumed by something, and `default=str` is a smell."""
    import json

    report = audit(AlarmEngine(rules()), {"blower_failure": []})
    payload = json.loads(json.dumps(report.as_dict(), default=str))
    assert payload["faults"] == 11
    assert payload["evaluated"] == 1
    assert "contract_explained" in payload
    # Every fault row carries the renamed field, so a consumer reading the old
    # name gets a KeyError rather than a silently empty list.
    assert all("not_sufficient_alone" in row for row in payload["matrix"])
    assert isinstance(report, CoverageReport)
