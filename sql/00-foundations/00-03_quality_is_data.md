# 00-03 — Quality is data, and `value` can be NULL

**Previous:** [00-02](00-02_the_six_tags.md) · **Next:** [01-beginner](../01-beginner/)

This is the most important lesson in the course, and it is short.

## The problem

A dissolved-oxygen probe fouls. It keeps reporting — a number, every second,
entirely plausible — and the number is wrong. Nothing about it looks wrong. It is
in range, it is finite, and on a chart it is a line.

If your historian stores that reading without recording that it is not to be
trusted, you have built a system that cannot tell the difference between:

* the process did this, and
* the instrument thinks this.

Those are different failures with the same appearance, and the second one is how a
plant quietly discharges out of permit for six hours while a chart shows a flat,
healthy, entirely fictional line.

## What this project does about it

Every reading carries a `quality` field alongside its `value`:

| `quality` | Meaning |
|---|---|
| 0 | Good — the instrument is working |
| 1 | Uncertain — the value may be wrong (drifting probe) |
| 2 | Bad — the value is not to be used (sensor failure) |

And when the reading is not a number at all, **`value` is NULL and `quality` is
2**. The row still exists. That is deliberate, and it is the reason `value` is the
one nullable column in the table.

```
time                  signal     value   quality
2026-09-26T07:15:00   do_mg_l     2.03    0
2026-09-26T07:15:01   do_mg_l     2.05    0
2026-09-26T07:15:02   do_mg_l     <null>  2      ← the instrument failed
2026-09-26T07:15:03   do_mg_l     2.11    0
```

The NULL row is distinguishable from **both** of the alternatives:

| Representation | Distinguishable from "no data"? | From "a wrong number"? |
|---|---|---|
| Row dropped | no — looks like a gap | yes |
| `value = 0` | yes | **no — it says the basin had no oxygen** |
| `value = "nan"` | yes | no, and it is a *string* in a float column |
| `value = NaN` | no — plots as a gap | no |
| **`value` NULL, `quality` 2** | **yes** | **yes** |

## This was not my design. It was the database's.

InfluxDB 3 fixes a column's type on its first write, and the line protocol has no
literal for `NaN` or `Infinity`. So every attempt to store a broken reading as a
number is **refused**:

```
value=NaN,quality=2i   -> "No fields were provided"
value="nan",quality=2i -> invalid field value for field 'value':
                         expected type iox::column_type::field::float
```

What *is* writable is a point carrying a quality and no value, and it lands exactly
as the table above shows.

InfluxDB 2.x would have accepted a quoted `"nan"` in a float column, or a float
`NaN` that renders as a gap. Both would have been quietly, permanently wrong. **The
refusal is the feature**, and the schema was rebuilt around what the database would
not accept.

### The question

> Over the last hour, how much of this plant's dissolved-oxygen record is
> trustworthy?

### The queries

```sql
SELECT
  count(*)                     AS readings,          -- rows that exist
  count(value)                 AS usable,            -- rows with a value
  count(*) - count(value)      AS value_is_null,     -- failed instruments
  min(time)                    AS first_seen,
  max(time)                    AS last_seen
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T00:00:00Z';
```

The quality breakdown, which needs one query per quality because this dialect has
no `CASE`:

```sql
SELECT time, quality, count(*) AS readings
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T00:00:00Z'
GROUP BY time(1h), quality
ORDER BY time;
```

`quality` is a tag in that grouping, so you get one row per hour per quality rather
than a pivot. Reading it is a little more work than a pivot and a lot more honest,
because the row that *should not exist* is a row you can see is missing.

To count one quality in one cell, filter for it:

```sql
SELECT count(*) AS bad_readings
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l' AND quality = 2;
```

### The part that will bite you

`COUNT(*)` and `COUNT(value)` are different numbers, and **that is the point**.

* `COUNT(*)` counts rows. Every reading that happened.
* `COUNT(value)` counts rows where the value is not NULL. Every reading you can
  actually use.

An aggregate like `mean(value)` ignores NULLs, which is the *right* behaviour: a
failed sensor contributes nothing rather than dragging the average to zero. But
`COUNT(*)` does not ignore them, so **a naive "how many readings do we have"
under-reports how much data actually exists**, and a naive "what fraction is bad"
computed as `bad / COUNT(*)` is measuring the wrong thing.

```sql
-- Wrong: every Bad row is also a NULL, so this divides the bad count by
-- readings-that-exist rather than by readings-you-can-use.
SELECT count(*) / count(*) FROM "wwtp"."aeration" WHERE quality = 2;

-- Right: the fraction of *usable* readings that are Bad.
SELECT
  count(*) * 1.0 / NULLIF(count(value), 0) AS fraction_of_usable_that_is_bad
FROM "wwtp"."aeration"
WHERE quality = 2;
```

`NULLIF(..., 0)` is there so an empty result is NULL rather than a division by
zero, which would be a number that looks like data and means nothing.

### Exercises

1. Which signals in this plant have the most `quality = 2` readings? Those are the
   instruments that need attention — and the query is a maintenance report, not a
   process question. (`GROUP BY time(1d), signal` with `quality = 2` in the
   `WHERE`, since there is no `CASE` to pivot on.)
2. Compute the mean of `value` with and without a `WHERE quality = 0` filter.
   They should agree, because `mean` ignores NULLs. **Verify that**, then explain
   why they agree and when they would *not*.
3. The `everything_at_once` fault scenario in `contracts/fault-scenarios.yaml`
   describes a degraded instrument hiding a process fault. Using only
   `quality`, can you tell the difference? (You cannot. That is the argument for
   Phase 4's alarm engine, and this is the exercise that motivates it.)
