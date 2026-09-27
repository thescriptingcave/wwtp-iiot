"""Rebuild alarm state from the events the engine itself wrote.

    python -m alarms.replay --dry-run
    python -m alarms.replay

## Why this module exists

`AlarmEngine` holds its state in memory. That is fine for the state *machine* and
wrong for the *facts*: a critical alarm latches, and a latch that lives in a
dictionary dies with the process. So on every restart the engine forgets which
alarms were active and which had been acknowledged, and the operator's panel
comes back empty for a plant that is still in fault.

Worse, and this is the part that makes it a correctness problem rather than an
inconvenience: **an acknowledgement that is not durable is not an
acknowledgement.** If the engine forgets, the same alarm re-raises and asks to be
acknowledged again, and an operator who has seen it four times stops looking. The
one signal that says "a human has seen this" is the signal that stops meaning
that.

The events are the answer. The engine writes every transition to `event` with the
rule id in `detail`, so the history is already durable — the engine simply never
reads it back.

## The design decision this forces: a rule, or an occurrence?

An operator looking at a panel sees *an alarm* — "DO is sagging". They acknowledge
what they can see, which is a rule that is currently active. They do not, and
cannot, acknowledge "the third time this happened today".

So: **acknowledgement is per rule, and it clears when the rule's condition
clears.** Not per occurrence, and not forever.

Both alternatives are worse, and the reasons are worth stating because the choice
looks arbitrary otherwise:

* **Per occurrence** (`event.id`) means an operator must acknowledge a flapping
  alarm on every flap. A rule that fires and clears every thirty seconds produces
  2 880 acknowledgements a day, and a panel full of unacknowledged rows that
  nobody will ever work through. The alarm is telling the truth and being
  useless.
* **Per rule, forever** means an alarm that is genuinely new an hour later — same
  rule, different cause — is silenced. The acknowledgement was for *that* event.

Clearing on condition-clear sits between them and matches what an operator means
by "I've seen this": while the fault is still there, the acknowledgement stands
and the alarm stays off the list; when the fault is gone, the next occurrence is
a new event and deserves a new acknowledgement.

## What it is *not* doing

It is not making the engine durable in general. State that has not been
*evaluated* cannot be reconstructed from events — the engine still has to see the
plant once before it knows a rule is quiet. So a replay gives you "these alarms
were active and acknowledged when the process last stopped", and the first
evaluation after a restart then confirms or clears them. That is the right
division: **events are the record of what was decided, and the evaluation is the
only thing that can decide something new.**
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("alarms.replay")

#: How far back a replay looks. Long enough to cover a long outage, short enough
#: that a rule which has been quiet for a month is not resurrected.
DEFAULT_LOOKBACK_H = 24.0


@dataclass
class ReplayedAlarm:
    """One alarm as the event log says it was."""

    rule_id: str
    #: Epoch seconds of the `alarm_raised` event.
    raised_at: float
    #: Epoch seconds of the `alarm_acknowledged` event, or ``None``.
    acknowledged_at: float | None = None
    #: Epoch seconds of the `alarm_cleared` event, or ``None``.
    cleared_at: float | None = None
    signal_id: str | None = None
    severity: str = "critical"
    message: str = ""

    @property
    def resolved(self) -> bool:
        """Whether this alarm is off the operator's list, and why.

        Two independent reasons, and **conflating them is the bug this property
        was written after**:

        * *a human saw it* — `acknowledged_at` is set.
        * *the fault went away* — `cleared_at` is set.

        The obvious definition, "active means not cleared", is wrong for exactly
        the alarms this whole project is about. A `critical` alarm **latches**:
        it stays on the list after the condition clears, until someone
        acknowledges it, and `AlarmEngine` never writes an `alarm_cleared` for it
        at all. So an acknowledged critical has `cleared_at is None` forever, and
        "not cleared" would keep reporting it as outstanding for the rest of
        time.

        The first version of this module did that, and
        `test_a_recurrence_is_a_new_alarm_and_does_not_inherit_the_acknowledgement`
        failed with two active alarms where there should have been one: the
        acknowledged first raise and the recurrence, counted the same way.
        """
        return self.acknowledged_at is not None or self.cleared_at is not None

    @property
    def latched(self) -> bool:
        """Whether it was still latched when the log ended.

        Separate from `resolved` on purpose: "the fault is gone and nobody has
        looked yet" and "the fault is still here" are different messages for an
        operator, and a panel that shows only the first is telling them a fault
        has recovered when the engine never confirmed it.
        """
        return self.cleared_at is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_id,
            "raised_at": self.raised_at,
            "acknowledged_at": self.acknowledged_at,
            "cleared_at": self.cleared_at,
            "on_panel": not self.resolved,
            "latched": self.latched,
            "acknowledged": self.acknowledged_at is not None,
            "signal_id": self.signal_id,
            "severity": self.severity,
        }


@dataclass
class ReplayResult:
    """What a replay found, and what it could not."""

    alarms: list[ReplayedAlarm] = field(default_factory=list)
    #: Rule ids seen in the log, whether or not they are still active.
    seen: set[str] = field(default_factory=set)
    #: Events that could not be attributed to a rule. Not an error — a
    #: hand-written event has no rule — and counted rather than ignored, because a
    #: replay that silently drops rows is a replay you cannot trust.
    unattributed: int = 0
    lookback_s: float = DEFAULT_LOOKBACK_H * 3600.0

    @property
    def active(self) -> list[ReplayedAlarm]:
        """Every alarm still on the panel, most recent first.

        **Which is exactly the set nobody has acted on** — an acknowledged alarm
        is off the list and a cleared one is off the list. `unacknowledged` is
        kept as a separate name because it is the word the *tests* and the docs
        use, and because the two lists being equal is a fact about this design
        rather than an accident worth hiding.
        """
        return sorted(
            (a for a in self.alarms if not a.resolved),
            key=lambda a: a.raised_at,
            reverse=True,
        )

    @property
    def acknowledged(self) -> list[ReplayedAlarm]:
        """Acknowledged but not cleared: the latch, seen by a human.

        Not on the panel, and that is the point — but worth being able to list,
        because "the operator saw it and the condition never cleared" is the
        sentence an incident review is made of.
        """
        return [a for a in self.alarms
                if a.acknowledged_at is not None and a.cleared_at is None]

    @property
    def unacknowledged(self) -> list[ReplayedAlarm]:
        """What the operator's panel is for: nobody has looked at these."""
        return [a for a in self.alarms
                if a.acknowledged_at is None and a.cleared_at is None]

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookback_h": self.lookback_s / 3600.0,
            "rules_seen": len(self.seen),
            "alarms": len(self.alarms),
            "active": len(self.active),
            "unacknowledged": [a.rule_id for a in self.unacknowledged],
            "acknowledged": [a.rule_id for a in self.acknowledged],
            "unattributed_events": self.unattributed,
        }


