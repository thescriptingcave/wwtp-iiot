"""The workshop dataset builder, and the three ways a panel can quietly lie.

`workshops/ml/build_dataset.py` exists because the stored hourly table has **no row
at all** inside the window of two of the three instrument faults. Measured on a
25-week seed: zero rows for `effluent_tss_stuck`, zero for `sensor_dead`, two for
`do_sensor_drift`. So the builder crosses every signal with every hour instead, and
an absent hour becomes a row with `n = 0`.

Everything here is a pure function over a frame, so the whole file runs without a
database — which is the point of splitting the IO out of `main()`. The properties
that matter are not the arithmetic; they are the three ways a tidy dataset can be
wrong while looking completely fine:

* **a hole the panel should have filled** — the bug the module exists to prevent;
* **a baseline that sees the hour it is describing** — target encoding, which is the
  single most common way a leakage-free-looking time series model inflates its score;
* **a baseline computed across signals** — a global median, which is not a baseline
  at all, just a number.
"""

from __future__ import annotations

import pandas as pd
import pytest
from workshops.ml.build_dataset import (
    COLUMNS,
    FEATURES,
    WEEK_DAYS,
    dense_panel,
    label_panel,
    per_signal_baseline,
    summarise,
)

HOURS = pd.date_range("2026-01-01", periods=48, freq="h", tz="UTC")
SIGNALS = ["A:ONE", "B:TWO"]


def stored_rows(
    rows: list[tuple[str, pd.Timestamp, float | None, int]],
) -> pd.DataFrame:
    """A `reading_1h`-shaped frame from `(signal, bucket, mean, n)` tuples.

    `n` is a separate argument rather than fixed at 1 because **the per-signal
    baseline is a median of `n`, not of `mean`.** A first draft of these tests
    varied `mean` and asserted the baseline moved, and it did not — the tests were
    checking a column the feature does not read, and would have passed whatever the
    grouping did.
    """
    return pd.DataFrame(
        [
            {
                "signal_id": sig, "bucket": bucket, "mean": value,
                "min": value, "max": value, "n": n,
            }
            for sig, bucket, value, n in rows
        ],
        columns=["signal_id", "bucket", "mean", "min", "max", "n"],
    )


# ── the panel is dense, which is the whole reason this module exists ──────────


def test_the_panel_has_a_row_for_every_signal_and_hour_even_with_no_data() -> None:
    """A hole here is the bug the module was written to prevent.

    The stored table omits hours the historian had nothing to write, so a query of it
    returns a frame whose shape is decided by the *fault schedule*: a dataset with
    more broken instruments would be a *smaller* dataset, and a model fitted on it
    would report a better score for a worse plant.
    """
    panel = dense_panel(stored_rows([]), SIGNALS, HOURS)
    assert len(panel) == len(SIGNALS) * len(HOURS)
    assert (panel["row_written"] == 0).all()
    assert (panel["n"] == 0).all()
    assert set(panel["signal_id"]) == set(SIGNALS)


def test_an_absent_hour_is_distinguishable_from_a_stored_null() -> None:
    """Two different faults, so two different columns.

    `sensor_dead` writes a row whose value is NULL; `effluent_tss_stuck` writes no
    row at all, because a frozen value never *changes* and this historian writes on
    change. Collapsing both into a single "missing" column throws away the
    distinction the fault schedule exists to teach — and it throws it away silently,
    because the frame is still rectangular and the labels are still valid.
    """
    absent = HOURS[0]
    null_row = HOURS[1]
    good = HOURS[2]
    panel = dense_panel(
        stored_rows([("A:ONE", null_row, None, 1), ("A:ONE", good, 4.2, 1)]),
        SIGNALS, HOURS,
    )
    a = panel[panel.signal_id == "A:ONE"].set_index("bucket")

    assert (a.loc[absent, "row_written"], a.loc[absent, "value_is_null"]) == (0, 1)
    assert (a.loc[null_row, "row_written"], a.loc[null_row, "value_is_null"]) == (1, 1)
    assert (a.loc[good, "row_written"], a.loc[good, "value_is_null"]) == (1, 0)
    # All three are "the value is not there"; only the first pair separates them.
    assert a.loc[absent, "value_is_null"] == a.loc[null_row, "value_is_null"]
    assert a.loc[absent, "row_written"] != a.loc[null_row, "row_written"]


