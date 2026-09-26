"""Deadband: deciding what is worth writing.

A plant publishes 57 signals at up to 50 scans a second. Writing all of them
every scan is 2 850 points a second, and almost all of them are the same number
as last time. That is not just wasteful, it is actively harmful: a historian
full of repeated values answers a trend question more slowly, costs more to
store, and makes change detection harder because the signal-to-noise ratio of the
data collapses.

A deadband solves it by asking a different question than "what is the value?" —
it asks "is the value *different enough* to be news?"

## Which kind of deadband

There are two, and they answer different questions.

**Absolute** — publish when ``|new - last| > threshold``. Simple, predictable,
and exactly right for a measurement with a meaningful resolution: a pH probe
reading 7.01 when it last read 7.00 has not become interesting.

**Relative (percentage of span)** — publish when the *change* is a significant
fraction of the *range*. Right for a signal whose useful resolution scales with
its magnitude, and wrong for one that does not: a flow that idles at 2 m³/h in a
plant that runs at 2 000 would need 20 m³/h of change to trigger a 1% band,
which is 1 000% of the current reading.

So the contract carries, per signal, a deadband and a mode. The default is
absolute with a zero threshold, which means "publish every change" — correct,
and a decision to revisit rather than a default to be proud of.

## Why it also tracks quality

A deadband on the *value* alone will happily skip publishing a value whose
*quality* has degraded, because the number did not move. A DO probe that starts
reading Uncertain at the same 2.03 mg/L it has read for an hour produces no
change, and therefore no notification that the instrument is now untrustworthy.

This is the bug that matters, and it is why :meth:`Deadband.accept` takes the
quality and treats it as independently newsworthy. Losing the validity of an
instrument is an event, not a value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from softplc.contract import Contract, Signal


class BandMode(StrEnum):
    """How a threshold is interpreted."""

    #: Publish when the absolute change exceeds the threshold. Units of the
    #: signal. The default, and correct for most measurements.
    ABSOLUTE = "absolute"

    #: Publish when the change exceeds ``threshold`` percent of the signal's
    #: engineering span. Correct where useful resolution scales with magnitude.
    RELATIVE = "relative"

    #: Publish every scan, no matter what. For state changes and counters, where
    #: "unchanged" is itself the information.
    ALWAYS = "always"


@dataclass(slots=True)
class BandRule:
    """One signal's deadband policy."""

    mode: BandMode = BandMode.ABSOLUTE
    threshold: float = 0.0
    #: Only used in ``RELATIVE`` mode: the engineering range from the contract.
    span: float = 0.0

    @classmethod
    def for_signal(cls, signal: Signal, default: float = 0.0) -> BandRule:
        """Build a rule from a contract signal, falling back to ``default``."""
        raw = getattr(signal, "deadband", None)
        mode_raw = str(getattr(signal, "deadband_mode", "absolute") or "absolute")
        try:
            mode = BandMode(mode_raw)
        except ValueError:
            # A contract with a typo in the mode must not take the gateway down.
            # An unknown mode is treated as the safe, chatty one: writing too
            # much is recoverable, writing too little loses data silently.
            mode = BandMode.ABSOLUTE
        threshold = float(raw) if raw is not None else float(default)
        # ``range_min``/``range_max``, not ``min``/``max``. Guessing the field
        # names with getattr() returned None for every signal, so every span was
        # 0.0 and relative mode was silently dead across the entire contract —
        # a filter that does nothing, in a component whose whole job is to
        # filter, and no error anywhere to say so.
        span = float(signal.range_span)
        return cls(mode=mode, threshold=threshold, span=span)


@dataclass(slots=True)
class _Entry:
    value: float = math.nan
    quality: int = -1
    seen: bool = False


