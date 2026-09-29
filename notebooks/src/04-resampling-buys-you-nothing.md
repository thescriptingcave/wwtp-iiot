---
title: "04 — Resampling buys you nothing"
subtitle: "Hourly buckets are not hourly samples, and the rollup tables are a different estimator, not a faster one"
---

# 04 — Resampling buys you nothing

## The question

> I want hourly averages. There is a `reading` table with four million rows and a
> `reading_1h` continuous aggregate with seven thousand. Which do I query, and does
> the answer change?

The rollup exists for speed and the fast answer is "use the rollup". That is
correct as advice and incomplete as an argument, because `reading_1h` does not
contain the same numbers as `reading`. On this data it disagrees by **22 %** on a
control signal, and the disagreement is not a rounding artefact or a bug — it is a
different question being answered.

This notebook works out which question, and what to write when you want the other
one.

## Setup

```python
import matplotlib.pyplot as plt
import pandas as pd
import psycopg
from storage.postgres.schema import dsn

from notebooks._style import apply_style, save, stamp, trend

apply_style()
pd.set_option("display.width", 130)

conn = psycopg.connect(dsn())

WINDOW = "ts >= (SELECT max(ts) FROM reading) - interval '7 days'"
VALVE = "AERATION:AHU-1:BLOWER_VALVE"

readings = pd.read_sql(
    f"SELECT signal_id, ts, value FROM reading WHERE {WINDOW}", conn
)
hourly = (
    readings.set_index("ts")
    .groupby("signal_id").value
    .resample("1h").mean()
    .unstack("signal_id")
)
rollup = pd.read_sql(
    f"SELECT * FROM reading_1h WHERE signal_id = '{VALVE}' ORDER BY bucket", conn
)
print(f"{len(readings):,} readings in the window, {hourly.shape[1]} signals")
print(f"reading_1h for {VALVE}: {len(rollup)} hourly buckets")
```

```output
4,287,657 readings in the window, 57 signals
reading_1h for AERATION:AHU-1:BLOWER_VALVE: 198 hourly buckets
```

## What an hourly bucket is not

The word "bucket" invites the assumption that a bucket is one sample. It is an
arbitrary number of samples, and the number changes every hour:

```python
counts = (
    readings.set_index("ts")
    .groupby("signal_id").value
    .resample("1h").count()
    .unstack("signal_id")
)

summary = pd.DataFrame({
    "hours": counts.notna().sum(),
    "min_n": counts.min(),
    "median_n": counts.median(),
    "max_n": counts.max(),
})
summary = summary[summary.hours >= 100].sort_values("max_n", ascending=False)
print(summary.head(8).to_string())
```

```output
                             hours  min_n  median_n  max_n
signal_id
AERATION:AHU-1:AIR_FLOW        169    4.0    3045.0  3600.0
AERATION:AHU-1:BLOWER_RPM      169    8.0    3592.0  3600.0
UTILITY:SITE:PLANT_POWER       169    7.0    3600.0  3600.0
AERATION:AHU-1:BLOWER_VALVE    169    2.0    1389.0  3600.0
PRIMARY:PRI-SCR-1:TORQUE       169  708.0    2830.0  3125.0
SECONDARY:SEC-SCR-1:TORQUE     169  690.0    2499.0  2861.0
EFFLUENT:FLOW:RESIDUAL_CL      169    0.0    1329.0  2580.0
EFFLUENT:DIS-CL-2:BACTI        169    0.0    1329.0  2580.0
```

**A single hourly bucket holds anywhere from 0 to 3,600 readings**, and the ratio
between a signal's quietest and median hour is around **700×** for
`AERATION:AHU-1:AIR_FLOW` (4 against 3,045). These signals declare a 1,000 ms
sample rate, so 3,600 is a *full* hour of data and anything less means the
historian had nothing to record.

