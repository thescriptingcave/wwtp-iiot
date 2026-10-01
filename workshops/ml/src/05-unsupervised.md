# 05 — Anomaly detection finds the odd tag, not the odd hour

The plan for this notebook was that unsupervised methods would confidently find the
storm rather than the faults, because the storm is the largest variance. That is
**false**, and what actually happens is more useful.

No labels are used to fit anything here. They are used afterwards, to score what the
method found — which is the only honest way to ask the question.

## The methods

```python
import numpy as np
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from workshops.ml._data import FEATURES, load_panel

panel = load_panel()

features = StandardScaler().fit_transform(
    panel[FEATURES].astype(float)
    .replace([np.inf, -np.inf], np.nan)
    .fillna(panel[FEATURES].astype(float).median())
)

faults = panel["is_fault"].to_numpy().astype(bool)
storm = panel["is_storm"].to_numpy().astype(bool)
print(f"{len(panel):,} rows, {len(FEATURES)} features, "
      f"{int(faults.sum())} fault hours, {int(storm.sum())} storm hours")
```

```output
28,728 rows, 9 features, 22 fault hours, 114 storm hours
```

`contamination` is the fraction the method will call anomalous. The panel is 0.077%
faults, so 0.1% is the honest setting and 1% is the one people reach for.

```python
pca = PCA(n_components=3).fit(features)
rebuilt = pca.inverse_transform(pca.transform(features))
reconstruction_error = ((features - rebuilt) ** 2).sum(axis=1)

print(f"{'method':<24}{'flagged':>9}{'faults':>9}{'storm':>8}")
for name, flag in [
    ("IsolationForest 0.1%", IsolationForest(contamination=0.001, random_state=0,
        n_estimators=300, n_jobs=-1).fit(features).predict(features) == -1),
    ("IsolationForest 1%", IsolationForest(contamination=0.01, random_state=0,
        n_estimators=300, n_jobs=-1).fit(features).predict(features) == -1),
    ("PCA(3) top 0.1%",
     reconstruction_error >= np.quantile(reconstruction_error, 0.999)),
    ("PCA(3) top 1%",
     reconstruction_error >= np.quantile(reconstruction_error, 0.99)),
]:
    hits, stormy = int((flag & faults).sum()), int((flag & storm).sum())
    print(f"{name:<24}{int(flag.sum()):>9}{hits:>9}{stormy:>8}")
print(f"{'available to find':<24}{'':>9}{int(faults.sum()):>9}{int(storm.sum()):>8}")
```

```output
method                    flagged   faults   storm
IsolationForest 0.1%           29        0       0
IsolationForest 1%            288        0       2
PCA(3) top 0.1%                30        0       0
PCA(3) top 1%                 288        1       1
available to find                       22     114
```

**It finds neither.** Not the faults — zero or one out of twenty-two. Not the storm —
zero or two out of a hundred and fourteen. It finds 288 hours and almost none of them
are the events we care about.

## What it found instead

```python
flag = IsolationForest(contamination=0.01, random_state=0, n_estimators=300,
                       n_jobs=-1).fit(features).predict(features) == -1
flagged = panel[flag]

print(f"{flagged['signal_id'].nunique()} distinct signals flagged, out of "
      f"{panel['signal_id'].nunique()}\n")
for signal, count in flagged["signal_id"].value_counts().head(3).items():
    total = int((panel["signal_id"] == signal).sum())
    print(f"  {signal:<26} {count:>4} of {total:>4} hours ({count / total:.0%})")
print(f"\n  median rows in a flagged hour     : {flagged['n'].median():.0f}")
print(f"  median rows in an ordinary hour   : {panel.loc[~flag, 'n'].median():.0f}")
```

```output
2 distinct signals flagged, out of 57

  INFLUENT:LIFT:STARTS        285 of  504 hours (57%)
  AERATION:AHU-1:AIR_FLOW       3 of  504 hours (1%)

  median rows in a flagged hour     : 226
  median rows in an ordinary hour   : 2
```

**285 of the 288 flags are one signal** — `INFLUENT:LIFT:STARTS`, over half of its
own hours — and a flagged hour has a median of 226 rows against 2 for everything
else.

The method has not detected anything wrong. It has detected that one tag is unlike
the other fifty-six.

## Why

Look at what "unlike the others" means when the features are the row's own values and
counts. The panel has 57 signals; 56 of them are quiet most of the time, and one is
not. An anomaly method asked "is this row unusual?" and answered, correctly and
uselessly, "yes, this row is from the busy signal."

That is *cross-sectional* reasoning: comparing a row to the population. A fault is a
*within-signal* deviation: the signal is doing something it does not normally do. No
column in the table separates those two ideas, because from the row's own point of
view "LIFT:STARTS on an ordinary Tuesday" and "LIFT:STARTS at four in the morning
because the pump restarted" look like the same kind of row.

Which is why the feature from notebook 03 — the signal's own recent history — is the
one that matters, and why an unsupervised method given the same panel cannot find
its way there. It is being asked a question about the population, and the answer is
about the population.

## What to do about it

Two options, and they are not equally good.

**Leave the signal identity out and see if that helps.** Not because it fixes the
problem — it will not — but because it is the first thing to try and it teaches you
what the method is keying on.

```python
without_signal = IsolationForest(contamination=0.01, random_state=0, n_estimators=300,
                                 n_jobs=-1).fit(features).predict(features) == -1
panel.loc[without_signal, "signal_id"].value_counts().head(3).to_string()
```

```output
'signal_id\nINFLUENT:LIFT:STARTS       285\nAERATION:AHU-1:AIR_FLOW      3'
```

Still the same tag. The signal identity is not in the feature list — the method
rediscovered it from the values. That is the answer: **you cannot remove the signal
from the data, because every row carries it.**

**Give the method the per-signal reference**, and it becomes a different method.

```python
with_reference = panel.copy()
normal_rate = with_reference.groupby("signal_id")["n"].transform("median")
with_reference["n_over_own_median"] = (
    with_reference["n"] / normal_rate.replace(0, np.nan))
reference_features = StandardScaler().fit_transform(
    with_reference[["n", "n_over_own_median", "base_24", "row_written",
                    "value_is_null"]].astype(float).fillna(0)
)
ref_model = IsolationForest(contamination=0.01, random_state=0, n_estimators=300,
                            n_jobs=-1).fit(reference_features)
ref_flag = ref_model.predict(reference_features) == -1
print(f"flagged {int(ref_flag.sum()):>5}   faults {int((ref_flag & faults).sum()):>3}"
      f" of {int(faults.sum())}   storm {int((ref_flag & storm).sum()):>3}"
      f" of {int(storm.sum())}")
```

```output
flagged   275   faults   8 of 22   storm   1 of 114
```

One fault instead of zero. One storm instead of two. That is not a success either —
with 22 positives it is one hour either way — and the honest reading is that adding
the reference changed the method from *wrong question* to *right question with no
data to answer it with*.

## Takeaways

- **Anomaly detection finds the odd tag out, not the odd hour.** 285 of 288 flags
  were one signal, on 57% of its own hours.
- **The failure is a question, not a threshold.** The method was asked "is this row
  unusual?" and the faults are *this signal* being unusual. Those need different
  inputs.
- **You cannot drop the signal from the data.** Remove it from the feature list and
  the method rediscovers it from the values.
- **Adding the per-signal reference fixes the question and not the answer.** One fault
  found instead of zero, on a sample too small to call that an improvement.
- **Unsupervised is not a free pass.** It is a different set of assumptions, and here
  they were the wrong ones.
- Next: the same panel, a task that needs no labels and no anomaly detection.
