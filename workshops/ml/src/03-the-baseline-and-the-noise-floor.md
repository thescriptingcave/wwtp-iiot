# 03 — The feature is right, and you cannot prove it

Notebook 02 left a thread: a fault hour and a quiet hour both have no row, and the
difference between them is not in the row. This notebook builds the feature that
carries that difference.

Then it does something more useful than claiming the feature works: **it shows you
that on this dataset you cannot tell whether it works.**

## The feature

For each signal, its own recent row count. An hour is suspicious if the signal went
silent *for a signal that does not normally go silent*.

```python
from workshops.ml._data import load_panel

panel = load_panel().sort_values(["signal_id", "bucket"]).reset_index(drop=True)

n = panel.groupby("signal_id")["n"]
panel["base_24"] = n.transform(lambda s: s.shift(1).rolling(24, min_periods=6).median())
panel["n_over_base"] = panel["n"] / panel["base_24"].replace(0, float("nan"))
panel["streak"] = n.transform(lambda s: s.eq(0).groupby((~s.eq(0)).cumsum()).cumsum())

NAIVE = ["mean", "min", "max", "n", "row_written", "value_is_null"]
BASELINE = [*NAIVE, "base_24", "n_over_base", "streak"]
```

The `shift(1)` matters and is not decoration. Without it the window includes the
hour being described, so a fault — which by definition differs from the signal's
recent history — partly helps label itself. That is target encoding, and it inflates
the score rather than the model.

## The separation is real, and large

```python
empty = panel[panel["n"] == 0]
fault = empty[empty["is_fault"] == 1]
quiet = empty[empty["is_fault"] == 0]

print(f"empty hours                        : {len(empty):,}")
print(f"  fault hour -- signal logged      : "
      f"{fault['base_24'].mean():>7.0f} rows in 24 h")
print(f"  quiet  hour -- signal logged      : "
      f"{quiet['base_24'].mean():>7.0f} rows in 24 h")
print(f"  ratio                             : "
      f"{fault['base_24'].mean() / quiet['base_24'].mean():.0f}x")
```

```output
empty hours                        : 11,639
  fault hour -- signal logged      :     596 rows in 24 h
  quiet  hour -- signal logged      :      20 rows in 24 h
  ratio                             : 29x
```

**Twenty-nine times.** The same absence, opposite conclusions, and the only thing
that separates them is the signal's own history. This is the answer to notebook 02's
closing question, and it is the right feature.

## Now try to prove it helps

```python
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score, precision_score, recall_score

from workshops.ml._data import held_out

train, test = held_out(panel, last=1)
print(f"train {len(train):,} / test {len(test):,}  "
      f"-- {int(test['is_fault'].sum())} fault hours in the test fold\n")

def report(name, features):
    model = RandomForestClassifier(n_estimators=300, random_state=0, n_jobs=-1)
    model.fit(train[features].fillna(-1), train["is_fault"])
    predicted = model.predict(test[features].fillna(-1))
    rec = recall_score(test["is_fault"], predicted)
    prec = precision_score(test["is_fault"], predicted, zero_division=0)
    print(f"{name:<22} recall {rec:>6.3f}"
          f"  precision {prec:>6.3f}"
          f"  F1 {f1_score(test['is_fault'], predicted, zero_division=0):>6.3f}")
    return test.assign(predicted=predicted)

report("naive features", NAIVE)
report("with the baseline", BASELINE)
```

