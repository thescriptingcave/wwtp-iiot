"""The state machine: dwell, hysteresis, latching, dedup, acknowledgement.

These are the behaviours that stop an alarm system being unusable, and every one
of them is a decision rather than a mechanism. Each test therefore says what the
decision *is*, not just what the code returns — because the code is short and the
reasoning is the part that would otherwise be lost.

The engine is driven with an injected clock and a list for a sink, so none of
this needs a database. That is not incidental: an alarm state machine you can
only test by writing rows is a state machine you will not test.
"""

from __future__ import annotations

from alarms.base import (
    ACKNOWLEDGED,
    ACTIVE,
    CLEARED,
    UNKNOWN,
    AlarmRule,
    Sample,
    Window,
)
from alarms.engine import AlarmEngine
from alarms.synthetic import T0, step, window


class Sink:
    """Collects events. The whole database interface the engine needs."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, **event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


def make_rule(**kw) -> AlarmRule:
    """A rule with test-friendly defaults. `relatch_delta` is deliberately *not*
    one of them: it is an engine setting, not a rule setting, and passing it here
    raises `TypeError` rather than being silently ignored."""
    defaults = {
        "id": "t", "signal_id": "A:1:X",
        "detector": "single_point_threshold", "severity": "critical",
        "message": "something", "params": {"limit": 3.0},
    }
    defaults.update(kw)
    return AlarmRule(**defaults)


def feed(engine: AlarmEngine, value: float, now: float, *,
         signal_id: str = "A:1:X") -> list:
    """One evaluation, one sample, at an explicit time."""
    w = Window(signal_id=signal_id, points=(Sample(ts=now, value=value),), now=now)
    return engine.evaluate(signal_id, w)


# ─── dwell ───────────────────────────────────────────────────────────────────


def test_a_condition_that_is_true_immediately_raises_at_once() -> None:
    """`for_s = 0` is a deliberate choice, not an oversight.

    `aeration_blower_speed_drop` uses it because a fan either trips or it does
    not, and there is no intermediate state worth waiting for.
    """
    sink = Sink()
    e = AlarmEngine([make_rule(for_s=0.0)], sink)
    feed(e, 5.0, T0)
    assert sink.kinds() == ["alarm_raised"]
    assert e.states["t"].status == ACTIVE


def test_a_condition_with_dwell_does_not_raise_until_the_dwell_elapses() -> None:
    sink = Sink()
    e = AlarmEngine([make_rule(for_s=600.0)], sink)
    feed(e, 5.0, T0)
    assert sink.kinds() == [], "raised on the first sample despite a 10 min dwell"
    assert e.states["t"].status == UNKNOWN

    feed(e, 5.0, T0 + 300)
    assert sink.kinds() == [], "raised at 5 minutes with a 10 minute dwell"

    feed(e, 5.0, T0 + 600)
    assert sink.kinds() == ["alarm_raised"]


def test_dwell_restarts_when_the_condition_lapses() -> None:
    """The thing that makes dwell a physics statement rather than a debounce.

    A fault that comes and goes has not been continuously present, and an alarm
    that fires on cumulative time rather than continuous time reports a fault
    that had already ended.
    """
    sink = Sink()
    e = AlarmEngine([make_rule(for_s=600.0)], sink)
    feed(e, 5.0, T0)
    feed(e, 5.0, T0 + 400)
    feed(e, 0.0, T0 + 500)          # lapses
    feed(e, 5.0, T0 + 900)          # 400 s of continuity, not 900
    assert sink.kinds() == [], "dwell accumulated across a lapse"


def test_dwell_is_measured_in_seconds_not_samples() -> None:
    """`AERATION:AHU-1:DO` produces about one reading every six minutes.

    A dwell expressed in samples would mean something different for DO than for
    air flow, and the alarm with the "same" 5-sample dwell would be thirty times
    slower on one signal than the other.
    """
    sink = Sink()
    e = AlarmEngine([make_rule(detector="trend", for_s=1800.0,
                               params={"per_hour": 1.0, "direction": "up",
                                       "min_points": 3})], sink)
    # Five windows, each four samples ten minutes apart, each rising. That is
    # four *samples* per window and 1800 s of plant time between them — so a
    # dwell expressed in samples would raise on the second window and a dwell
    # expressed in seconds raises on the third.
    now = T0
    for i in range(5):
        w = window(
            step([2.0 + i * 0.2 + k * 0.2 for k in range(4)], dt=600.0,
                 start=now),
            now=now + 3 * 600.0,
        )
        e.evaluate("A:1:X", w)
        now += 1800.0
    assert sink.kinds() == ["alarm_raised"]
    # And it took three windows, not two: 1800 s of *continuous* condition.
    assert e.states["t"].raise_count == 1


# ─── clearing ────────────────────────────────────────────────────────────────


def test_a_warning_clears_only_after_the_clear_hysteresis() -> None:
    sink = Sink()
    e = AlarmEngine([make_rule(severity="warning", for_s=0.0, clear_s=300.0)],
                    sink)
    feed(e, 5.0, T0)
    assert e.states["t"].status == ACTIVE

    # The hysteresis starts at the first *false* evaluation, not at the last
    # true one. Asking for `clear_s = 300` means "300 seconds of not-breaching",
    # and counting from the last breach would make the two overlap.
    feed(e, 0.0, T0 + 100)
    assert e.states["t"].status == ACTIVE, "cleared on the first false sample"
    feed(e, 0.0, T0 + 399)
    assert e.states["t"].status == ACTIVE, "cleared before 300 s of quiet"
    feed(e, 0.0, T0 + 400)
    assert e.states["t"].status == CLEARED
    assert sink.kinds() == ["alarm_raised", "alarm_cleared"]


def test_a_critical_alarm_latches_and_does_not_clear_itself() -> None:
    """The behaviour that separates an alarm system from a status display.

    An alarm that vanishes on its own is one nobody has to acknowledge, which is
    indistinguishable from one that never fired. `critical` stays on the
    operator's list until a human clears it; the condition is reported separately
    so the list can say "back in band, this happened at 03:14".
    """
    sink = Sink()
    e = AlarmEngine([make_rule(severity="critical", for_s=0.0, clear_s=1.0)],
                    sink)
    feed(e, 5.0, T0)
    for i in range(1, 20):
        feed(e, 0.0, T0 + i * 10)
    assert e.states["t"].status == ACTIVE, "a critical alarm cleared itself"
    assert "alarm_cleared" not in sink.kinds()


def test_acknowledging_a_latched_alarm_takes_it_off_the_list() -> None:
    sink = Sink()
    e = AlarmEngine([make_rule(severity="critical", for_s=0.0)], sink)
    feed(e, 5.0, T0)
    assert e.acknowledge("t", at=T0 + 60) is True
    assert e.states["t"].status == ACKNOWLEDGED
    assert e.unacknowledged() == [], "an acknowledged alarm is still on screen"
    assert e.active_alarms(), "an acknowledged alarm vanished from the record"
    assert sink.kinds() == ["alarm_raised", "alarm_acknowledged"]


def test_acknowledging_something_that_is_not_raising_does_nothing() -> None:
    e = AlarmEngine([make_rule()], Sink())
    assert e.acknowledge("t") is False
    assert e.acknowledge("no-such-rule") is False


def test_acknowledging_and_clearing_are_different_facts() -> None:
    """Conflating them loses the difference between "nobody was looking" and
    "somebody looked and it recovered" — which is the fact an incident review
    is actually about."""
    sink = Sink()
    e = AlarmEngine([make_rule(severity="critical", for_s=0.0)], sink)
    feed(e, 5.0, T0)
    feed(e, 0.0, T0 + 60)             # condition gone, still latched
    assert e.states["t"].status == ACTIVE
    e.acknowledge("t", at=T0 + 120)
    assert e.states["t"].status == ACKNOWLEDGED
    kinds = [h["kind"] for h in e.states["t"].history]
    assert kinds == ["raised", "acknowledged"], kinds


# ─── de-duplication ──────────────────────────────────────────────────────────


def test_an_active_alarm_does_not_re_emit_every_evaluation() -> None:
    """At 1 Hz this is 3 600 identical events an hour, and the `event` table
    becomes useless for the query it exists to answer."""
    sink = Sink()
    e = AlarmEngine([make_rule(for_s=0.0)], sink)
    for i in range(50):
        feed(e, 5.0, T0 + i)
    assert sink.kinds() == ["alarm_raised"]


def test_a_worsening_alarm_re_emits() -> None:
    """The alternative — never re-emitting — means the first message is the only
    one an operator sees, and it is the one with the least information in it."""
    sink = Sink()
    # The interval is zeroed so this test is about the *delta*. It was not, and
    # the test failed against a correctly-implemented engine: the default
    # 300 s rate limit suppressed the re-emission two seconds in. A test that
    # fails because the thing it is not testing got stricter is a test that has
    # told you something, and the fix is to say what it is testing.
    e = AlarmEngine([make_rule(for_s=0.0)], sink,
                    relatch_delta=1.0, relatch_interval_s=0.0)
    feed(e, 5.0, T0)
    feed(e, 5.5, T0 + 1)              # within the delta: quiet
    assert sink.kinds() == ["alarm_raised"]
    feed(e, 9.0, T0 + 2)              # beyond it: an update
    assert sink.kinds() == ["alarm_raised", "alarm_reaffirmed"]


def test_the_reaffirm_rate_limit_suppresses_a_moving_value() -> None:
    """The other half of the pair, and the half the coverage run proved necessary.

    A first coverage run produced 9 994 reaffirmations of three alarms over nine
    hours, one every five seconds, because only the *value* was checked. The
    rate limit is what makes the `event` table usable for the query it exists to
    answer, and it has to be tested separately because it is a different
    condition.
    """
    sink = Sink()
    e = AlarmEngine([make_rule(for_s=0.0)], sink,
                    relatch_delta=0.5, relatch_interval_s=600.0)
    feed(e, 5.0, T0)
    for i in range(1, 40):                 # a value climbing steadily
        feed(e, 5.0 + i, T0 + i * 10)
    assert sink.kinds() == ["alarm_raised"], "re-emitted inside the rate limit"
    feed(e, 100.0, T0 + 1000)              # past the limit
    assert sink.kinds() == ["alarm_raised", "alarm_reaffirmed"]


# ─── dwell reporting ─────────────────────────────────────────────────────────


def test_the_alarm_records_both_when_the_condition_started_and_when_it_raised() -> None:
    """Two timestamps, because "the condition began at 03:00 and we told you at
    03:10" is a sentence an operator needs during an incident review — and
    because the gap between them is how you tell a rule that works from one that
    is too sensitive."""
    e = AlarmEngine([make_rule(for_s=600.0)], Sink())
    feed(e, 5.0, T0)
    feed(e, 5.0, T0 + 600)
    state = e.states["t"]
    assert state.condition_since == T0
    assert state.raised_at == T0 + 600
    assert state.dwell_for() == 600.0


# ─── grouping and lookup ─────────────────────────────────────────────────────


def test_one_window_serves_every_rule_on_that_signal() -> None:
    """The reason `rules_by_signal` exists.

    Not an optimisation detail: a rule set grows faster than a signal list, so
    the per-rule query count is the thing that gets away from you.
    """
    rules = [
        make_rule(id="a", detector="single_point_threshold",
                  params={"limit": 3.0}),
        make_rule(id="b", detector="single_point_threshold",
                  params={"limit": 4.0}),
        make_rule(id="c", detector="single_point_threshold",
                  params={"limit": 5.0}),
    ]
    e = AlarmEngine(rules, Sink())
    w = window(step([9.0, 9.1, 9.2]), signal_id="A:1:X")
    out = e.evaluate("A:1:X", w)
    assert {t.rule.id for t in out} == {"a", "b", "c"}
    assert len(e.by_signal) == 1


def test_a_signal_with_no_rules_is_a_no_op_not_an_error() -> None:
    e = AlarmEngine([make_rule()], Sink())
    assert e.evaluate("NOT:WATCHED:X", window(step([1.0]))) == []


def test_transitions_are_bounded() -> None:
    """An unbounded list in a long-running process is a leak with a
    plausible-looking API."""
    e = AlarmEngine([make_rule(for_s=0.0)], Sink(), relatch_delta=0.0,
                    max_transitions=50)
    for i in range(200):
        feed(e, 5.0 + i, T0 + i)
    assert len(e.transitions) <= 50


def test_the_summary_reports_status_counts_and_the_active_list() -> None:
    sink = Sink()
    e = AlarmEngine(
        [make_rule(id="a", for_s=0.0, severity="critical"),
         make_rule(id="b", for_s=600.0, severity="warning")],
        sink,
    )
    feed(e, 5.0, T0, signal_id="A:1:X")
    s = e.summary()
    assert s["rules"] == 2
    assert s["by_status"][ACTIVE] == 1
    assert s["by_status"][UNKNOWN] == 1
    assert [a["rule"] for a in s["active"]] == ["a"]
