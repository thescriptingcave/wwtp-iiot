# 01-02 — When was it doing that?

**Previous:** [01-01](01-01_ask_a_question.md) · **Next:** [01-03](01-03_aggregating.md)

`WHERE` on a time-series table is not the `WHERE` you are used to, for one reason:
**the interesting predicates are almost always about time, and time is a range.**

## The question

> The blower tripped at 03:14. Show me what the aeration tank was doing from an
> hour before to an hour after.

## Ranges, not equality

```sql
SELECT time, value, quality
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T02:14:00Z'
  AND time <  '2026-09-26T04:14:00Z'
ORDER BY time;
```

Three things in that `WHERE` clause that are not general SQL habits.

**Half-open intervals: `>= start AND < end`.** Not `BETWEEN`, and not `<=`. An
instant belongs to exactly one window if every window is half-open, and to two if
any is closed. With a 1 Hz signal, using `<=` on both ends double-counts the
boundary sample in every adjacent window — invisible in a total, visible in every
average. This habit is worth forming here because it costs nothing and it is
correct.

**RFC 3339, not epoch numbers.**

```sql
AND time >= '2026-09-26T02:14:00Z'     -- works
AND time >= '1758845640000'            -- "'1758845640000' is not a valid timestamp"
```

Writes carry integer timestamps; *filters* want a date string. The two forms are
not interchangeable and nothing says so.

**Relative time, when you mean "recently".**

```sql
AND time >= now() - INTERVAL '1 hour'
```

`now()` is evaluated per query, which is what you want for a live dashboard and
almost never what you want for an investigation. When you are chasing a specific
event, **write the literal timestamp** — otherwise you re-run the query an hour
later, get a different window, and cannot reproduce what you saw.

## The question behind the question

The blower trip is not one event. It is a **sequence**, and the sequence is the
point:

> The tank holds about 40 kg of dissolved oxygen. When the air stops, the
> dissolved oxygen does not stop — it drains, over one to two hours. Only *then*
> does effluent ammonia break through, hours after that.

Which means an alarm on ammonia, fired at 03:20, would blame the wrong unit. Air
was the fault. Ammonia was the consequence.

```sql
SELECT time, signal, mean(value) AS mean_value
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
   OR signal = 'air_flow_m3h'
   OR signal = 'nh4_out_mg_l'
  AND time >= '2026-09-26T02:14:00Z'
  AND time <  '2026-09-26T08:00:00Z'
GROUP BY time(10m), signal
ORDER BY time;
```

## What to notice

**The three signals come back stacked, one row per signal per bucket.** This is
not a limitation worked around — it is how the data is stored, and it is what the
aggregates want anyway. You asked for "the mean of each of these three signals in
each ten-minute window", and that is precisely what you got.

The pivot you would write in Postgres — `mean(CASE WHEN signal = 'do_mg_l' THEN
value END)` — **is a parse error here, because this dialect has no `CASE`
expression.** That is the single biggest difference between this course and every
SQL-for-metrics tutorial, and [`_shared/DIALECT.md`](../_shared/DIALECT.md) is
worth reading before you write your first query.

**There is no `IN` operator either.** `signal IN ('a','b')` is a parse error here;
write the `OR`s out. That is three more lines of `WHERE` than a Postgres query, and
it is the price of a dialect with no `CASE` and no `IN`.

**`GROUP BY time(10m), signal`** is the whole bucketing idiom: the duration is a
bare literal, and every dimension you group by must be named — including `signal`.
`GROUP BY time(10m)` alone parses and then fails at planning.

**Look at the order the columns go bad in.** Air flow drops first and immediately.
Dissolved oxygen sags over the next hour. Ammonia holds, then climbs hours later.
That ordering is the whole reason this project has an alarm engine designed around
*what a fault does not show* rather than around thresholds.

## Exercises

1. Find the storm in the seeded data. What does influent flow do, and how long
   before effluent turbidity follows? Write the literal timestamps you used.
2. Change the bucket to `INTERVAL '1 minute'` and then `INTERVAL '1 hour'`. At which
   interval does the ammonia breakthrough stop being visible? Why does that matter
   for choosing a dashboard refresh?
3. Count the readings in the window with `COUNT(*)` and with `COUNT(value)`. They
   differ. Which one tells you whether the *data* was complete, and which tells
   you whether the *instruments* were working?
