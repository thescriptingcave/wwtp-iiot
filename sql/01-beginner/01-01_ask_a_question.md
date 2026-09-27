# 01-01 — What is the plant doing right now?

**Previous:** [01 README](README.md) · **Next:** [01-02](01-02_filtering.md)

The simplest useful question, and the one that exposes why a time-series table
needs all six tags.

## The question

> What is the dissolved oxygen in the aeration tank right now, and where did that
> number come from?

## The naive query, and why it is wrong

```sql
SELECT value FROM "wwtp"."aeration" WHERE signal = 'do_mg_l';
```

This returns 86 400 rows for one day of data, oldest first, and you have no idea
which one is "now". Two things are missing, and both are habits rather than
syntax:

* **an order**, so the last row is the latest;
* **a limit**, so the database does not send you a day of readings to show you one.

```sql
SELECT time, value, quality, source
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
ORDER BY time DESC          -- newest first. `time` is the ONLY valid ORDER BY.
LIMIT 10;
```

`ORDER BY time DESC` and `LIMIT 10` are the two things standing between you and the
answer, and the second is not optional in practice: without it the database hands
you a day of readings so you can look at the last one.

`ORDER BY time DESC` is not a stylistic choice. `ORDER BY signal` is a **parse
error**: *"invalid ORDER BY, expected TIME column"*. InfluxQL will not order by
anything else, and when you need a different order you aggregate first and order by
the aggregate (see [01-03](01-03_aggregating.md)).

## The query that answers the question

One row per signal for the current state of the plant:

```sql
SELECT time, signal, eu, value, quality, source, equipment
FROM "wwtp"."aeration"
WHERE time >= now() - 10m
ORDER BY time;
```

Note `now() - 10m`, not `now() - INTERVAL '10 minutes'`. This dialect wants a Go
duration literal; `INTERVAL` is a parse error, and the message names no
alternative. See [`_shared/DIALECT.md`](../_shared/DIALECT.md).

And note that `ORDER BY signal` — which is what you would reach for — is **also** a
parse error. `ORDER BY` accepts `time` and nothing else, so this returns one row
per reading rather than one row per signal. Getting the latest value *per signal*
is a `02-intermediate` question, and the honest answer is that this dialect cannot
answer it in one query.

And the specific reading, with its units and validity attached — which is the
point of `eu` and `quality` being columns:

```sql
SELECT time, value AS dissolved_oxygen_mg_l, quality, source
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
ORDER BY time DESC
LIMIT 1;
```

You will want to turn `quality` into a word. **This dialect has no `CASE`
expression**, so you cannot, and the honest options are to do it in the application
or to keep a lookup table there. `02-intermediate` revisits this.

## What to notice

Three things, in order of how much they will save you later.

**`eu` travels with the reading.** You did not have to know that DO is in mg/L —
you asked. That is the entire argument for putting engineering units in the
historian rather than in a wiki page that drifts.

**`quality` travels with it too.** A number without its validity is half a
sentence. See [00-03](../00-foundations/00-03_quality_is_data.md).

**`source` is there so you can distrust it.** `modbus` and `opcua` read the same
probe. When they disagree you want to know which one you are looking at before you
conclude anything about the plant.

## A query worth stealing

Every stage of this course needs "the most recent value of each signal". This is
the shape, and you will write it again in [02-intermediate](../02-intermediate/)
with a window function instead of a subquery:

```sql
SELECT time, signal, eu, value, quality
FROM "wwtp"."aeration"
WHERE time >= now() - 10m
  AND quality = 0
ORDER BY time;
```

The `quality = 0` is not optional in practice. Including `quality = 2` rows means
including `value = NULL`, and a dashboard that shows a blank next to a real number
is a dashboard nobody trusts.

## Exercises

1. Do the same for the `effluent` table. Which signals are outside their normal
   band right now?
2. Filter to `quality = 2` and count how many instruments are currently
   untrustworthy. Then try to add a `CASE` that labels the quality, and read the
   parse error. You will meet that error again, and it is the most important thing
   this dialect does not have.
3. `SELECT ... WHERE time >= now() - INTERVAL '10 minutes'` will return **nothing**
   if the seeder wrote history in the past. Check what the actual time range of the
   data is, and write the query against that instead. (You will need `min` and
   `max` over `time`.)