```output
train 19,152 / test 9,576  -- 7 fault hours in the test fold

naive features         recall  0.429  precision  1.000  F1  0.600
with the baseline      recall  0.286  precision  1.000  F1  0.444
              mean          min          max       n  row_written  \
336    1938.590729  1334.869101  3196.505995    40.0            1   
337    1465.096642  1354.893946  1575.339834    12.0            1   
338    1806.057937  1595.398214  2016.948197    22.0            1   
339    2328.249393  2036.988259  2619.723235    30.0            1   
340    3132.836328  2639.799260  3626.562705    50.0            1   
...            ...          ...          ...     ...          ...   
28723   610.764421   552.043571   639.036491  3600.0            1   
28724   536.013863   503.858036   619.929068  3600.0            1   
28725   506.744253   495.977047   534.161778  3600.0            1   
28726   503.953038   495.173176   527.667675  3600.0            1   
28727   507.081248   475.298593   531.701854  3600.0            1   

       value_is_null  base_24  n_over_base  missing_streak  week  \
336                0   3033.0     0.013188               0     2   
337                0   3033.0     0.003956               0     2   
338                0   3033.0     0.007254               0     2   
339                0   3033.0     0.009891               0     2   
340                0   3033.0     0.016485               0     2   
...              ...      ...          ...             ...   ...   
28723              0   3600.0     1.000000               0     2   
28724              0   3600.0     1.000000               0     2   
28725              0   3600.0     1.000000               0     2   
28726              0   3600.0     1.000000               0     2   
28727              0   3600.0     1.000000               0     2   

                         bucket                 signal_id fault_type  \
336   2026-09-22 00:00:00+00:00   AERATION:AHU-1:AIR_FLOW        NaN   
337   2026-09-22 01:00:00+00:00   AERATION:AHU-1:AIR_FLOW        NaN   
338   2026-09-22 02:00:00+00:00   AERATION:AHU-1:AIR_FLOW        NaN   
339   2026-09-22 03:00:00+00:00   AERATION:AHU-1:AIR_FLOW        NaN   
340   2026-09-22 04:00:00+00:00   AERATION:AHU-1:AIR_FLOW        NaN   
...                         ...                       ...        ...   
28723 2026-09-28 19:00:00+00:00  UTILITY:SITE:PLANT_POWER        NaN   
28724 2026-09-28 20:00:00+00:00  UTILITY:SITE:PLANT_POWER        NaN   
28725 2026-09-28 21:00:00+00:00  UTILITY:SITE:PLANT_POWER        NaN   
28726 2026-09-28 22:00:00+00:00  UTILITY:SITE:PLANT_POWER        NaN   
28727 2026-09-28 23:00:00+00:00  UTILITY:SITE:PLANT_POWER        NaN   

       is_fault  is_storm  streak  predicted  
336           0         0       0          0  
337           0         0       0          0  
338           0         0       0          0  
339           0         0       0          0  
340           0         0       0          0  
...         ...       ...     ...        ...  
28723         0         0       0          0  
28724         0         0       0          0  
28725         0         0       0          0  
28726         0         0       0          0  
28727         0         0       0          0  

[9576 rows x 17 columns]
```

**The score went down.** The feature with a 29× separation behind it made the model
worse, and if this were a real project you would now be arguing about whether to keep
it.

## The reason is that the test fold has seven rows to be right about

```python
detail = report("naive features", NAIVE)
faults = detail[detail["is_fault"] == 1]
print()
by_fault = faults.groupby("fault_type")["predicted"].agg(labelled="size", caught="sum")
print(by_fault.to_string())
```

```output
naive features         recall  0.429  precision  1.000  F1  0.600

                    labelled  caught
fault_type                          
do_sensor_drift            4       3
effluent_tss_stuck         2       0
sensor_dead                1       0
```

**Seven fault hours.** The model caught 3 of 4 drifts, 0 of 2 stuck sensors, and 0 of
1 dead sensor. Change which single hour is in the test fold and the drift line alone
moves the F1 by more than a tenth.

So run the same model, the same features, twenty times, and change *only* the split:

