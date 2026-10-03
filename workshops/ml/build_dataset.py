"""Build the machine-learning dataset: one tidy table, and the labels to go with it.

    uv run --extra workshop python -m workshops.ml.build_dataset --weeks 25

Emits `(week, bucket, signal_id, mean, min, max, n, ..., is_fault, fault_type)` as
CSV, ready for `pd.read_csv` with no database and no seed step in the way.

## The one thing that has to be right, and it is not the modelling

**The panel is dense on purpose.** A naive query of the hourly table gives
`reading_1h` as stored, and that table has **no row at all** inside the window of
two of the three instrument faults — measured, on a 25-week seed: zero rows for
`effluent_tss_stuck`, zero for `sensor_dead`, two for `do_sensor_drift`. A
classifier fitted on that is not a weak classifier; it is one with no positive
examples for two thirds of the classes, and every number it produces is a
statement about the sample rather than about the plant.

So the panel crosses every signal with every hour and left-joins the stored rows.
An hour with no row becomes a row with `n = 0`, and **the absence is the finding**:
this historian writes on change, so a frozen or dead instrument goes silent, and
that silence is the only evidence there is.

## Three row states, and the reason two of them are not enough

A stored hour can be in one of three states, and the builder keeps all three
distinguishable because they are *not* the same:

| State | `row_written` | `value_is_null` | Means |
|---|---|---|---|
| a value | 1 | 0 | the instrument is answering |
| a NULL value | 1 | 1 | `sensor_dead` — the instrument stopped answering |
| no row | 0 | 1 | `effluent_tss_stuck` — it never changed, so nothing was written |

Collapsing the last two into one "missing" column destroys the distinction between
a dead instrument and a frozen one, which is the distinction the fault schedule
exists to teach. Measured on a 3-week seed at 1 s:

| Class | hours | `row_written` | `value_is_null` | median `n` |
|---|---|---|---|---|
| `do_sensor_drift` | 10 | 100% | 0% | 17 |
| `effluent_tss_stuck` | 8 | 0% | 100% | 0 |
| `sensor_dead` | 4 | 0% | 100% | 0 |
| normal | 28,706 | 60% | 41% | 2 |

Read the last row before designing anything. **41% of ordinary hours are in
exactly the state a broken instrument is in**, and `sensor_dead` and
`effluent_tss_stuck` are in the *same* state as each other. The row state is
therefore necessary and nowhere near sufficient, which is the honest shape of the
problem and the reason `per_signal_baseline` below exists.

The drift also inverts the intuition: its median `n` is **17 against a normal
median of 2**. A drifting value keeps changing, so the deadband keeps writing, so
the fault makes the instrument *louder*. A detector that looks for quiet finds the
stuck sensor and misses the drift; one that looks for busy finds the drift and
fires on every diurnal swing.

## What the per-signal baseline is for

11,639 empty hours in that panel, of which **12 are fault hours — 0.1%.** Absence on
its own is close to worthless, which is why a `RandomForest` on the naive features
reaches only F1 0.500 on a held-out fifth. What separates a fault hour from a quiet
hour is that the *same signal* was busy yesterday: a fault hour's signal logged 326
rows in the preceding 24 h, a quiet hour's signal logged 20. One trailing median per
signal takes F1 from **0.500 to 0.800** at precision 1.000, and accuracy reads
0.9997 either way — it does not notice. Reaching 1.000 needs a *forward* window,
which reads the future; see `per_signal_baseline`.

## Where the labels come from

`notebooks._data.known_event_instances`, which is the seeder's own schedule function
(`storage.seed.schedule.event_windows`) turned into timestamps. Not a second
implementation: a dataset whose answer key is one edit away from being silently
wrong is the worst thing this repository could hand a workshop.

**The storm is labelled separately, as `is_storm`, and is not `is_fault`.** It is
the weather rather than a broken instrument, and conflating the two would teach that
a fault detector should fire during a storm. It is also the largest single anomaly
in the data, which is exactly why the unsupervised exercise has it find the wrong
thing.

## Known limitation, recorded rather than fixed

`--storm-after` arms **one** storm, at the end of the window, so the final week is
contaminated by it. That is wrong for a "score your detector on a held-out week"
exercise, and the fix is a recurring storm, which is a change to the seeder and not
to this module. Until then, hold out a week from the *middle* of the window.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

#: Hours per "week" in the emitted `week` column. Seven because the whole repository
#: speaks in weeks, not because weeks are the right unit for a grouped split — they
#: are simply the unit the pinned seed is expressed in.
WEEK_DAYS = 7

#: Trailing window for the per-signal baseline, in hours. A day, so it spans a full
#: diurnal cycle: a shorter window would call the plant's own 07:00 peak an anomaly.
BASELINE_WINDOW_H = 24

#: Decimal places the panel's `mean` is rounded to, and why that number.
#:
#: Postgres computes `AVG()` by summing a group, and the order it sums in is not
#: fixed across runs -- parallel workers, and how many of them, are a matter of
#: sizing and load rather than of the data. So two builds of the panel over
#: byte-identical readings disagree at roughly the tenth decimal place.
#:
#: Six is four orders of magnitude coarser than that noise and eight finer than
#: anything the workshop quotes, so it removes the instability without touching
#: any measurement. See `dense_panel`.
MEAN_DECIMALS = 6

#: The columns a model is given. Deliberately excludes the labels, the week and the
#: bucket, so that "the model found it" cannot mean "the model was told the answer".
FEATURES = (
    "mean",
    "min",
    "max",
    "n",
    "row_written",
    "value_is_null",
    "base_24",
    "n_over_base",
    "missing_streak",
)

#: Everything else the emitted file carries.
META = ("week", "bucket", "signal_id", "fault_type", "is_fault", "is_storm")

COLUMNS = FEATURES + META


def dense_panel(
    stored: pd.DataFrame,
    signal_ids: list[str],
    buckets: Any,
) -> pd.DataFrame:
    """Every `(signal, hour)` that could exist, whether or not a row was written.

    `stored` needs `signal_id`, `bucket`, `mean`, `min`, `max` and `n`. `buckets` is
    anything `pd.date_range` accepts, or any datetime sequence.

    The three row states are preserved as two columns rather than one, because
    "a row with a NULL value" and "no row at all" are different faults and merging
    them costs the whole distinction. See the module docstring.
    """
    import pandas as pd  # noqa: PLC0415

    grid = pd.MultiIndex.from_product(
        [signal_ids, pd.DatetimeIndex(buckets)], names=["signal_id", "bucket"],
    ).to_frame(index=False)
    keep = [c for c in ("signal_id", "bucket", "mean", "min", "max", "n")
            if c in stored.columns]
    panel = grid.merge(stored[keep], on=["signal_id", "bucket"], how="left")
    panel["row_written"] = panel["n"].notna().astype("int8")
    panel["n"] = panel["n"].fillna(0).astype("float64")
    panel["value_is_null"] = panel["mean"].isna().astype("int8")
    # **Round `mean`, so the panel is a reproducible artefact.**
    #
    # The seed is deterministic: two runs of the same window produce byte-identical
    # readings. `AVG()` is not. Postgres may sum a group across parallel workers,
    # and the number of workers and the order they combine in are not fixed, so
    # two runs over identical rows produce means that differ in the last bits:
    #
    #     1119.808718833635   vs   1119.8087188336349
    #
    # Only `mean` is affected — `min`, `max` and `n` are order-independent, and
    # comparing two builds of the panel found `mean` the only field that moved, in
    # 8,482 of 28,728 rows.
    #
    # That noise is ~1e-10, which is invisible on its own and **not** invisible
    # downstream. `06-predictive` fits a `RandomForestRegressor` to the panel, and a
    # forest is chaotic: one split landing 1e-10 to the other side changes the tree,
    # and the reported MAE moved 0.3414 -> 0.3400 with nothing else changing. The
    # prose claim was pinned to a number that was never a property of the data, only
    # of the order the database happened to add it up in.
    #
    # Rounding to six decimals is four orders of magnitude coarser than the noise and
    # eight finer than anything the workshop measures (MAE is quoted to four), so it
    # costs no accuracy and makes the panel -- and therefore every number downstream
    # of it -- identical on every machine. `tests/test_workshop_dataset.py` fails if
    # the rounding is ever undone, and names why the tolerance on that assertion
    # cannot be loosened.
    panel["mean"] = panel["mean"].round(MEAN_DECIMALS)
    return panel.sort_values(["signal_id", "bucket"], ignore_index=True)


def per_signal_baseline(
    panel: pd.DataFrame,
    window_h: int = BASELINE_WINDOW_H,
) -> pd.DataFrame:
    """How busy *this* signal normally is, which is the feature that finds a fault.

    A trailing median of `n` per signal, the ratio of the current hour to it, and the
    length of the current run of empty hours. All computed **within** a signal, from
    the rows themselves, with no reference to the label — a model that has been told
    which hours are broken has already been told.

    ## There is no forward-looking window here, and that is the point

    An earlier draft of this docstring claimed the function produced both a trailing
    and a leading baseline. It did not, and the sentence was wrong in the way that
    matters: the prototype's headline F1 of 1.000 had been measured **with** the
    forward window, so the number the plan was built on was partly bought by reading
    the future. Measured on the emitted CSV, held-out fifth:

    | features | accuracy | recall | precision | F1 |
    |---|---|---|---|---|
    | naive (`n`, `row_written`, `value_is_null`) | 0.9997 | 0.333 | 1.000 | 0.500 |
    | **causal — what this function emits** | 0.9998 | 0.667 | 1.000 | **0.800** |
    | the same plus a forward 24 h window | 1.0000 | 1.000 | 1.000 | **1.000** |

    So the perfect score is available, and it is worth nothing: a detector that reads
    tomorrow's row count knows whether the instrument was broken today, and cannot be
    run on live data. **0.800 is the honest number and 1.000 is the lie**, which is a
    better exercise than anything this module could assert — so the forward window is
    left out on purpose, and the workshop adds it and watches the score go up.

    Note that accuracy reads 0.9997, 0.9998 and 1.0000 across those three rows. It
    does not notice any of it.

    ## Why `min_periods=6`

    With a 24-hour window and a plant that can be quiet for a while, a stricter floor
    leaves the first fault instances of a run unlabelled for want of history, which
    reads as "no signal here" rather than "not enough history yet".
    """
    panel = panel.sort_values(["signal_id", "bucket"], ignore_index=True)
    if window_h <= 0:
        raise ValueError(
            f"baseline window must be positive hours, got {window_h}. A zero-width "
            f"window is an empty median and a negative one is a backwards range: both "
            f"return a frame rather than raising, and both leave the model with a "
            f"constant column — a feature that shows up in an importance plot and "
            f"means nothing."
        )
    n = panel.groupby("signal_id")["n"]
    panel[f"base_{window_h}"] = n.transform(
        lambda s: s.shift(1).rolling(window_h, min_periods=6).median())
    panel["n_over_base"] = panel["n"] / panel[f"base_{window_h}"].replace(
        0, float("nan"))
    panel["missing_streak"] = n.transform(
        lambda s: s.eq(0).groupby((~s.eq(0)).cumsum()).cumsum()).astype("int32")
    return panel


def label_panel(
    panel: pd.DataFrame,
    instances: list[tuple[str, str, Any, Any, int]],
    storm: tuple[Any, Any] | None = None,
    week_days: int = WEEK_DAYS,
) -> pd.DataFrame:
    """Attach `fault_type`, `is_fault`, `is_storm` and `week`.

    `week` counts 7-day blocks **from the start of the window**, not calendar
    Mondays. The seed's start is whatever `--end` and `--weeks` put it there — a
    25-week window ending 2026-09-29 opens on a Tuesday — so calendar weeks would
    cut the first block short and make the first group a different size from the
    rest, which is a confound in a grouped split wearing a date's clothing.

    This is the only place a week label exists, and it exists **only in the emitted
    file**. `reading` is `(ts, signal_id, value, quality, source)` and is written by
    the live gateway, so a `week` column there would be a production schema change
    made for a workshop, and it would be populated by something that has no business
    knowing what a modelling week is.
    """
    panel = panel.copy()
    panel["fault_type"] = ""
    for fault, signal_id, start, end, _nth in instances:
        hit = (panel["signal_id"] == signal_id) & (panel["bucket"] >= start) & (
            panel["bucket"] < end)
        panel.loc[hit, "fault_type"] = fault
    panel["is_fault"] = (panel["fault_type"] != "").astype("int8")
    if storm is not None:
        panel["is_storm"] = (
            (panel["bucket"] >= storm[0]) & (panel["bucket"] < storm[1])
        ).astype("int8")
    else:
        panel["is_storm"] = 0
    panel["week"] = _week_index(panel["bucket"], panel["bucket"].min(), week_days)
    return panel


def _week_index(bucket: pd.Series, origin: Any, week_days: int) -> pd.Series:
    """`int` week index from `origin`, as `int16` because 25 weeks fits in 300."""
    delta = (bucket - origin).dt.total_seconds()
    return (delta // (week_days * 86_400)).astype("int16")


def _by_fault(panel: pd.DataFrame) -> dict[str, int]:
    """Fault name to labelled hours, for the summary line."""
    hits = panel.loc[panel["is_fault"] == 1, "fault_type"]
    counts: dict[str, int] = hits.value_counts().to_dict()
    return counts


def summarise(panel: pd.DataFrame) -> str:
    """The numbers a reader wants before loading the file, and all of them real."""
    lines = [
        f"{len(panel):,} rows = {panel['signal_id'].nunique()} signals "
        f"x {panel['bucket'].nunique():,} hours",
        f"  weeks           {panel['week'].nunique()}",
        f"  positives       {int(panel['is_fault'].sum()):,} "
        f"({panel['is_fault'].mean():.4%}); majority-class accuracy "
        f"{1 - panel['is_fault'].mean():.4f}",
        f"  by fault        {_by_fault(panel)}",
        f"  storm hours     {int(panel['is_storm'].sum()):,}",
        f"  no row written  {int((panel['row_written'] == 0).sum()):,} "
        f"({(panel['row_written'] == 0).mean():.1%})",
        "  the three row states, which is what a model actually sees:",
    ]
    states = panel.assign(
        state=panel["fault_type"].replace("", "normal"))
    for name, g in states.groupby("state"):
        lines.append(
            f"    {name:<20}{len(g):>8,}  row_written {g['row_written'].mean():>4.0%}"
            f"  value_is_null {g['value_is_null'].mean():>4.0%}"
            f"  median n {g['n'].median():>6.0f}")
    return "\n".join(lines)


def _read_source(dsn: str, table: str) -> tuple[Any, list[str], Any]:
    """`(stored, signal_ids, buckets)` for one aggregate table.

    A missing database is caught here and re-raised with the command, because
    psycopg's own message -- `FATAL: database "wwtp_ml" does not exist` -- names the
    database and not the thing that creates it, and the reader of a workshop has no
    reason to know that a seed step exists.
    """
    import psycopg  # noqa: PLC0415

    try:
        conn = psycopg.connect(dsn)
    except psycopg.OperationalError as exc:
        if "does not exist" in str(exc):
            raise SystemExit(
                "  the workshop database has not been seeded yet.\n\n"
                "      make workshop\n\n"
                "  (which seeds the window and then builds this panel.)"
            ) from exc
        raise
    with conn:
        stored = _fetch(conn, f"SELECT * FROM {table}")
        bounds = conn.execute(
            f"SELECT min(bucket), max(bucket) FROM {table}").fetchone()
        if bounds is None:
            raise SystemExit(f"{table} is empty; seed it before building a panel")
        lo, hi = bounds
        signals = [r[0] for r in conn.execute("SELECT id FROM signal ORDER BY 1")]
    import pandas as pd  # noqa: PLC0415

    # Coerce the bounds rather than passing `tz=` alongside them. `pd.date_range`
    # takes *either* tz-aware bounds *or* a `tz=`, and supplying both raises
    # "exactly three must be specified" — which is a message about the wrong thing
    # entirely, and which did not fire on one database and fired on another for no
    # reason anyone could act on. `tz="UTC"` alone would also have been wrong: it
    # *localises* naive bounds rather than converting aware ones, and every bucket
    # in this project is UTC by construction.
    start, end = (pd.Timestamp(b).tz_convert("UTC") for b in (lo, hi))
    return stored, signals, pd.date_range(start, end, freq="h")


def _fetch(conn: Any, sql: str) -> Any:
    import pandas as pd  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(sql)
        return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def _storm_window() -> tuple[Any, Any]:
    """The one storm, as timestamps, from the module that decides where it is.

    Not reimplemented here. An earlier version of this took a `--storm-after` flag
    and computed `end - after_h` to `end`, which is the storm's 36-hour *lead-in*
    and not its 2-hour body — so it labelled 2,052 hours as storm instead of 114, and
    the summary printed the wrong number without complaining, because a wrong label
    column is exactly as valid-looking as a right one.

    That is the same class of bug as everything else in this repository: a second
    implementation of a decision somebody else already owns. `storm_window()` is the
    pinned week's storm, which is correct here because the workshop dataset is seeded
    with `--end 2026-09-29T00:00:00Z --storm-after 36`, and a flag that can disagree
    with the seed is a flag that will.
    """
    import pandas as pd  # noqa: PLC0415
    from notebooks._data import storm_window  # noqa: PLC0415

    return tuple(pd.Timestamp(t) for t in storm_window())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dsn", default=None,
        help="source database. Defaults to $WORKSHOP_DSN, and failing that to the "
             "POSTGRES_* in .env with dbname=wwtp_ml -- the same derivation "
             "`make workshop-seed` uses, so the two cannot disagree and nobody has "
             "to type a connection string. The database name is the workshop's own "
             "and never the plant's: the builder creates and fills its own.",
    )
    parser.add_argument("--table", default="reading_1h",
                        help="source aggregate table (default: reading_1h)")
    parser.add_argument("--out", type=Path,
                        default=Path("workshops/ml/dataset.csv"),
                        help="where to write the CSV (default: beside this module)")
    parser.add_argument("--days", type=int, default=25 * 7,
                        help="length of the seeded window; must match the seed, "
                             "because the labels are derived from its length")
    parser.add_argument("--every-hours", type=float, default=36.0,
                        help="the fault interval the source was seeded with "
                             "(default: 36, which is what the seeder default is)")
    parser.add_argument("--end", default="2026-09-29T00:00:00Z",
                        help="end of the seeded window; must match the seed")
    parser.add_argument("--baseline-hours", type=int, default=BASELINE_WINDOW_H,
                        help="trailing window for the per-signal baseline, in hours")
    args = parser.parse_args(argv)

    from notebooks._data import known_event_instances  # noqa: PLC0415

    from workshops.ml._data import panel_dsn  # noqa: PLC0415

    dsn = args.dsn or panel_dsn()
    stored, signals, buckets = _read_source(dsn, args.table)
    panel = dense_panel(stored, signals, buckets)
    panel = per_signal_baseline(panel, args.baseline_hours)
    panel = label_panel(panel,
                        known_event_instances(days=args.days, end=args.end,
                                               every_h=args.every_hours),
                        storm=_storm_window())
    print(summarise(panel))

    missing = [c for c in COLUMNS if c not in panel.columns]
    if missing:
        raise SystemExit(f"panel is missing {missing}; that is a bug in this module")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    panel[list(COLUMNS)].to_csv(args.out, index=False)
    print(f"\nwrote {args.out}  ({args.out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
