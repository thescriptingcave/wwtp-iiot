"""The engine: verdicts in, alarm state transitions out, events emitted.

The state machine is the part that stops an alarm system being unusable, and it
is where most of the design decisions are.

## Why dwell time is not a debounce

A detector returns a *current truth*: "the condition holds right now". Turning
that into an alarm needs two durations, and they mean different things:

* **`for_s`** — how long the condition must hold before the alarm *raises*. This
  is not noise rejection, it is a statement about the physics. A blower trip
  moves RPM in under a second and DO over two hours; `for_s = 0` on the first and
  `for_s = 600` on the second is the difference between an alarm you can act on
  and one that fires on the plant breathing.
* **`clear_s`** — how long the condition must be false before the alarm *clears*.
  Always positive, and always longer than people expect. A rule that clears the
  instant its condition goes false will chatter whenever a signal sits on the
  limit, and **a chattering alarm is one operators learn to ignore, which is
  functionally the same as not having it.**

## Latching

`critical` alarms latch. Once raised they stay on the operator's list until
acknowledged, *even after the condition has cleared*. The condition is reported
separately, so the list shows "DO is back in band, this happened at 03:14".

An alarm that disappears by itself is an alarm nobody has to acknowledge, which
is indistinguishable from one that never fired. For `warning` severity the alarm
auto-clears, because a warning that latches is how a mimic diagram fills with
permanently-acknowledged icons and stops being read.

## Deduplication

An alarm that is already active does not re-emit on every evaluation. It re-emits
only if the *observed value* moved by more than `relatch_delta`, so a slowly
worsening alarm produces a periodic update instead of either silence or a flood.
The default is deliberately small; the alternative — no re-emission at all — means
the first message is the only one an operator ever sees, and it is the one with
the least information in it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from alarms.base import (
    ACKNOWLEDGED,
    ACTIVE,
    CLEARED,
    UNKNOWN,
    AlarmRule,
    Verdict,
    Window,
)
from alarms.detectors import detect
from alarms.rules import rules as all_rules
from alarms.rules import rules_by_signal

log = logging.getLogger("alarms.engine")

#: Severities that latch until acknowledged.
LATCHING: frozenset[str] = frozenset({"critical"})


class EventSink(Protocol):
    """Where events go. A protocol so the engine needs no database."""

    def emit(self, *, kind: str, severity: str, message: str,
             signal_id: str | None = None, equipment_id: str | None = None,
             detail: dict[str, Any] | None = None) -> None: ...


@dataclass
class AlarmState:
    """One rule's current state, and the history of how it got there."""

    rule: AlarmRule
    status: str = UNKNOWN
    #: When the condition began holding. `None` means it is not holding.
    condition_since: float | None = None
    #: When the condition last stopped holding.
    clear_since: float | None = None
    #: When the alarm was raised — which is *not* when the condition began, and
    #: the difference is the dwell time. Both timestamps are kept because "the
    #: condition started at 03:00 and we told you at 03:10" is a sentence an
    #: operator needs during an incident review.
    raised_at: float | None = None
    last_verdict: Verdict | None = None
    last_emitted_observed: float | None = None
    last_emitted_at: float | None = None
    raise_count: int = 0
    #: Every transition, for the coverage report and for post-incident review.
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status in (ACTIVE, ACKNOWLEDGED)

    def dwell_for(self) -> float | None:
        """Seconds between the condition starting and the alarm raising.

        The dwell time the rule asked for is a constant; this is the dwell the
        data actually delivered, and the two are worth comparing. A rule that
        raises in 4 minutes when it asked for 10 has either been given a
        condition that was already true when it started being measured, or its
        detector is more sensitive than intended, and both are worth knowing
        before an incident rather than during one.
        """
        if self.condition_since is None or self.raised_at is None:
            return None
        return round(self.raised_at - self.condition_since, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.id,
            "signal": self.rule.signal_id,
            "detector": self.rule.detector,
            "status": self.status,
            "severity": self.rule.severity,
            "raised_at": self.raised_at,
            "condition_since": self.condition_since,
            "dwell_s": self.dwell_for(),
            "observed": (
                self.last_verdict.observed if self.last_verdict else None
            ),
            "raise_count": self.raise_count,
        }


@dataclass(frozen=True, slots=True)
class Transition:
    """Something changed, and here it is."""

    kind: str                  # "raised" | "reaffirmed" | "cleared" | "acknowledged"
    rule: AlarmRule
    state: AlarmState
    at: float
    verdict: Verdict

    def event(self) -> dict[str, Any]:
        return {
            "kind": f"alarm_{self.kind}",
            "severity": self.rule.severity,
            "message": self.rule.message,
            "signal_id": self.rule.signal_id,
            "equipment_id": self.rule.equipment_id,
            "detail": {
                "rule": self.rule.id,
                "detector": self.rule.detector,
                "observed": self.verdict.observed,
                "reason": dict(self.verdict.detail),
                "for_s": self.rule.for_s,
                "dwell_s": self.state.dwell_for(),
            },
        }


