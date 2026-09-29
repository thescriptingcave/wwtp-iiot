---
title: "02 — Three kinds of nothing"
subtitle: "No data, bad data, and no change are three different facts, and `dropna` cannot tell them apart"
---

# 02 — Three kinds of nothing

## The question

> A signal has gaps. The gaps need handling. How many kinds of gap are there, and
> does the handling differ?

The obvious answer is one: *no data*. And the obvious handling is one line:

<!-- illustrative: the line this notebook is about, not a step -->
<!-- check: skip -->
```output
df.dropna()
```

That line is the subject of this notebook, because on this dataset it is wrong in
a way that is invisible in the output — it produces a number, the number is
plausible, and nothing tells you what it cost you.

## Setup

Two tables matter. `signal` holds one row per tag — identity, unit, range, and
the **declared** sample rate. `reading` holds the values. They are separate
because the metadata is stored 57 times instead of four million, and because
`value` can be NULL, which a flat tag table could not express.

```python
import matplotlib.pyplot as plt
import pandas as pd
import psycopg
from storage.postgres.schema import dsn

from notebooks._style import apply_style, save, signal_meta, stamp, trend

apply_style()
pd.set_option("display.width", 130)

conn = psycopg.connect(dsn())

signals = pd.read_sql("SELECT * FROM signal ORDER BY area, id", conn)
readings = pd.read_sql(
    """
    SELECT signal_id, ts, value, quality
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '7 days'
    """,
    conn,
)

print(f"{len(readings):,} readings across "
      f"{readings.signal_id.nunique()} signals, one week")
print(f"quality counts: {readings.quality.value_counts().to_dict()}")
```

```output
4,287,657 readings across 57 signals, one week
quality counts: {0: 4287620, 1: 36, 2: 1}
```

**A week, 4.29 million rows, and exactly one of them is `Bad`.** Hold that
number. Everything in this notebook is about the other 4,287,656.

## The three kinds

There are three ways a signal can fail to give you a number, and they mean
different things.

| kind | what happened | is the process fine? | is the number there? |
|---|---|---|---|
| **no data** | nothing was recorded | unknown | no |
| **bad data** | something *was* recorded, flagged | unknown | no — but flagged |
| **no change** | the value did not move, so nothing was stored | **yes, demonstrably** | **yes — it's just the last one** |

The third is the one that catches people, and it is not an edge case here. This
historian is **change-triggered**: a row is written when a value *differs* from the
last one, not on a schedule. So a signal that is behaving perfectly is a signal
that is mostly absent.

## Kind one: no data, thirteen times over

Count the quiet signals:

```python
per_signal = readings.groupby("signal_id").agg(
    rows=("value", "size"),
    with_value=("value", "count"),
    distinct=("value", "nunique"),
    worst_quality=("quality", "max"),
)
per_signal["no_value"] = per_signal.rows - per_signal.with_value

quiet = per_signal[per_signal.rows <= 5]
print(f"signals with 5 or fewer rows in a week: {len(quiet)}")
print()
print(quiet.join(signals.set_index("id")[["field", "unit", "sample_ms"]]).to_string())
```

```output
signals with 5 or fewer rows in a week: 13

                        rows  with_value  distinct  worst_quality no_value              field       unit  sample_ms
AERATION:AHU-1:MLSS         1           1         1              0        0       mlss_mg_l       mg/L       60000
AERATION:AHU-1:SETPOINT_DO  1           1         1              0        0  setpoint_do_mg_l       mg/L        5000
AERATION:AHU-1:SRT          1           1         1              0        0             srt_d          d     3600000
EFFLUENT:FLOW:CONDUCTIVITY  1           1         1              0        0   conduct_uS_cm      uS/cm        5000
INFLUENT:FLOW:CONDUCTIVITY  1           1         1              0        0   conduct_uS_cm      uS/cm        5000
PRIMARY:PRI-CL-1:BLANKET    1           1         1              0        0        blanket_m         m        2000
PRIMARY:PRI-CL-1:TEMP       1           1         1              0        0           temp_c       Cel       10000
SECONDARY:SEC-CL-1:BLANKET  1           1         1              0        0        blanket_m         m        2000
SITE:WEATHER:BARO           1           1         1              0        0         baro_hpa      hPa       60000
SITE:WEATHER:RAIN           1           1         1              0        0       rain_mm_h     mm/h       60000
SITE:WEATHER:STORM          1           1         1              0        0       storm_flag {Boolean}        1000
SLUDGE:DIG-1:TEMP           1           1         1              0        0           temp_c       Cel       60000
SLUDGE:THK-1:TS             1           1         1              0        0            ts_pct         %       10000
```

