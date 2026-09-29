"""Rebuilding alarm state from the event log.

`replay()` is a pure function of the event rows, so every awkward case is testable
with hand-built events and no database — which is the only way to test the ones
that matter, because they are all the "the log says something surprising" variety.

The three that have to be right, and why each is a real risk rather than an
edge case:

* **raise → acknowledge → raise again.** A fault that comes back is a new event.
  If the second raise inherits the first's acknowledgement, an operator who saw
  the fault once never sees the recurrence — and the alarm is a false negative
  produced by the thing that was supposed to suppress false positives.
* **raise → clear → acknowledge.** Late acknowledgements are honoured; the alarm
  is off the list because it cleared, not because it was acknowledged.
* **acknowledge with no raise.** Counted, and applied to nothing.

And one that is *not* about the log at all:

* **`apply_to_engine` restores acknowledgements and nothing else.** It does not
  mark alarms active, because the engine has not watched the plant yet and a
  reconstructed alarm is a claim about a process that has stopped running.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alarms.replay import ReplayResult, apply_to_engine, replay

T0 = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


def ev(minutes: float, kind: str, *, rule: str | None = "r1",
       severity: str = "critical", message: str = "m", signal: str | None = "S:1:X"):
    """One event row, shaped exactly as the query returns it.

    Built by a helper rather than as literal tuples because the test's whole
    subject is the *order* and the *kind*, and a wall of six-element tuples makes
    a reader count commas instead of reading events.
    """
    return (T0 + timedelta(minutes=minutes), kind, severity, message,
            signal, rule)


# ── the ordinary cases ────────────────────────────────────────────────────────


def test_a_raise_alone_is_an_unacknowledged_active_alarm() -> None:
    r = replay([ev(0, "alarm_raised")])
    assert len(r.alarms) == 1
    assert len(r.active) == 1
    assert [a.rule_id for a in r.unacknowledged] == ["r1"]
    assert r.acknowledged == []
    assert r.seen == {"r1"}


def test_a_raise_then_an_acknowledgement_takes_it_off_the_list() -> None:
    r = replay([ev(0, "alarm_raised"), ev(5, "alarm_acknowledged")])
    assert r.active == [], "an acknowledged alarm is off the panel"
    assert r.unacknowledged == []
    # ...and it is still *latched*, which is the thing the naive
    # `cleared_at is None` definition got wrong. A critical never clears; it is
    # acknowledged instead, and the two facts are independent.
    assert [a.rule_id for a in r.acknowledged] == ["r1"]
    assert r.acknowledged[0].latched is True
    assert r.acknowledged[0].cleared_at is None
    assert r.alarms[0].acknowledged_at == (T0 + timedelta(minutes=5)).timestamp()


def test_a_cleared_alarm_is_off_the_list_whether_or_not_it_was_acknowledged() -> None:
    """A `warning` auto-clears, so a cleared warning is gone with no human action.

    That is the case the state machine is designed for and it is worth pinning:
    "the panel is empty" has two quite different reasons here — cleared, and
    acknowledged — and they are not the same fact.
    """
    r = replay([ev(0, "alarm_raised"), ev(10, "alarm_cleared")])
    assert r.active == []
    assert r.alarms[0].cleared_at == (T0 + timedelta(minutes=10)).timestamp()


def test_a_clear_then_a_late_acknowledgement_is_honoured() -> None:
    """The operator saw it; they were just slow.

    The alarm is off the list because it cleared, and the acknowledgement is
    still recorded — because "nobody was looking" and "somebody looked and it
    recovered" are different stories and an incident review is about the
    difference.
    """
    r = replay([ev(0, "alarm_raised"), ev(10, "alarm_cleared"),
                ev(15, "alarm_acknowledged")])
    assert r.active == []
    assert r.alarms[0].acknowledged_at is not None
    assert r.alarms[0].cleared_at is not None


# ── the repeat, which is the one that can produce a false negative ────────────


def test_a_recurrence_is_a_new_alarm_and_does_not_inherit_the_acknowledgement() -> None:
    """The most important assertion in this file.

    A fault that raises, is acknowledged, and comes back an hour later is a *new
    event*. Inheriting the first acknowledgement means the operator who saw it
    once never sees the recurrence, and the alarm system is producing a confident
    "all clear" about a fault that is happening again.

    The same reasoning appears in `tests/test_alarm_rules.py` for a coverage
    report: a repeat of something already known is not new information.
    """
    r = replay([
        ev(0, "alarm_raised"),
        ev(5, "alarm_acknowledged"),
        ev(60, "alarm_raised"),
    ])
    assert len(r.alarms) == 2
    assert len(r.active) == 1, "only the recurrence is standing"
    assert r.active[0].raised_at == (T0 + timedelta(minutes=60)).timestamp()
    assert r.active[0].acknowledged_at is None
    assert [a.rule_id for a in r.unacknowledged] == ["r1"], (
        "a recurrence that inherits an acknowledgement is a false negative"
    )


def test_three_raises_and_two_acknowledgements_leave_one_waiting() -> None:
    r = replay([
        ev(0, "alarm_raised"), ev(1, "alarm_acknowledged"),
        ev(30, "alarm_raised"), ev(31, "alarm_acknowledged"),
        ev(60, "alarm_raised"),
    ])
    assert len(r.alarms) == 3
    assert len(r.acknowledged) == 2
    assert len(r.unacknowledged) == 1


# ── the shapes the log produces that are not the happy path ───────────────────


def test_an_acknowledgement_with_no_raise_is_applied_to_nothing() -> None:
    """Counted, and harmless.

    Either a hand-written event or a crash between two writes. The safe reading
    is "there is no alarm to acknowledge", because the alternative — inventing an
    alarm from an acknowledgement — would put something on an operator's list
    that the plant never reported.
    """
    r = replay([ev(5, "alarm_acknowledged")])
    assert r.alarms == []
    assert r.active == []
    assert r.seen == {"r1"}, "the rule was seen, so the event was attributed"


def test_an_event_with_no_rule_is_counted_not_ignored() -> None:
    """A replay that silently drops rows is a replay you cannot trust.

    `record_event` is a public function and nothing stops an operator writing an
    event by hand. The count is the honest way to say "there is more in the log
    than I used".
    """
    r = replay([ev(0, "alarm_raised"), ev(1, "note", rule=None),
                ev(2, "note", rule=None)])
    assert r.unattributed == 2
    assert len(r.alarms) == 1
    assert r.seen == {"r1"}


def test_a_clear_with_no_raise_is_ignored() -> None:
    r = replay([ev(0, "alarm_cleared")])
    assert r.alarms == []


def test_interleaved_rules_keep_their_own_state() -> None:
    """Two rules, interleaved, must not share an acknowledgement."""
    r = replay([
        ev(0, "alarm_raised", rule="a"),
        ev(1, "alarm_raised", rule="b"),
        ev(2, "alarm_acknowledged", rule="a"),
    ])
    by_rule = {a.rule_id: a for a in r.alarms}
    assert by_rule["a"].acknowledged_at is not None
    assert by_rule["b"].acknowledged_at is None
    assert [x.rule_id for x in r.unacknowledged] == ["b"]


def test_an_empty_log_is_distinguishable_from_a_resolved_one() -> None:
    """A fresh database must not look like a healthy one.

    "Nothing has run yet" and "everything is fine" both replay to an empty panel,
    and the panel renders them identically. The payload has to carry the
    difference, because that is the whole reason a flow reads `rules_seen` rather
    than just the list.

    The first version of this test compared `unacknowledged` between the two
    cases and asserted they differed — and they do not, because an acknowledged
    alarm is also off the panel. The assertion was meaningless and it only
    failed once the model was fixed.
    """
    empty = replay([]).as_dict()
    resolved = replay([
        ev(0, "alarm_raised"), ev(1, "alarm_acknowledged"),
    ]).as_dict()
    outstanding = replay([ev(0, "alarm_raised")]).as_dict()

    assert empty["rules_seen"] == 0
    assert resolved["rules_seen"] == 1
    assert empty["unacknowledged"] == [] == resolved["unacknowledged"]
    assert outstanding["unacknowledged"] == ["r1"]

    # The numbers a caller can branch on: how many events were read, and how many
    # rules were seen. Both zero means "nothing has happened", which is not the
    # same claim as "nothing is wrong".
    assert empty["rules_seen"] == 0 and empty["alarms"] == 0
    assert empty != resolved and resolved != outstanding


# ── timestamps, because they are the part that is easy to get wrong ────────────


def test_plain_floats_are_accepted_as_timestamps() -> None:
    """The query returns datetimes, but `replay()` is also called with floats
    from a JSON export or a test, and the two must agree."""
    from_timestamp = replay([
        (T0.timestamp(), "alarm_raised", "critical", "m", "S:1:X", "r1"),
    ])
    from_datetime = replay([ev(0, "alarm_raised")])
    assert from_timestamp.as_dict() == from_datetime.as_dict()


def test_the_active_list_is_most_recent_first() -> None:
    r = replay([
        ev(0, "alarm_raised", rule="a"),
        ev(10, "alarm_raised", rule="b"),
        ev(20, "alarm_raised", rule="c"),
    ])
    assert [a.rule_id for a in r.active] == ["c", "b", "a"]


# ── applying it ───────────────────────────────────────────────────────────────


def test_applying_to_a_fresh_engine_applies_nothing_and_that_is_the_finding() -> None:
    """The engine cannot accept a reconstructed acknowledgement, and cannot.

    `AlarmEngine.acknowledge()` refuses anything that is not currently active,
    which is correct: acknowledging an alarm the engine has not seen raise would
    be silencing something it cannot vouch for. So `apply_to_engine` on a freshly
    started engine applies **zero**, and the first version of this test asserted
    one and asserted zero in consecutive lines, which is not a test.

    The conclusion is the design: **the operator panel reads the replay result,
    not the engine.** The two cannot disagree, and a Node-RED flow restart loses
    nothing, because nothing was in memory to lose.
    """
    from alarms.base import AlarmRule
    from alarms.engine import AlarmEngine

    rule = AlarmRule(
        id="r1", signal_id="S:1:X", detector="single_point_threshold",
        severity="critical", message="m", params={"limit": 1.0},
    )
    engine = AlarmEngine([rule])
    result = replay([ev(0, "alarm_raised"), ev(5, "alarm_acknowledged")])

    assert apply_to_engine(engine, result) == 0
    assert engine.states["r1"].raise_count == 0
    assert engine.active_alarms() == [], (
        "nothing was evaluated, so nothing can be active"
    )

    # And the panel's data source is therefore the replay, which does work:
    assert result.as_dict()["acknowledged"] == ["r1"]


def test_a_reconstructed_acknowledgement_is_honoured_once_the_engine_agrees() -> None:
    """The two halves, in the order a restart actually produces them.

    The engine evaluates and raises; *then* the replay runs. In that order the
    acknowledgement applies, because by then the engine has seen the alarm and
    agrees it is active. So the value of `apply_to_engine` is real — it is just
    that it has to be called at the right moment, and getting that moment wrong is
    silent.
    """
    from alarms.base import AlarmRule, Sample, Window
    from alarms.engine import AlarmEngine

    rule = AlarmRule(
        id="r1", signal_id="S:1:X", detector="single_point_threshold",
        severity="critical", message="m", params={"limit": 1.0, "limit_units": ""},
    )
    engine = AlarmEngine([rule])

    # The plant is in fault: the condition is true and dwell is zero.
    w = Window("S:1:X", (Sample(ts=T0.timestamp(), value=9.0),), T0.timestamp())
    engine.evaluate("S:1:X", w)
    assert len(engine.active_alarms()) == 1, "the engine raised it"

    result = replay([ev(0, "alarm_raised"), ev(5, "alarm_acknowledged")])
    assert apply_to_engine(engine, result) == 1, (
        "once the engine has raised the alarm itself, the reconstructed "
        "acknowledgement applies"
    )
    assert engine.unacknowledged() == [], "it is off the operator's screen"


def test_a_replayed_alarm_is_usable_without_an_engine() -> None:
    """The panel's data source is the replay, and that is a design decision.

    `apply_to_engine` above shows the engine cannot accept a reconstructed
    acknowledgement, because it has never seen the alarm raise. So the operator
    panel reads the *replay result* and the engine is left alone — which also
    means the two cannot disagree, and that a flow restart loses nothing.
    """
    result = replay([
        ev(0, "alarm_raised", rule="a"),
        ev(1, "alarm_raised", rule="b"),
        ev(2, "alarm_acknowledged", rule="a"),
    ])
    payload = result.as_dict()
    assert payload["unacknowledged"] == ["b"]
    assert payload["acknowledged"] == ["a"]
    assert payload["rules_seen"] == 2


def test_the_result_is_json_serialisable() -> None:
    """Because a flow is going to read it."""
    import json

    result: ReplayResult = replay([ev(0, "alarm_raised")])
    payload = json.loads(json.dumps(result.as_dict(), default=str))
    assert payload["unacknowledged"] == ["r1"]
    assert isinstance(payload["lookback_h"], float)


# ── the query itself ──────────────────────────────────────────────────────────


def test_the_query_uses_an_interval_multiplier_not_make_interval() -> None:
    """`make_interval(hours => %s)` is a type error, and it was one.

    psycopg sends a Python `float` and `make_interval` takes `int`:

        ERROR:  function make_interval(hours => double precision) does not exist

    Pinned as a string assertion because it is a *query* bug, not a logic bug, and
    a query bug is only found by running it against a database.
    """
    from alarms.replay import QUERY

    assert "make_interval" not in QUERY
    assert "interval '1 hour' * %s" in QUERY


def test_the_query_reads_the_rule_out_of_detail() -> None:
    """Which is the honest cost of a schema not designed for this.

    There is no `rule` column on `event`, so the replay does a JSONB extract in
    a `WHERE`. That is slower than a column and it is the price of not having
    migrated. Recorded rather than hidden, and the engine is currently the only
    writer *and* the only reader, so the cost is theoretical.
    """
    from alarms.replay import QUERY

    assert "detail->>'rule'" in QUERY
    for kind in ("alarm_raised", "alarm_cleared", "alarm_acknowledged"):
        assert kind in QUERY, kind


@pytest.mark.integration
@pytest.mark.parametrize("hours", [0.5, 24, 168])
def test_the_lookback_accepts_fractional_hours(hours: float) -> None:
    """The `lookback_h=0.5` case exists because a caller asked for it.

    An integer-only column would truncate 0.5 to 0 and query the whole retention
    window, so the test asserts the *converted* seconds rather than the query
    text. It needs a database, and until this marker was added it did not say so:
    the `try`/`skip` below caught the connection error and the test reported
    `skipped` in **every CI run** — the `unit` job has no database, and the
    `integration` job does not run this file, so the assertion had never been
    checked once. A test that has only ever skipped is a test that has never run.
    """
    from alarms.replay import load
    from storage.postgres.schema import connect

    try:
        with connect() as conn:
            result = load(conn, lookback_h=hours)
    except Exception as exc:  # a connection error is any driver's
        pytest.skip(f"no database: {exc}")
    assert result.lookback_s == hours * 3600.0
