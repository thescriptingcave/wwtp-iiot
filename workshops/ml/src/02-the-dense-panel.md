# 02 — Two of the three faults left no rows

Notebook 01 fitted a model to this panel and it did not work very well. This one asks
why, and the answer is not the model.

## What you need

The panel, from `make workshop-dataset`. No database, no Docker.

## The panel

One row per signal per hour. `row_written` says whether the historian recorded
anything at all for that signal in that hour; `n` is how many readings it recorded.

```python
from workshops.ml._data import load_panel

panel = load_panel()
print(f"{len(panel):,} rows = {panel['signal_id'].nunique()} signals "
      f"x {panel['bucket'].nunique()} hours")
print(f"hours with no row written: {(panel['row_written'] == 0).sum():,} "
      f"({(panel['row_written'] == 0).mean():.1%})")
```

```output
28,728 rows = 57 signals x 504 hours
hours with no row written: 11,635 (40.5%)
```

**Two fifths of the hours have no row.** Not a null, not a zero — no row, because
this historian writes a reading when the value *changes*, and 40% of the time it does
not.

## What that costs you

The obvious way to build a modelling set is to query the table. Here is what that
gives you, expressed on the panel by throwing away the hours where nothing was
written — which is exactly what a query cannot do for you.

```python
stored = panel[panel["row_written"] == 1]
print(f"a query of the stored table returns {len(stored):,} rows, not {len(panel):,}")

faults = panel[panel["is_fault"] == 1]
survivors = stored[stored["is_fault"] == 1]
print(f"fault hours in the panel          : {len(faults)}")
print(f"fault hours a query would keep    : {len(survivors)}")
print()
print("what is left, by kind:")
print(faults["fault_type"].value_counts().to_string())
print()
print("what survives a query, by kind:")
print(survivors["fault_type"].value_counts().to_string())
```

```output
a query of the stored table returns 17,093 rows, not 28,728
fault hours in the panel          : 22
fault hours a query would keep    : 10

what is left, by kind:
fault_type
do_sensor_drift       10
effluent_tss_stuck     8
sensor_dead            4

what survives a query, by kind:
fault_type
do_sensor_drift    10
```

**Twelve of the twenty-two fault hours simply do not exist in a queried table.** Two
of the three fault kinds are gone entirely.

This is not a small-sample problem. A model fitted on the queried set is not a weak
model — it is a model that has never been shown a stuck sensor or a dead one, and
every number it reports is a statement about the sample rather than about the plant.

## Why those two faults leave nothing behind

A frozen sensor reports the same value as the last good reading, and a dead one
reports nothing. Neither is a *change*, so a change-triggered historian writes
nothing, and the evidence that the instrument is broken is the **absence of a row**.

```python
for kind, group in faults.groupby("fault_type"):
    print(f"{kind:<20} hours {len(group):>3}  row_written "
          f"{group['row_written'].mean():>4.0%}  n {group['n'].median():>3.0f}")
```

```output
do_sensor_drift        hours  10  row_written  100%  n  17
effluent_tss_stuck     hours   8  row_written    0%  n   0
sensor_dead            hours   4  row_written    0%  n   0
```

The drift is the odd one out, and for an interesting reason. Its value is *moving* —
that is what a drift is — so the historian keeps writing, and the fault is the
**loudest** thing in the data: a median of 17 readings an hour where an ordinary quiet
hour has 2.

A detector that looks for silence finds the stuck sensor and misses the drift. One
that looks for noise finds the drift and fires on every diurnal swing. Neither
description is wrong, and they are not the same problem.

## The trap, which is the part to take away

You could now decide that empty hours are noise, drop them, and move on. Before you
do:

```python
empty = panel[panel["row_written"] == 0]
ordinary = panel[panel["is_fault"] == 0]

print(f"empty hours                     : {len(empty):,}")
print(f"  of which a fault hour         : {int(empty['is_fault'].sum())} "
      f"({empty['is_fault'].mean():.2%})")
print()
empty_ordinary = int((ordinary["row_written"] == 0).sum())
print(f"ordinary hours                  : {len(ordinary):,}")
print(f"  of which also empty           : {empty_ordinary:,} "
      f"({ordinary['row_written'].eq(0).mean():.0%})")
```

```output
empty hours                     : 11,635
  of which a fault hour         : 12 (0.10%)

ordinary hours                  : 28,706
  of which also empty           : 11,623 (40%)
```

**Only 0.10% of empty hours are faults.** Absence, on its own, finds almost nothing —
and 40% of perfectly healthy hours look exactly like a broken instrument.

So the panel's `row_written` column is not a data-quality note. It is a *measurement*,
and for two of the three faults it is the only measurement there is. Dropping it as
missingness discards the signal and keeps the noise.

## The last twist

`sensor_dead` and `effluent_tss_stuck` both leave `row_written = 0` and
`value_is_null = 1`. They are in the *same state*, and nothing in the stored rows
distinguishes them — an instrument that stopped answering and one that stopped
changing both produce nothing at all.

```python
broken = faults[faults["fault_type"] != "do_sensor_drift"]
print(broken.groupby("fault_type")[["row_written", "value_is_null"]]
      .agg(["min", "max"]).to_string())
```

```output
                   row_written     value_is_null    
                           min max           min max
fault_type                                          
effluent_tss_stuck           0   0             1   1
sensor_dead                  0   0             1   1
```

To tell those two apart you need something the row does not contain. That is the
subject of the next notebook, and it is the whole of the answer.

## Takeaways

- **A query of the table is not the same data as the event log.** It is the event log
  with its quietest events removed, and the quiet ones are two thirds of your faults.
- **Densify the panel before you model it.** Cross the signals with the hours and
  left-join; an absent hour becomes a row with `n = 0`.
- **`dropna()` is not a cleanup here — it is the deletion of the finding.** 0.10% of
  empty hours are faults, and 40% of ordinary hours are empty.
- **A fault can be loud.** A drifting instrument writes *more* rows than a healthy
  one, and looking only for silence will miss it.
- **Two faults can share a state**, and when they do, the row cannot separate them.
- Next: the per-signal baseline — the one feature that finds both.