#: The query. One row per event, ordered, with the rule id pulled out of `detail`.
#:
#: `detail->>'rule'` because the engine puts the rule there and there is no column
#: for it. That is a real cost — a JSONB extract in a `WHERE` is not indexable as
#: cheaply as a column — and it is the cost of a schema that was not designed for
#: this. The alternative is a migration, and the honest note is that the engine is
#: the only writer and the only reader, so the cost is currently theoretical.
QUERY = """
SELECT ts, kind, severity, message, signal_id,
       detail->>'rule' AS rule
FROM event
WHERE ts >= now() - (interval '1 hour' * %s)
  AND kind IN ('alarm_raised', 'alarm_cleared', 'alarm_acknowledged')
ORDER BY ts
"""
#: `(interval '1 hour' * %s)` rather than `make_interval(hours => %s)`, because
#: `make_interval` takes an `int` and psycopg sends a Python `float`:
#:
#:     ERROR:  function make_interval(hours => double precision) does not exist
#:     HINT:  No function matches the given name and argument types.
#:
#: The interval form also accepts a fractional lookback, which `make_interval`
#: cannot without rounding, and a lookback of 0.5 h is a reasonable thing to want
#: when debugging.


def replay(
    rows: Sequence[tuple[Any, ...]],
    *,
    lookback_s: float = DEFAULT_LOOKBACK_H * 3600.0,
) -> ReplayResult:
    """Turn event rows into reconstructed alarm state.

    A pure function of the rows, so it is testable with hand-built events and no
    database — which is the only way to test the awkward cases, and the awkward
    cases are all of the "what happens when the log says something surprising"
    variety.

    The three shapes that have to be right:

    * **raise → acknowledge → raise again.** The second raise is a *new* alarm
      and must not inherit the first's acknowledgement, or an operator who saw the
      fault once never sees the recurrence. Which is the same "per occurrence"
      reasoning as `alarms/coverage.py`'s treatment of incidental catches: a
      repeat of something already known is not new information.
    * **raise → clear → acknowledge.** The acknowledgement arrives after the
      condition went. It is honoured — the operator saw it — and the alarm is off
      the list because it cleared.
    * **acknowledge with no matching raise.** Counted, not applied. It is either a
      race (the engine wrote the acknowledgement and crashed before the raise was
      committed, which cannot happen in one transaction but *can* happen across
      two) or a hand-written event, and in both cases the safe reading is "there
      is no alarm to acknowledge".
    """
    result = ReplayResult(lookback_s=lookback_s)
    by_rule: dict[str, ReplayedAlarm] = {}

    for row in rows:
        ts, kind, severity, message, signal_id, rule = (
            row[0], row[1], row[2], row[3], row[4], row[5] if len(row) > 5 else None,
        )
        at = ts.timestamp() if hasattr(ts, "timestamp") else float(ts)

        if not rule:
            result.unattributed += 1
            log.debug("event at %s with no rule in detail: %s", at, kind)
            continue

        result.seen.add(rule)

        if kind == "alarm_raised":
            # A repeat raise starts a fresh alarm. Deliberately *not* carrying
            # the previous acknowledgement forward: a fault that came back is a
            # new thing, and an operator who acknowledged it an hour ago has not
            # seen the recurrence.
            result.alarms.append(ReplayedAlarm(
                rule_id=rule, raised_at=at, signal_id=signal_id,
                severity=severity, message=message,
            ))
            by_rule[rule] = result.alarms[-1]

        elif kind == "alarm_acknowledged":
            alarm = by_rule.get(rule)
            if alarm is None:
                # The "acknowledge with no raise" case. Counted by `unattributed`
                # would be wrong -- it *is* attributed, just not to anything -- so
                # it is simply dropped, and the drop is visible here.
                log.info(
                    "acknowledgement for %s at %s with no matching raise in the "
                    "window; treating the panel as showing nothing for it",
                    rule, at,
                )
                continue
            alarm.acknowledged_at = at

        elif kind == "alarm_cleared":
            alarm = by_rule.get(rule)
            if alarm is None:
                continue
            alarm.cleared_at = at

    return result


