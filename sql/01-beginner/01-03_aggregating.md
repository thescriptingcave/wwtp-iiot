# 01-03 — What is the average, and over what?

**Previous:** [01-02](01-02_filtering.md) · **Next:** [01-04](01-04_time_buckets.md)

`avg()` is easy. *What you average over* is not, and the difference is the
difference between a number and a measurement.

## The question

> What is this plant's typical dissolved oxygen, and typical effluent ammonia,
> over the last day?

## The obvious query

```sql
SELECT avg(value) FROM reading WHERE signal_id = 'AERATION:AHU-1:DO';
```

One number. It is a real number, computed from real data, and it is nearly
meaningless, for three reasons worth taking in order.

**It ignores the interval.** A mean over a day and a mean over a week are
different numbers answering different questions, and this query does not say
which it computed.

**It does not say how much data it had.** `avg` of 10 readings and `avg` of
86 400 are equally weighted in the output and not equally trustworthy.

**It ignores the unit.** If you forget the `signal_id` filter you are averaging
dissolved oxygen in mg/L with blower speed in rev/min. You get a number, and it is
nonsense. This is the first mistake everyone makes, and it is worth doing once
deliberately — drop the filter and look at what comes back.

## The query that answers the question

```sql
SELECT
    r.signal_id,
    s.unit,
    count(*)                AS readings_total,
    count(r.value)          AS readings_usable,
    count(*) - count(r.value) AS readings_without_a_value,
    avg(r.value)            AS mean_value,
    min(r.value)            AS min_value,
    max(r.value)            AS max_value
FROM reading r
JOIN signal s ON s.id = r.signal_id
WHERE r.signal_id IN ('AERATION:AHU-1:DO', 'EFFLUENT:FLOW:NH4')
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY r.signal_id, s.unit
ORDER BY r.signal_id;
```

```
       signal_id        | unit | readings_total | readings_usable | ... |  mean_value  |  min_value  |  max_value
------------------------+------+----------------+-----------------+-----+--------------+-------------+-------------
 AERATION:AHU-1:DO     | mg/L |            229 |             229 |   0 | 2.131304676  | 1.324079173 | 2.670028198
 EFFLUENT:FLOW:NH4      | mg/L |             98 |              98 |   0 | 7.837183410  | 5.628561568 | 9.535034909
```

### What to notice

**`count(*)` and `count(value)` are both in the output on purpose.** When they are
equal, every reading that happened had a value, and the aggregates describe the
whole period. When they differ, part of the window had no usable data and the mean
silently describes less than you think. From
[00-03](../00-foundations/00-03_quality_is_data.md): `count(*)` counts rows,
`count(value)` counts rows with a value, and `avg` ignores the `NULL`s — so it is a
mean over what you can use, which is right, and it is right *by accident of SQL's
null handling* rather than by anything you wrote.

**`min` and `max` next to `mean`** is the cheapest anomaly detector ever written. A
mean sitting comfortably between a min and a max which are both implausible is
telling you about two different things and you should not trust it.

**`IN (...)` for several signals.** The previous version of this course could not
write that — no `IN` in the dialect — and the lesson had to run one query per
signal. It is worth noticing how much of the *shape* of a query is decided by what
the database can express.

## `HAVING`: filtering the aggregate, not the rows

```sql
SELECT
    r.signal_id,
    s.unit,
    s.normal_low,
    count(*)       AS n,
    avg(r.value)   AS mean_value
FROM reading r
JOIN signal s ON s.id = r.signal_id
WHERE r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY r.signal_id, s.unit, s.normal_low
HAVING count(*) >= 50 AND avg(r.value) > s.normal_low
ORDER BY mean_value DESC;
```

**`s.normal_low` has to be in the `GROUP BY`, and leaving it out is one of the
best error messages in SQL:**

```
ERROR:  column "s.normal_low" must appear in the GROUP BY clause
        or be used in an aggregate function
LINE 1: ...P BY 1,2 HAVING count(*) >= 50 AND avg(r.value) > s.normal_l...
                                                             ^
```

Read what it is actually telling you. You grouped by `signal_id` and `unit`, and
then asked about `normal_low` — a column you did not group by and did not
aggregate. Postgres is refusing to guess which group's `normal_low` you meant,
because there is only one and the query as written does not say so.

There *is* a case where Postgres will let you get away with it: if you group by
the **primary key** of a table, every other column of that table is
functionally dependent on it and need not be named. Group by `s.id` and
`s.normal_low` is legal on its own. That is a real feature and a real trap,
because the *reason* the query above fails is that you grouped by `r.signal_id` —
a column of `reading` — and not by `s.id`.