def test_a_stored_row_keeps_its_row_count_and_an_absent_one_gets_zero() -> None:
    panel = dense_panel(
        stored_rows([("A:ONE", HOURS[0], 7.0, 1)]), SIGNALS, HOURS[:3]
    )
    a = panel[panel.signal_id == "A:ONE"]
    assert a["n"].tolist() == [1.0, 0.0, 0.0]
    assert a["row_written"].tolist() == [1, 0, 0]


# ── the baseline must not see the hour it describes ──────────────────────────


def test_the_baseline_uses_only_the_past() -> None:
    """No `shift`, and this is target encoding.

    A trailing median that includes the current hour answers "is this hour unusual?"
    partly with the current hour, so a fault — which is by definition unusual — helps
    label itself. The score goes up, the model goes into production, and the
    baseline it was compared against was not a baseline.

    So the test is: change *this* hour's count and the baseline for *this* hour must
    not move, while the baseline for every later hour must.
    """
    # A six-hour window, and four of its six values changed. The first draft used a
    # 24-hour window and changed one value, and the baseline did not move -- which is
    # not a bug but arithmetic: the median of 24 values is between the 12th and the
    # 13th, so altering one of them changes nothing. A test that quietly cannot fail
    # is worse than no test, because it looks like coverage.
    window = 6
    rows = [(s, h, 10.0, 10) for s in SIGNALS for h in HOURS]
    before = per_signal_baseline(dense_panel(stored_rows(rows), SIGNALS, HOURS), window)
    rows_b = [
        (s, h, 9999.0, 9999) if s == SIGNALS[0] and h in tuple(HOURS[20:24])
        else (s, h, v, n)
        for s, h, v, n in rows
    ]
    after = per_signal_baseline(
        dense_panel(stored_rows(rows_b), SIGNALS, HOURS), window)

    column = f"base_{window}"  # the column is named for the window, not always 24

    def key(frame, signal, hour):
        return frame[(frame.signal_id == signal)
                     & (frame.bucket == hour)][column].iloc[0]

    assert key(before, SIGNALS[0], HOURS[20]) == key(after, SIGNALS[0], HOURS[20]), (
        "the baseline for an hour moved when that hour's own row count changed, "
        "so the feature is reading the row it is meant to be judging"
    )
    assert key(before, SIGNALS[0], HOURS[24]) != key(after, SIGNALS[0], HOURS[24]), (
        "a change at hour 20 must reach the baseline at hour 24, or the baseline is "
        "not tracking anything at all"
    )


def test_the_baseline_is_computed_within_a_signal() -> None:
    """A global median is not a baseline; it is a constant.

    Two signals with wildly different normal rates — one busy, one almost silent —
    must not share a reference, or the quiet signal's ordinary hour looks like a
    collapse against the busy signal's ordinary hour. That mistake is invisible in
    the aggregate and shows up only as "the detector works on some tags".
    """
    busy = [(SIGNALS[0], h, 100.0, 400) for h in HOURS]
    quiet = [(SIGNALS[1], h, 1.0, 2) for h in HOURS]
    panel = per_signal_baseline(dense_panel(stored_rows(busy + quiet), SIGNALS, HOURS))
    base = panel[panel.bucket == HOURS[-1]].set_index("signal_id")["base_24"]
    assert base[SIGNALS[0]] == 400.0
    assert base[SIGNALS[1]] == 2.0
    assert base[SIGNALS[0]] != base[SIGNALS[1]]