**Thirteen of fifty-seven signals reported once in a week.** Not zero times, not
twice — *once*, and all thirteen at the same instant:

```python
q = readings[readings.signal_id.isin(quiet.index)]
print(f"those 13 rows share {q.ts.nunique()} timestamp: {q.ts.iloc[0]}")
print(f"which is {q.ts.iloc[0] - readings.ts.min()} into the dataset")
```

```output
those 13 rows share 1 timestamp: 2026-09-22 01:43:25.774697+00:00
which is 0 days 00:00:00 into the dataset
```

The opening value, and nothing since. Look at the `sample_ms` column: the
contract declares a rate between 1 second and 1 hour for each of these tags, and
`AERATION:AHU-1:SRT` — sludge retention time, declared at **3,600,000 ms**, one
sample per hour — reported once in a week.

**So what happened?** Either the value has not moved by more than its deadband for
seven days, or the instrument is dead and nobody noticed. Both are consistent with
what is in the table. The table does not say.

## Kind two: bad data, exactly once

One row in 4.29 million has a value that is genuinely absent, and it is flagged.
That is the seeder's `sensor_dead` fault — an instrument that stopped answering,
stored as `value = NULL, quality = 2`.

```python
dead = readings[readings.quality >= 2]
print(dead.to_string(index=False))
print()
signal = dead.signal_id.iloc[0]
meta = signal_meta(signal)
print(f"signal : {signal}")
print(f"field  : {meta['field']}  [{meta['unit']}]")
print(f"row    : value={dead.value.iloc[0]}  quality={dead.quality.iloc[0]}")
```

```output
            signal_id                        ts  value  quality
INFLUENT:LIFT:CURRENT 2026-09-27 12:45:48.774697+00:00   NaN        2

signal : INFLUENT:LIFT:CURRENT
field  : current_a  [A]
row    : value=nan  quality=2
```

**The value is NULL and the quality is 2.** The instrument said "I have no number"
and the reason is in the row, not in a gap.

The two aggregates are not interchangeable, which is the whole point of the
`quality` column:

```python
row = readings[readings.signal_id == signal]
print(f"count(*)     — rows that happened  : {len(row)}")
print(f"count(value) — rows you can use    : {row.value.count()}")
print(f"difference   — instrument was dead : {len(row) - row.value.count()}")
```

```output
count(*)     — rows that happened  : 31182
count(value) — rows you can use    : 31181
difference   — instrument was dead : 1
```

`count(*)` counts the row. `count(value)` skips it. The difference is one, and it
is the only place in four million rows where anyone recorded an instrument
failure.

**And note what `dropna` would do to that row.** It would delete it — and with it
the only evidence in the database that a lift stopped reporting.

## Kind three: no change, 30,718 times

The third kind is not a gap at all, which is why it is the dangerous one. Find the
signals that report often but vary hardly:

```python
busy = per_signal[per_signal.rows > 1000].nsmallest(4, "distinct")
print(busy.join(signals.set_index("id")[["field", "unit", "sample_ms"]]).to_string())
```

