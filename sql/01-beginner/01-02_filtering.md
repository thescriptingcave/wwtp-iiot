# 01-02 — Filtering, and knowing what you filtered

**Previous:** [01-01](01-01_ask_a_question.md) · **Next:** [01-03](01-03_aggregating.md)

`WHERE` is the easiest clause in SQL and the easiest to get subtly wrong. The
subtlety is not the syntax. It is that **a filter changes what your aggregates
mean**, and the query cannot tell you that you did it.

## The question

> When was dissolved oxygen in the aeration basin outside its normal band of
> 1.5–3.0 mg/L over the last day?

## The obvious attempt

```sql
SELECT ts, value
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND (value < 1.5 OR value > 3.0)
  AND ts >= (SELECT max(ts) FROM reading) - interval '1 day'
ORDER BY ts;
```

**Zero rows.** Which is a real answer — the aeration basin is well controlled, and
if you had instead asked about effluent ammonia you would get 51 of them in a
single day. The trouble is that zero rows is also what a typo produces, and you
cannot tell the difference from the output.

## Put the band in the database, not in the query

Here is the thing the relational schema buys you, and it is worth pausing on. The
normal band is **in `signal`**. Not typed into the query, not repeated in every
dashboard, not copied into four million historical rows — in a column, once:

```sql
SELECT id, unit, range_min, range_max, normal_low, normal_high
FROM signal
WHERE id = 'EFFLUENT:FLOW:NH4';
```

```
         id             |  unit  | range_min | range_max | normal_low | normal_high
-----------------------+--------+-----------+-----------+------------+-------------
 EFFLUENT:FLOW:NH4     | mg/L   |       0.0 |        30 |        0.5 |        8.0
```

Now the query can *ask* whether a reading is in its band, without anybody having
to remember what the band is:

```sql
SELECT
    r.ts,
    r.value,
    s.unit,
    s.normal_low,
    s.normal_high,
    CASE
        WHEN r.value < s.normal_low THEN 'below'
        WHEN r.value > s.normal_high THEN 'above'
        ELSE 'in band'
    END AS position
FROM reading r
JOIN signal s ON s.id = r.signal_id
WHERE r.signal_id = 'EFFLUENT:FLOW:NH4'
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
  AND (r.value < s.normal_low OR r.value > s.normal_high)
ORDER BY r.ts;
```

Two things to notice.

**The `CASE` expression.** This is the thing the previous version of this course
could not write at all — the dialect had no conditional expressions, which is
written up in
[`_shared/INFLUXQL-NOTES.md`](../_shared/INFLUXQL-NOTES.md). It is also, now,
completely unremarkable, which is the honest characterisation of most of what a
better tool buys you.

**The join inside the `WHERE` clause.** `r.value < s.normal_low` refers to a
column from `signal`, and the `WHERE` is evaluated after the `JOIN`. That is legal
and it is not obvious. It is also *the same query*, logically, as filtering on
`value < 2.0` — but the second version is a lie the moment somebody retunes the
probe.

## The filters you will actually want

<!-- check: skip -->
```sql
-- One of several signals
WHERE signal_id IN ('EFFLUENT:FLOW:NH4', 'AERATION:AHU-1:NH4_IN',
                    'AERATION:AHU-1:DO')

-- Readings in a specific window, anchored to the data rather than the clock
WHERE ts >= '2026-09-26 00:00:00+00'
  AND ts <  '2026-09-27 00:00:00+00'

-- Only readings you trust
WHERE quality = 0

-- Only readings that have a value at all
WHERE value IS NOT NULL
```

Note the deliberate ordering habit: **half-open intervals**. `>= start AND < end`,
never `BETWEEN`. `BETWEEN` is inclusive at both ends, which is exactly right for
consecutive buckets and exactly wrong at a window boundary — you get every row at
midnight twice, or once, depending on how you wrote it. Both are defensible; what
is not defensible is not knowing which one you have.

## `IS NULL` and `IS DISTINCT FROM` are not the same as `= NULL`

<!-- check: skip -->
```sql
-- Does not work. Always returns no rows.
WHERE value = NULL

-- Does work
WHERE value IS NULL
```

`NULL` means *unknown*, and unknown does not equal anything, including itself. This
is not a quirk to work around, it is three-valued logic and it is load-bearing
throughout this course. From
[00-03](../00-foundations/00-03_quality_is_data.md): a `NULL` value is a **Bad**
reading, and the single most common way to get this wrong is to write
`WHERE value = NULL` and conclude there are no broken instruments.

The same three-valued logic is why `NOT IN` is dangerous:

<!-- check: skip -->
```sql
-- Returns no rows if ANY of the ids is NULL, even if others match
WHERE signal_id NOT IN (SELECT equipment_id FROM signal)
```

`IN` works by comparing against every candidate, and one `NULL` candidate poisons
the whole thing. `NOT EXISTS` does not have this problem, and
[02-intermediate](02-intermediate/) is where you will meet both.

## The habit that makes filtering safe

**Count before you filter.** Every time. It takes one extra query and it is the
difference between "the plant was fine" and "I filtered out the interesting part":

```sql
SELECT
    count(*)                                          AS everything,
    count(*) FILTER (WHERE r.value IS NOT NULL)      AS with_a_value,
    count(*) FILTER (WHERE r.value < s.normal_low
                       OR r.value > s.normal_high)   AS out_of_band,
    count(*) FILTER (WHERE r.quality = 0)            AS good_quality
FROM reading r
JOIN signal s ON s.id = r.signal_id
WHERE r.signal_id = 'EFFLUENT:FLOW:NH4'
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '1 day';
```

`FILTER (WHERE …)` is the readable way to say *of these rows, the ones where…*,
without a `CASE` and an `ELSE 0`. You will use it constantly.

Now you can read the result as a sentence: "98 readings, all of them with a value,
51 of them out of band, and every one of them Good quality." A reader knows
exactly what they are looking at and what has been discarded.

## Exercises

1. `AERATION:AHU-1:DO` has an out-of-band count of 0. Find three signals in this
   plant that *do* go out of band, using only `signal` and `reading`, and say which
   you would want to be paged for.
2. Write the same out-of-band query twice: once with a hard-coded threshold, once
   against `signal.normal_low`/`normal_high`. Then change one signal's
   `normal_high` in the database and re-run both. Which one was right, and what did
   the other one become?
3. Prove that `value = NULL` returns no rows and `value IS NULL` returns some:
   write both, and then write the query that counts Bad readings *correctly*
   without touching `value` at all.
4. `BETWEEN` versus `>= … AND < …` on a six-hour window. Write a window query both
   ways, find a row that lands on the boundary, and show which version you would
   use for a shift report and why.
5. Find a `NOT IN` query on this schema that silently returns nothing because of a
   `NULL`, using only the tables you have. You may need to look at
   [02-intermediate](02-intermediate/) for the shape, or invent it.