def test_the_baseline_survives_a_signal_that_is_entirely_empty() -> None:
    """A quiet tag must not divide by zero or poison its neighbours.

    `n_over_base` divides by the baseline, and a signal that has never written a row
    has a baseline of zero. The `replace(0, nan)` is load-bearing: without it the
    ratio is infinite and `RandomForest` refuses the matrix, which is at least loud.
    """
    rows = [(SIGNALS[0], h, 10.0, 10) for h in HOURS]
    panel = per_signal_baseline(dense_panel(stored_rows(rows), SIGNALS, HOURS))
    quiet = panel[panel.signal_id == SIGNALS[1]]
    # The median of nothing but zeros is zero, not missing -- which is why the
    # division needs guarding rather than relying on the column being absent. The
    # leading NaNs are the `min_periods` floor doing its job: no history yet is not
    # the same as a baseline of zero, and the two mean opposite things to a detector.
    settled = quiet["base_24"].dropna()
    assert len(settled) > 0
    assert (settled == 0.0).all()
    assert quiet["base_24"].iloc[:6].isna().all()
    assert quiet["n_over_base"].isna().all(), (
        "0/0 must be NaN, not inf: an inf in the matrix makes RandomForest refuse to "
        "fit, which is at least loud, but a silently clipped one is not"
    )


def test_the_missing_streak_counts_consecutive_empty_hours() -> None:
    """Silence is the evidence, so its length is a feature — and it must be a run
    length within one signal, not a cumulative total."""
    rows = [(SIGNALS[0], h, 10.0, 1) for i, h in enumerate(HOURS)
            if i not in (5, 6, 7, 20, 21)]
    panel = per_signal_baseline(dense_panel(stored_rows(rows), SIGNALS, HOURS[:24]))
    streak = panel[panel.signal_id == SIGNALS[0]].set_index("bucket")["missing_streak"]
    assert streak[HOURS[4]] == 0
    assert streak[HOURS[5]] == 1
    assert streak[HOURS[7]] == 3
    assert streak[HOURS[8]] == 0, "the run must reset when a row comes back"
    assert streak[HOURS[20]] == 1
    assert streak[HOURS[21]] == 2
    assert streak[HOURS[22]] == 0, "the run ends when a row comes back"


# ── labels ───────────────────────────────────────────────────────────────────


def test_a_fault_window_is_half_open_so_two_faults_can_share_a_boundary() -> None:
    """`[start, end)`, so the hour a fault ends is the hour the next one starts.

    An inclusive end double-counts the boundary hour, and with the 36-hour recurrence
    the kinds rotate through three positions, so inclusive ends would quietly
    relabel the first hour of every third instance.
    """
    panel = dense_panel(stored_rows([]), SIGNALS, HOURS[:24])
    instances = [("do_sensor_drift", "A:ONE", HOURS[5], HOURS[7], 0)]
    out = label_panel(panel, instances)
    a = out[out.signal_id == "A:ONE"].set_index("bucket")["is_fault"]
    assert a[HOURS[4]] == 0
    assert a[HOURS[5]] == 1
    assert a[HOURS[6]] == 1
    assert a[HOURS[7]] == 0, "the end hour belongs to whatever comes next"


def test_the_storm_is_not_a_fault() -> None:
    """It is the weather, and conflating the two teaches a detector to fire on rain.

    It is also the largest single anomaly in the data, which is what makes it the
    right answer for the unsupervised exercise and the wrong answer for the
    supervised one.
    """
    panel = dense_panel(stored_rows([]), SIGNALS, HOURS[:24])
    out = label_panel(panel, [], storm=(HOURS[5], HOURS[7]))
    assert out["is_fault"].sum() == 0
    assert out["is_storm"].sum() == len(SIGNALS) * 2


def test_the_week_index_counts_from_the_window_start_not_the_calendar() -> None:
    """Calendar weeks would make the first group a different size from the rest.

    The seed's start is wherever `--end` and `--weeks` put it — a 25-week window
    ending 2026-09-29 opens on a Tuesday — so bucketing on Mondays cuts the first
    block short. In a grouped split that is a confound wearing a date's clothing:
    the held-out group is not like the others.
    """
    start = pd.Timestamp("2026-04-07T00:00:00Z")  # a Tuesday
    two_weeks = pd.date_range(start, periods=WEEK_DAYS * 2 * 24, freq="h", tz="UTC")
    out = label_panel(dense_panel(stored_rows([]), SIGNALS, two_weeks), [])
    a = out[out.signal_id == SIGNALS[0]]
    assert a.groupby("week").size().to_dict() == {0: WEEK_DAYS * 24, 1: WEEK_DAYS * 24}
    assert a.set_index("bucket").loc[two_weeks[WEEK_DAYS * 24 - 1], "week"] == 0
    assert a.set_index("bucket").loc[two_weeks[WEEK_DAYS * 24], "week"] == 1
    # Calendar-Monday bucketing would put the boundary at 2026-04-13, five days in,
    # and week 0 would hold 5 days against week 1's 7.
    assert start.dayofweek != 0