```output
                              rows  with_value  distinct  worst_quality  no_value                 field  unit  sample_ms
PRIMARY:PRI-CL-1:UNDERFLOW   30718       30718         4              0         0  underflow_solids_pct     %        5000
INFLUENT:LIFT:CURRENT        31182       31181       311              2         1             current_a     A        1000
INFLUENT:FLOW:FLOW            2570        2570       912              0         0                 flow_m3h   m3/h        1000
INFLUENT:LIFT:FLOW          131996      131996      1238              0         0                 flow_m3h   m3/h        1000
```

**`PRIMARY:PRI-CL-1:UNDERFLOW`: 30,718 readings, and 4 distinct values.**

That is a signal that reported every five seconds for seven days and moved four
times. It is a primary clarifier underflow **solids percentage**, on a 0.5–4.0 %
scale, and it is the *healthiest* signal in the table by every measure a naive
analyst would use: high row count, no gaps, no NULLs, quality `Good` throughout.

And it is also the one you would most want to question.

Plot it, with a second signal underneath for contrast:

```python
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True)

underflow = readings[readings.signal_id == "PRIMARY:PRI-CL-1:UNDERFLOW"]
underflow_h = underflow.set_index("ts").resample("1h")["value"].mean()
trend(ax1, {"underflow": underflow_h}, "PRIMARY:PRI-CL-1:UNDERFLOW")
stamp(ax1, underflow_h, window="1 h", source="reading (1 h buckets)", expected_s=1)
ax1.set_title(
    f"PRIMARY:PRI-CL-1:UNDERFLOW — {len(underflow):,} rows, "
    f"{underflow.value.nunique()} distinct values", loc="left")

flow = readings[readings.signal_id == "INFLUENT:FLOW:FLOW"]
flow_h = flow.set_index("ts").resample("1h")["value"].mean()
trend(ax2, {"influent flow": flow_h}, "INFLUENT:FLOW:FLOW")
stamp(ax2, flow_h, window="1 h", source="reading (1 h buckets)", expected_s=1)
ax2.set_title("INFLUENT:FLOW:FLOW — same plot, a signal that varies", loc="left")

save(fig, "02_three_kinds")
```

The two panels are the same three lines of code with a different signal id. The
top one is a step function; the bottom is a signal doing its job. **Nothing in
the top panel says which it is.**

## The fabrication, done properly

The thirteen quiet signals are *not* the interesting case, and it is worth seeing
why before moving on. `ffill` cannot fabricate forward from a reading that sits at
the very start of the dataset — there is nothing after it to fill:

```python
mlss = readings[readings.signal_id == "AERATION:AHU-1:MLSS"]
mlss_h = mlss.set_index("ts")["value"].resample("1h").mean()
print(f"{mlss.value.iloc[0]:.2f} mg/L, read {len(mlss)} time(s)")
print(f"hourly buckets resample produced: {len(mlss_h)}")
print(f"with a real value:               {int(mlss_h.notna().sum())}")
print(f"after ffill:                     {int(mlss_h.ffill().notna().sum())}")
```

```output
3000.00 mg/L, read 1 time(s)
hourly buckets resample produced: 1
with a real value:               1
after ffill:                     1
```

**The resample produced one bucket, not 168.** `resample` spans the data it is
given, and this data is one reading — so there is no week-long grid here to fill
at all. `ffill` leaves it alone, and a reader might reasonably conclude the danger
is theoretical. It is not. It needs a signal that reports occasionally
rather than once, and `PRIMARY:PRI-CL-1:UNDERFLOW` is exactly that:

```python
underflow = readings[readings.signal_id == "PRIMARY:PRI-CL-1:UNDERFLOW"]
u_h = underflow.set_index("ts")["value"].resample("1h").mean()
u_filled = u_h.ffill()

runs = u_filled.groupby((u_filled != u_filled.shift()).cumsum()).size()
print(f"raw readings          : {len(underflow):,} over 7 days")
print(f"distinct values       : {underflow.value.nunique()}")
print(f"1h buckets with data  : {int(u_h.notna().sum())} of {len(u_h)}")
print(f"after ffill           : {int(u_filled.notna().sum())} of {len(u_h)}")
print(f"longest run of one value, in hours: {runs.max()}")
```

