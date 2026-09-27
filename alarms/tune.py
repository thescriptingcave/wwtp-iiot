"""Measure each rule's separation between a healthy plant and the fault it claims.

`docs/ALARMS.md` records the first real finding of this project's alarm phase:
`aeration_do_sagging` fired on **ten of eleven** faults *and on the healthy
plant*, because its 0.15 mg/L per hour threshold sat inside the diurnal
distribution. It was fixed by measuring 1 618 healthy windows and moving the
threshold above the steepest one.

That worked once, by hand, in a throwaway script. This module is the same
measurement, for every rule, repeatably — because doing it by hand five more times
is how you get it wrong twice.

    python -m alarms.tune                      # every rule, every scenario
    python -m alarms.tune --rule aeration_do_sagging
    python -m alarms.tune --json               # machine-readable

## What it measures

For each rule, in a **healthy** run and in a run of each fault that rule claims:

* the distribution of the detector's `observed` value;
* the healthy 1st and 99th percentiles — the band a healthy plant stays inside;
* the fault's 1st and 99th percentiles;
* whether the rule's **current** threshold separates them, and by how much.

The last one is the point. A threshold that sits *inside* the healthy band is a
false-positive generator however reasonable it looks, and a threshold that sits
outside the fault's band is a rule that cannot detect what it claims to.

## Why it drives the plant rather than reading the database

Same reasoning as `scenarios.py`, and the reason is that this measurement has to
be runnable in a unit test. Four thousand physics evaluations and a detector call
per rule per sample is about forty seconds; routing the values through `reading`
and back would be a seeder run, a database, and a hydration layer, and the answer
would be identical.

**It does not apply the deadband.** Every sample is recorded at the record rate, so
no rule has to survive a six-minute gap between dissolved-oxygen readings. That
flatters the rules, it is recorded in `docs/ALARMS.md`, and `make alarms` runs the
real path. A tuning tool that measured a harder problem than production would push
thresholds the wrong way.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from softplc.contract import Contract
from softplc.contract import contract as get_contract
from softplc.faults.engine import FaultEngine
from softplc.process.plant import Plant

from alarms.base import AlarmRule, Sample, Verdict, Window
from alarms.detectors import detect
from alarms.rules import rules as all_rules
from alarms.scenarios import DEFAULT_HOURS, RECORD_DT, SCAN_DT

log = logging.getLogger("alarms.tune")

#: Simulated hours per run. Eight covers the slowest signature in the library —
#: the blower trip's ammonia response is "two to six hours" — and it is
#: `scenarios.DEFAULT_HOURS`, so the tuning table and the coverage matrix are
#: measured under identical conditions and their numbers are comparable.
#:
#: Referenced rather than redeclared because two reports about the same rules
#: computed over different horizons are two reports that cannot be compared, and
#: the first version of this file had a stray import of a name that does not
#: exist rather than this one.
HOURS = DEFAULT_HOURS

#: Fraction of healthy history to ignore, so the run is measured after the plant
#: has settled rather than from its first sample.
SETTLE_S = 900.0


@dataclass
class Distribution:
    """One detector's output over one run."""

    samples: int = 0
    values: list[float] = field(default_factory=list)
    fired: int = 0

    def add(self, verdict: Verdict) -> None:
        self.samples += 1
        if verdict.active:
            self.fired += 1
        if verdict.observed is not None and not math.isnan(verdict.observed):
            self.values.append(verdict.observed)

    def percentile(self, p: float) -> float | None:
        if not self.values:
            return None
        ordered = sorted(self.values)
        k = (len(ordered) - 1) * (p / 100.0)
        lo, hi = math.floor(k), math.ceil(k)
        if lo == hi:
            return ordered[int(k)]
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)

    @property
    def fired_fraction(self) -> float:
        return self.fired / self.samples if self.samples else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "fired": self.fired,
            "fired_fraction": round(self.fired_fraction, 5),
            "p01": _round(self.percentile(1)),
            "p50": _round(self.percentile(50)),
            "p99": _round(self.percentile(99)),
            "min": _round(min(self.values)) if self.values else None,
            "max": _round(max(self.values)) if self.values else None,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


@dataclass
class RuleTuning:
    """How one rule behaves on a healthy plant and on the fault it claims."""

    rule: AlarmRule
    healthy: Distribution
    faults: dict[str, Distribution] = field(default_factory=dict)

    @property
    def false_positive_rate(self) -> float:
        return self.healthy.fired_fraction

    @property
    def detects(self) -> dict[str, float]:
        return {f: d.fired_fraction for f, d in self.faults.items()}

    @property
    def misses_claimed_faults(self) -> list[str]:
        return [f for f, rate in self.detects.items() if rate == 0.0]

    def verdict(self) -> str:
        """One word, because that is what a tuning table needs in a column."""
        if self.false_positive_rate > 0:
            if self.misses_claimed_faults:
                return "noisy and blind"
            return "noisy"
        if self.misses_claimed_faults:
            return "blind"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.id,
            "detector": self.rule.detector,
            "severity": self.rule.severity,
            "claims": list(self.rule.detects),
            "verdict": self.verdict(),
            "false_positive_rate": round(self.false_positive_rate, 5),
            "healthy": self.healthy.as_dict(),
            "faults": {f: d.as_dict() for f, d in self.faults.items()},
        }