The eight rows above are the busiest signals in the plant, sorted by maximum. The
signals with the *most unequal* buckets are the quiet ones — `INFLUENT:LIFT:CURRENT`
has a median of 4 readings an hour and a maximum of 1,068, and
`PRIMARY:PRI-CL-1:UNDERFLOW` has a median of **zero**, meaning most hours contain
nothing at all.

Now the arithmetic that follows from that, which is the whole notebook:

```python
# The mean of the hourly means treats every hour as one observation.
# The mean of the readings treats every reading as one observation.
# These are the same number only when every bucket holds the same count.

series = readings[readings.signal_id == VALVE]
buckets = hourly[VALVE].dropna()
weights = counts[VALVE].reindex(buckets.index)

unweighted = buckets.mean()                       # every hour one vote
weighted = (buckets * weights).sum() / weights.sum()   # every reading one vote
raw = series.value.mean()                         # what AVG(value) does in SQL

print(f"mean of {len(buckets)} hourly means : {unweighted:.6f}")
print(f"count-weighted mean of the same   : {weighted:.6f}")
print(f"AVG(value) over the raw readings  : {raw:.6f}")
print(f"weighted vs AVG difference        : {abs(weighted - raw):.2e}")
```

```output
mean of 169 hourly means : 23.086545
count-weighted mean of the same   : 29.362352
AVG(value) over the raw readings  : 29.362352
weighted vs AVG difference        : 3.55e-15
```

**The count-weighted mean of the hourly means is `AVG(value)`, to fifteen decimal
places.** Not approximately. Identically, always, for any data — it is the same
sum divided by the same count.

That is worth pausing on, because it means `AVG` over the raw table is not a
shortcut that is close to the right answer. It **is** the right answer to a
specific question:

> *What is the average of the readings that were recorded?*

And bucketing to hourly and averaging the buckets answers a **different** question:

> *What is the average of the hours in which anything was recorded?*

Both are legitimate. Only one of them is what `AVG(value)` does, and the difference
is not small:

```python
print(f"unweighted (each hour one vote) : {unweighted:.2f} %")
print(f"count-weighted (AVG(value))     : {raw:.2f} %")
print("difference                      : "
      f"{100 * (unweighted - raw) / raw:+.1f} %")
```

```output
unweighted (each hour one vote) : 23.09 %
count-weighted (AVG(value))     : 29.36 %
difference                      : -21.4 %
```

**21.4 % on a control signal.** The reason is notebook 02's finding arriving as
arithmetic: the historian samples when the value *moves*, so hours when the blower
was working have 3,600 readings and hours when it was not have 106. Giving each
hour one vote gives the quiet hours the same weight as the busy ones, and the quiet
hours are the ones with the low values.

## How large the error gets, across every signal

It is not one unlucky tag:

```python
errors = []
for sig in hourly.columns:
    buckets = hourly[sig].dropna()
    series = readings[readings.signal_id == sig].value.dropna()
    if len(buckets) < 100 or len(series) < 1000:
        continue
    avg = series.mean()
    if not avg:
        continue
    errors.append((abs(buckets.mean() - avg) / abs(avg), sig, avg, buckets.mean()))

print(f"signals with >=100 hourly means and >=1000 readings: {len(errors)}")
for _, sig, avg, buckets in sorted(errors, reverse=True)[:8]:
    print(f"  {sig:28} AVG {avg:9.3f}   mean-of-hours {buckets:9.3f}   "
          f"{100 * (buckets - avg) / avg:+6.1f}%")
```

```output
signals with >=100 hourly means and >=1000 readings: 24
  AERATION:AHU-1:BLOWER_VALVE  AVG    29.362   mean-of-hours    23.087   -21.4%
  INFLUENT:LIFT:WETWELL_LEVEL  AVG     3.086   mean-of-hours     3.453   +11.9%
  EFFLUENT:FLOW:TURBIDITY     AVG    10.170   mean-of-hours     9.217    -9.4%
  SECONDARY:SEC-CL-1:OVERFLOW  AVG    18.501   mean-of-hours    16.804    -9.2%
  EFFLUENT:FLOW:TSS           AVG    18.478   mean-of-hours    16.785    -9.2%
  AERATION:AHU-1:AIR_FLOW     AVG  6451.764   mean-of-hours  5985.074    -7.2%
  EFFLUENT:DIS-CL-2:BACTI     AVG   124.092   mean-of-hours   115.398    -7.0%
  INFLUENT:LIFT:FLOW         AVG  1803.183   mean-of-hours  1697.746    -5.8%
```

