---
title: "08 — Rolling-origin validation"
subtitle: "Why a random split ranks the wrong model first, and why the baseline you compare against matters more than the model"
---

# 08 — Rolling-origin validation

## The question

> I want to predict lift-station flow an hour ahead. Which of four models is best,
> and how would I know?

The tempting protocol is five-fold cross-validation: shuffle the rows, hold a fifth out,
average the error. On a time series it answers a different question from the one being
asked, and this notebook shows it giving the wrong ranking. Four models, one of which is
not a model at all but the baseline every model has to beat.

## Setup

```python
%matplotlib inline
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from notebooks._data import connect, storm_window
from notebooks._style import apply_style, save, stamp

apply_style()
pd.set_option("display.width", 130)

conn = connect()
storm_start, storm_end = (pd.Timestamp(t) for t in storm_window())
SIGNAL = "INFLUENT:LIFT:FLOW"
HORIZON = 6  # steps of ten minutes: one hour ahead
PER_DAY = 144  # ten-minute steps in a day

minute_means = pd.read_sql(
    "SELECT bucket, mean FROM reading_1m WHERE signal_id = %s", conn, params=(SIGNAL,)
).set_index("bucket")["mean"]
grid = minute_means.reindex(
    pd.date_range("2026-09-22", "2026-09-29", freq="1min", tz="UTC", inclusive="left")
)
series = grid.resample("10min").mean()
print(f"{len(series)} ten-minute steps, {int(series.isna().sum())} empty")
series = series.interpolate(limit=3).dropna()

frame = pd.DataFrame({"now": series})
frame["previous"] = series.shift(1)
frame["target"] = series.shift(-HORIZON)  # what we predict
frame["yesterday"] = series.shift(PER_DAY - HORIZON)  # the target's own time, a day ago
clock = frame.index.hour + frame.index.minute / 60
frame["sin"], frame["cos"] = (
    np.sin(2 * np.pi * clock / 24),
    np.cos(2 * np.pi * clock / 24),
)
frame = frame.dropna()
day_number = (
    frame.index.floor("1D") - pd.Timestamp("2026-09-22", tz="UTC")
).days.to_numpy()
target = frame.target.to_numpy()
print(f"{len(frame)} usable rows, forecasting {HORIZON * 10} minutes ahead")
```

```output
1008 ten-minute steps, 18 empty
864 usable rows, forecasting 60 minutes ahead
```

Every row carries only what would be known at the moment of the forecast: the last
reading, its recent change, the value at the target's own time yesterday, and the clock.
Nothing in a row looks forward.

## Four models

```python
FEATURES = np.column_stack(
    [
        np.ones(len(frame)),
        frame.now,
        frame.now - frame.previous,
        frame.yesterday,
        frame["sin"],
        frame["cos"],
    ]
)


def persistence(_train, test):
    """The next hour will look like this minute."""
    return frame.now.to_numpy()[test]


def yesterday(_train, test):
    """The next hour will look like the same hour yesterday."""
    return frame.yesterday.to_numpy()[test]


def linear(train, test):
    """Least squares on the last reading, its change, yesterday and the clock."""
    beta = np.linalg.lstsq(FEATURES[train], target[train], rcond=None)[0]
    return FEATURES[test] @ beta


def neighbour_in_time(train, test):
    """Predict with the average of the nearest training targets on either side."""
    ordered = np.sort(train)
    out = []
    for i in test:
        j = np.searchsorted(ordered, i)
        low = ordered[j - 1] if j > 0 else ordered[0]
        high = ordered[j] if j < len(ordered) else ordered[-1]
        out.append((target[low] + target[high]) / 2)
    return np.array(out)


MODELS = {
    "persistence": persistence,
    "yesterday": yesterday,
    "linear": linear,
    "neighbour in time": neighbour_in_time,
}


def mae(model, train, test):
    return float(np.mean(np.abs(MODELS[model](train, test) - target[test])))
```

## The usual way: shuffle, split, average