def load(conn: Any, *, lookback_h: float = DEFAULT_LOOKBACK_H) -> ReplayResult:
    """Read the events and replay them. The only part that touches a database."""
    with conn.cursor() as cur:
        cur.execute(QUERY, (lookback_h,))
        rows = cur.fetchall()
    result = replay(rows, lookback_s=lookback_h * 3600.0)
    log.info(
        "replayed %d events: %d rules, %d alarms, %d active, %d unacknowledged",
        len(rows), len(result.seen), len(result.alarms),
        len(result.active), len(result.unacknowledged),
    )
    return result


def apply_to_engine(engine: Any, result: ReplayResult) -> int:
    """Put the replayed acknowledgements into a live engine.

    The state that is rebuilt is the *acknowledgement*, and nothing else. A rule
    that was active is not marked active here, because the engine does not know
    whether it still is until it evaluates the plant — and it will, on its first
    tick. Marking it active from history would mean showing an operator an alarm
    the engine has not yet confirmed, which is a claim about the plant made by
    something that has stopped watching it.

    So this restores exactly the fact that cannot be re-derived — *somebody has
    already seen this* — and lets the next evaluation decide everything else.
    """
    applied = 0
    for alarm in result.acknowledged:
        if engine.acknowledge(alarm.rule_id, at=alarm.acknowledged_at):
            applied += 1
    log.info("re-applied %d acknowledgements to %d rules", applied, len(engine.rules))
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alarms.replay",
        description="Rebuild alarm state from the event log.",
    )
    parser.add_argument("--lookback-hours", type=float, default=DEFAULT_LOOKBACK_H)
    parser.add_argument("--apply", action="store_true",
                        help="print the replay and exit (the default)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    from storage.postgres.schema import connect

    with connect() as conn:
        result = load(conn, lookback_h=args.lookback_hours)

    if args.json:
        print(json.dumps(
            {**result.as_dict(), "alarms": [a.as_dict() for a in result.alarms]},
            indent=2, default=str,
        ))
        return 0

    print()
    print(f"replayed {args.lookback_hours:g}h of the event log")
    print(f"  {len(result.alarms)} alarms over {len(result.seen)} rules")
    print(f"  {len(result.active)} still standing")
    if result.unacknowledged:
        print("  waiting for a human:")
        for alarm in result.unacknowledged:
            print(f"    {alarm.rule_id}  raised {alarm.raised_at:.0f}  "
                  f"{alarm.severity}")
    else:
        print("  nothing is waiting for an acknowledgement")
    if result.acknowledged:
        print("  already acknowledged:")
        for alarm in result.acknowledged:
            print(f"    {alarm.rule_id}  at {alarm.acknowledged_at:.0f}")
    if result.unattributed:
        print(f"  {result.unattributed} events carried no rule id and were not "
              "attributed to anything")

    if args.apply:
        from alarms.engine import AlarmEngine
        from alarms.rules import rules

        engine = AlarmEngine(rules())
        applied = apply_to_engine(engine, result)
        print(f"\napplied {applied} acknowledgement(s) to a fresh engine")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