**Seven of twenty-four signals disagree by 5 % or more.** The largest is a
*blower valve* — a control signal, the kind you would least expect to be sensitive
to how you bucket it.

## And the rollup is not the same window

So use `reading_1h`, it is 300× smaller. Check the window first:

```python
raw_span = pd.read_sql(
    f"SELECT min(ts) AS first, max(ts) AS last, count(*) AS rows "
    f"FROM reading WHERE signal_id = '{VALVE}'",
    conn,
).iloc[0]

print(f"reading     {raw_span.rows:>9,} readings  "
      f"{raw_span['first']} -> {raw_span['last']}")
print(f"reading_1h  {len(rollup):>9,} buckets    "
      f"{rollup.bucket.min()} -> {rollup.bucket.max()}")
print(f"reading_1h  n column sums to {rollup.n.sum():,.0f}")
print(f"difference: {rollup.n.sum() - raw_span.rows:+,.0f} readings")
```

```output
reading       268,086 readings  2026-09-22 01:43:25.774697+00:00 -> 2026-09-29 01:43:22.774697+00:00
reading_1h        198 buckets    2026-09-20 18:00:00+00:00 -> 2026-09-29 01:00:00+00:00
reading_1h  n column sums to 306,775
difference: +38,689 readings
```

**The rollup covers more data than the raw table currently holds — 38,689 readings
the `reading` table does not have.** It starts nearly two days earlier.

This is not a bug to be reported. The continuous aggregate is created once and
never rewritten; the raw table was re-seeded. So:

> **`avg(mean)` over the whole of `reading_1h` is not an answer to a question about
> `reading`.** You have to restrict the rollup to the window you meant, and the
> window you meant is the one the raw table's `min(ts)` and `max(ts)` give you.

Which is the second reason the rollup is slower to reach for than it looks: you
cannot simply `SELECT avg(mean) FROM reading_1h`. You have to write the window
clause, and then check that it returns what you expected.

```python
lo = raw_span["first"].replace(minute=0, second=0, microsecond=0)
hi = raw_span["last"].ceil("h")
restricted = rollup[(rollup.bucket >= lo) & (rollup.bucket < hi)]

print(f"unrestricted rollup : {len(rollup)} buckets, "
      f"avg(mean) = {rollup['mean'].mean():.3f}")
print(f"same window as raw  : {len(restricted)} buckets, "
      f"avg(mean) = {restricted['mean'].mean():.3f}")
print(f"pandas mean-of-hours: {unweighted:.3f}")
print(f"AVG(value)          : {raw:.6f}")
```

```output
unrestricted rollup : 198 buckets, avg(mean) = 22.774
same window as raw  : 169 buckets, avg(mean) = 23.087
pandas mean-of-hours: 23.087
AVG(value)          : 29.362352
```

Restricted to the raw table's own window, the rollup gives **23.087** and pandas
gives **23.087**. They agree — and the fact that they do is worth stating, because
it is the strongest argument for using the rollup:

> **The two implementations of "the hourly mean over the window" agree once the
> window matches.** The 22.774 you get by forgetting the window clause is not a
> disagreement about bucketing. It is an answer about two extra days.

That is a better outcome than I expected. It means the estimator question is
genuinely the one that matters here, and that the *implementation* — rollup versus
pandas, SQL versus Python — is not a source of error once you pin the window.

It also means the failure mode is the dangerous kind: **a wrong window returns a
plausible number.** `22.774` is within 1.3 % of the right answer, which is close
enough that nothing fails and nobody looks.

