# 06 — A random split makes a good model out of a clock

Last one. The task changes: instead of finding faults, predict the next hour of a
signal. The dataset punishes you the same way, in a different costume.

The target is effluent TSS, at 95% hourly coverage. DO would be the more obvious
choice and cannot be used: see the note at the end.

## The target

```python
from workshops.ml._data import load_panel

SIGNAL = "EFFLUENT:FLOW:TSS"
series = load_panel()
series = series[series["signal_id"] == SIGNAL]
series = series.sort_values("bucket").reset_index(drop=True)
print(f"{SIGNAL}: {len(series):,} hours, "
      f"{series['mean'].notna().mean():.0%} with a value, "
      f"mean {series['mean'].mean():.2f}, sd {series['mean'].std():.2f}")
```

```output
EFFLUENT:FLOW:TSS: 504 hours, 95% with a value, mean 17.07, sd 3.63
```

## The baseline to beat

Not a model. Three lines of arithmetic: the mean of the same hour on the previous
three days. In a plant this is a strong baseline, and it is what a model has to earn
its place against.

```python
hours = series["bucket"].dt.hour.to_numpy()
values = series["mean"].reset_index(drop=True)

series["pred_naive"] = values.groupby(hours).transform(
    lambda column: column.shift(1).rolling(3, min_periods=1).mean()
).fillna(values.shift(1).rolling(24, min_periods=6).mean())

usable = series.dropna(subset=["pred_naive", "mean"]).reset_index(drop=True)
usable["hour_of_day"] = usable["bucket"].dt.hour
cut = int(0.8 * len(usable))
train, test = usable.iloc[:cut], usable.iloc[cut:]
print(f"usable hours {len(usable):,}; held-out final fifth {len(test):,}")
```

```output
usable hours 472; held-out final fifth 95
```

## Two models, and the split between them

```python
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error


def report(name, features, model):
    # **Returns nothing, on purpose.** A function called as a cell's last statement
    # has its return value echoed as `Out[n]`, so returning the MAE put a bare
    # 0.3413774083547015 under the table and the gate rejected it. The gate is right
    # to: the notebook should print what it means to say, not what a function
    # happened to end on.
    model.fit(train[features].fillna(-1), train["mean"])
    predicted = model.predict(test[features].fillna(-1))
    print(f"  {name:<40} MAE {mean_absolute_error(test['mean'], predicted):.4f}")

print(f"  {'same hour, previous 3 days':<40} "
      f"MAE {mean_absolute_error(test['mean'], test['pred_naive']):.4f}")
report("LinearRegression on hour-of-day", ["hour_of_day"], LinearRegression())
report("RandomForest on hour-of-day", ["hour_of_day"],
       RandomForestRegressor(n_estimators=200, random_state=0, n_jobs=-1))
report("RandomForest on the panel's features",
       ["mean", "min", "max", "n", "base_24", "row_written"],
       RandomForestRegressor(n_estimators=200, random_state=0, n_jobs=-1))
```

```output
  same hour, previous 3 days               MAE 0.4367
  LinearRegression on hour-of-day          MAE 2.3475
  RandomForest on hour-of-day              MAE 0.3304
  RandomForest on the panel's features     MAE 0.3395
```

Three things in that table.

**The linear model is worse than useless.** MAE 2.35 against a target with a standard
deviation of 4.46, and it is the *worst* row. Hour-of-day is a circle, not a number:
23:00 and 00:00 are adjacent, and a straight line through them puts the worst
prediction in the middle of the day. A tree handles the wrap for free; a regression
cannot see it at all.

**The tree beats the naive baseline**, 0.330 against 0.437. So the claim this workshop
started from — *a seasonal naive beats every model* — is wrong, and it is worth being
explicit that it is wrong, because it would have been a satisfying thing to believe.

**The panel's own features do not help.** 0.340 against 0.330 for hour-of-day alone.

## Why nothing else matters here