Group by the identity you mean.

**`WHERE` runs before `GROUP BY`; `HAVING` runs after.** That is the entire
distinction and it is worth being able to state it, because the two look
interchangeable and are not:

* `WHERE avg(value) > 2` — **error**: `aggregate functions are not allowed in
  WHERE`. The rows do not know their average yet; they have not been grouped.
* `HAVING avg(value) > 2` — fine, because by then the groups exist.

A rule of thumb that holds nearly always: **`WHERE` filters rows, `HAVING` filters
groups.** If your `HAVING` condition mentions no aggregate, you probably wanted
`WHERE`.

**The `HAVING` clause refers to a column of `signal`, not of `reading`.** That is
the payoff of grouping by identity rather than by a string you typed in: "signals
whose average is above their own normal band" is expressible, and it stays correct
when somebody retunes a probe. The previous dialect could not express it at all.

## The question the average cannot answer

> What is the *typical* dissolved oxygen?

`avg` answers "what is the arithmetic mean of the values that passed the deadband",
which is a subtly different question. The deadband keeps readings that *changed*,
so a settled signal is under-sampled and a moving one is over-sampled — and
`avg` weights them equally.

```sql
-- Does the sampling rate vary by hour? If it does, the plain mean is suspect.
SELECT
    date_trunc('hour', r.ts) AS bucket,
    count(*)                 AS n,
    avg(r.value)             AS mean_value
FROM reading r
WHERE r.signal_id = 'AERATION:AHU-1:DO'
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY 1
ORDER BY 1;
```

If `n` is steady, the plain mean is fine. If it swings, then some hours are
better sampled than others and the weighted question is the right one — which is
`04-expert`, and which is a real problem in a real plant.

## Your aggregate is not bit-reproducible, and that is not a bug

Run this query. Then run it again. Then run it four more times.

<!-- check: skip -->
```sql
SELECT
    s.unit,
    count(*)     AS n,
    avg(r.value) AS mean_value
FROM reading r
JOIN signal s ON s.id = r.signal_id
WHERE r.signal_id = 'AERATION:AHU-1:AIR_FLOW'
  AND r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY s.unit;
```

Five consecutive runs of that query on this machine, on this data:

```
 6391.155254170624
 6391.155254170615
 6391.155254170614
 6391.1552541706105
 6391.15525417062
```

**The same query, on the same data, giving different answers in the sixteenth
significant digit.** About one part in 10¹⁵. This is not a TimescaleDB quirk and
it is not an InfluxDB quirk. It is floating-point arithmetic.

(If you are running this through `psql` you will see a stable number, because
`psql` and this project both happen to get the same plan. That is luck, not a
guarantee, and it is worth noticing *which* tools hide it and which do not — the
difference between a result you can reproduce and one you happened to get.)

### Why

Floating-point addition is not associative. `(a + b) + c` and `a + (b + c)` are
different numbers, and the last bits are where that shows. So `sum()` and `avg()`
over `DOUBLE PRECISION` depend entirely on **the order the rows are added in**.

Postgres aggregates in parallel by default. It picks a number of workers from the
table size, decides how to divide the rows among them, and each worker produces a
partial sum. The partial sums are then combined. The plan is not pinned, so the
division of labour is not pinned, so the addition order is not pinned, so the last
bits of the answer are not pinned.

```sql
-- and it is the plan, not the data
EXPLAIN SELECT avg(value) FROM reading WHERE signal_id = 'AERATION:AHU-1:AIR_FLOW';
```

You will find `Gather` and `Parallel Append` in there. On a four-million-row table
that is the *right* plan — it is four times faster — and it is also the reason the
sixteenth digit moves.

### When this matters, and when it does not

**It does not matter** for anything a human reads. Nobody has ever been wrong about
a plant because an average was 6391.155254170616 rather than 6391.15525417062.

**It matters** in three places, and you will meet all three:

1. **Tests.** `assert avg == 6391.15525417062` fails on a Tuesday. Assert with a
   tolerance, or round, or compare aggregates that are integers.
2. **Reproducibility of a report.** If a compliance return has to be reproducible
   from a database snapshot, "the same snapshot gives a different answer" is a
   real problem, and the fix is to store the result rather than recompute it.
3. **Change detection.** A query that alarms on "the mean moved by more than
   0.001" will not fire on this. A query that alarms on "the mean changed" will
   fire constantly.

The fix for the second and third is not to fight the arithmetic. It is to
**round at the point of comparison**, to a precision that means something to the
process:

```sql
-- 0.1 m3/h on a 26 000 m3/h range: a real change, and far above the noise floor
SELECT round(avg(value)::numeric, 1) FROM reading
WHERE signal_id = 'AERATION:AHU-1:AIR_FLOW';
```