def test_the_feature_list_cannot_see_the_answer() -> None:
    """`is_fault`, `fault_type` and `week` are not features, and this is asserted.

    A label left in the feature matrix is the one mistake that produces a perfect
    score and no model, and it is invisible in the output — the number is *too*
    good, which is the hardest kind of wrong to notice and the easiest to ship.
    """
    assert not {"is_fault", "fault_type", "is_storm", "week"} & set(FEATURES)
    assert "bucket" not in FEATURES, (
        "an absolute timestamp is a feature too, and it is a proxy for the label "
        "here: the faults are on a fixed 36-hour schedule, so a tree can learn the "
        "date instead of the symptom"
    )
    assert set(FEATURES) <= set(COLUMNS)
    assert set(COLUMNS) - set(FEATURES) == {
        "week", "bucket", "signal_id", "fault_type", "is_fault", "is_storm"}


# ── the summary, because it is the only thing a reader sees first ────────────


def test_the_summary_reports_the_base_rate_a_reader_needs() -> None:
    """The majority-class accuracy belongs in the first six lines anyone reads.

    A file of 239,400 rows with 194 positives is a file where 0.999 is free, and
    anyone who loads it without that number in front of them will report accuracy.
    """
    panel = dense_panel(
        stored_rows([("A:ONE", h, 5.0, 1) for h in HOURS]), SIGNALS, HOURS)
    out = label_panel(panel, [("sensor_dead", "A:ONE", HOURS[0], HOURS[1], 0)])
    text = summarise(out)
    assert "majority-class accuracy" in text
    assert "0.0" in text
    assert "majority-class accuracy" in text
    assert "sensor_dead" in text, "the per-class row states are the point of it"
    assert "row_written" in text and "value_is_null" in text


def test_an_instrument_coming_back_online_does_not_produce_an_infinite_ratio() -> None:
    """`n > 0` against a baseline of zero is the case the guard exists for.

    The first version of this test used a signal that is silent for the *whole*
    window, where `n` is also zero and `0/0` is already `NaN` -- so it passed with
    the `.replace(0, nan)` removed, and was checking nothing. The real case is an
    instrument that has been quiet and then answers: `n = 5` over a baseline of `0`
    divides to `inf`, and `RandomForest` refuses a matrix containing one.
    """
    rows = [(SIGNALS[0], h, 10.0, 5) for h in HOURS[24:]]
    panel = per_signal_baseline(dense_panel(stored_rows(rows), SIGNALS, HOURS))
    a = panel[panel.signal_id == SIGNALS[0]].set_index("bucket")
    assert a.loc[HOURS[24], "base_24"] == 0.0, "it really was silent for a day first"
    assert a.loc[HOURS[24], "n"] == 5.0
    assert a.loc[HOURS[24], "n_over_base"] != float("inf")
    assert pd.isna(a.loc[HOURS[24], "n_over_base"])


@pytest.mark.parametrize("bad", [0, -1])
def test_an_impossible_baseline_window_is_refused(bad: int) -> None:
    """A zero window is an empty median; a negative one is a backwards one.

    Both are refused here rather than being handed to `rolling`, which raises its
    own `ValueError` for the same inputs -- so the test has to match **this**
    module's message. `match="window"` matched pandas' too, and the validation could
    be deleted entirely without the suite noticing: a test that passes for the wrong
    reason is the one that is hardest to remove later.
    """
    panel = dense_panel(stored_rows([]), SIGNALS, HOURS)
    with pytest.raises(ValueError, match="baseline window must be positive hours"):
        per_signal_baseline(panel, bad)
