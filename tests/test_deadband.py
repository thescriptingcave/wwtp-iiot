"""Tests for the deadband.

Most of these exist because of a specific way a deadband goes wrong. Each test
names the failure it prevents, because a test named ``test_deadband_2`` tells
the next reader nothing about why the code is shaped the way it is.
"""

from __future__ import annotations

import math

import pytest
from gateway.deadband import BandMode, BandRule, Deadband
from softplc.contract import contract

C = contract()


# ─── the obvious case ─────────────────────────────────────────────────────────


def test_a_settled_signal_publishes_nothing() -> None:
    db = Deadband({s: BandRule(BandMode.ABSOLUTE, 0.5) for s in C.signals})
    assert db.accept("AERATION:AHU-1:DO", 2.00)
    for _ in range(50):
        assert not db.accept("AERATION:AHU-1:DO", 2.00)
    assert db.accepted_since_start()["AERATION:AHU-1:DO"] == 1
    assert db.suppression_ratio("AERATION:AHU-1:DO") == pytest.approx(50 / 51)


def test_a_change_beyond_the_band_publishes() -> None:
    db = Deadband({"AERATION:AHU-1:DO": BandRule(BandMode.ABSOLUTE, 0.5)})
    assert db.accept("AERATION:AHU-1:DO", 2.00)
    assert not db.accept("AERATION:AHU-1:DO", 2.40)
    assert db.accept("AERATION:AHU-1:DO", 2.60)


def test_the_band_is_exclusive_at_the_boundary() -> None:
    """Exactly at the threshold is not beyond it. Documented so the choice is
    visible: a reader who assumes ``>=`` will one day find an extra point."""
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 0.5)})
    assert db.accept("X", 1.0)
    assert not db.accept("X", 1.5)
    assert db.accept("X", 1.5001)


# ─── the bug this is really about ─────────────────────────────────────────────


def test_a_quality_change_publishes_even_when_the_value_does_not_move() -> None:
    """A probe going Uncertain at the value it has read all hour is an event.

    Filtering on the value alone would publish nothing, and the historian would
    carry on showing a confident number from an instrument that had already told
    us it no longer knows.
    """
    db = Deadband({"AERATION:AHU-1:DO": BandRule(BandMode.ABSOLUTE, 5.0)})
    assert db.accept("AERATION:AHU-1:DO", 2.00, quality=0)
    for _ in range(100):
        assert not db.accept("AERATION:AHU-1:DO", 2.00, quality=0)
    assert db.accept("AERATION:AHU-1:DO", 2.00, quality=1), (
        "degrading an instrument must be published"
    )
    # And it must not spam: the next Good reading is also news, but the hundred
    # after that are not.
    assert db.accept("AERATION:AHU-1:DO", 2.00, quality=0)
    assert not db.accept("AERATION:AHU-1:DO", 2.00, quality=0)


def test_a_suppressed_reading_does_not_become_the_baseline() -> None:
    """The failure mode that silently kills a signal forever.

    The baseline is the last *published* value, not the last *seen* one. If a
    withheld reading became the baseline, each step would be compared against
    its own immediate predecessor and a signal moving steadily at 0.6 with a
    band of 1.0 would never publish — at 0.6 per scan or per hour. It would
    vanish from the historian while the plant carried on reporting it.

    Note what is *not* being asserted: the cumulative distance travelled. It is
    irrelevant, because the previous point is already stored. A deadband
    guarantees the historian never has a gap wider than the band; it does not
    guarantee anything about total movement.
    """
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 1.0)})
    assert db.accept("X", 0.0)
    assert not db.accept("X", 0.6)      # 0.6 from the baseline
    assert db.accept("X", 1.2), "1.2 from the last *published* value must publish"


def test_a_slow_ramp_eventually_publishes() -> None:
    """The same property as above, as a trend rather than a single step."""
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 0.1)})
    published = [db.accept("X", i * 0.01) for i in range(200)]
    assert sum(published) > 1, "a monotonic drift must not be filtered to nothing"
    # The most recent point must be recent. A deadband that published once at the
    # start and then nothing would leave a historian whose last reading is hours
    # old while the value is quietly still moving.
    assert max(i for i, ok in enumerate(published) if ok) >= 190


# ─── first reading, always mode, non-finite ───────────────────────────────────


def test_the_first_reading_always_publishes() -> None:
    """There is nothing to compare against, and a historian that starts an hour
    late cannot answer "when did this begin?"."""
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 1000.0)})
    assert db.accept("X", 0.0)


def test_always_mode_publishes_everything() -> None:
    """For state and counters, 'unchanged' is the information."""
    db = Deadband({"X": BandRule(BandMode.ALWAYS)})
    assert all(db.accept("X", 5.0) for _ in range(10))


def test_nan_publishes_and_does_not_stall_the_signal() -> None:
    """A NaN comparison is always False, so a naive filter suppresses NaN
    forever and the tag disappears. Recording the fault beats losing it."""
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 1.0)})
    assert db.accept("X", 1.0)
    assert db.accept("X", math.nan)
    assert db.accept("X", math.nan)
    assert db.accept("X", 1.0), "recovery from NaN must publish"