```python
by_hour = usable.groupby("hour_of_day")["mean"].transform("mean")
explained = by_hour.var() / usable["mean"].var()
naive_mae = mean_absolute_error(test["mean"], test["pred_naive"])
naive_share = naive_mae / test["mean"].std()
print(f"variance explained by hour-of-day alone: {explained:.1%}")
print(f"sd of the target in the test fold       : {test['mean'].std():.3f}")
print(f"the naive baseline's MAE is             : "
      f"{naive_share:.1%} of it")
```

```output
variance explained by hour-of-day alone: 90.0%
sd of the target in the test fold       : 4.460
the naive baseline's MAE is             : 9.8% of it
```

**Ninety per cent of the variance is the clock.** The plant runs on a daily cycle, the
cycle is in the data, and a three-line arithmetic baseline already gets within a tenth
of a standard deviation.

There is very little left for a sequence model to find. A `Conv1d` or a `GRU` over
this series would be a ~200 MB dependency learning the remainder of a 10% band, and it
would lose to `shift(1).rolling(3)`. That is the argument against buying a GPU, and
it is an argument made of measurements rather than of principle.

## The part that is genuinely worth the hour

The same model, the same features, one change — the split.

```python
from sklearn.model_selection import train_test_split

random_train, random_test = train_test_split(usable, test_size=0.2, random_state=0)
cheating = RandomForestRegressor(n_estimators=200, random_state=0, n_jobs=-1)
cheating.fit(random_train[["hour_of_day"]], random_train["mean"])
predicted = cheating.predict(random_test[["hour_of_day"]])
random_mae = mean_absolute_error(random_test["mean"], predicted)

honest = RandomForestRegressor(n_estimators=200, random_state=0, n_jobs=-1)
honest.fit(train[["hour_of_day"]], train["mean"])
time_mae = mean_absolute_error(test["mean"], honest.predict(test[["hour_of_day"]]))

print(f"  hour-of-day forest, random split : MAE {random_mae:.4f}")
print(f"  hour-of-day forest, time split   : MAE {time_mae:.4f}")
print(f"  the random split is {time_mae / random_mae:.1f}x better, "
      "and neither is a forecast")
```

```output
  hour-of-day forest, random split : MAE 0.0627
  hour-of-day forest, time split   : MAE 0.3304
  the random split is 5.3x better, and neither is a forecast
```

**A random split makes this model five times better, and the improvement is entirely
an illusion.**

With a random split, the hour you want to predict has near-neighbours *from the same
day* in the training set. The model is not forecasting; it is interpolating between
two values it has already seen, one hour either side. Move the split forward in time
and those neighbours are gone, and the score becomes the number you would actually
get in production.

This is the same trap `notebooks/08` measures on the flow meter — 61.7 m³/h becoming
494.1 — reproduced on a modelling task, and it is the single most common way a
time-series model reports a number that cannot be reproduced.

And notice: the *random* score of 0.063 is better than anything in the two previous
notebooks, on a real problem, and it is worth nothing.

## A note on choosing the target

The obvious target for a wastewater plant is dissolved oxygen, and it cannot be used:
`AERATION:AHU-1:DO` has a value in only 53% of hours, because it moves slowly and the
deadband suppresses most of it. Across all 57 tags, 22 are under 50% covered and
`INFLUENT:FLOW:CONDUCTIVITY` writes 356 rows in three weeks.

**Any tag can be a feature; only a dense one can be a target.** Check coverage before
you choose, not after you have fitted something.

## Takeaways

- **Beat a baseline you believe in.** "Same hour, previous three days" is three lines
  and gets within 9.8% of a standard deviation.
- **Check the shape of your feature before the model.** Hour-of-day is a circle, and
  the linear model on it is the worst row in the table.
- **90% of the variance was the clock.** There is not much left, and a sequence model
  would be a large dependency for the remainder of a small band.
- **A random split inflated this model 5.3×.** Nothing about the model changed; the
  training set gained the answer from both sides of the hour being predicted.
- **Choose a target you can actually forecast.** 53% coverage is not a modelling
  problem, it is a data problem, and it is visible before you fit anything.