class AlarmEngine:
    """Evaluates rules against windows and owns the resulting state.

    Deliberately ignorant of where the windows come from and where the events go.
    `windows_for` is a callable and the sink is a protocol, so the whole state
    machine — dwell, hysteresis, latching, dedup — is testable with a dict of
    hand-built windows and a list of events. That is most of this file's value
    and it costs nothing.
    """

    def __init__(
        self,
        rule_set: Sequence[AlarmRule] | None = None,
        sink: EventSink | None = None,
        *,
        relatch_delta: float = 0.0,
        relatch_interval_s: float = 300.0,
        max_transitions: int = 10_000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.rules = tuple(rule_set) if rule_set is not None else all_rules()
        self.by_signal = rules_by_signal(self.rules)
        self.states: dict[str, AlarmState] = {
            r.id: AlarmState(rule=r) for r in self.rules
        }
        self.sink = sink
        self.relatch_delta = relatch_delta
        #: Minimum wall-clock gap between re-emissions of the same alarm.
        #:
        #: `relatch_delta` alone was not enough, and the first coverage run proved
        #: it: 9 994 of 10 000 transitions were reaffirmations of three alarms,
        #: one every five seconds, for nine hours. A rate limit alone would not be
        #: enough either — an alarm whose value is *static* would then never
        #: update, which is the failure mode in the other direction. Both are
        #: needed: move the value, and not more often than this.
        self.relatch_interval_s = relatch_interval_s
        self._clock = clock or time.time
        #: Every transition, in order. Bounded, because an unbounded list in a
        #: long-running process is a leak with a plausible-looking API — and a
        #: constructor argument rather than an attribute set after the fact,
        #: because a cap you have to remember to apply is a cap that does not
        #: get applied.
        self.transitions: list[Transition] = []
        self.max_transitions = int(max_transitions)

    # ─── evaluation ──────────────────────────────────────────────────────────

    def evaluate(self, signal_id: str, window: Window) -> list[Transition]:
        """Evaluate every rule watching `signal_id` against one window.

        One window serves all of a signal's rules, which is why `rules_by_signal`
        exists. A per-rule query would be 13 queries instead of 9 here and the gap
        widens as rules are added.
        """
        out: list[Transition] = []
        for rule in self.by_signal.get(signal_id, ()):
            out.extend(self._evaluate_one(rule, window))
        return out

    def evaluate_one(self, rule: AlarmRule, window: Window) -> list[Transition]:
        """Evaluate a single rule, trimmed to the history it declares it needs.

        Public because `scenarios.py` drives the engine directly and would
        otherwise have to duplicate the trimming to get the same cost.
        """
        return self._evaluate_one(rule, window.recent(rule.lookback_s()))

    def evaluate_all(self, windows: dict[str, Window]) -> list[Transition]:
        out: list[Transition] = []
        for signal_id, window in windows.items():
            out.extend(self.evaluate(signal_id, window))
        return out

    def _evaluate_one(self, rule: AlarmRule, window: Window) -> list[Transition]:
        state = self.states[rule.id]
        now = window.now
        verdict = detect(rule, window)
        state.last_verdict = verdict

        if verdict.active:
            state.clear_since = None
            if state.condition_since is None:
                state.condition_since = now
        else:
            state.condition_since = None
            if state.clear_since is None:
                state.clear_since = now

        if verdict.active and not state.active:
            if state.condition_since is not None and \
                    now - state.condition_since >= rule.for_s:
                return [self._raise(state, verdict, now)]
            return []

        if not verdict.active and state.active:
            # A latched alarm stays put; an auto-clearing one needs the
            # condition false for long enough first.
            if rule.severity in LATCHING:
                return []
            if state.clear_since is not None and \
                    now - state.clear_since >= rule.clear_s:
                return [self._clear(state, verdict, now)]
            return []

        if verdict.active and state.active:
            return self._maybe_reaffirm(state, verdict, now)

        return []

    # ─── transitions ─────────────────────────────────────────────────────────

    def _record(self, t: Transition) -> Transition:
        self.transitions.append(t)
        self._trim()
        return t

    def _trim(self) -> None:
        """Bound the log, dropping *reaffirmations* first.

        The first version dropped from the front, which is the obvious thing and
        the wrong one: the transitions that got evicted were the `raised` records
        at the start of the run, so a coverage report over a long run silently
        lost every fault detection and reported the faults as blind spots. The
        bound is only safe if what it discards is the least valuable thing, and
        "a repetition of an alarm that is already known to be active" is that
        thing by a wide margin.
        """
        overflow = len(self.transitions) - self.max_transitions
        if overflow <= 0:
            return
        keep_from = 0
        dropped = 0
        while dropped < overflow and keep_from < len(self.transitions):
            if self.transitions[keep_from].kind == "reaffirmed":
                dropped += 1
            else:
                keep_from += 1
        if dropped == 0:
            # Everything left is significant, so the bound is doing what it can
            # and the cap is simply too small for the run.
            log.warning(
                "alarm transition log is full of significant events at %d; "
                "raising max_transitions or accepting the truncation",
                self.max_transitions,
            )
            return
        self.transitions = [
            t for i, t in enumerate(self.transitions)
            if t.kind == "reaffirmed" or i < keep_from or i >= keep_from + dropped
        ][: self.max_transitions]

    def _emit(self, t: Transition) -> None:
        if self.sink is not None:
            self.sink.emit(**t.event())

    def _raise(self, state: AlarmState, verdict: Verdict, now: float) -> Transition:
        state.status = ACTIVE
        state.raised_at = now
        state.raise_count += 1
        state.last_emitted_observed = verdict.observed
        state.last_emitted_at = now
        state.last_emitted_at = now
        t = self._record(Transition("raised", state.rule, state, now, verdict))
        state.history.append({"at": now, "kind": "raised",
                              "observed": verdict.observed})
        log.warning("ALARM %s: %s (observed=%s)", state.rule.id,
                    state.rule.message, verdict.observed)
        self._emit(t)
        return t

    def _clear(self, state: AlarmState, verdict: Verdict, now: float) -> Transition:
        state.status = CLEARED
        t = self._record(Transition("cleared", state.rule, state, now, verdict))
        state.history.append({"at": now, "kind": "cleared"})
        log.info("alarm %s cleared after %.0f s", state.rule.id,
                 now - (state.raised_at or now))
        self._emit(t)
        return t

    def _maybe_reaffirm(self, state: AlarmState, verdict: Verdict,
                        now: float) -> list[Transition]:
        """Re-emit only if the value moved **and** enough time has passed.

        Both conditions, because either alone is wrong in a way that matters:

        * **movement only** — a worsening alarm emits on every evaluation. The
          first coverage run produced 9 992 reaffirmations of three alarms over
          nine hours, one every five seconds, and the `event` table became
          useless for the query it exists to answer.
        * **time only** — an alarm whose value is static never updates, so the
          first message an operator ever sees is the one with the least
          information in it.

        So: the value has to have moved by more than `relatch_delta`, *and*
        `relatch_interval_s` has to have elapsed.
        """
        previous = state.last_emitted_observed
        previous_at = state.last_emitted_at
        if previous is None or verdict.observed is None:
            state.last_emitted_observed = verdict.observed
            return []
        if abs(verdict.observed - previous) <= self.relatch_delta:
            state.last_emitted_observed = verdict.observed
            return []
        if previous_at is not None and \
                now - previous_at < self.relatch_interval_s:
            return []
        state.last_emitted_observed = verdict.observed
        state.last_emitted_at = now
        t = self._record(
            Transition("reaffirmed", state.rule, state, now, verdict)
        )
        self._emit(t)
        return [t]

    # ─── operator actions ────────────────────────────────────────────────────

    def acknowledge(self, rule_id: str, at: float | None = None) -> bool:
        """Clear a latched alarm off the operator's list.

        Separate from `_clear` on purpose: acknowledging says "a human has seen
        this", and conflating the two loses the information that distinguishes
        *"nobody was looking"* from *"somebody looked and it recovered"*.
        """
        state = self.states.get(rule_id)
        if state is None or not state.active:
            return False
        state.status = ACKNOWLEDGED
        t = self._record(Transition(
            "acknowledged", state.rule, state, at or self._clock(),
            state.last_verdict or Verdict.ok(),
        ))
        state.history.append({"at": t.at, "kind": "acknowledged"})
        self._emit(t)
        return True

    # ─── introspection ───────────────────────────────────────────────────────

    def active_alarms(self) -> list[AlarmState]:
        """Everything raised and not yet cleared, acknowledged or not.

        What an incident review wants: the full set, because "somebody saw it" is
        itself a fact worth having.
        """
        return [s for s in self.states.values() if s.active]

    def unacknowledged(self) -> list[AlarmState]:
        """What is on the operator's screen right now.

        Separate from `active_alarms` because those are two different questions
        and answering the first when you were asked the second produces a panel
        full of greyed-out icons that everyone learns to ignore. The whole reason
        `critical` latches is so that "nobody has looked at this yet" is
        distinguishable from "this is fine now".
        """
        return [
            s for s in self.states.values()
            if s.status == ACTIVE and s.rule.severity in LATCHING
        ]

    def latched(self) -> list[AlarmState]:
        """Every latched alarm, acknowledged or not — the shift handover list."""
        return [s for s in self.states.values() if s.status in (ACTIVE, ACKNOWLEDGED)]

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for s in self.states.values():
            by_status[s.status] = by_status.get(s.status, 0) + 1
        return {
            "rules": len(self.rules),
            "signals": len(self.by_signal),
            "by_status": by_status,
            "active": [s.as_dict() for s in self.active_alarms()],
            "transitions": len(self.transitions),
        }

    def states_for(self, fault_id: str) -> list[AlarmState]:
        """Every state whose rule claims to detect ``fault_id``."""
        return [s for s in self.states.values() if fault_id in s.rule.detects]


def evaluate_windows(
    engine: AlarmEngine,
    windows: Iterable[Window],
) -> list[Transition]:
    """Convenience for tests and the coverage report."""
    out: list[Transition] = []
    for window in windows:
        out.extend(engine.evaluate(window.signal_id, window))
    return out