```python
import numpy as np

labels = panel["is_fault"].to_numpy()
rows = np.arange(len(panel))

def spread(features, label):
    matrix = panel[features].fillna(-1).to_numpy()
    scores = []
    for seed in range(20):
        shuffled = np.random.default_rng(seed).permutation(rows)
        cut = int(0.8 * len(panel))
        tr, te = shuffled[:cut], shuffled[cut:]
        model = RandomForestClassifier(n_estimators=200, random_state=0, n_jobs=-1)
        model.fit(matrix[tr], labels[tr])
        scores.append(f1_score(labels[te], model.predict(matrix[te]), zero_division=0))
    scores = np.array(scores)
    print(f"{label:<24} F1 {scores.min():.3f} to {scores.max():.3f}"
          f"   mean {scores.mean():.3f}   range {scores.max() - scores.min():.3f}")
    return scores

naive_scores = spread(NAIVE, "naive features")
baseline_scores = spread(BASELINE, "with per-signal baseline")

difference = abs(baseline_scores.mean() - naive_scores.mean())
widest = max(naive_scores.max() - naive_scores.min(),
             baseline_scores.max() - baseline_scores.min())
print()
print(f"difference between the two means : {difference:.3f}")
print(f"widest range inside one of them  : {widest:.3f}")
print(f"so the split noise is {widest / difference:.0f}x the thing being measured")
```

```output
naive features           F1 0.000 to 0.857   mean 0.345   range 0.857
with per-signal baseline F1 0.000 to 0.750   mean 0.365   range 0.750

difference between the two means : 0.020
widest range inside one of them  : 0.857
so the split noise is 42x the thing being measured
```

Read those numbers carefully.

The variation from *which hours you happened to test on* is about **forty times**
larger than the difference between the two feature sets. The two configurations are
not distinguishable, and neither is anything else you would try on a panel with 22
positives in it.

## So what was the 29× for?

The separation is real — it is arithmetic on the panel, and it does not depend on a
split. What does not survive is the *claim* that the feature improves a score, and
that claim needs more positive examples than this dataset has.

This is worth sitting with, because the instinct is to keep tuning until one split
looks good. That is how you end up with a feature that scores 0.800 on the fold you
tried and 0.000 on the next one, and a number in a slide that nobody can reproduce.

The honest position is:

- the mechanism is understood and does not depend on a split — an instrument that
  logged 596 rows a day and then logged none is broken, and an instrument that logs
  20 a day and then logs none is not;
- the *measurement* is not yet possible on 22 positives, and no amount of modelling
  fixes that. It is a property of the sample, not of the model.

```python
import math

whole = int(labels.sum())
per_fold = whole * 0.2
print(f"positives in the whole panel        : {whole}")
print(f"in a 20% test fold                  : {per_fold:.0f} on average")
print("for a recall to +/-0.10 you want at least 10 positives in the fold,")
print(f"so the panel needs about            : {math.ceil(10 / 0.2):.0f} positives")
print(f"this panel has                      : {whole}")
print(f"so it is short by a factor of        : {math.ceil(10 / 0.2) / whole:.1f}")
```

```output
positives in the whole panel        : 22
in a 20% test fold                  : 4 on average
for a recall to +/-0.10 you want at least 10 positives in the fold,
so the panel needs about            : 50 positives
this panel has                      : 22
so it is short by a factor of        : 2.3
```

## Takeaways

- **A per-signal baseline is the right feature, and the reason is arithmetic rather
  than a score**: 29× separates a fault hour from a quiet one where no other column
  separates them at all.
- **`shift(1)` or it does not count.** A window that includes the hour it describes
  is the hour labelling itself.
- **A score that moves with the split is not measuring the feature.** Here the
  within-configuration spread is about 40× the between-configuration difference, so
  the experiment has no power at all.
- **Twenty-two positives cannot support a claim about a feature.** You want at least
  ten positive examples in the *test* fold for a recall figure to mean anything, so
  the panel needs about 50; it has 22, and a random split leaves about 4.
- **Read the per-class breakdown, not the aggregate.** "F1 0.600" was 3 of 4 drifts,
  0 of 2 stuck and 0 of 1 dead. The aggregate hid a class the model has never found.
- Next: a number that *can* be improved without more data — and the one that is not.
