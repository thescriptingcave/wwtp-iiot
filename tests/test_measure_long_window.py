"""`measure_long_window` must not leak the future, and must measure what 03 measures.

**Why this needs a test.** The script's job is to settle whether notebook 03's
conclusion survives 25 weeks of data. Two ways it could quietly fail to do that:

1. **Leakage.** If `base_24` included the hour it describes, the 25-week run would
   report a large effect, and the natural reading would be "more data fixes it" --
   which would be false, and would send a room off to build a 6 GB dataset for
   nothing. `shift(1)` is the entire defence, and it is one character.
2. **Measuring a different quantity.** Notebook 03 reports 42x from naive-vs-baseline.
   Folding the forward window into that ratio gives 49x, because it swings the full
   0.000 to 1.000. Both are "the split noise is bigger than the effect"; only one
   is the number in the notebook, and a script that prints 49x where the notebook
   says 42x invites a reader to think one of them is a typo.

Neither is visible by reading the output. Both are one edit away.
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest
from workshops.ml import measure_long_window as subject


def a_panel(hours: int = 60, signals: int = 3) -> pd.DataFrame:
    """A small panel with a per-signal row count that varies by hour of day."""
    rows = []
    for signal in range(signals):
        for hour in range(hours):
            count = hour % 7
            rows.append(
                {
                    "signal_id": f"S{signal}",
                    "bucket": pd.Timestamp("2026-09-22", tz="UTC")
                    + pd.Timedelta(hours=hour),
                    "n": count,
                    "mean": float(count),
                    "min": float(count),
                    "max": float(count),
                    "row_written": int(count > 0),
                    "value_is_null": int(count == 0),
                    "is_fault": int(hour % 11 == 0),
                    "fault_type": "sensor_dead" if hour % 11 == 0 else None,
                    "week": 0,
                }
            )
    return pd.DataFrame(rows)


class TestNoFutureLeakage:
    """`base_24` at hour t must be a function of hours before t only."""

    def test_changing_an_hour_does_not_change_its_own_baseline(self) -> None:
        """The direct test: perturb hour t, and `base_24` at hour t must not move.

        Everything else in the frame stays fixed, so any movement is the window
        reading its own hour.
        """
        original = subject.causal_features(a_panel())

        bumped = a_panel()
        bumped.loc[bumped["bucket"] == bumped["bucket"].min(), "n"] = 999
        bumped.loc[bumped["bucket"] == bumped["bucket"].min(), "mean"] = 999.0
        after = subject.causal_features(bumped)

        first_hour = original["bucket"] == original["bucket"].min()
        assert original.loc[first_hour, "base_24"].equals(
            after.loc[first_hour, "base_24"]
        ), (
            "changing hour t's own row count changed base_24 at hour t, so the "
            "window includes the hour it describes. That is target encoding: it "
            "inflates the score, and on the 25-week panel it would manufacture the "
            "effect notebook 03 says is unmeasurable."
        )

    def test_the_baseline_is_shifted_by_one_not_zero(self) -> None:
        """The mechanism, read off the source rather than inferred from output."""
        source = inspect.getsource(subject.causal_features)
        assert "shift(1).rolling" in source, (
            "the trailing window no longer uses shift(1). See "
            "test_changing_an_hour_does_not_change_its_own_baseline for why that "
            "one character is the whole defence."
        )

    def test_the_forward_window_is_the_only_thing_that_reads_ahead(self) -> None:
        """It must exist -- notebook 04 is about it -- and be named as the cheat."""
        source = inspect.getsource(subject.causal_features)
        assert "shift(-1).rolling" in source, (
            "the forward window is gone. Notebook 04 exists to show that the "
            "strongest feature on the panel cannot ship; a measurement that drops "
            "it cannot be used to check that lesson."
        )
        assert "base_24_fwd" in subject.FORWARD, (
            "base_24_fwd is not listed in FORWARD, so the unshippable feature is "
            "no longer reported as a separate column and would be compared like a "
            "legitimate one."
        )


class TestItMeasuresWhatNotebook03Measures:
    def test_the_ratio_compares_naive_against_the_baseline(self) -> None:
        """Not against the forward window, which would give 49x instead of 42x."""
        source = inspect.getsource(subject.report)
        body = source[source.index("difference =") : source.index("forward_spread =")]
        assert "forward_scores" not in body, (
            "the split-noise ratio folds the forward window into the numerator. "
            "The forward window's F1 swings the full 0.000 to 1.000, so including "
            "it inflates the ratio (49x against notebook 03's 42x) and changes "
            "which quantity is being reported."
        )
        assert "naive_scores" in body and "added_scores" in body, (
            "the ratio no longer compares the naive features against the per-signal "
            "baseline, so it is no longer notebook 03's ratio at all."
        )

    def test_the_feature_sets_match_the_notebook(self) -> None:
        """A drifted copy of 03's feature list measures a different experiment."""
        assert subject.NAIVE == [
            "mean",
            "min",
            "max",
            "n",
            "row_written",
            "value_is_null",
        ], "the naive feature set drifted from notebook 03's six columns"
        assert subject.ADDED == ["base_24", "n_over_base", "streak"], (
            "the per-signal baseline drifted from notebook 03's three columns"
        )

    def test_the_seeds_are_enough_to_estimate_the_spread(self) -> None:
        """20 folds is what notebook 03 reports; a smaller number is a different
        measurement, and the spread would shrink toward zero for the wrong reason."""
        assert subject.SEEDS >= 20, (
            f"only {subject.SEEDS} folds. Notebook 03's 42x comes from 20, and a "
            "narrower sweep understates the spread and would make a weak "
            "experiment look powered."
        )


class TestTheThresholdsArePinned:
    """The bands are numbers somebody chose; the test must not inherit them.

    Moving `NO_POWER_ABOVE` from 10.0 to 20.0 passes a test written as
    `verdict(NO_POWER_ABOVE) == "NO POWER"` -- the constant moves, the assertion
    moves with it, and nothing goes red. That was caught by mutation testing this
    file, which is the only reason it is written. So the values are pinned here,
    in digits, against the docstring's reasoning.
    """

    def test_power_threshold(self) -> None:
        assert subject.POWER_BELOW == 2.0, (
            "the power threshold moved. 2x is the point at which a difference "
            "between two feature sets is larger than the spread from re-splitting "
            "one of them."
        )

    def test_no_power_threshold(self) -> None:
        assert subject.NO_POWER_ABOVE == 10.0, (
            "the no-power threshold moved. At 10x the split explains an order of "
            "magnitude more variance than the effect, which is what notebook 03's "
            "42x means and what the docs quote."
        )


class TestTheVerdictThresholdIsStated:
    """The verdict words are a claim, and 2 and 10 are where it changes."""

    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [
            (1.0, "POWER"),
            (subject.POWER_BELOW, "BORDERLINE"),
            (5.0, "BORDERLINE"),
            (subject.NO_POWER_ABOVE, "NO POWER"),
            (42.0, "NO POWER"),
        ],
    )
    def test_bands(self, ratio: float, expected: str) -> None:
        assert subject.verdict(ratio).startswith(expected)

    def test_the_side_by_side_uses_the_same_thresholds(self) -> None:
        """Two copies of "10" drift apart, and one of them decides the conclusion."""
        source = inspect.getsource(subject.main)
        assert "NO_POWER_ABOVE" in source, (
            "main() compares against a bare 10 instead of NO_POWER_ABOVE, so the "
            "narrative under the table can disagree with the verdict above it."
        )
        assert "> 10" not in source and "< 10" not in source, (
            "main() still hardcodes 10 rather than reading NO_POWER_ABOVE."
        )