`round(x, 1)` returns `NUMERIC`, not `DOUBLE PRECISION`, and `NUMERIC` addition *is*
associative. Rounding also tells the reader how much precision you are claiming,
which is information a raw float never carries.

> **Round to the precision the answer is meaningful at, and no further.**

Dissolved oxygen is reported to 0.01 mg/L. Ammonia permit limits are reported to
0.1 mg/L. Flow to whole m³/h. Carrying fifteen digits past that point is not
precision, it is noise with extra steps.

### The part that is not just about the value: the *order* moves too

Take the `HAVING` query from the top of this lesson and run it fifteen times. Two
different orderings come back, with the same 31 rows in each.

That is the same jitter, one level up. Two signals' daily means differ by about
10⁻¹², and the query says `ORDER BY mean_value DESC` — so which of them comes
first is decided by a bit that parallel aggregation re-rolls every time. Not the
value being wrong. **The ranking being unstable.**

That is a much nastier property than a noisy digit, because:

* a diff of two reports shows a changed number, which somebody investigates;
* a diff of two rankings shows rows in a different order, which reads as
  *"the database is broken"* and is much harder to dismiss.

The fix is the same one, and it is worth being deliberate about it: **round in the
`ORDER BY` as well as in the `SELECT`.**

<!-- check: skip -->
```sql
-- Unstable: the sort key is a float whose last bits move
ORDER BY mean_value DESC

-- Stable: the sort key is NUMERIC, whose addition is associative
ORDER BY round(mean_value::numeric, 2) DESC
```

And when two rows genuinely tie after rounding, they will still swap — so add a
deterministic tie-break:

<!-- check: skip -->
```sql
ORDER BY round(mean_value::numeric, 2) DESC, signal_id
```

**Any `ORDER BY` on a float needs a tie-breaker to be reproducible.** That is true
of `rank()`, of `ntile()`, of `dense_rank()`, of every "top N" query you will ever
write, and it is the single most common source of "the dashboard reordered itself
and nobody knows why".

## When the window is a column, not a constant

Hard-coding `interval '1 day'` is fine for a question and wrong for anything you
want to reuse. Put the window in a CTE and change it in one place:

<!-- check: skip -->
```sql
WITH window AS (
    SELECT max(ts) - interval '1 day' AS start FROM reading
)
SELECT
    r.signal_id,
    count(*)     AS n,
    avg(r.value) AS mean_value
FROM reading r, window w
WHERE r.ts >= w.start
  AND r.signal_id IN ('AERATION:AHU-1:DO', 'EFFLUENT:FLOW:NH4')
GROUP BY r.signal_id
ORDER BY r.signal_id;
```

The anchor is `max(ts)`, **not** `now()`. The seeded data is in the past and
`now()` is the wall clock; anchoring to the data's own latest timestamp is the
habit that makes a query work against historical data and live data alike.

## Exercises

1. Compute `avg(value)` for `AERATION:AHU-1:DO` twice: once with `quality = 0` in
   the `WHERE`, once without. They agree — explain why, and then say when they
   would *not*.
2. Which signal has the largest gap between `readings_total` and `readings_usable`?
   What does that tell you about the plant? (The answer is currently "nothing",
   and working out *why* nothing is the useful part.)
3. `avg(value)` over an hour where only two readings exist returns a number. Is that
   number wrong? What would you add to the query so a reader could tell?
4. Write the `HAVING` query from a subquery, the way you would have had to in the
   previous dialect. Which is more readable, and does the answer change?
5. For each of five signals, compute `avg(value)` over the last day and over the
   last hour. Which ones differ most, and what does a large difference tell you
   about the deadband rather than about the plant?
6. `count(*)` and `count(value)` are equal in all of the data so far. Construct a
   `reading` row where they are not, using only an `INSERT` the schema allows.
7. Write a query that computes `avg(value)` for one signal twice — once with
   `double precision`, once as `::numeric` — and run each five times. Which one
   is reproducible, and why does the cast to `numeric` change that?
8. The retention tiers are 1 s, 1 min and 1 h. `reading_1h` stores
   `avg(value)`, which by the above is not bit-reproducible. Work out what that
   means for a query that reads a historical mean from `reading_1h` and compares
   it with a freshly computed mean from `reading`.
9. Find two signals in this plant whose daily means differ by less than 10⁻⁹, then
   `ORDER BY` a float that separates them and run the query twenty times. How many
   orderings do you get? Now add `round(…::numeric, 2)` to the `ORDER BY` and
   repeat.