```python
rng = np.random.default_rng(20260929)
order = rng.permutation(len(frame))
folds = np.array_split(order, 5)

random_cv = pd.Series(
    {
        name: np.mean(
            [mae(name, np.setdiff1d(order, fold), np.sort(fold)) for fold in folds]
        )
        for name in MODELS
    }
)
print("mean absolute error, five random folds  [m3/h]")
print(random_cv.round(1).sort_values().to_string())
```

```output
mean absolute error, five random folds  [m3/h]
yesterday             40.5
neighbour in time     61.7
linear                97.0
persistence          255.1
```

Five random folds say the best model is *yesterday*, at **40.5** m³/h, then the **neighbour
in time** at **61.7**, then the linear model at **97.0**, and persistence last at
**255.1**. On that evidence you would ship yesterday — or, if you wanted something
"learned", the neighbour model, which beats the linear one by more than a third.

The neighbour model has learned nothing. It predicts with the average of the nearest
training targets on either side in time, which is interpolation. A shuffled split puts a
training row ten minutes away from almost every test row, and the autocorrelation of
notebook 05 does the rest.

## The honest way: train on the past, test on the day after

```python
rows = []
for test_day in range(2, 7):  # 24 September to 28 September
    train = np.where(day_number < test_day)[0]
    test = np.where(day_number == test_day)[0]
    rows.append(
        {
            "test day": str(frame.index[test][0].date()),
            "trained on days": test_day,
            **{name: mae(name, train, test) for name in MODELS},
        }
    )
rolling = pd.DataFrame(rows).set_index("test day")
print("mean absolute error, one row per forecast origin  [m3/h]")
print(rolling.round(1).to_string())

rolling_mean = rolling[list(MODELS)].mean()
comparison = pd.DataFrame(
    {
        "random folds": random_cv,
        "rolling origins": rolling_mean,
    }
)
comparison["rolling / random"] = (
    comparison["rolling origins"] / comparison["random folds"]
)
print()
print(
    comparison.round(
        {"random folds": 1, "rolling origins": 1, "rolling / random": 1}
    ).to_string()
)
```

```output
mean absolute error, one row per forecast origin  [m3/h]
            trained on days  persistence  yesterday  linear  neighbour in time
test day                                                                      
2026-09-24                2        223.5        0.0     8.6              472.8
2026-09-25                3        223.5        0.0     4.8              472.8
2026-09-26                4        223.5        0.0     3.3              472.8
2026-09-27                5        413.5      119.3   121.2              559.6
2026-09-28                6        202.4      124.5   147.1              492.4

                   random folds  rolling origins  rolling / random
persistence               255.1            257.3               1.0
yesterday                  40.5             48.7               1.2
linear                     97.0             57.0               0.6
neighbour in time          61.7            494.1               8.0
```

Now train on the past and test on the day after, five times. Read the bottom block.
**The neighbour model goes from 61.7 to 494.1, eight times worse**, and falls from second
place to last, below persistence at **257.3**. The linear model *improves*, from
**97.0** to **57.0**. The order of the two flips.

The reason is what the neighbour model does when it has no neighbours. Past the last
training row every test point's nearest neighbour is that one row, so it forecasts one
constant for the whole day: **472.8** m³/h of error on each of the three calm days,
identically, because the calm days are identical. A random split never asked it to
extrapolate.

```python
fig, ax = plt.subplots(figsize=(11, 4.6))
positions = np.arange(len(MODELS))
ax.bar(positions - 0.2, comparison["random folds"], width=0.4, label="random folds")
ax.bar(
    positions + 0.2, comparison["rolling origins"], width=0.4, label="rolling origins"
)
ax.set_xticks(positions, comparison.index)
ax.set_ylabel("mean absolute error  [m3/h]")
ax.set_title(
    "Which model is best depends on how you asked", loc="left", fontweight="bold"
)
ax.legend()
stamp(
    ax,
    window="5 origins / 5 folds",
    source="reading_1m, 10 min means",
    extra="INFLUENT:LIFT:FLOW, one hour ahead",
)
save(fig, "08_random_vs_rolling")
```