def _window_for(
    history: dict[str, list],
    rule: AlarmRule,
    now: float,
) -> Window:
    lookback = rule.lookback_s()
    cutoff = now - lookback
    return Window(
        signal_id=rule.signal_id,
        points=tuple(p for p in history.get(rule.signal_id, ()) if p.ts >= cutoff),
        now=now,
    )


def measure_run(
    rule_set: Sequence[AlarmRule],
    *,
    fault_id: str | None,
    hours: float = HOURS,
    contract: Contract | None = None,
) -> dict[str, Distribution]:
    """One simulated run; the observed distribution of every rule in ``rule_set``."""
    c = contract or get_contract()
    plant = Plant(c=c)
    faults = FaultEngine(plant)
    if fault_id:
        faults.arm(fault_id, 0.0)

    out = {r.id: Distribution() for r in rule_set}
    # Every rule needs every signal it watches, plus the signals its *own* rule
    # reads. The rule set already covers the rules; the cross-validation rules
    # need two observations at one instant, which the simulator does not produce.
    signals = sorted({r.signal_id for r in rule_set})
    history: dict[str, list] = {s: [] for s in signals}

    every = max(1, int(RECORD_DT / SCAN_DT))
    total = int(hours * 3600 / SCAN_DT)
    keep_s = max((r.lookback_s() for r in rule_set), default=3600.0) * 2.0

    for i in range(total):
        snapshot = faults.step(SCAN_DT)
        now = faults.now_s
        if i % every or now < SETTLE_S:
            continue
        for signal_id in signals:
            history[signal_id].append(
                Sample(ts=now, value=snapshot.values.get(signal_id),
                       quality=snapshot.quality.get(signal_id, 0))
            )
            history[signal_id] = [
                p for p in history[signal_id] if p.ts >= now - keep_s
            ]
        for rule in rule_set:
            verdict = detect(rule, _window_for(history, rule, now))
            out[rule.id].add(verdict)

    return out


def tune(
    rule_set: Sequence[AlarmRule] | None = None,
    *,
    hours: float = HOURS,
    contract: Contract | None = None,
    faults: Sequence[str] | None = None,
) -> list[RuleTuning]:
    """Healthy, plus one run per fault the rule set claims. About 40 s a run."""
    c = contract or get_contract()
    rules = list(rule_set if rule_set is not None else all_rules(c))

    claimed = {f for r in rules for f in r.detects}
    if faults is not None:
        claimed &= set(faults)
    # Faults claimed by nobody still need a healthy-style run, because a rule that
    # only *incidentally* fires on them is exactly what this tool is looking for.
    available = set(FaultEngine(Plant(c=c)).faults)
    wanted = sorted(claimed & available) or sorted(available)[:1]

    log.info("healthy baseline, %d rules, %.0f h", len(rules), hours)
    healthy = measure_run(rules, fault_id=None, hours=hours, contract=c)

    results: dict[str, RuleTuning] = {
        r.id: RuleTuning(rule=r, healthy=healthy[r.id]) for r in rules
    }
    for fault_id in wanted:
        log.info("fault %s", fault_id)
        run = measure_run(rules, fault_id=fault_id, hours=hours, contract=c)
        for rule in rules:
            results[rule.id].faults[fault_id] = run[rule.id]
    return list(results.values())


# ── rendering ────────────────────────────────────────────────────────────────


def render(results: Sequence[RuleTuning]) -> str:
    w = max((len(r.rule.id) for r in results), default=20)
    lines = [
        f"{'rule'.ljust(w)}  {'verdict':<16} {'healthy FP':>10}  "
        f"{'healthy p01..p99':>26}  detects",
        f"{'-' * w}  {'-' * 16} {'-' * 10}  {'-' * 26}  {'-' * 30}",
    ]
    for r in sorted(results, key=lambda x: (x.verdict(), x.rule.id)):
        p01, p99 = r.healthy.percentile(1), r.healthy.percentile(99)
        band = (f"{_fmt(p01)} .. {_fmt(p99)}"
                if p01 is not None else "(no samples)")
        detects = ", ".join(
            f"{f.replace('_', '-')}: {rate:+.0%}"
            for f, rate in sorted(r.detects.items())
        ) or "—"
        lines.append(
            f"{r.rule.id.ljust(w)}  {r.verdict():<16} "
            f"{r.false_positive_rate:>9.1%}  {band:>26}  {detects}"
        )
    lines.append("")
    noisy = sum(1 for r in results if r.false_positive_rate > 0)
    blind = sum(1 for r in results if r.misses_claimed_faults)
    print_lines = lines
    print_lines.append(
        f"{len(results)} rules: {noisy} fire on a healthy plant, "
        f"{blind} miss a fault they claim"
    )
    return "\n".join(print_lines)


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.4g}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alarms.tune",
        description="Measure each rule's separation between health and its fault.",
    )
    parser.add_argument("--rule", nargs="*", help="a subset of rule ids")
    parser.add_argument("--hours", type=float, default=HOURS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(message)s",
    )

    rule_set = all_rules()
    if args.rule:
        wanted = set(args.rule)
        rule_set = tuple(r for r in rule_set if r.id in wanted)
        if not rule_set:
            log.error("no rules match %s", sorted(wanted))
            return 2

    results = tune(rule_set, hours=args.hours)
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2, default=str))
    else:
        print()
        print(render(results))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
