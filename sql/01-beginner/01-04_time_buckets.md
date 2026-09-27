# 01-04 — `time_bucket`, and choosing the interval that does not hide the answer

**Previous:** [01-03](01-03_aggregating.md) · **Next:** [02-intermediate](../02-intermediate/)

The last beginner lesson, and the one that makes this a time-series course.

## The syntax

```sql
GROUP BY time(1h)
```

`time(1h)` truncates each timestamp down to the start of its containing bucket.
Every reading in 07:00:00–07:59:59 becomes `07:00:00`. Group by that and you have
an hourly average.

```sql
SELECT time, mean(value) AS dissolved_oxygen
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T00:00:00Z'
GROUP BY time(1h), signal
ORDER BY time;
```

Four things about that line, each of which is a parse error if you get it wrong:

| Written | Result |
|---|---|
| `GROUP BY time(1h)` | works |
| `GROUP BY time(INTERVAL '1 hour')` | **parse error** — this dialect has no `INTERVAL` |
| `GROUP BY time(1h), signal` | works — and the tag is **required** |
| `GROUP BY time(1h)` alone | parses, then **fails at planning** |
| `GROUP BY h` (the alias) | **fails** — repeat the expression |
| `HAVING count(value) >= 30` | **parse error** — no `HAVING` in this dialect |

And `time_bucket()` does exist as a function, but it cannot be grouped by on this
build. `GROUP BY time(...)` is the idiom.

## Why the interval is part of the question

"Average DO per hour" and "average DO per day" are both correct answers to
different questions. The interval is not a performance knob — **it decides what is
visible.**

This is the demonstration. Same data, three intervals, three different stories:

```sql
SELECT time, mean(value) AS dissolved_oxygen, count(value) AS n
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T03:00:00Z'
  AND time <  '2026-09-26T05:00:00Z'
GROUP BY time(1m), signal
ORDER BY time;
```

```sql
-- Same two hours, hourly.
SELECT time, mean(value) AS dissolved_oxygen, count(value) AS n
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T03:00:00Z'
  AND time <  '2026-09-26T05:00:00Z'
GROUP BY time(1h), signal
ORDER BY time;
```

```sql
-- Same two hours, daily. One row.
SELECT time, mean(value) AS dissolved_oxygen, count(value) AS n
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T03:00:00Z'
  AND time <  '2026-09-26T05:00:00Z'
GROUP BY time(1d), signal
ORDER BY time;
```

**At one minute** you see the drain begin the instant the blower trips. **At one
hour** you see two numbers and no event at all — a blower trip lasting forty
minutes is entirely inside one bucket and is averaged away. **At one day** you see
a single number, and if you had only that you would conclude nothing happened.

This is not a quirk of the storage engine. It is the central hazard of aggregating
time series, and it has a name: choosing an interval wider than the event you are
looking for. **A daily average cannot contain a fault that lasted forty minutes.**

## The rule of thumb, and where it breaks

> Choose an interval **shorter** than the shortest event you need to see.

For this plant:

| Question | Interval | Why |
|---|---|---|
| Is the plant healthy right now? | 10 s | Slower and a fault is already over |
| Did a blower trip happen? | 1 min | The event is tens of minutes |
| Is nitrification degrading? | 1 h | The trend is over weeks |
| Is the seasonal pattern shifting? | 1 day | The trend is over months |

It breaks when the data is *not* evenly sampled — and it never is, because of the
deadband. A settled signal is stored rarely; a signal in distress is stored often.
So **each bucket holds a different number of points**, and averaging the averages
across buckets is wrong. The fix is in
[02-intermediate](../02-intermediate/), and the symptom is a number that is
slightly, invisibly wrong — worst kind.

## `count` next to `mean`, always

Every query above selects `count(value) AS n` alongside the mean. It is there so
the reader can see how much data each bucket is built from. A bucket with `n = 2` and
a bucket with `n = 3600` look identical in a chart of means and are not remotely
the same measurement.

You can go further and refuse to show a bucket at all:

```sql
SELECT time, mean(value) AS do_mg_l, count(value) AS n
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T03:00:00Z'
GROUP BY time(1m), signal
ORDER BY time;
```

…and then **filter on `n` in the application**, because this dialect has no
`HAVING` either. The alternative is a subquery, and the scalar form of that is a
parse error too (see `01-03`). So: select the count, and drop the thin buckets when
you render.

That is a real limitation with a real cost — you cannot ask the database to hide
its own gaps, so a dashboard has to decide what a `n` of 2 means. It is the same
problem as the gap-filling in `03-advanced`, arriving earlier than you would like.

## Exercises

1. Run all three intervals above over the seeded storm. Which interval first hides
   the influent spike? Which first hides the effluent response?
2. Add `count(value) AS n` to a daily query over a week and find the day with the
   fewest readings. What was the plant doing?
3. **The hard one.** A signal reads 2.0 for an hour (3600 readings), then 2.5 for
   one minute (60 readings), then 2.0 again. What does `mean(value)` report for
   the hour? What *should* it report? Write both numbers down — the gap between
   them is the subject of `02-intermediate`.
