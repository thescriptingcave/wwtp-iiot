# 01-04 — Buckets: asking about time, not moments

**Previous:** [01-03](01-03_aggregating.md) · **Next:** [02-intermediate](../02-intermediate/)

Everything so far asked about *this plant*, which is a timeless question. The
moment you ask *when*, you have to decide how to divide time up, and that decision
is where most time-series SQL goes wrong.

## The question

> What did each hour of air flow to the aeration basin look like over the last day?

## The obvious attempt

```sql
SELECT ts, avg(value) FROM reading
WHERE signal_id = 'AERATION:AHU-1:AIR_FLOW'
  -- Bounded, so the row count is reproducible. Every lesson here bounds its
  -- window; `tools/check_sql.py` runs each query three times and fails any that
  -- return a different number of rows twice, which is how a lesson with a live
  -- plant writing into the database gets caught.
  AND ts < (SELECT max(ts) FROM reading WHERE source = 'seed')
GROUP BY ts;
```

One group per reading, so `avg` of one value is that value. You have written a
`SELECT` with extra steps.

## The query that answers the question

```sql
SELECT
    date_trunc('hour', r.ts) AS bucket,
    count(*)                 AS n,
    avg(r.value)             AS mean_value,
    min(r.value)             AS min_value,
    max(r.value)             AS max_value
FROM reading r
WHERE r.signal_id = 'AERATION:AHU-1:AIR_FLOW'
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY 1
ORDER BY 1 DESC;
```

```
       bucket        |  n   |  mean_value  |   min_value   |   max_value
---------------------+------+--------------+---------------+---------------
 2026-09-27 02:00:00 | 2745 | 3537.695940  | 3354.953489   | 3640.448551
 2026-09-27 01:00:00 | 2732 | 3494.311417  | 3382.782903   | 3586.078644
 2026-09-27 00:00:00 | 3275 | 3503.288625  | 3392.864655   | 3645.591905
```

`n` is in the thousands because air flow is a noisy signal with a wide deadband —
the busy end of the spread from [01-01](01-01_ask_a_question.md). It is a good
signal to learn on precisely because the buckets are full.

`date_trunc('hour', ts)` is the whole trick. It rounds a timestamp **down** to the
start of its hour, so every reading in 03:47 becomes 03:00:00, and they all land
in one group.

## `date_trunc` versus `time_bucket`, and why there are two

`date_trunc` is standard Postgres and it has one limitation that matters here: it
only understands calendar units.

<!-- check: skip -->
```sql
SELECT date_trunc('hour',  ts) FROM …   -- fine
SELECT date_trunc('15 minutes', ts) …   -- fine
SELECT date_trunc('7 minutes',  ts) …   -- ERROR: unit "7 minutes" not recognized
```

That is a real problem for a plant, because **you do not get to choose your
bucket size based on what the calendar does.** A 7-minute bucket is the obvious
choice for a 10-minute sampling interval. `time_bucket` does it:

```sql
SELECT
    time_bucket(interval '15 minutes', r.ts) AS bucket,
    count(*)     AS n,
    avg(r.value) AS mean_value
FROM reading r
WHERE r.signal_id = 'AERATION:AHU-1:AIR_FLOW'
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '6 hours'
GROUP BY 1
ORDER BY 1 DESC;
```

```
        bucket         |  n  |  mean_value
-----------------------+-----+------------
 2026-09-27 02:45:00   | 566 | 3557.404554
 2026-09-27 02:30:00   | 737 | 3545.670260
 2026-09-27 02:15:00   | 730 | 3532.064467
```

`time_bucket(interval, ts)` is a TimescaleDB function. It also has an optional
third argument — an origin — which `date_trunc` has no equivalent of, and which is
the difference between buckets aligned to midnight and buckets aligned to whenever
the data happens to start. That matters more than it sounds; it is the subject of
exercise 4.

**Prefer `time_bucket` in this project.** It is the same shape for every interval,
it handles non-calendar sizes, and it is what the continuous aggregates in
[03-advanced](../03-advanced/) use, so a query you write here behaves the same way
when you later point it at `reading_1m`.

## The two traps

**Trap one: bucketing by the wrong end of the interval.** `date_trunc` rounds
*down*, which means 03:47 belongs to the bucket *labelled* 03:00 — the bucket
covering 03:00 to 04:00. It is not the bucket "for 03:47", and if you are
reporting hourly averages for a shift that runs 06:00 to 18:00, the bucket
labelled 18:00 contains 18:00 to 19:00, which is half outside the shift.

<!-- check: skip -->
```sql
-- Correct: half-open, so a boundary reading is counted exactly once
WHERE ts >= '2026-09-26 06:00:00+00'
  AND ts <  '2026-09-26 18:00:00+00'
```

**Trap two: empty buckets vanish.** This is the big one, and it is not a SQL
problem at all — it is a property of `GROUP BY`.

<!-- check: skip -->
```sql
-- "Which hours had the blower offline?"
SELECT date_trunc('hour', ts) AS bucket, count(*)
FROM reading
WHERE signal_id = 'AERATION:AHU-1:BLOWER_RPM' AND value = 0
GROUP BY 1
ORDER BY 1;
```

```
 bucket | count
--------+-------
(0 rows)
```

**Zero rows, and the blower was not fine.** Look at what it actually did during
the seeded storm:

