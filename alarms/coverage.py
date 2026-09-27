"""Audit the alarm system against the fault library.

Every rule in `alarms/rules.py` names the faults it claims to catch, and
`contracts/fault-scenarios.yaml` names, per fault, how the fault *is* detectable
and how it is **not**. This module puts the two side by side and reports where
they disagree.

That is the thing an alarm system almost never has and almost always needs.

## Why this is worth building

A rule set is a set of claims. `aeration_do_sagging` claims to catch
`blower_failure`. Nothing in normal operation checks that claim: the rule fires
when it fires, and the fact that it has never fired in a week of healthy data is
either reassuring or meaningless depending on whether a blower ever tripped.

The seeder can answer it. It replays the plant model through each of the eleven
faults in the contract, so "did this rule fire?" is a question about a database
that already exists. The output is a matrix:

```
                                    storm blower  sensor  effluent   blind
                                    _inflow failure  flatline  tss      spots
  aeration_blower_speed_drop           .      YES     .         .         0
  aeration_do_sagging                  .      YES     .         .         0
  ...
  faults with no rule                 0      0        0         1         1
```

Three numbers at the bottom, and they are the ones that matter:

* **blind spots** — faults no rule catches. Each one is a scenario the plant can
  be in and nobody will be told.
* **unused rules** — rules that never fire in any scenario. Dead configuration,
  and dead configuration in an alarm system is a liability: it is one more thing
  an operator has been trained to ignore.
* **contract disagreements** — the contract says a fault is *not* detectable by
  method X, and a rule using X fires on it anyway. That means either the contract
  is wrong or the rule is, and this project should not have to guess which.

## What it cannot do

It runs against **seeded** data, which means simulated faults with known
parameters. It says nothing about a real plant, real fouling, or real sensor
failure modes. A green matrix here is evidence that the rules are wired to the
faults they claim, and it is not evidence that the rules would help anybody.
Those are different claims and the difference is the whole reason this project is
a simulation.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from alarms.engine import AlarmEngine, Transition

log = logging.getLogger("alarms.coverage")

#: Where the fault library lives, and the key it is under.
FAULTS_PATH = "contracts/fault-scenarios.yaml"


@dataclass
class FaultCoverage:
    """What the rules did, and did not, do for one fault."""

    fault_id: str
    kind: str
    title: str
    #: Whether the fault was actually run. **This is not a detail.**
    #:
    #: The first version of this report had no such field, and asked for three
    #: faults out of eleven reported *"8 of 11 blind spots"* — because the other
    #: eight had no transitions simply by never having been simulated. A fault
    #: that was not tested is an *unknown*, and reporting it as a missed
    #: detection is worse than reporting nothing: it invites a rule to be deleted
    #: on the strength of a measurement that was never taken.
    evaluated: bool = False
    #: Rules that named this fault in `detects`, and whether they fired.
    claimed: dict[str, bool] = field(default_factory=dict)
    #: Rules that fired during the fault without having claimed it. Not a bug —
    #: an incidental catch, and worth knowing about.
    incidental: list[str] = field(default_factory=list)
    contract_detectable_by: list[str] = field(default_factory=list)
    contract_not_detectable_by: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return any(self.claimed.values())

    @property
    def earliest_detection_s(self) -> float | None:
        """Seconds from the fault starting to the first claimed rule raising.

        The number an operator would call "how much warning did I get", and the
        one this whole project exists to make small.
        """
        return self._earliest

    _earliest: float | None = None

    @property
    def blind_spot(self) -> bool:
        """Run, and no rule that *claims* this fault fired.

        The first version treated any incidental catch as "not a blind spot",
        because a rule firing at all felt like evidence. It is not, and the
        coverage run showed why: `sludge_blanket_thickening` and `lift_pump_failure`
        were each "found" by six rules that were not looking for them. Retune
        those six — and two of them demonstrably needed retuning during this
        phase — and the fault is invisible again.

        A coincidence standing in front of a blind spot is still a blind spot, and
        the honest report says so. Incidental catches are real information and are
        reported as such, under `incidental`.
        """
        return self.evaluated and not self.detected

    @property
    def untested(self) -> bool:
        return not self.evaluated

    @property
    def incidentally_caught(self) -> bool:
        """Run, unclaimed, and *something* fired.

        The state that is easiest to mistake for working. Worth its own name
        because "caught by six rules" and "caught by the rule that says it will
        be" are different claims and only one of them survives a retune.
        """
        return (self.evaluated and not self.detected
                and bool(self.incidental))


@dataclass
class CoverageReport:
    """The matrix, plus the three numbers that are worth reading."""

    rows: list[FaultCoverage]
    unused_rules: list[str]
    disagreements: list[str]
    evaluated_transitions: int = 0

    @property
    def blind_spots(self) -> list[str]:
        """Faults no claiming rule found, whether or not something else fired."""
        return [r.fault_id for r in self.rows if r.blind_spot]

    @property
    def luck(self) -> list[str]:
        return [r.fault_id for r in self.rows if r.incidentally_caught]

    @property
    def detected_count(self) -> int:
        return sum(1 for r in self.rows if r.detected)

    @property
    def total_faults(self) -> int:
        return len(self.rows)

    @property
    def evaluated_faults(self) -> int:
        return sum(1 for r in self.rows if r.evaluated)

    @property
    def untested(self) -> list[str]:
        return [r.fault_id for r in self.rows if r.untested]

    def as_dict(self) -> dict[str, Any]:
        return {
            "faults": self.total_faults,
            "evaluated": self.evaluated_faults,
            "detected": self.detected_count,
            "blind_spots": self.blind_spots,
            "incidentally_caught": self.luck,
            "untested": self.untested,
            "unused_rules": self.unused_rules,
            "contract_disagreements": self.disagreements,
            "matrix": [
                {
                    "fault": r.fault_id,
                    "kind": r.kind,
                    "detected": r.detected,
                    "blind_spot": r.blind_spot,
                    "claimed_by": dict(r.claimed),
                    "incidental": r.incidental,
                    "not_detectable_by": r.contract_not_detectable_by,
                }
                for r in self.rows
            ],
        }

    def render(self) -> str:
        """A table, for a terminal. Widths are computed rather than guessed."""
        name_w = max(
            [len(r.fault_id) for r in self.rows]
            + [len("fault / rule"), len("blind spots")]
        )
        rule_w = max(
            [len(x) for x in self._rule_names()] + [len("rule")]
        )
        lines = [
            f"{'fault'.ljust(name_w)}  {'detected':>9}  {'by rule':<{rule_w}}  "
            f"contract says NOT:",
            f"{'-' * name_w}  {'-' * 9}  {'-' * rule_w}  {'-' * 17}",
        ]
        for r in self.rows:
            who = ", ".join(
                rid.replace("aeration_", "").replace("influent_", "")
                for rid, fired in r.claimed.items() if fired
            ) or "—"
            note = ", ".join(r.contract_not_detectable_by) or "—"
            verdict = (
                "YES" if r.detected
                else ("by luck" if r.incidentally_caught
                      else ("no" if r.evaluated else "untested"))
            )
            lines.append(
                f"{r.fault_id.ljust(name_w)}  "
                f"{verdict.rjust(9)}  "
                f"{who:<{rule_w}}  {note}"
            )
        lines.append("")
        lines.append(
            f"{self.detected_count}/{self.evaluated_faults} of the "
            f"{self.total_faults} faults detected "
            f"({self.total_faults - self.evaluated_faults} never run)"
        )
        lines.append(
            f"blind spots: {', '.join(self.blind_spots) or 'none'}"
        )
        if self.luck:
            lines.append(
                "caught only incidentally (not a claim, and it does not survive "
                f"a retune): {', '.join(self.luck)}"
            )
        if self.untested:
            lines.append(f"never run: {', '.join(self.untested)}")
        lines.append(
            f"rules that never fired: {', '.join(self.unused_rules) or 'none'}"
        )
        if self.disagreements:
            lines.append("contract disagreements:")
            lines.extend(f"  {d}" for d in self.disagreements)
        return "\n".join(lines)

    def _rule_names(self) -> list[str]:
        return [rid for r in self.rows for rid in r.claimed]


def load_faults(path: str = FAULTS_PATH) -> list[dict[str, Any]]:
    """The fault library, straight from the contract.

    Read as YAML and not imported through `softplc.faults`, because this module
    is about the *declaration* — what the contract claims — and the fault engine
    is about the *simulation*. They are deliberately allowed to disagree, and
    `disagreements` is where that shows up.
    """
    with Path(path).open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return list(raw.get("faults", []))


def _earliest_raise(
    transitions: Sequence[Transition], rule_ids: set[str],
) -> float | None:
    raised = [
        t.at for t in transitions
        if t.kind == "raised" and t.rule.id in rule_ids
    ]
    return min(raised) if raised else None


def audit(
    engine: AlarmEngine,
    transitions_by_fault: dict[str, list[Transition]],
    faults_path: str = FAULTS_PATH,
) -> CoverageReport:
    """Cross-check rules against the contract's claims.

    `transitions_by_fault` maps a fault id to every transition the engine
    produced while that fault was active. The caller runs the engine against
    seeded scenarios; this function only compares, which keeps it testable with
    a hand-built set of transitions and no simulation at all.
    """
    faults = load_faults(faults_path)
    rule_index = {r.id: r for r in engine.rules}

    rows: list[FaultCoverage] = []
    unused = {r.id for r in engine.rules}
    disagreements: list[str] = []

    for fault in faults:
        fid = fault["id"]
        expects = fault.get("expects", {}) or {}
        row = FaultCoverage(
            fault_id=fid,
            kind=fault.get("kind", "?"),
            title=fault.get("title", ""),
            evaluated=fid in transitions_by_fault,
            contract_detectable_by=list(expects.get("detectable_by", [])),
            contract_not_detectable_by=list(
                expects.get("NOT_detectable_by", [])
            ),
        )
        transitions = transitions_by_fault.get(fid, [])

        for rid, rule in rule_index.items():
            if fid in rule.detects:
                fired = any(
                    t.rule.id == rid and t.kind == "raised"
                    for t in transitions
                )
                row.claimed[rid] = fired
                if fired:
                    unused.discard(rid)

        for rid in {t.rule.id for t in transitions if t.kind == "raised"}:
            if rid not in row.claimed:
                row.incidental.append(rid)
            # A rule that fired is a used rule, whether or not it claimed this
            # fault. Counting "never fired" over the claimed set only would have
            # reported two rules as dead configuration when they had in fact
            # raised an alarm in every run.
            if any(t.rule.id == rid and t.kind == "raised" for t in transitions):
                unused.discard(rid)

        # A rule using a method the contract says cannot find this fault, and
        # which found it anyway, means one of the two is wrong. Say which, and
        # do not guess.
        for rid in row.claimed:
            if not row.claimed[rid]:
                continue
            method = rule_index[rid].detector
            if method in row.contract_not_detectable_by:
                disagreements.append(
                    f"{fid}: rule {rid} uses {method!r}, which the contract "
                    "lists under NOT_detectable_by, and it fired. Either the "
                    "contract is wrong or the rule is."
                )
        rows.append(row)

    return CoverageReport(
        rows=rows,
        unused_rules=sorted(unused),
        disagreements=disagreements,
        evaluated_transitions=sum(len(v) for v in transitions_by_fault.values()),
    )
