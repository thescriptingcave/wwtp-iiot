# 02-03 — Window functions: comparing a row to its neighbours

**Previous:** [02-02](02-02_joins.md) · **Next:** [02-04](02-04_gaps.md)

`GROUP BY` collapses many rows into one and throws the rest away. That is right
when you want a summary and wrong the moment you want to keep the detail *and* add
context to it.

A **window function** computes across a set of rows and returns a value for each
one. Same rows in, same rows out, plus a number.

## The question

> Which readings of dissolved oxygen are unusual — for *this* signal, at *this*
> time of day?

The second half is the hard half, and it is the part `GROUP BY` cannot do at all.

## The query you would otherwise write

<!-- check: skip -->
```sql
-- A subquery per row, 4 000 times over
SELECT r.ts, r.value,
       (SELECT avg(r2.value)
        FROM reading r2
        WHERE r2.signal_id = r.signal_id
          AND extract(hour FROM r2.ts) = extract(hour FROM r.ts)) AS typical
FROM reading r
WHERE r.signal_id = 'AERATION:AHU-1:DO';
```

Correct, and it will take a very long time. Postgres will not turn a correlated
subquery into a join for you, so this is 4 000 separate scans. `EXPLAIN` it and
watch the `SubPlan` node with `rows=1` repeated beneath it.

## The window version

```sql
SELECT
    r.ts,
    r.value,
    round(avg(r.value) OVER (
        PARTITION BY r.signal_id, extract(hour FROM r.ts)
    )::numeric, 3)                       AS typical_for_this_hour,
    round((r.value - avg(r.value) OVER (
        PARTITION BY r.signal_id, extract(hour FROM r.ts)
    ))::numeric, 3)                      AS deviation
FROM reading r
WHERE r.signal_id = 'AERATION:AHU-1:DO'
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '3 days'
ORDER BY r.ts;
```

```
            ts             |      value       | typical_for_this_hour | deviation
----------------------------+------------------+-----------------------+----------
 2026-09-24 02:56:35.886867 | 2.0475771021588094 |             2.037 |    0.011
 2026-09-24 03:19:14.886867 | 2.0275752389823363 |             2.019 |    0.009
 2026-09-24 03:55:08.886867 | 2.007575139010864  |             2.019 |   -0.011
 2026-09-24 08:18:07.886867 | 1.9874158568803881 |             2.393 |   -0.406
```

The last row is the one to look at. At 08:18 the reading is 1.987, and the mean
for hour 8 across three days is 2.393 — a deviation of −0.406, ten times anything
else in the window. Either the aeration basin genuinely sagged that morning, or
something is wrong with the probe. **The query does not tell you which, and that
is correct: it computed a deviation, not a verdict.** Turning one into the other
is alarm design, and it is a separate decision with a cost — see exercise 5.

One scan. Every row keeps its own value and gains a comparison against the mean of
its own hour-of-day.

### The three parts of the clause

<!-- check: skip -->
```sql
<function>() OVER (PARTITION BY … ORDER BY … <frame>)
```

**`PARTITION BY` — the "group by" that does not group.** Rows are computed
independently within each partition. The result is one row per input row, which is
the whole point.

**`ORDER BY` — the running order.** Without it, functions like `lag` and `running
total` have nothing to walk along, and `sum()` over a partition is the same as
`sum()` over the group. *It also changes the default frame*, which is the third
part and the one everybody forgets.

**The frame — which rows count.** This is the subtle one.

## `ORDER BY` changes the answer, and that is the trap

```sql
-- Without ORDER BY: the frame is the whole partition
SELECT ts, sum(value) OVER (PARTITION BY signal_id)
FROM reading WHERE signal_id = 'AERATION:AHU-1:DO';

-- With ORDER BY: the frame is rows up to and including the current one
SELECT ts, sum(value) OVER (PARTITION BY signal_id ORDER BY ts)
FROM reading WHERE signal_id = 'AERATION:AHU-1:DO';
```

The first gives the same total on every row. The second gives a running total that
climbs.

The default frame is `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`, and for
a time series `RANGE` is a poor choice, because **`RANGE` groups peers**. Rows
with *identical* `ORDER BY` values are all in the frame together. With a timestamp
that almost never happens; with a `date_trunc`'d hour it happens constantly, and
the "running total" jumps at the end of every hour rather than at every row.

**On a time series, always say the frame explicitly:**

<!-- check: skip -->
```sql
sum(value) OVER (
    PARTITION BY signal_id
    ORDER BY ts
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
)                                    -- a true running total
```

`ROWS` counts physical rows; `RANGE` counts *values*. For anything ordered by a
timestamp that can tie — and a bucket boundary is a tie by definition — `ROWS` is
what you meant.

## The four functions worth knowing

### `lag` and `lead` — the previous and next value

