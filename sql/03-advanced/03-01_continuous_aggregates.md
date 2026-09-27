# 03-01 — Continuous aggregates: choosing a tier, and the query that lies to you

**Previous:** [02-04](../02-intermediate/02-04_gaps.md) ·
**[Next: 03-02](03-02_chunks.md)** · **[Back to the course](../README.md)**

There are three tables in this database and they hold the same plant. Choosing
wrongly costs you between three milliseconds and thirty seconds, and the reason it
costs anything at all is a design decision in `storage/postgres/schema.sql` that
this lesson exists to explain.

## The question

> I want the hourly mean of dissolved oxygen for the last day. Which table do I
> read?

## The obvious answer, and why it is only right half the time

<!-- check: skip -->
```sql
-- Correct. Just slow.
SELECT time_bucket('1 hour', ts) AS bucket, avg(value)
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY bucket
ORDER BY bucket;
```

This is the query you would write, and it is the right answer whenever the
question is *"what happened"*. It reads 1 645 rows for dissolved oxygen in a
seeded week, because the deadband suppresses most of them.

It is the wrong answer when the question is *"what is the plant doing now"* or
*"show me the trend"*, because:

* **it re-reads the same rows on every call.** A dashboard refreshes every ten
  seconds and gets slower as the retention window grows, because the window is
  the whole table.
* **it makes the caller choose a bucket size.** Nothing stops a dashboard from
  asking for `time_bucket('1 second', ...)` over a day, which is 86 400 groups of
  one row each, computed from 4.3 million.
* **and it re-computes the same aggregate every time nobody changed.**

## The three tables

| Table | Bucket | Built from | Retention | Rows in a seeded week |
|---|---|---|---|---|
| `reading` | none | the gateway | 7 days | ~4 290 000 |
| `reading_1m` | 1 minute | `reading` | 90 days | ~1 700 000 |
| `reading_1h` | 1 hour | `reading` | indefinitely | ~28 000 |

The two aggregates roll up **from `reading`, not from each other**. That is
the single most important decision in the schema and it is easy to get wrong:

<!-- check: skip -->
```sql
-- What this would compute, if someone rolled 1 h up from reading_1m:
--   the mean of hourly means of 1-minute means.
-- Which is the mean of the means, weighted by how many readings each minute
-- happened to contain. It is wrong, and it is wrong silently.
```

The deadband means a minute with six readings and a minute with six hundred
contribute equally to the average of averages. Averaging averages is only valid
when every group has the same number of members, and here they differ by two
orders of magnitude. `tests/integration/test_postgres.py::test_both_tier_buckets_come_from_raw_so_the_mean_is_a_true_mean`
seeds a deliberately uneven hour and asserts the two tiers agree — because
without that test this is a bug you would find by being suspicious, not by being
shown.

## The complete query

```sql
SELECT bucket, mean, min, max, n
FROM reading_1h
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND bucket >= (SELECT time_bucket('1 hour', max(ts)) FROM reading) - interval '1 day'
ORDER BY bucket;
```

Two differences from the version above, and both matter.

**`time_bucket` on the anchor.** The raw query can say
`(SELECT max(ts) FROM reading) - interval '1 day'` and the `WHERE` does the
rest. `reading_1h` stores *bucketed* timestamps, so `max(ts) - 1 day` lands in
the middle of a bucket and the first partial bucket is included with only some of
its data. Bucketing the anchor puts both sides on the same grid, which is the
general rule: **when you compare against a bucketed table, bucket the anchor
too.**

**No `GROUP BY`.** The table is already grouped. Adding one is not merely
redundant, it is a hint that you may have picked the wrong tier.

The column is called `mean`, not `avg`, and that is worth a moment.
`avg` is a *function*; a continuous aggregate's output column has to be an
ordinary column, because it is a stored value rather than something computed
per query. It is a small thing that becomes obvious the first time you write
`SELECT avg(mean) FROM reading_1h` by accident and get an error about a missing
`GROUP BY`.

## The part that surprises people: `materialized_only = true`

Both aggregates are declared:

<!-- check: skip -->
```sql
CREATE MATERIALIZED VIEW reading_1h
WITH (timescaledb.materialized_only = true) AS ...
```

That means **a query against `reading_1h` will not compute missing buckets from
`reading`.** It returns what has been materialised, and if a bucket has not been
refreshed it is simply not there.

The default is the opposite — `materialized_only = false` lets the planner
recompute unfilled ranges on the fly, so the view is always *complete* and always
*correct*. That sounds strictly better and in a dashboard it is.

This project chose `true` for one reason: **a silently-computed bucket has an
unknown cost.** With it off, a query whose time range happens to include one
unrefreshed bucket gets a plan that mixes a cheap index scan with an expensive
aggregate over raw rows, and the cost of that query is not something you can
reason about from the query text. With it on, a stale bucket is *visibly absent*,
which is a bug you notice, rather than a plan you do not.

The price is the refresh lag, and you have to manage it. `storage/postgres/schema.py`
attaches a policy; lesson [03-03](03-03_retention.md) is about its sibling.

### How to see the lag