## The thing bucketing destroys

The arithmetic above is all about the aggregate. Here is what the aggregate throws
away.

An hourly bucket with one reading and an hourly bucket with 3,600 readings are
given the same vote by `avg(mean)`, and both arrive at the rollup as a single
number with no memory of which it was:

```python
lift = pd.read_sql(
    "SELECT bucket, n, mean, min, max FROM reading_1h "
    "WHERE signal_id = 'INFLUENT:LIFT:CURRENT' AND n > 0 "
    "ORDER BY n ASC LIMIT 6",
    conn,
)
print(lift.to_string(index=False))
print()
print("and at the other end, the same signal:")
full = pd.read_sql(
    "SELECT bucket, n, mean, min, max FROM reading_1h "
    "WHERE signal_id = 'INFLUENT:LIFT:CURRENT' ORDER BY n DESC LIMIT 3",
    conn,
)
print(full.to_string(index=False))
```

```output
                   bucket  n      mean       min       max
2026-09-22 02:00:00+00:00  1 11.308153 11.308153 11.308153
2026-09-27 13:00:00+00:00  1 40.710596 40.710596 40.710596
2026-09-29 01:00:00+00:00  1 15.370466 15.370466 15.370466
2026-09-20 19:00:00+00:00  1 11.308153 11.308153 11.308153
2026-09-20 18:00:00+00:00  2  5.403906  0.000000 10.807812
2026-09-21 22:00:00+00:00  2  5.403906  0.000000 10.807812

and at the other end, the same signal:
                    bucket    n      mean       min       max
2026-09-28 20:00:00+00:00 1068 20.375653  0.0 40.751307
2026-09-27 20:00:00+00:00 1068 20.375653  0.0 40.751307
2026-09-26 20:00:00+00:00 1068 20.375653  0.0 40.751307
```

Read the first table again. The hour at `13:00` on 27 September contributes
**40.71 A** — one reading — to `avg(mean)`. The hours at the bottom contribute
20.38 A from 1,068 readings each.

**One measurement outweighs a thousand**, because the aggregate cannot tell the
difference. And the lift current's declared normal range is 20–70 A, so a lone
40.71 A reading looks plausible; you would have no way of knowing it is the only
evidence for that hour.

The `n` column fixes this, and it is the reason every rollup here carries one:

```python
fig, ax = plt.subplots(figsize=(11, 3.8))
trend(ax, {"hourly mean": restricted["mean"]}, VALVE,
      ylabel="blower valve  [%]")
ax.set_ylim(0, 40)
stamp(ax, window=f"{len(restricted)} h",
      source="reading_1h.mean, window matched to reading",
      extra="the mean column alone")
save(fig, "04_valve_hourly_mean")

fig, ax = plt.subplots(figsize=(11, 3.8))
ax.bar(restricted.bucket, restricted.n, width=0.035, color="#1f77b4")
ax.set_ylabel("readings in the hour")
ax.set_title(f"{VALVE} — the same hours, by how much evidence each one has",
             loc="left")
format_time = ax.get_xaxis()
stamp(ax, window=f"{len(restricted)} h",
      source="reading_1h.n, window matched to reading",
      extra=f"min {int(restricted.n.min())}, "
            f"median {restricted.n.median():.0f}, max {int(restricted.n.max())}")
save(fig, "04_valve_counts")

print("readings per hour, same signal: "
      f"min {int(restricted.n.min())}, median {restricted.n.median():.0f}, "
      f"max {int(restricted.n.max())}")
```

**One plot is smooth and one is a picket fence, and they describe the same 169
hours.** The first says "the valve averaged 23 %". The second says the average
rests on a number of readings that varies by a factor of 1,800 for this signal —
from 2 in the quietest hour to 3,600 in the busiest.

That is the trade resampling makes: a smoother series, in exchange for the evidence
behind each point. Sometimes that is the right trade. It is never right by default,
and the factor of 1,800 is the size of the question you have to answer to know.