@dataclass(slots=True)
class Deadband:
    """Stateful filter deciding which value changes are worth publishing.

    One instance per gateway process. It is *not* thread-safe, deliberately:
    the scan loop is single-threaded and a lock here would be a claim that this
    is contended when it is not.
    """

    rules: dict[str, BandRule]
    _last: dict[str, _Entry]
    _counts: dict[str, int]
    _offered: dict[str, int]

    def __init__(self, rules: dict[str, BandRule] | None = None) -> None:
        self.rules = dict(rules or {})
        self._last = {}
        #: Published and suppressed tallies, per signal. Kept honestly: a
        #: deadband that reports "1 published" for everything looks like it is
        #: filtering when it may not be.
        self._counts = {}
        self._offered = {}

    # ─── policy ──────────────────────────────────────────────────────────────

    def rule_for(self, signal_id: str) -> BandRule:
        return self.rules.get(signal_id, BandRule())

    def set_rule(self, signal_id: str, rule: BandRule) -> None:
        self.rules[signal_id] = rule
        # Changing a policy invalidates the comparison baseline: the new rule
        # needs the last value to compare against, and keeping a baseline that
        # was accumulated under the old policy is how a threshold change
        # silently swallows the next hour of data.
        self._last.pop(signal_id, None)

    @classmethod
    def from_contract(cls, contract: Contract, default: float = 0.0) -> Deadband:
        """Build from every signal in the contract."""
        return cls({
            sid: BandRule.for_signal(sig, default=default)
            for sid, sig in contract.signals.items()
        })

    # ─── the decision ────────────────────────────────────────────────────────

    def accept(self, signal_id: str, value: float, quality: int = 0) -> bool:
        """Decide whether this reading is worth publishing.

        Returns ``True`` and updates the baseline when the reading is newsworthy.
        Returns ``False`` and leaves the baseline alone when it is not — so a
        suppressed reading never becomes the reference for the next comparison.

        That last point is the one that is easy to get wrong. If a suppressed
        reading *did* update the baseline, a signal drifting slowly and steadily
        would be compared against itself and never publish at all.
        """
        rule = self.rule_for(signal_id)
        self._offered[signal_id] = self._offered.get(signal_id, 0) + 1

        # A quality change is always news. The number may be identical.
        prev = self._last.get(signal_id)
        if prev is not None and prev.seen and prev.quality != quality:
            self._remember(signal_id, value, quality)
            self._counts[signal_id] = self._counts.get(signal_id, 0) + 1
            return True

        if rule.mode is BandMode.ALWAYS:
            self._remember(signal_id, value, quality)
            self._counts[signal_id] = self._counts.get(signal_id, 0) + 1
            return True

        # The first reading of a signal is always published. There is nothing to
        # compare it against, and a historian whose first hour of a tag is empty
        # is a historian that cannot answer "when did this start?".
        if prev is None or not prev.seen:
            self._remember(signal_id, value, quality)
            self._counts[signal_id] = self._counts.get(signal_id, 0) + 1
            return True

        if not self._exceeds(rule, prev.value, value):
            return False

        self._remember(signal_id, value, quality)
        self._counts[signal_id] = self._counts.get(signal_id, 0) + 1
        return True

    @staticmethod
    def _exceeds(rule: BandRule, old: float, new: float) -> bool:
        # A NaN comparison is always False, so a NaN would suppress a reading
        # forever and the signal would simply vanish from the historian. Treat
        # non-finite values as newsworthy and let the storage layer decide what
        # to do with them — losing the fact that a sensor started returning NaN
        # is strictly worse than recording it.
        if not (math.isfinite(old) and math.isfinite(new)):
            return True

        if rule.mode is BandMode.RELATIVE and rule.span > 0.0:
            # Relative to the span, not to the current value. Dividing by the
            # value would make a deadband that tightens as the signal falls,
            # which is the opposite of what anyone wants.
            return abs(new - old) > (rule.threshold / 100.0) * rule.span
        return abs(new - old) > rule.threshold

    def _remember(self, signal_id: str, value: float, quality: int) -> None:
        self._last[signal_id] = _Entry(value=value, quality=quality, seen=True)

    # ─── introspection ───────────────────────────────────────────────────────

    def accepted_since_start(self) -> dict[str, int]:
        """Readings actually published, per signal."""
        return dict(self._counts)

    def offered_since_start(self) -> dict[str, int]:
        """Readings presented to the deadband, per signal.

        The pair ``(offered, accepted)`` is the only honest measure of whether a
        deadband is doing anything. A threshold of 0.0 on a settled signal
        accepts everything, and that is a *finding*, not a configuration success.
        """
        return dict(self._offered)

    def suppression_ratio(self, signal_id: str) -> float:
        """Fraction of readings withheld, in ``0.0 .. 1.0``."""
        offered = self._offered.get(signal_id, 0)
        if offered == 0:
            return 0.0
        return 1.0 - (self._counts.get(signal_id, 0) / offered)

    def baseline(self, signal_id: str) -> _Entry | None:
        return self._last.get(signal_id)

    def reset(self) -> None:
        self._last.clear()
        self._counts.clear()
        self._offered.clear()
