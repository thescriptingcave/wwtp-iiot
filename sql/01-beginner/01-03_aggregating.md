# 01-03 — What is the average, and over what?

**Previous:** [01-02](01-02_filtering.md) · **Next:** [01-04](01-04_time_buckets.md)

`mean()` is easy. *What you average over* is not, and the difference is the
difference between a number and a measurement.

## The question

> What is this plant's typical dissolved oxygen, and typical effluent ammonia, over
> the last day?

## The obvious query

```sql
SELECT mean(value) FROM "wwtp"."aeration" WHERE signal = 'do_mg_l';
```

One number. It is a real number, computed from real data, and it is nearly
meaningless, for three reasons worth taking in order.

**It mixes signals.** If you forget the `signal` filter you are averaging
dissolved oxygen in mg/L with blower speed in rpm and air flow in m³/h. You get a
number, and it is nonsense. This is not a hypothetical; it is the first mistake
everyone makes.

**It ignores the interval.** A mean over a day and a mean over a week are
different numbers answering different questions, and the query does not say which
it computed.

**It does not say how much data it had.** `mean` of 10 readings and `mean` of
86 400 are equally weighted in the output and not equally trustworthy.

## The query that answers the question

```sql
SELECT
  time,
  signal,
  eu,
  count(*)                AS readings_total,
  count(value)            AS readings_usable,
  count(*) - count(value) AS readings_without_a_value,
  mean(value)             AS mean_value,
  min(value)              AS min_value,
  max(value)              AS max_value
FROM "wwtp"."aeration"
WHERE time >= now() - 24h
  AND quality = 0
GROUP BY time(1h), signal, eu
ORDER BY time;
```

Four things in that `GROUP BY` that are all load-bearing:

* **`time(1h)`** — bucketing, and the duration is a bare literal (`1h`, not
  `INTERVAL '1 hour'`).
* **`signal, eu`** — every grouped dimension must be *named*. `GROUP BY time(1h)`
  alone parses and then fails at planning.
* **`ORDER BY time`** — the only valid order. You cannot order by `signal`, and you
  cannot order by `mean_value`, so the rows arrive in time order and you read
  across.
* **No `CASE`** — this dialect has none, so the `count()` of each quality is a
  separate query rather than a column. That is why `readings_without_a_value` is
  written as `count(*) - count(value)` instead: it is arithmetic, not a
  conditional.

### What to notice

**`count(*)` and `count(value)` are both in the output on purpose.** When they are
equal, every reading that happened had a value, and the aggregates describe the
whole period. When they differ, part of the window had no usable data and the mean
silently describes less than you think.

**`min` and `max` next to `mean`** is the cheapest anomaly detector ever written. A
mean that sits comfortably between a min and a max which are both implausible is
telling you about two different things and you should not trust it.

**`quality = 0` in the `WHERE`.** This is the habit from [00-03](../00-foundations/00-03_quality_is_data.md).
It is also redundant for `mean`, because `mean` ignores NULLs anyway — and that
redundancy is worth noticing rather than removing. If you *drop* the filter, `mean`
gives the same number. If you *drop the row* with `quality = 1`, you get a
different one. Two different decisions that look identical in the query.

## When the window is a column, not a constant

Hard-coding `now() - INTERVAL '24 hours'` is fine for a question, and wrong for
anything you want to reuse. Put the window in a subquery and you can change it in
one place:

```sql
SELECT
  time,
  signal,
  mean(value) AS mean_value,
  count(value) AS n
FROM "wwtp"."aeration"
WHERE time >= now() - 24h
GROUP BY time(1h), signal
ORDER BY time;
```

The subquery is `max(time) - INTERVAL '24 hours'`, **not** `now()`. The seeded data
is in the past; `now()` is the wall clock. Using the data's own latest timestamp as
the anchor is the habit that makes a query work against historical data and live
data alike.

## Exercises

1. Compute the mean of `value` for `do_mg_l` twice: once with `quality = 0` in the
   `WHERE`, once without. They agree — explain why, and then say when they would
   *not*.
2. Which signal in `aeration` has the largest gap between `readings_total` and
   `readings_usable`? What does that tell you about the plant?
3. `mean(value)` over an hour where only two readings exist returns a number. Is
   that number wrong? What would you add to the query so a reader could tell?