```sql
-- The newest bucket that actually exists, per tier.
SELECT 'reading_1m' AS tier, max(bucket) AS newest FROM reading_1m
UNION ALL
SELECT 'reading_1h', max(bucket) FROM reading_1h
UNION ALL
SELECT 'reading', max(ts) FROM reading;
```

Three numbers, and they should be within one refresh interval of each other. If
`reading_1h` is hours behind `reading`, every dashboard built on it is showing a
plant that stopped existing — silently, because the query succeeded.

## When to use which tier

A decision you will make constantly, so it is worth having a rule rather than a
preference.

| You want | Use | Why |
|---|---|---|
| The last few minutes | `reading` | nothing is materialised that recent, and you need the actual samples |
| Today, on a dashboard | `reading_1m` | a day of raw DO is 1 645 rows; a day of 1-minute is 1 440 groups that are already computed |
| A week, a month, a trend | `reading_1h` | 168 groups instead of 12 000 |
| An exact value at an instant | `reading` | the aggregates are means, and a mean is not a value |
| A count, or a "did it move" question | `reading` | the aggregates carry `n`, but they cannot tell you *when* within the bucket |
| An export for someone else | `reading_1h` | 4.3 million rows is not a file anyone can open |

The rule underneath: **the aggregates answer questions about buckets. Only
`reading` can answer questions about moments.**

## The mistake this stage exists to prevent

Not "use the wrong tier" — that costs time and is obvious once you know. The
mistake is **assuming `reading_1h` is a smaller `reading`.**

It is not. It is a *different table* containing *different facts*, and three
things follow that have nothing to do with speed:

1. **A mean is not a peak.** `max` in `reading_1h` is the max of the bucket, so
   it is preserved. But the *timestamp* of that max is gone. If you need to know
   when the blower tripped, `reading_1h` cannot tell you and no amount of
   cleverness with the aggregate will recover it.
2. **A count is not a reading.** `n` tells you the bucket had 41 samples. It does
   not tell you what any of them said. A signal stuck at one value and a signal
   moving within its deadband both produce rows; only the second has variance.
3. **A gap is not the same at both tiers.** This is the important one, and it is
   where [02-04](../02-intermediate/02-04_gaps.md) meets this lesson.

## Gaps survive the rollup — differently

The deadband's ambiguity, which 02-04 is entirely about, is *not* fixed by the
aggregates. It gets a second form.

```sql
-- Signals that have gone quiet, measured at the hourly tier.
-- The bug: this cannot return a signal with no buckets at all, for exactly the
-- reason 02-04 opens with. A `GROUP BY` over readings never mentions the
-- signals that are missing from the readings.
SELECT s.id, s.unit, max(h.bucket) AS last_hour
FROM signal s
LEFT JOIN reading_1h h ON h.signal_id = s.id
GROUP BY s.id, s.unit
HAVING max(h.bucket) IS NULL
    OR max(h.bucket) < (SELECT max(bucket) FROM reading_1h) - interval '2 hours'
ORDER BY last_hour NULLS FIRST;
```

The `LEFT JOIN` is doing the work, and it is doing it *before* the `HAVING`. That
is the lesson of 02-02 arriving again one level up: **a filter on a computed
value cannot see rows the join threw away, so the join has to bring the missing
ones back as `NULL` first.**

Thirteen of the 57 signals produce exactly one reading in a seeded week. At the
hourly tier they produce roughly one bucket. This query finds them, and 02-04's
finds the same thirteen at the raw tier — from the same underlying fact, arriving
at two different resolutions.

## Exercises

1. Time all three versions of the hourly-mean query for `AERATION:AHU-1:DO` over
   one day, using `EXPLAIN (ANALYZE, BUFFERS)`. Record the `actual time` and the
   `Buffers: shared read` count for each. Which of the three numbers changed most
   between tiers, and why do you think that one is the informative one?
2. The aggregated tables carry `source` as a grouping column as well as
   `signal_id`. Count the rows in `reading_1h` for one signal. Then count
   distinct `(bucket, source)`. What have you just discovered about whether the
   two protocol faces are being stored separately at each tier, and is that what
   you would expect from `storage/postgres/schema.sql`?
3. `reading_1h` is `materialized_only`. Delete a row from `reading` and re-run
   your hourly-mean query for a range containing it. Now run the same query
   against `reading`. What did you have to do to see the effect, and what would
   you have had to do with `materialized_only = false`?
4. Write the "which signals have gone quiet" query at the 1-minute tier. Thirteen
   signals have one reading in a week — how many buckets do they have at that
   tier, and is the query from this lesson still the right shape?
5. Find a signal where the hourly `avg` is inside its `normal_low..normal_high`
   band but the hourly `min` is outside it. Then find one where the opposite is
   true. What would you conclude about alarm thresholds if you only had the
   aggregates?
6. `time_bucket('1 hour', ...)` in TimescaleDB takes an *optional origin*. Read
   what the default is, then bucket with an explicit origin of `'2000-01-01'`.
   Do the bucket boundaries move? If they do, which of the queries in this lesson
   silently depends on the default — and what happens to it on a server whose
   timezone differs from yours?