```sql
SELECT min(value), max(value) FROM reading
WHERE signal_id = 'AERATION:AHU-1:BLOWER_RPM';
```

```
     min      |      max
--------------+--------------
 7.6858409488 | 2398.9360770382
```

It bottomed out at 7.7 rev/min — a blower spinning down against a dying air
header, and about as stopped as a blower gets. `value = 0` found none of it,
because a process model does not produce exactly zero. **Never test a process
value for equality with a constant.** A `DO` probe reads 0.003, not 0. A valve
reads 0.1 %, not 0. The threshold has to be a threshold:

```sql
-- 33 readings below 100 rev/min: the actual near-stop
SELECT date_trunc('hour', ts) AS bucket, count(*), min(value)
FROM reading
WHERE signal_id = 'AERATION:AHU-1:BLOWER_RPM'
  AND value < 100
GROUP BY 1
ORDER BY 1;
```

That is a separate, and much more common, way for a "find the anomalies" query to
return nothing and be believed. It is a *comparison* bug rather than a
`GROUP BY` bug, but the symptom and the disappointment are identical.

And here is the same question asked about a signal where the answer really is
gaps. `AERATION:AHU-1:DO` has *two* readings in one hour and *forty* in the next:

```
       bucket        |  n  |  mean_value  |  min_value  |  max_value
---------------------+-----+--------------+-------------+-------------
 2026-09-27 02:00:00 |    1 | 2.046707404  | 2.046707404 | 2.046707404
 2026-09-27 03:00:00 |    2 | 2.016700025  | 2.006698748 | 2.026701302
 2026-09-27 08:00:00 |   40 | 2.401557219  | 2.027854850 | 2.670028198
```

Those are not missing hours. The signal was steady and the deadband did its job.
Which is exactly why you cannot tell a gap from an outage by counting rows.

That returns only the hours that *contain a reading equal to zero*. The hours where
the blower was offline and **nothing was recorded** are not in the result, because
there is no row to group. The query cannot distinguish "the blower was running" from
"the gateway was down", and it will confidently show you a chart with no gaps in a
period that was entirely gaps.

**This is the single most important thing to understand about aggregating a
time series, and it is not specific to this database.** Every time-series store has
this problem. It is why `02-intermediate` has a lesson about generating the buckets
that *should* exist and left-joining the data onto them.

## `generate_series` makes the missing buckets visible

```sql
WITH buckets AS (
    SELECT generate_series(
        date_trunc('hour', (SELECT min(ts) FROM reading)),
        date_trunc('hour', (SELECT max(ts) FROM reading)),
        interval '1 hour'
    ) AS bucket
)
SELECT
    b.bucket,
    count(r.signal_id) AS n,          -- 0, not NULL, for an empty hour
    avg(r.value)       AS mean_value
FROM buckets b
LEFT JOIN reading r
       ON r.signal_id = 'AERATION:AHU-1:AIR_FLOW'
      AND date_trunc('hour', r.ts) = b.bucket
GROUP BY b.bucket
ORDER BY b.bucket;
```

`generate_series(timestamp, timestamp, interval)` produces one row per interval
across the range. That is a table of buckets that *definitely exists*, whether or
not any data does — and the `LEFT JOIN` keeps the empty ones, with `n = 0`.

Note the join condition includes the signal filter. That is deliberate and it is the
classic mistake: filtering in the `WHERE` clause of a `LEFT JOIN` turns it back into
an inner join, and every empty bucket disappears again — silently, because the query
still returns rows.

<!-- check: skip -->
```sql
-- Wrong. Looks identical, returns no empty buckets.
FROM buckets b
LEFT JOIN reading r ON date_trunc('hour', r.ts) = b.bucket
WHERE r.signal_id = 'AERATION:AHU-1:AIR_FLOW'
```

## The window, anchored to the data

Every query in this lesson uses

<!-- check: skip -->
```sql
WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 day'
```

and that is a habit worth forming deliberately. `now() - interval '1 day'` is the
wall clock minus a day. The seeded data ends whenever you seeded it, which is in
the past, so `now()` gives you **nothing**. The subquery form works against
historical data and live data alike.

The cost is that the anchor is computed twice if you use it twice. When that
starts to bother you, that is what a CTE is for, and it is the first lesson of
[02-intermediate](../02-intermediate/).

## Exercises

1. Write the hourly query with `date_trunc` and with `time_bucket`. Confirm they
   agree for hourly buckets. Then write one with a 45-minute bucket using each, and
   explain why only one of them is possible.
2. Find a window of a few hours where `AERATION:AHU-1:DO` has **no** readings at
   all — it has hours with `n = 1`, so empty hours certainly exist. Then produce a
   query that shows the gap as a row of `n = 0`. What does the gap mean, given what
   you know about the deadband — and what else could it mean?
3. Using the `generate_series` version, find the longest gap between consecutive
   readings for each of five signals. Which one would you investigate first, and
   what would you check to tell a deadband artefact from a real outage?
4. `time_bucket` takes an optional third argument, an origin. Write a 7-minute
   bucket query twice: once aligned to midnight, once aligned to an origin you
   choose. What changes, and when would you want the second form?
5. The deadband means "a row exists when the value moved". Write a query that
   estimates the *observed* rate of a signal by dividing readings by seconds
   covered. Then explain why that number is not the instrument's sample rate, and
   what it actually measures. `signal.sample_ms` has the real answer.
