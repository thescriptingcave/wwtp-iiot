# 04 — The only feature that moves the score is the one you cannot use

Notebook 03 ended with a measurement too weak to support a claim. This one looks for
a feature that *can* move the number, finds one, and then has to explain why it is
worthless.

## First, what the data says on its own

Before any model: a fault hour and a quiet hour, side by side, with each signal's
own recent rate.

```python
from workshops.ml._data import load_panel

panel = load_panel().sort_values(["signal_id", "bucket"]).reset_index(drop=True)

n = panel.groupby("signal_id")["n"]
panel["base_24"] = n.transform(lambda s: s.shift(1).rolling(24, min_periods=6).median())
panel["n_over_base"] = panel["n"] / panel["base_24"].replace(0, float("nan"))
panel["streak"] = n.transform(lambda s: s.eq(0).groupby((~s.eq(0)).cumsum()).cumsum())
panel["base_24_fwd"] = n.transform(
    lambda s: s.shift(-1).rolling(24, min_periods=6).median())

CAUSAL = ["mean", "min", "max", "n", "row_written", "value_is_null",
          "base_24", "n_over_base", "streak"]

def summarise(frame, label):
    if label == "quiet empty":
        group = frame[(frame["n"] == 0) & (frame["is_fault"] == 0)]
    else:
        group = frame[frame["fault_type"] == label]
    return (len(group), group["n"].median(), group["base_24"].median(),
            group["base_24_fwd"].median())

header = f"{'':<20}{'hours':>7}{'n':>6}{'base_24':>10}{'base_24_fwd':>13}"
print(header)
for label in ("do_sensor_drift", "effluent_tss_stuck", "sensor_dead", "quiet empty"):
    h, med_n, past, future = summarise(panel, label)
    print(f"  {label:<18}{h:>7}{med_n:>6.0f}{past:>10.0f}{future:>13.0f}")
```

```output
                      hours     n   base_24  base_24_fwd
  do_sensor_drift         10    17         1            2
  effluent_tss_stuck       8     0       891          890
  sensor_dead              4     0         6            4
  quiet empty           11627     0         0            0
```

Read the last two columns together, because the last row is the key.

**A quiet empty hour had a quiet day yesterday too** — `base_24` is 0, because the
signal does not write much and yesterday was the same. A fault hour did not: 891 rows
a day for a stuck effluent sensor, 6 for a dead lift current, and for the drift, 1
against a current of 17.

So the causal rule looks obvious: *silent now, and not usually silent* is a fault.
State it explicitly and see.

```python
panel["was_busy"] = ((panel["n"] == 0) & (panel["base_24"].fillna(0) > 0)).astype(int)

flagged = panel[panel["was_busy"] == 1]
print(f"hours the rule flags : {len(flagged):,}")
print(f"  of which faults    : {int(flagged['is_fault'].sum())}")
print(f"  precision          : {flagged['is_fault'].mean():.4f}")
print(f"  the panel's base rate is {panel['is_fault'].mean():.4f}, so this is "
      f"{flagged['is_fault'].mean() / panel['is_fault'].mean():.0f}x better")
```

```output
hours the rule flags : 1,956
  of which faults    : 12
  precision          : 0.0061
  the panel's base rate is 0.0008, so this is 8x better
```

**Eight times better than guessing, and still useless** — 1,944 false alarms. Silence
on a signal that was usually busier is common enough in a three-week window to be
worthless on its own.

## The forward window

One more column: the same 24-hour median, but looking *ahead*.

```python
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score, precision_score, recall_score

from workshops.ml._data import held_out

train, test = held_out(panel, last=1)
print(f"held-out final week: {len(test):,} rows, "
      f"{int(test['is_fault'].sum())} fault hours\n")

def report(name, features):
    model = RandomForestClassifier(n_estimators=300, random_state=0, n_jobs=-1)
    model.fit(train[features].fillna(-1), train["is_fault"])
    predicted = model.predict(test[features].fillna(-1))
    truth = test["is_fault"]
    print(f"{name:<34} recall {recall_score(truth, predicted):>6.3f}"
          f"  precision {precision_score(truth, predicted, zero_division=0):>6.3f}"
          f"  F1 {f1_score(truth, predicted, zero_division=0):>6.3f}")
    return test.assign(predicted=predicted)

causal = report("causal features", CAUSAL)
cheating = report("causal + a FORWARD window", [*CAUSAL, "base_24_fwd"])
```