```sql
WITH hourly AS (
    SELECT
        time_bucket(interval '1 hour', ts) AS bucket,
        avg(value)                         AS mean_do
    FROM reading
    WHERE signal_id = 'AERATION:AHU-1:DO'
      AND ts >= (SELECT max(ts) FROM reading) - interval '2 days'
    GROUP BY 1
)
SELECT
    bucket,
    round(mean_do::numeric, 3)                AS mean_do,
    round(lag(mean_do)  OVER w::numeric, 3)   AS previous,
    round(lead(mean_do) OVER w::numeric, 3)   AS next,
    round((mean_do - lag(mean_do) OVER w)::numeric, 3) AS change
FROM hourly
WINDOW w AS (ORDER BY bucket)
ORDER BY bucket;
```

```
        bucket         | mean_do | previous |   next   |  change
-----------------------+---------+----------+----------+----------
 2026-09-25 03:00:00+00 | 2.024   |    NULL  | 2.004    |    NULL
 2026-09-25 04:00:00+00 | 2.004   | 2.024    | 2.393    |   -0.020
 2026-09-25 08:00:00+00 | 2.393   | 2.004    | 1.896    |    0.389
```

Notice the gaps in the bucket list: 03:00, 04:00, then 08:00. Hours 05:00 to 07:00
have no rows at all, and `lag` jumps straight from 04:00 to 08:00. `lag(x, 1)`
means *"the previous **row**"*, not *"the previous **hour**"*. It has no idea how
much time passed, and the `change` of 0.389 spans four hours rather than one.

That is a genuine trap, not a hypothetical, and [02-04](02-04_gaps.md) is about
it: **a window function over a gappy time series silently compares across the
gap.** If the change is a rate — m³/h per hour — then dividing by the elapsed time
is not optional.

`lag(x, 1)` is the default; `lag(x, 3)` is three rows back.

**`WINDOW w AS (ORDER BY …)` is a named window.** Without it you write
`lag(mean_do) OVER (ORDER BY bucket)` and `mean_do - lag(mean_do) OVER (ORDER BY
bucket)` and they are two separate specifications that happen to be identical.
With a name, they cannot drift apart.

Note the first row's `previous` is `NULL`. **The first row of every window function
with an `ORDER BY` has no predecessor**, and arithmetic with `NULL` is `NULL`, so
the first `change` is `NULL` rather than 0. That is correct — there was no change,
there was a starting point — and it will silently drop the row from any `avg` you
compute over `change`.

### `rank` — position within a group

```sql
SELECT
    s.id AS signal_id,
    s.unit,
    date_trunc('day', r.ts) AS day,
    round(avg(r.value)::numeric, 3) AS mean_value,
    rank() OVER (
        PARTITION BY date_trunc('day', r.ts)
        ORDER BY avg(r.value) DESC
    )                       AS rank_that_day
FROM reading r
JOIN signal s ON s.id = r.signal_id
WHERE r.signal_id IN ('EFFLUENT:FLOW:NH4', 'AERATION:AHU-1:NH4_OUT')
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '2 days'
GROUP BY s.id, s.unit, date_trunc('day', r.ts)
ORDER BY day, rank_that_day;
```

```
          signal_id           |  unit  |       day         | mean_value | rank_that_day
------------------------------+--------+-------------------+------------+---------------
 AERATION:AHU-1:NH4_OUT       | mg/L   | 2026-09-25 00:00+00|      7.742 |             1
 EFFLUENT:FLOW:NH4            | mg/L   | 2026-09-25 00:00+00|      7.742 |             2
 AERATION:AHU-1:NH4_OUT       | mg/L   | 2026-09-26 00:00+00|      7.837 |             1
 EFFLUENT:FLOW:NH4            | mg/L   | 2026-09-26 00:00+00|      7.837 |             2
 AERATION:AHU-1:NH4_OUT       | mg/L   | 2026-09-27 00:00+00|      9.035 |             1
 EFFLUENT:FLOW:NH4            | mg/L   | 2026-09-27 00:00+00|      9.035 |             1
```

`rank() OVER (PARTITION BY … ORDER BY …)` works over the `GROUP BY` result, which
is the combination that trips people up: you aggregate first, then rank the
aggregates. That is a different thing from ranking the raw rows, and it is almost
always what you meant. Note that `date_trunc('day', r.ts)` appears in both the
`GROUP BY` and the window's `PARTITION BY` — the two are unrelated clauses and
both happen to need it here.

**Read the last two rows carefully.** Both signals show `9.035`, and both get rank
1. The *displayed* values are equal, but the underlying floats are not — they
differ somewhere past the third decimal — so `rank()` does not see a tie.

That is the float-jitter lesson from
[01-03](../01-beginner/01-03_aggregating.md) arriving somewhere it can do real
damage. A "top 3" panel that ranks on a float will show two things tied at rank 1
on Tuesday and one at rank 1 and one at rank 2 on Wednesday, from identical data.

**Rank on the rounded value, and always add a tie-break:**

<!-- check: skip -->
```sql
rank() OVER (
    PARTITION BY date_trunc('day', r.ts)
    ORDER BY round(avg(r.value)::numeric, 3) DESC, s.id
) AS rank_that_day
```