```output
raw readings          : 30,718 over 7 days
distinct values       : 4
1h buckets with data  : 50 of 165
after ffill           : 165 of 165
longest run of one value, in hours: 23
```

**This is the whole notebook in one line.** Fifty measurements become 165 — every
bucket in the signal's own span — and one of the four values is held for **23
consecutive hours**, which is a day of a chart showing a flat line that is one
reading drawn forward. Nothing marks which hour is real.

Compare it with a signal that genuinely varies, treated identically:

```python
lift = readings[readings.signal_id == "INFLUENT:LIFT:FLOW"]
l_h = lift.set_index("ts")["value"].resample("1h").mean()
l_filled = l_h.ffill()
l_runs = l_filled.groupby((l_filled != l_filled.shift()).cumsum()).size()
print("INFLUENT:LIFT:FLOW - same code, same ffill")
print(f"  1h buckets with data : {int(l_h.notna().sum())} of {len(l_h)}")
print(f"  after ffill          : {int(l_filled.notna().sum())} of {len(l_h)}")
print(f"  longest run          : {l_runs.max()} h")
```

```output
INFLUENT:LIFT:FLOW — same code, same ffill
  1h buckets with data : 169 of 169
  after ffill          : 169 of 169
  longest run          : 8 h
```

**169 of 169 before and after.** On a signal that actually reports, `ffill` is
harmless — it fills almost nothing, and the longest flat run is 8 hours, which is
the signal being steady rather than stuck.

Both charts look like a line. One is 115 fabricated hours and one is a
measurement. **The two are indistinguishable after the fill, which is exactly why
the fill has to record what it did.**

## The count is the answer

Take the count *before* any filling, from the rows that exist:

```python
u_honest = (
    underflow.set_index("ts")
    .resample("1h")
    .agg(rows=("value", "size"), mean=("value", "mean"))
)
print(f"the same {len(u_honest)} buckets, counted rather than filled:")
print(f"  buckets with at least one reading : {int((u_honest.rows > 0).sum())}")
print(f"  buckets ffill would have invented : {int((u_honest.rows == 0).sum())}")
print(f"  buckets with more than 4 readings: {int((u_honest.rows > 4).sum())}")
```

```output
the same 165 buckets, counted rather than filled:
  buckets with at least one reading : 50
  buckets ffill would have invented : 115
  buckets with more than 4 readings: 49
```

**115 invented hours, and the count says so.** The `mean` column alone cannot
distinguish them from the 50 real ones; `rows` can, and it costs nothing.

Note the third line too: 49 of the 50 buckets that *do* have data hold more than
four readings. So the problem is not a thin sample — it is 115 buckets with no
sample at all, sitting between them, indistinguishable in the `mean` column.

That is the rule for the rest of this series: **carry the count next to every
aggregate.** `rows`, `distinct` and `bad` are three extra columns, and between
them they answer the three questions this database can answer about its own
completeness.

## The count is the answer

Take the count *before* any filling, from the rows that exist:

```python
rows_present = (
    readings[readings.signal_id.isin(quiet.index)]
    .groupby("signal_id")
    .size()
)
print("actual rows in the whole week:")
print(rows_present.to_string())
```

```output
actual rows in the whole week:
AERATION:AHU-1:MLSS              1
AERATION:AHU-1:SETPOINT_DO        1
AERATION:AHU-1:SRT                1
EFFLUENT:FLOW:CONDUCTIVITY        1
INFLUENT:FLOW:CONDUCTIVITY        1
PRIMARY:PRI-CL-1:BLANKET          1
PRIMARY:PRI-CL-1:TEMP             1
SECONDARY:SEC-CL-1:BLANKET        1
SITE:WEATHER:BARO                 1
SITE:WEATHER:RAIN                 1
SITE:WEATHER:STORM                1
SLUDGE:DIG-1:TEMP                 1
SLUDGE:THK-1:TS                   1
```

**One each.** And finding that required no values at all — only the count of rows
that exist:

