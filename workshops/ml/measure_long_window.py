"""Does the workshop's conclusion survive 25 weeks of data?

    uv run --extra workshop python -m workshops.ml.measure_long_window

**The question.** Notebook 03 concludes that the per-signal baseline is the right
feature and that *this panel cannot show you it works*: 22 positive hours, about 4 in
a test fold, and a split-to-split spread 42x the difference between two feature
sets. That conclusion is a claim about the sample, not about the feature, and the
stated remedy is more faults -- 25 weeks, a fault every 36 hours, which is 116
instances instead of 22.

This module runs the same experiment on both panels and prints the comparison, so
the remedy is either confirmed or refuted rather than asserted. It reads whichever
panel `WORKSHOP_PANEL` names, twice: once on the 3-week default and once on the
25-week panel from `make workshop-long`.

**What counts as a result here.** Not "the F1 went up". With a few dozen positives,
the F1 goes up and down on the split alone -- that is the whole finding of notebook
03. The quantity that decides it is the ratio of the split-to-split spread *within*
one configuration to the difference *between* configurations. When that ratio drops
below about 2, the experiment has power and a difference means something; while it
is above about 10, it does not. Every other number in the table is context for that
one.

**This is a measurement, not a gate.** It prints; it does not assert. The gate that
checks the notebooks is `make workshop-notebooks`, and a measurement that fails
loudly on new data would be a gate in disguise -- a third thing that breaks when
someone improves the seeder.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score, precision_score, recall_score

SEEDS = 20

#: The six columns a participant has before reading notebook 03.
NAIVE = ["mean", "min", "max", "n", "row_written", "value_is_null"]

#: What notebook 03 tells them to add. `shift(1)` on both rolling windows: a window
#: that includes the hour it describes is the hour labelling itself.
ADDED = ["base_24", "n_over_base", "streak"]

#: What notebook 04 adds, and then refuses to ship.
FORWARD = ["base_24_fwd"]


def causal_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Add the trailing per-signal baseline, exactly as the notebooks do."""
    panel = panel.sort_values(["signal_id", "bucket"]).reset_index(drop=True)
    counts = panel.groupby("signal_id")["n"]
    panel["base_24"] = counts.transform(
        lambda s: s.shift(1).rolling(24, min_periods=6).median()
    )
    panel["n_over_base"] = panel["n"] / panel["base_24"].replace(0, np.nan)
    panel["streak"] = counts.transform(
        lambda s: s.eq(0).groupby((~s.eq(0)).cumsum()).cumsum()
    )
    panel["base_24_fwd"] = counts.transform(
        lambda s: s.shift(-1).rolling(24, min_periods=6).median()
    )
    return panel


def separation(panel: pd.DataFrame) -> tuple[float, float, float]:
    """The 29x from notebook 03: a fault hour's signal is not usually silent.

    This is arithmetic on the panel and does not depend on a split, which is why
    notebook 03 treats it as the part that survives while the score does not. If
    25 weeks changes this, the *mechanism* changed and the lesson needs rewriting
    rather than the measurement.
    """
    empty = panel[panel["n"] == 0]
    fault = empty[empty["is_fault"] == 1]["base_24"].mean()
    quiet = empty[empty["is_fault"] == 0]["base_24"].mean()
    return fault, quiet, fault / max(quiet, 1e-9)


def fold_scores(panel: pd.DataFrame, features: list[str]) -> tuple[np.ndarray, int]:
    """F1 across `SEEDS` random folds, plus the smallest test-fold positive count.

    The count is returned because it is the thing that explains the spread. A
    reader who sees "F1 ranges 0.000 to 0.857" and then "3 positives in the
    smallest fold" stops wondering whether the feature helps.
    """
    labels = panel["is_fault"].to_numpy()
    matrix = panel[features].fillna(-1).to_numpy()
    rows = np.arange(len(panel))
    cut = int(0.8 * len(panel))

    scores: list[float] = []
    fewest = len(panel)
    for seed in range(SEEDS):
        shuffled = np.random.default_rng(seed).permutation(rows)
        train, test = shuffled[:cut], shuffled[cut:]
        model = RandomForestClassifier(n_estimators=200, random_state=0, n_jobs=-1)
        model.fit(matrix[train], labels[train])
        predicted = model.predict(matrix[test])
        scores.append(f1_score(labels[test], predicted, zero_division=0))
        fewest = min(fewest, int(labels[test].sum()))
    return np.array(scores), fewest