`dense_rank` differs from `rank` on ties — `1, 2, 2, 2, 5` versus `1, 2, 2, 2, 4` —
and for *"which day was worst?"* you usually want `dense_rank`, so ties share a
position and nothing is skipped.

### `ntile` — split into buckets

<!-- Deliberately left without a tie-breaker, because the point of the example
     is that it is non-deterministic: `check_sql.py` runs every query three
     times and fails one whose result changes, so a query meant to demonstrate
     this hazard cannot also be required to be stable. -->
<!-- check: skip -->
```sql
SELECT
    signal_id,
    ntile(4) OVER (ORDER BY avg(value)) AS quartile
FROM reading
WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY signal_id;
```

Careful: `ntile` over a `GROUP BY` result ranks by a *nondeterministic* ordering
when you do not name a tie-breaker, so two signals with the same mean can swap
quartiles between runs. Add a deterministic tie-break (`ORDER BY avg(value),
signal_id`) if the assignment has to be stable.

## The classic mistake: a window function in `WHERE`

<!-- check: skip -->
```sql
-- ERROR: window functions are not allowed in WHERE
SELECT ts, value FROM reading
WHERE avg(value) OVER (PARTITION BY signal_id) > 2;
```

The error is accurate and the reason is worth internalising: **`WHERE` runs before
the window is computed.** A window function needs the whole partition, and the
`WHERE` is what chooses the partition. So the answer cannot exist yet.

The way out is a subquery or a CTE — compute the window, then filter:

```sql
WITH scored AS (
    SELECT
        r.signal_id,
        avg(r.value) OVER (PARTITION BY r.signal_id) AS signal_mean
    FROM reading r
    WHERE r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
)
SELECT DISTINCT ON (sc.signal_id)
    sc.signal_id,
    round(sc.signal_mean::numeric, 3) AS signal_mean,
    s.unit,
    s.normal_high
FROM scored sc
JOIN signal s ON s.id = sc.signal_id
WHERE sc.signal_mean > s.normal_high
ORDER BY sc.signal_id, sc.signal_mean DESC;
```

```
          signal_id           | signal_mean |  unit  | normal_high
------------------------------+-------------+--------+-------------
 AERATION:AHU-1:NH4_OUT       |       7.837 | mg/L   |         5.0
 EFFLUENT:FLOW:NH4            |       7.794 | mg/L   |         5.0
```

`DISTINCT ON (col)` is Postgres' "one row per group" and it comes with a rule that
has caught out everybody at least once: **the `ORDER BY` must start with the
`DISTINCT ON` column.** Postgres will not let you order by something that is not
in the select list —

```
ERROR:  for SELECT DISTINCT, ORDER BY expressions must appear in select list
```

— and the error names the constraint rather than the fix. The first `ORDER BY`
expression is what picks the surviving row from each group; anything after it
orders the groups.

```
          signal_id           | signal_mean |  unit  | normal_high
------------------------------+-------------+--------+-------------
 AERATION:AHU-1:NH4_OUT       |       7.837 | mg/L   |         5.0
 EFFLUENT:FLOW:NH4            |       7.794 | mg/L   |         5.0
```

Note the second `JOIN`. The window function partitioned by `signal_id` and
produced no other column, so the normal band is not in scope and has to be joined
back in. That is the honest cost of a window over a single column, and it is
usually cheaper than the correlated subquery it replaces.

Same rule as `HAVING`, and the same underlying fact: **`WHERE` filters rows,
everything computed needs rows, so a computed value can only be filtered after.**

## Exercises

1. For `AERATION:AHU-1:DO`, compute the hourly mean and then its 6-hour moving
   average with `avg(...) OVER (ORDER BY bucket ROWS BETWEEN 5 PRECEDING AND
   CURRENT ROW)`. What is different about the first five rows, and is `NULL` the
   right answer for them?
2. The same moving average with `RANGE` instead of `ROWS`. Given that `bucket` is
   an hourly `time_bucket`, do they differ? Construct the case where they would.
3. Find the biggest single-reading jump in each signal over the last day, using
   `lag`. Then check the *time* between the two readings. What does a huge change
   across one second tell you that a huge change across an hour does not?
4. Rank each signal by how many readings it produced today, and identify the five
   quietest. For each, state whether you would expect that from its `deadband` and
   its `sample_ms`. (This is exercise 3 of [02-02](02-02_joins.md), done properly.)
5. Write a query that finds readings more than three standard deviations from their
   signal's mean. Then decide: is that a good anomaly detector for a signal whose
   *normal* behaviour is a slow ramp? What would be better?
6. `first_value(x)` and `last_value(x)` with a bare `ORDER BY` return the first and
   last row of the **whole partition**, not of the frame — a well-known surprise.
   Predict what `last_value(value) OVER (ORDER BY ts)` returns for every row of a
   partition, then check.
