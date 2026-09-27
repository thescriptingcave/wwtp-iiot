"""Run the fault library through the plant and see which rules actually fire.

This is what turns `alarms/coverage.py` from a report about a hand-built set of
transitions into a measurement. The loop is:

    for each fault in the contract:
        arm it on a healthy plant
        step the model for N hours at the scan rate
        record the published values once a second
        feed each signal's window to the engine
        collect the transitions

and the output is a matrix of fault against rule.

## No database, on purpose

Every other part of this project either reads from Postgres or is a test that
does not. This is neither, and that is a deliberate choice worth defending.

The question being answered is *"given a plant that behaves this way, does this
rule fire?"* — and the plant model is a pure function of its inputs. Routing the
values through `reading` and back out would add a seeder run, a database, a
five-minute wall clock and a hydration layer, and the answer would be identical.
It would also make the coverage report unavailable in a unit test, which is
exactly where a report about alarm coverage belongs.

The database path exists separately, in `alarms/main.py`, and it is what runs in
the container. This module is the measurement instrument, and instruments should
be simpler than the thing they measure.

## The deadband is not applied here, and that is a real difference

`SoftPlc._run` applies the deadband before publishing, so a settled signal produces
almost no readings. This module records **every** sample at the record rate and
hands the engine every one of them.

That is a difference from production and it flatters the alarm rules: no rule has
to survive a six-minute gap between DO readings. It is done anyway because the
question here is *"can this rule see this fault"* and the deadband is a
*storage* decision, not a detection one. `docs/LEARNING-LOG.md` records it as the
one place this measurement is optimistic, and `coverage.py` reports the sample
count so the reader can judge.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from softplc.contract import Contract
from softplc.contract import contract as get_contract
from softplc.faults.engine import FaultEngine
from softplc.process.plant import Plant

from alarms.base import AlarmRule, Sample, Window
from alarms.engine import AlarmEngine, Transition
from alarms.rules import rules as all_rules

log = logging.getLogger("alarms.scenarios")

#: Physics step for the coverage run.
#:
#: The live soft PLC uses 20 ms, and this is ten times coarser on purpose. The
#: signatures this module measures are hours long — a DO sag over one to two
#: hours, ammonia over two to six — and at that scale the integration error from
#: a 100 ms step is many orders of magnitude below the effect being detected. The
#: cost of the finer step is 1 440 000 physics evaluations per fault instead of
#: 288 000, and eleven faults is the difference between a report you run and a
#: report you run once.
#:
#: What this *would* break is a signature shorter than a few seconds. There is
#: none in the fault library, and `docs/LEARNING-LOG.md` records it as the
#: assumption a future fault would have to check.
SCAN_DT = 0.1

#: Seconds between recorded samples.
#:
#: Five seconds, not one, and the reason is arithmetic rather than taste. A
#: `trend` rule with `min_points = 4` on a 5 s cadence sees the same shape as on
#: a 1 s cadence, and the run gets five times cheaper: 5 760 recorded samples
#: rather than 28 800 for an eight-hour fault. The `rate_of_change` detector does
#: care about spacing, and the fault library's fastest signature is a fan dropping
#: over a 30 s ramp, which 5 s resolves six times over.
RECORD_DT = 5.0

#: How long to run each fault. Long enough for the slowest signature the contract
#: claims — the blower trip's ammonia response is "two to six hours" — and short
#: enough to run eleven of them in a test.
DEFAULT_HOURS = 8.0


@dataclass
class FaultRun:
    """The result of running one fault."""

    fault_id: str
    transitions: list[Transition]
    samples: int
    hours: float
    #: Peak value seen per signal, for reading alongside the matrix.
    peaks: dict[str, float] = field(default_factory=dict)

    def raised(self, rule_id: str) -> bool:
        return any(
            t.kind == "raised" and t.rule.id == rule_id
            for t in self.transitions
        )

    def first_raise(self, rule_id: str) -> float | None:
        times = [
            t.at for t in self.transitions
            if t.kind == "raised" and t.rule.id == rule_id
        ]
        return min(times) if times else None


class _NullSink:
    """Events are collected on the engine, not emitted anywhere here."""

    def emit(self, **_: Any) -> None:
        return None


def run_fault(
    fault_id: str,
    *,
    contract: Contract | None = None,
    hours: float = DEFAULT_HOURS,
    engine: AlarmEngine | None = None,
    rule_set: Sequence[AlarmRule] | None = None,
    include_baseline_hours: float = 1.0,
) -> FaultRun:
    """Arm one fault on a healthy plant and record what the rules do.

    `include_baseline_hours` of healthy operation come first, deliberately. A rule
    that fires on a quiet plant is worse than a rule that does not exist, and
    without a baseline period there is no way to tell the two apart — every rule
    would look like it "caught" the fault if it happened to be twitchy.
    """
    c = contract or get_contract()
    plant = Plant(c=c)
    faults = FaultEngine(plant)
    eng = engine or AlarmEngine(
        rule_set if rule_set is not None else all_rules(), _NullSink()
    )
    # Fresh state per fault: a rule that stayed active from the previous fault
    # would make every subsequent fault look detected.
    eng = AlarmEngine(
        eng.rules, _NullSink(), relatch_delta=eng.relatch_delta,
        relatch_interval_s=eng.relatch_interval_s,
        max_transitions=eng.max_transitions,
    )

    baseline_s = include_baseline_hours * 3600.0
    total_s = baseline_s + hours * 3600.0

    if fault_id and fault_id != "baseline":
        if fault_id not in faults.faults:
            raise ValueError(
                f"unknown fault {fault_id!r}; the engine offers "
                f"{sorted(faults.faults)}"
            )
        # Armed at zero and left to expire on its own declared duration, so the
        # recovery half of the signature is captured rather than cut off.
        faults.arm(fault_id, 0.0)

    # A ring per signal: the engine needs a *window*, and the longest rule needs
    # 7200 s of history. Holding the whole run for eleven faults would be tens of
    # millions of samples.
    from alarms.rules import rules_by_signal

    longest = max((r.params.get("stuck_s", 0.0) or 0.0 for r in eng.rules),
                  default=0.0)
    longest = max(longest, max((r.for_s for r in eng.rules), default=0.0))
    keep_s = longest + 3600.0

    # A deque with a maxlen, not a list that is filtered. The list version was
    # O(signals x history) per recorded sample and dominated the whole run: the
    # physics is 46 us a step and the window rebuild was an order of magnitude
    # more than that.
    from collections import deque

    maxlen = int(keep_s / RECORD_DT) + 4
    log.debug("history maxlen %d samples per signal", maxlen)
    history: dict[str, deque[Sample]] = {}
    peaks: dict[str, float] = {}
    steps = int(total_s / SCAN_DT)
    every = max(1, int(RECORD_DT / SCAN_DT))

    for i in range(steps):
        snapshot = faults.step(SCAN_DT)
        now = faults.now_s
        if i % every:
            continue
        for signal_id, value in snapshot.values.items():
            if signal_id not in history:
                history[signal_id] = deque(maxlen=maxlen)
            history[signal_id].append(
                Sample(ts=now, value=value,
                       quality=snapshot.quality.get(signal_id, 0))
            )
            if value is not None:
                peaks[signal_id] = max(peaks.get(signal_id, value), value)

        for signal_id in rules_by_signal(eng.rules):
            eng.evaluate(
                signal_id,
                Window(signal_id=signal_id, points=history[signal_id], now=now),
            )
        if fault_id and i % (every * 600) == 0 and i:
            log.info("  %s t+%.0fs", fault_id, now)

    return FaultRun(
        fault_id=fault_id,
        transitions=list(eng.transitions),
        samples=sum(len(v) for v in history.values()),
        hours=total_s / 3600.0,
        peaks=peaks,
    )


def run_all(
    fault_ids: Iterable[str],
    *,
    hours: float = DEFAULT_HOURS,
    **kw: Any,
) -> dict[str, FaultRun]:
    """Every fault in turn. About four seconds each, so eleven is under a minute."""
    out: dict[str, FaultRun] = {}
    for fid in fault_ids:
        log.info("running fault %s", fid)
        out[fid] = run_fault(fid, hours=hours, **kw)
    return out


def measure(
    fault_ids: Iterable[str] | None = None,
    *,
    hours: float = DEFAULT_HOURS,
    **kw: Any,
) -> tuple[AlarmEngine, dict[str, FaultRun]]:
    """Run the library and return the engine alongside the runs.

    The engine is the *last* one, which is not useful — the per-fault results are
    the output. It is returned because a caller usually wants the rule set and
    this avoids a second call to `rules()`.
    """
    c = kw.pop("contract", None) or get_contract()
    if fault_ids is None:
        fault_ids = sorted(FaultEngine(Plant(c=c)).faults)
    runs = run_all(fault_ids, hours=hours, contract=c, **kw)
    return AlarmEngine(all_rules(c), _NullSink()), runs