def time_split(
    panel: pd.DataFrame, features: list[str], weeks: int
) -> tuple[float, float, float, int]:
    """The honest split: the last `weeks` calendar weeks, held out entirely."""
    order = sorted(panel["week"].unique())
    cut = order[-weeks]
    train, test = panel[panel["week"] < cut], panel[panel["week"] >= cut]
    model = RandomForestClassifier(n_estimators=300, random_state=0, n_jobs=-1)
    model.fit(train[features].fillna(-1), train["is_fault"])
    predicted = model.predict(test[features].fillna(-1))
    truth = test["is_fault"]
    return (
        recall_score(truth, predicted, zero_division=0),
        precision_score(truth, predicted, zero_division=0),
        f1_score(truth, predicted, zero_division=0),
        int(truth.sum()),
    )


#: Below this ratio the experiment has power and a difference between two feature
#: sets means something. Above :data:`NO_POWER`, it does not, whatever the F1 says.
POWER_BELOW = 2.0
NO_POWER_ABOVE = 10.0


def verdict(ratio: float) -> str:
    """Words for a split-noise ratio. The thresholds are where the answer changes.

    Factored out so a test can pin the bands rather than re-implementing them --
    a test that copies the logic it is checking checks its own copy, which is
    how a threshold drifts without anything going red.
    """
    if ratio < POWER_BELOW:
        return "POWER -- a difference would mean something"
    if ratio < NO_POWER_ABOVE:
        return "BORDERLINE"
    return "NO POWER -- the split decides the answer"