## What to write instead

Three rules, each of which is a different query:

**If you want the average of the recorded readings** — which is what a compliance
report means — use `AVG(value)` over `reading`. It is already correct, it needs no
bucketing, and it is the cheapest possible aggregate:

```sql
SELECT avg(value) AS mean_pct
FROM reading
WHERE signal_id = 'AERATION:AHU-1:BLOWER_VALVE'
  AND ts >= (SELECT max(ts) FROM reading) - interval '7 days';
```

**If you want the average hour** — which is what a time-series chart implies, and
which is what `avg(mean)` gives you — say so, and restrict the window:

```sql
SELECT avg(mean) AS mean_of_hours
FROM reading_1h
WHERE signal_id = 'AERATION:AHU-1:BLOWER_VALVE'
  AND bucket >= date_trunc('hour', (SELECT min(ts) FROM reading))
  AND bucket <  date_trunc('hour', (SELECT max(ts) FROM reading)) + interval '1 h';
```

**If you want to know whether an hour's value is worth anything**, carry the count
and treat it as evidence rather than as a number:

```sql
SELECT bucket, mean, n,
       CASE WHEN n = 0     THEN 'no data'
            WHEN n < 30    THEN 'thin'
            ELSE 'ok' END AS evidence
FROM reading_1h
WHERE signal_id = 'AERATION:AHU-1:BLOWER_VALVE'
ORDER BY n ASC
LIMIT 10;
```

That third query is the one a monitoring system should run, and it is not a query
that aggregates at all.

## Takeaways

- **`AVG(value)` is the count-weighted mean, exactly.** The count-weighted mean of
  hourly means equals it to 14 decimal places, for any data. Bucketing does not
  approximate `AVG`; it computes something else.
- **The two disagree by up to 21 % on this plant**, and the worst case is a control
  signal. Seven of twenty-five comparable signals differ by 5 % or more.
- **The estimator changes because the buckets are unequal**, and they are very
  unequal: 0 to 3,600 readings in an hour, a factor of 34 on one signal.
- **The rollup covers a different window than the raw table** — 38,689 readings and
  nearly two extra days — because continuous aggregates are created once and never
  rewritten. `avg(mean)` over all of `reading_1h` answers a question `reading`
  cannot be asked.
- **Once the window matches, the rollup and pandas agree exactly** — both 23.087.
  The 22.774 from a missing window clause is not a disagreement about bucketing; it
  is an answer about two extra days, and it lands within 1.3 % of the right answer,
  which is close enough that nothing fails.
- **A bucket with one reading gets the same vote as one with 3,600**, and the
  `mean` column cannot tell you. That is what the `n` column is for.

## Exercises

1. **Prove the identity, generally.** `AVG(value)` equals the count-weighted mean
   of bucket means whenever the buckets partition the readings. Find a case where
   it does *not* — overlapping buckets, or a window that cuts a bucket in half —
   and quantify the error.
2. **Find the worst signal.** Rank every signal by relative disagreement between
   the two estimators, including the ones this notebook skipped for having fewer
   than 1,000 readings. Does the ranking change when you restrict to signals with
   48 hourly means, and why?
3. **Make the rollup and pandas agree.** The rollup gave 23.104 and pandas 23.087.
   Align the bucket edges and recompute. How close do they get, and what is the
   floor on that agreement?
4. **The spike that survives.** Find an hour where a genuine transient is visible
   in the raw readings and absent from the hourly mean. Then find the smallest
   bucket width that still shows it — and say what that width does to the noise.
5. **Which estimator for which question?** For each of these three, name the
   estimator and defend it: a regulatory compliance figure, a chart on a dashboard,
   and a model that predicts the next hour.

---

**Next:** [05 — Autocorrelation](05-autocorrelation.ipynb) ·
**Back to the series** [README](README.md) ·
**Previous:** [03 — Choosing your tier](03-choosing-a-tier.ipynb)
