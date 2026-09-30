# 01 — A model that learned nothing scores 99.93%

Load the panel, fit the laziest model there is, and get a number that would pass
review. This notebook is about that number, and about what it is not.

## What you need

The panel, built by `make workshop-dataset` — a CSV, no database, no Docker. If it
is not there, the README says how to get it.

## The data

```python
from workshops.ml._data import load_panel, summarise

panel = load_panel()
print(summarise(panel))
```

```output
28,728 rows = 57 signals x 504 hours
  positives       22 (0.0766%); majority-class score 0.9992
  by fault        {'do_sensor_drift': 10, 'effluent_tss_stuck': 8, 'sensor_dead': 4}
  storm hours     114
  no row written  11,635 (40.5%)
```

One row per signal per hour. `is_fault` is the label: an hour in which the seeder
broke that instrument.

**Twenty-two positives in 28,728 rows.** Before we fit anything, that number is the
most important thing on the screen, and almost nobody looks at it.

Two other lines deserve a moment now, before we get to them:

- **`no row written  11,635 (40.5%)`** — two fifths of the hours have no row at
  all. That is not a bug and it is not missing data. It is what a historian that
  writes on change does to a plant with quiet instruments, and it is the subject of
  notebooks 02 and 03.
- **`by fault`** — only three fault kinds exist, and they are not equally common.
  A classifier that scores well here may have learned one of them and ignored the
  other two. Notebook 04 looks at that.

## The laziest model there is

`DummyClassifier` does not learn anything. It looks at the training labels, sees
that "not a fault" is the common answer, and answers that forever. It is the floor
every real model has to clear.

```python
from workshops.ml._data import FEATURES, held_out

train, test = held_out(panel, last=1)
print(f"train {len(train):,} rows ({train['is_fault'].sum()} faults), "
      f"test {len(test):,} rows ({test['is_fault'].sum()} faults)")
print(f"features: {', '.join(FEATURES)}")
```

```output
train 19,152 rows (15 faults), test 9,576 rows (7 faults)
features: mean, min, max, n, row_written, value_is_null, base_24, n_over_base, missing_streak
```

The split is by **week**, and it is by week deliberately. A random split of a time
series puts rows from *after* the test hour into the training set, and notebook 03
measures exactly what that buys you on this panel. For now, a time split: every
week up to the last, then the last.

```python
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, recall_score


def score(name, model):
    model.fit(train[FEATURES], train["is_fault"])
    predicted = model.predict(test[FEATURES])
    print(f"{name:<26} accuracy {accuracy_score(test['is_fault'], predicted):.4f}"
          f"   recall {recall_score(test['is_fault'], predicted, zero_division=0):.4f}")
    return predicted

score("DummyClassifier", DummyClassifier(strategy="most_frequent"))
predicted = score("RandomForest",
                  RandomForestClassifier(n_estimators=200, random_state=0, n_jobs=-1))
```

```output
DummyClassifier            accuracy 0.9993   recall 0.0000
RandomForest               accuracy 0.9995   recall 0.4286
```

Read that again. **A model that learned nothing scores 0.9993. The forest scores
0.9995** — *better* — and catches 3 of the 7 faults.

That is the worst version of this trap, and worth sitting with. The real model
**wins** on the metric. If you ranked these two by accuracy you would pick the
forest, and you would be right to. But the forest is wrong about 4 faults in 7 and
false-alarms on 1 hour in 4, while the dummy is wrong in a way nobody would ship:
it never detects anything, ever.

The two accuracies differ in the fourth decimal place, on a test set of 9,576
hours. Every decision you would make on that difference is a decision made on
noise.

## What the accuracy is made of

```python
truth = test["is_fault"].to_numpy()
correct = truth == predicted

print(f"correct on ordinary hours : {correct[truth == 0].mean():.4f}")
print(f"correct on fault hours    : {correct[truth == 1].mean():.4f}")
print()
share_normal = (truth == 0).mean()
share_fault = (truth == 1).mean()
print("accuracy is the weighted mean of those two:")
print(f"  {share_normal:.4f} x {correct[truth == 0].mean():.4f}"
      f"  +  {share_fault:.4f} x {correct[truth == 1].mean():.4f}"
      f"  =  {correct.mean():.4f}")
```

```output
correct on ordinary hours : 0.9999
correct on fault hours    : 0.4286

accuracy is the weighted mean of those two:
  0.9993 x 0.9999  +  0.0007 x 0.4286  =  0.9995
```

There it is. The 99.95% is almost entirely the first line. **Fault hours are
0.07% of the data, so they cannot move the score no matter how badly the model does
on them.** The forest gets **42.86%** of the fault hours right; a model that got
**0%** of them right would score 0.9992 — a difference of 0.0003.

A metric whose entire dynamic range is 0.0003 is not measuring the thing you care
about. It is measuring how common ordinary hours are, and that was never in
question.

## The same two models, honestly reported

```python
from sklearn.metrics import f1_score, precision_score

rows = []
for name, model in [("DummyClassifier", DummyClassifier(strategy="most_frequent")),
                    ("RandomForest", RandomForestClassifier(
                        n_estimators=200, random_state=0, n_jobs=-1))]:
    p = model.fit(train[FEATURES], train["is_fault"]).predict(test[FEATURES])
    rows.append((name,
                 accuracy_score(test["is_fault"], p),
                 recall_score(test["is_fault"], p, zero_division=0),
                 precision_score(test["is_fault"], p, zero_division=0),
                 f1_score(test["is_fault"], p, zero_division=0)))

print(f"{'model':<20}{'accuracy':>10}{'recall':>9}{'precis':>9}{'F1':>8}")
for name, acc, rec, prec, f1 in rows:
    print(f"{name:<20}{acc:>10.4f}{rec:>9.4f}{prec:>9.4f}{f1:>8.4f}")
```

```output
model                 accuracy   recall   precis      F1
DummyClassifier         0.9993   0.0000   0.0000  0.0000
RandomForest            0.9995   0.4286   0.7500  0.5455
```

Now the table says something. The forest has **recall 0.4286** — 3 of 7 — and
**precision 0.7500**, so one hour in four that it calls a fault is not one. A
detector in a plant looks like that: it misses more than half of what happened, and
it cries wolf often enough that an operator will start ignoring it.

`DummyClassifier` gets 0.0000 on all three, and the reason matters. It is not
"perfectly precise" — **it never spoke.** A precision of zero with a recall of zero
is not precision, it is silence, and in a metrics table the two are easy to read as
the same thing. Reporting only precision would make the model that does nothing look
flawless.

## Takeaways

- **0.9993 was free.** A `DummyClassifier` gets it by doing nothing, so it is
  evidence of nothing — and the real model *beat* it.
- **The base rate is the first thing to look at** — before the model, the features
  and the split. It sets the floor and it tells you what your metric has to beat.
- **Accuracy on an imbalanced problem is a weighted average of one thing you care
  about and one thing you do not**, and the weights are set by the data rather than
  by you.
- **Recall 0.4286 and precision 0.7500 is not a good detector.** It misses more
  than half the faults and false-alarms on a quarter of its calls, which is the
  combination that trains an operator to ignore the alarm.
- **Precision 0.0000 is not "perfectly precise".** Read recall beside it, always.

Next: why a frozen sensor produces *no rows at all*, and what that does to a
modelling set built by querying the database.