| question | the column that answers it |
|---|---|
| did the value move? | `count(DISTINCT value)` per window |
| did the instrument answer? | `count(*) FILTER (WHERE quality >= 2)` |
| how stale is what I have? | `max(ts) - max(ts) OVER (...)` |

None of them is `value`, and all three survive `dropna`.

## Per bucket, the three at once

```python
bucket = (
    readings[readings.signal_id == "INFLUENT:LIFT:CURRENT"]
    .set_index("ts")
    .resample("1h")
    .agg(
        mean=("value", "mean"),
        rows=("value", "size"),                             # did anything happen
        distinct=("value", "nunique"),                     # did it move
        bad=("quality", lambda q: int((q >= 2).sum())),     # did it say it was broken
    )
)
print(bucket.tail(3).to_string())
print()
trustworthy = bucket[(bucket.rows > 0) & (bucket.bad == 0)]
print(f"trustworthy buckets        : {len(trustworthy)} of {len(bucket)}")
print(f"buckets with a bad reading : {int((bucket.bad > 0).sum())}")
print(f"buckets where nothing moved: {int((bucket.distinct <= 1).sum())}")
```

That is a window that answers all three questions at once, and it costs three
extra columns. The one that is missing is the interesting one: **there is no
column that tells you a signal is stuck rather than steady.** `distinct` tells you
it did not move; whether that is correct is a question about the process, and no
amount of querying the `reading` table answers it.

## The rule

Three columns, and they answer three different questions:

```text
mean      the number, if there is one
rows      did anything happen at all
bad       did the instrument say it was broken
```

The practical consequence: **`dropna()` is not missing-data handling, it is
missing-*metadata* handling.** It throws away the `quality` column's entire reason
for existing, because a row with `quality = 2` and no value is not a row with
nothing in it — it is a row that says something.

## Takeaways

- **"No data" is three things**: the instrument did not answer, the instrument
  answered that it is broken, or the value did not move. `dropna` collapses all
  three into one indistinguishable hole.
- **Thirteen signals reported once in a week**, all at the dataset's first
  timestamp, all with a declared sample rate from 1 s to 1 h. The data cannot say
  whether they are steady or dead.
- **`PRIMARY:PRI-CL-1:UNDERFLOW` reported 30,718 times and moved 4 times.** By
  every naive measure it is the healthiest signal in the plant.
- **`resample().mean()` does not fabricate on this data, and `ffill` does.** The
  difference is one line, and after it there is nothing left to tell the two
  apart. The count is what survives.
- **`value = NULL, quality = 2` is a row with a claim in it.** It is the only
  place this dataset records an instrument failure, and `dropna` deletes it.
- **Nothing in the schema distinguishes a stuck sensor from a steady one.** That
  needs an expectation from outside the data, and a query that assumes it should
  say which.

## Exercises

1. **Find the boundary.** `signal.deadband` is per-signal. Find the deadband value
   at which a signal would stop being able to distinguish "steady" from "dead",
   and say what a monitoring system should do on the far side of it.
2. **Prove the fabrication.** Take `INFLUENT:LIFT:FLOW` (131,996 rows, 1,238
   distinct), resample to 1 h, and `ffill`. Count the buckets where the filled
   value is more than 10 minutes stale. Do the same for the underflow signal. Which
   one is harder to catch, and why?
3. **The chart that tells the truth.** Redraw notebook 04's figure with bucket
   height proportional to `rows` rather than `mean`. What does it stop you seeing,
   and is that a fair trade?
4. **The one this notebook cannot answer.** Propose a check that would
   distinguish a stuck `PRIMARY:PRI-CL-1:UNDERFLOW` from a genuinely steady one,
   using a second signal. Then say which of the three expectations in
   `sql/04-expert/04-02` yours relies on, and why.

---

**Next:** [03 — Choosing your tier](03-choosing-a-tier.ipynb) ·
**Back to the series** [README](README.md) ·
**Previous:** [01 — Meet the plant](01-meet-the-plant.ipynb)