Two bars per model, one number asked two ways. Persistence and *yesterday* barely move.
The neighbour model is the only bar whose story changes, and it is the model a random
split would have preferred to the linear one.

```python
storm_day = np.where(day_number == 5)[0]
train = np.where(day_number < 5)[0]
shown = storm_day[
    (frame.index[storm_day] >= storm_start - pd.Timedelta(hours=3))
    & (frame.index[storm_day] < storm_end + pd.Timedelta(hours=4))
]

fig, ax = plt.subplots(figsize=(11, 4.8))
times = frame.index[shown] + pd.Timedelta(minutes=10 * HORIZON)
ax.plot(times, target[shown], color="black", linewidth=2.2, label="what happened")
for name in ("yesterday", "linear", "neighbour in time"):
    ax.plot(times, MODELS[name](train, shown), label=name)
ax.axvspan(storm_start, storm_end, color="#d62728", alpha=0.12, label="storm")
ax.set_ylabel("lift-station flow  [m3/h]")
ax.set_xlabel("time being forecast (UTC)")
ax.legend(loc="upper right", fontsize=8, ncols=2)
ax.set_title(
    "The storm day, forecast an hour ahead from the previous four days",
    loc="left",
    fontweight="bold",
)
stamp(
    ax,
    pd.Series(target[shown], index=times),
    window="10 h",
    source="reading_1m, 10 min means",
    expected_s=600,
)
save(fig, "08_storm_day")
```

On the storm day nobody wins. *Yesterday* misses by **119.3** m³/h, the linear model by
**121.2**, persistence by **413.5**, and the neighbour model by **559.6**. And the day after
is a trap: yesterday is now the storm, so the baseline that scored **0.0** on three calm
days scores **124.5**, and the linear model that leaned on it scores **147.1**.

No model trained on four calm days could have known the storm was coming, and a
validation that averages five origins into one number hides that in its mean.

## What to do

1. **Split by time, and only by time.** Every fold's training data must end before its
   test data begins, with a gap at least as long as the horizon.
2. **Use several origins and report each one.** The linear model's mean of **57.0**
   conceals a range from **3.3** to **147.1**.
3. **Put a baseline in every table.** *Yesterday* scored **0.0** on the calm days because
   this simulator's calm days repeat exactly. A real plant would not do that, but a
   seasonal-naive baseline is still famously hard to beat on real ones, and a model that
   cannot beat it should not ship.
4. **Treat an event as a sample of one.** One storm cannot rank models. It can only show
   that none of them anticipates it, which is the argument for *detecting* events
   (notebook 09) rather than forecasting them.
5. **Distrust a big gap between random and rolling.** A ratio of **8.0** is a model that
   has memorised the calendar.

## Takeaways

- **Random folds ranked the neighbour model second and rolling origins ranked it last**,
  with its error going from **61.7** to **494.1** m³/h. A model that interpolates in time
  looks brilliant under a shuffled split.
- **The baseline is the result.** *Yesterday* scored **40.5** and **48.7** under the two
  schemes, and beat every other model on all five test days.
- **Report every origin.** The linear model's per-day error runs from **3.3** to **147.1**
  m³/h, and the mean of **57.0** describes none of the five days.
- **An event is a sample of one.** On the storm day every model, including the baseline,
  misses by more than **119** m³/h.

## Exercises

1. **Add the gap.** Insert a gap of one horizon between each training set and its test
   day. Which models change, and does the neighbour model's rolling error move?
2. **Fix the neighbour model honestly.** Replace "nearest in time" with "nearest in the
   *clock*" — the same time of day on another day. What does the random split say now,
   and what does the rolling one?
3. **Expanding versus sliding.** Rolling origins here train on all the past. Repeat with a
   sliding window of two days and compare the storm-day error.
4. **Score the interval.** Give each forecast a prediction interval from the training
   residuals, and report coverage per origin. On which days does it fail?

---

**Next:** [09 — Anomaly detection](09-anomaly-detection.ipynb) ·
**Back to the series** [README](README.md) ·
**Previous:** [07 — What are you estimating](07-what-are-you-estimating.ipynb)