```output
held-out final week: 9,576 rows, 7 fault hours

causal features                      recall  0.286  precision  1.000  F1  0.444
causal + a FORWARD window            recall  0.714  precision  1.000  F1  0.833
```

**F1 nearly doubles, precision stays perfect, and there are no false alarms at all.**
It is the best number anyone will see in this workshop.

And it finds the two faults nothing else found:

```python
for label, frame in (("causal", causal), ("with the forward window", cheating)):
    faults = frame[frame["is_fault"] == 1]
    print(f"--- {label} ---")
    caught = faults.groupby("fault_type")["predicted"].agg(
        labelled="size", caught="sum")
    print(caught.to_string())
    print(f"false alarms: {int(frame.loc[frame['is_fault'] == 0, 'predicted'].sum())}"
          f" of {int((frame['is_fault'] == 0).sum()):,}")
    print()
```

```output
--- causal ---
                    labelled  caught
fault_type                          
do_sensor_drift            4       2
effluent_tss_stuck         2       0
sensor_dead                1       0
false alarms: 0 of 9,569

--- with the forward window ---
                    labelled  caught
fault_type                          
do_sensor_drift            4       2
effluent_tss_stuck         2       2
sensor_dead                1       1
false alarms: 0 of 9,569
```

The stuck sensor goes from 0 of 2 to 2 of 2. The dead one from 0 of 1 to 1 of 1.
**Zero false alarms in 9,569 ordinary hours.**

## Why it works, and why that is the problem

```python
quiet = panel[(panel["n"] == 0) & (panel["is_fault"] == 0)]
broken = panel[panel["is_fault"] == 1]
print("fault hours with a non-zero forward window : "
      f"{(broken['base_24_fwd'] > 0).mean():.0%}")
print("quiet empty hours with a non-zero one     : "
      f"{(quiet['base_24_fwd'] > 0).mean():.0%}")
```

```output
fault hours with a non-zero forward window : 100%
quiet empty hours with a non-zero one     : 16%
```

Every fault hour has *future* hours where the signal is writing again. A quiet hour
usually has future quiet hours.

The feature is asking "does this instrument recover?" — and for a sustained fault the
answer is no. That is genuinely informative, and it is the reason this window works
where the causal ones did not.

**It also cannot be deployed.** At 03:00, when your instrument has been silent for
an hour and you are deciding whether to raise an alarm, tomorrow's row count does not
exist. A detector that reads the future to decide about the past is not a detector,
and the fact that it scores 0.833 with zero false alarms is exactly what makes it
tempting.

## The honest position

Three things, and the third is the one to take away.

1. **The mechanism is understood and does not need the future.** A stuck instrument
   is a signal that wrote 891 rows a day and now writes none. That is a fact about
   the past.
2. **The measurement does not support the causal features**, for the reason in
   notebook 03: 22 positives, four in a test fold, and a split-to-split spread forty
   times the effect.
3. **So the best number on this panel was obtained by cheating, and the second-best
   by guessing.** Neither is a result about the plant. The panel cannot yet answer
   the question, and the honest response to a number you cannot trust is to say so,
   not to keep the one that flattered you.

What would fix it is not a better model. It is more faults: a 25-week window carries
116 fault instances instead of 22, which puts about 23 in a test fold rather than 4,
and at that point the causal baseline either works or it does not, and you will know
which.

## Takeaways

- **A quiet hour and a fault hour differ in their own history**, and the history is in
  the past: 891 rows a day, or 6, or 1 — against 0 for a genuinely quiet tag.
- **Stating the rule explicitly does not rescue it.** "Silent and usually not silent"
  is eight times better than the base rate and still produces 1,944 false alarms.
- **A forward window is the strongest feature on this panel and the only one you
  cannot ship.** F1 0.833, precision 1.000, zero false alarms — and it needs tomorrow.
- **The best score is not the truest one.** Check every feature for a direction in
  time before you believe an improvement.
- **When the measurement is too weak, the answer is more data, not a better model.**
  Next: what a method that needs no labels at all finds here.