def report(path: Path) -> dict[str, float]:
    panel = causal_features(pd.read_csv(path, parse_dates=["bucket"], low_memory=False))
    positives = int(panel["is_fault"].sum())
    fault_base, quiet_base, ratio = separation(panel)

    naive_scores, naive_fewest = fold_scores(panel, NAIVE)
    added_scores, added_fewest = fold_scores(panel, [*NAIVE, *ADDED])
    forward_scores, _ = fold_scores(panel, [*NAIVE, *ADDED, *FORWARD])

    # **Naive against baseline only.** This is the ratio notebook 03 reports (42x)
    # and it is the comparison that decides whether the *per-signal baseline* can be
    # shown to help. Folding the forward window in here would inflate the numerator
    # -- it swings the full 0.000 to 1.000 -- and quietly change the quantity being
    # measured. The forward window's spread is printed separately below.
    difference = abs(added_scores.mean() - naive_scores.mean())
    widest = max(
        naive_scores.max() - naive_scores.min(),
        added_scores.max() - added_scores.min(),
    )
    ratio_spread = widest / difference if difference else float("inf")
    forward_spread = forward_scores.max() - forward_scores.min()

    naive_time = time_split(panel, NAIVE, 1)
    added_time = time_split(panel, [*NAIVE, *ADDED], 1)
    forward_time = time_split(panel, [*NAIVE, *ADDED, *FORWARD], 1)

    name = path.name
    print(f"\n{'=' * 78}\n{name}\n{'=' * 78}")
    print(
        f"  {len(panel):,} rows   {panel['signal_id'].nunique()} signals   "
        f"{panel['bucket'].nunique()} hours   {panel['week'].nunique()} weeks"
    )
    base_rate = panel["is_fault"].mean()
    print(f"  {positives} positive fault hours   base rate {base_rate:.4%}")

    print("\n  THE MECHANISM (independent of any split)")
    print(f"    a fault hour's signal logged {fault_base:>7.0f} rows in the last 24 h")
    print(f"    a quiet  hour's signal logged {quiet_base:>7.0f}")
    print(f"    ratio {ratio:>6.1f}x")

    print("\n  HELD-OUT FINAL WEEK (F1)")
    held = [
        ("naive features", naive_time),
        ("+ per-signal baseline", added_time),
        ("+ forward window (unshippable)", forward_time),
    ]
    for label, (rec, _prec, f1, n_pos) in held:
        print(f"    {label:<34}{f1:>7.3f}   ({n_pos} positives)   recall {rec:.3f}")

    print(f"\n  SPLIT SENSITIVITY, {SEEDS} random folds")
    for label, scores in (
        ("naive features", naive_scores),
        ("+ per-signal baseline", added_scores),
        ("+ forward window", forward_scores),
    ):
        print(
            f"    {label:<24}F1 {scores.min():.3f} to {scores.max():.3f}"
            f"   mean {scores.mean():.3f}"
        )
    print(
        f"    fewest positives in any fold: naive {naive_fewest}, "
        f"with baseline {added_fewest}"
    )

    print(
        f"    the forward window swings {forward_scores.min():.3f} to "
        f"{forward_scores.max():.3f}, a spread of {forward_spread:.3f}"
    )

    print("\n  THE NUMBER THAT DECIDES IT (naive vs per-signal baseline)")
    print(f"    difference between the two means : {difference:.3f}")
    print(f"    widest spread inside one of them  : {widest:.3f}")
    print(f"    ratio                            : {ratio_spread:.0f}x")
    print(f"    verdict                          : {verdict(ratio_spread)}")

    return {
        "weeks": int(panel["week"].nunique()),
        "positives": positives,
        "separation": ratio,
        "naive_f1": naive_time[2],
        "added_f1": added_time[2],
        "forward_f1": forward_time[2],
        "ratio": ratio_spread,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--panel",
        type=Path,
        action="append",
        help="a panel CSV; repeatable. Defaults to the 3-week and any long panel.",
    )
    args = parser.parse_args(argv)

    here = Path(__file__).parent
    panels = args.panel or [
        here / "dataset.csv",
        Path(os.environ.get("LONG_PANEL", here / "dataset-25wk.csv")),
    ]

    results = []
    for path in panels:
        if path.exists():
            results.append(report(path))
        else:
            print(f"\n  skipped: {path} does not exist")

    if not results:
        print("\nno panels found. Run `make workshop` and `make workshop-long` first.")
        return 1

    if len(results) == 2:
        short, long = results
        print(f"\n{'=' * 78}\nSIDE BY SIDE\n{'=' * 78}")
        left = f"{short['weeks']}-week"
        right = f"{long['weeks']}-week"
        print(f"  {'quantity':<36}{left:>11}{right:>11}{'change':>11}")
        print(f"  {'-' * 69}")
        rows = [
            ("positive fault hours", "{:,.0f}", "positives"),
            ("separation (fault vs quiet)", "{:,.1f}", "separation"),
            ("F1, naive features", "{:.3f}", "naive_f1"),
            ("F1, + per-signal baseline", "{:.3f}", "added_f1"),
            ("F1, + forward window", "{:.3f}", "forward_f1"),
            ("split noise / effect", "{:,.0f}x", "ratio"),
        ]
        for label, fmt, key in rows:
            a, b = short[key], long[key]
            print(
                f"  {label:<36}{fmt.format(a):>11}{fmt.format(b):>11}"
                f"{fmt.format(b - a):>11}"
            )

        print()
        if short["ratio"] > NO_POWER_ABOVE and long["ratio"] < NO_POWER_ABOVE:
            print(f"  The {left} panel has no power and the {right} one does, which")
            print("  is exactly the claim notebook 03 makes. The baseline's effect is")
            print("  now measurable -- see the F1 rows above for whether it is real.")
        elif long["ratio"] > NO_POWER_ABOVE:
            needed = long["positives"] * long["ratio"] / 2
            per_week = long["positives"] / long["weeks"]
            print(
                f"  **The {right} panel still has no power**, at "
                f"{long['ratio']:.0f}x, down from {short['ratio']:.0f}x on {left}."
            )
            print("  Notebook 03's conclusion stands at this sample size. The effect")
            print("  is measurable enough to extrapolate though: noise/effect falls")
            print(f"  roughly as 1/positives, so reaching 2x needs ~{needed:.0f}.")
            print(f"  That is about {needed / per_week:.0f} weeks.")
        else:
            print("  Both panels have power. Compare the F1 rows directly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