def test_infinity_is_also_news() -> None:
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 1.0)})
    db.accept("X", 1.0)
    assert db.accept("X", math.inf)


# ─── relative mode ────────────────────────────────────────────────────────────


def test_relative_mode_uses_the_span_not_the_value() -> None:
    """A relative band measured against the current value tightens as the signal
    falls, which is backwards. A flow idling at 2 m³/h with a 1% band would need
    20 m³/h of change to trigger."""
    db = Deadband({"X": BandRule(BandMode.RELATIVE, 1.0, span=2000.0)})
    assert db.accept("X", 2.0)
    # 1% of a 2000 span is 20 — reached by a change of 20 from a value of 2.
    assert not db.accept("X", 2.0 + 19.0)
    assert db.accept("X", 2.0 + 21.0)


def test_relative_mode_falls_back_to_absolute_without_a_span() -> None:
    db = Deadband({"X": BandRule(BandMode.RELATIVE, 1.0, span=0.0)})
    assert db.accept("X", 100.0)
    assert not db.accept("X", 100.5)
    assert db.accept("X", 102.0)


# ─── rules from the contract ──────────────────────────────────────────────────


def test_rules_come_from_the_contract() -> None:
    db = Deadband.from_contract(C)
    assert set(db.rules) == set(C.signals)
    for rule in db.rules.values():
        assert rule.span >= 0.0


def test_an_unknown_mode_degrades_to_the_chatty_one() -> None:
    """A typo in the contract must not take the gateway down, and must not
    silently start dropping data either. Writing too much is recoverable."""
    sig = C.signals["AERATION:AHU-1:DO"]
    object.__setattr__(sig, "deadband_mode", "relitive")
    try:
        assert BandRule.for_signal(sig).mode is BandMode.ABSOLUTE
    finally:
        object.__setattr__(sig, "deadband_mode", "absolute")


def test_changing_a_rule_forgets_the_baseline() -> None:
    """Otherwise a threshold change silently swallows data until the new band is
    exceeded relative to a value accumulated under the old one."""
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 0.1)})
    db.accept("X", 10.0)
    assert db.baseline("X") is not None
    db.set_rule("X", BandRule(BandMode.ABSOLUTE, 0.1))
    assert db.baseline("X") is None
    assert db.accept("X", 10.0), "the first reading under a new rule publishes"


# ─── the counters are honest ──────────────────────────────────────────────────


def test_offered_and_accepted_are_both_counted() -> None:
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 1.0)})
    for i in range(10):
        db.accept("X", float(i))
    assert db.offered_since_start()["X"] == 10
    # 0, 1 … 9 with a band of 1.0. The baseline is the last *published* value, so
    # 0 publishes (first reading), 1 is exactly at the band and does not, 2 is
    # 2.0 beyond 0 and does, 3 is 1.0 beyond 2 and does not, and so on: five of
    # ten. Pinned because it depends on two decisions that are individually
    # invisible — the boundary is exclusive, and the baseline is the last
    # published value rather than the last seen one.
    assert db.accepted_since_start()["X"] == 5
    assert db.suppression_ratio("X") == pytest.approx(0.5)
    assert db.suppression_ratio("NEVER_SEEN") == 0.0


def test_a_zero_threshold_suppresses_only_exact_repeats() -> None:
    """A deadband of 0.0 does not mean "publish everything" — it means "publish
    any change at all", which still drops a signal that is sitting still.

    Worth being explicit about, because it is the difference between a threshold
    of 0.0 being a safe default and being an expensive one. A settled DO reading
    costs nothing to skip; a settled reading that nobody skips is a point per
    scan per signal forever.
    """
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 0.0)})
    assert db.accept("X", 1.0)
    for _ in range(4):
        assert not db.accept("X", 1.0)
    assert db.accept("X", 1.0 + 1e-9)
    # 2 published of 6 offered. Counted explicitly because "roughly 80%"
    # is not a number anyone can check.
    assert db.suppression_ratio("X") == pytest.approx(2 / 3)


def test_always_mode_is_what_actually_publishes_everything() -> None:
    """If you want no filtering at all, ask for it, rather than inferring it from
    a zero threshold that only looks like no filtering."""
    db = Deadband({"X": BandRule(BandMode.ALWAYS)})
    for _ in range(5):
        assert db.accept("X", 1.0)
    assert db.suppression_ratio("X") == 0.0


def test_reset_clears_everything() -> None:
    db = Deadband({"X": BandRule(BandMode.ABSOLUTE, 1.0)})
    db.accept("X", 1.0)
    db.reset()
    assert db.baseline("X") is None
    assert db.accepted_since_start() == {}
    assert db.offered_since_start() == {}


def test_a_signal_with_no_rule_gets_a_default() -> None:
    """Signals arrive from the plant, not from the contract. An unknown tag must
    be published, not dropped."""
    db = Deadband()
    assert db.accept("NOT:IN:CONTRACT", 1.0)
    assert not db.accept("NOT:IN:CONTRACT", 1.0)
