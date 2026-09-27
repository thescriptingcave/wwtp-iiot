# 03-04 — Reading `EXPLAIN`, which is the skill this stage is really for

**Previous:** [03-03](03-03_retention.md) · **[Back to the course](../README.md)**

Everything else in this stage is a thing the storage engine does for you. This
one is a thing you do, and it is the only lesson where the skill does not
transfer to any other database — the plan a hypertable produces has nodes in it
that a plain table never produces.

## The question

> This query is slow. How do I find out why without guessing?

## The only form you need

<!-- check: skip -->
```sql
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT TEXT) <your query>;
```

Four options and all four earn their place:

* **`ANALYZE`** — actually runs the query. Without it you get the planner's
  *guess* about cost, which is a model, not a measurement.
* **`BUFFERS`** — page reads. This is the number that tells you whether you have
  a CPU problem or an I/O problem, and the two have opposite fixes.
* **`VERBOSE`** — real output column names and every filter, so you can see the
  planner discarding a restriction you thought was being used.
* **`FORMAT TEXT`** — the default, and stated explicitly because `FORMAT JSON`
  exists and is the right choice for a tool reading plans. Do not use JSON by
  hand; it is unreadable and it is not more precise.

**`ANALYZE` runs the query.** For a `SELECT` that is harmless. For anything with
a side effect in a CTE — and `DELETE` inside a `WITH` is a real habit — it
performs the deletion. This is not a warning about a rare case; `EXPLAIN
ANALYZE` on a writing statement is one of the oldest ways to lose data, and this
project has already lost a week of seeded history to a program that ran SQL from a
directory.

## The output, top to bottom

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT signal_id, count(*), avg(value)
FROM reading
WHERE ts >= '2026-09-22 00:00:00+00' AND ts < '2026-09-23 00:00:00+00'
GROUP BY signal_id;
```

You get a tree, deepest node first, and the tree is read **inside out**:

* **The top line is the total.** `actual time=…` on the outermost node is the
  wall clock for everything. If it is fast, nothing below it matters.
* **`Buffers: shared hit=… read=…`** — `hit` is in cache, `read` is not. A query
  with `hit=900` and `read=0` is a CPU problem. A query with `read=90000` is an
  I/O problem, and **no amount of rewriting the query fixes that** — you need
  more cache or fewer rows.
* **A `Seq Scan` is not automatically bad.** On 4 290 000 rows a sequential scan
  may be the right plan. The sin is a *sequential scan on a hypertable where chunk
  exclusion should have applied*, and you can see that because the chunk names
  appear in the plan. Lesson [03-02](03-02_chunks.md).
* **`rows=…` in the plan is the planner's estimate.** When the estimate and
  `actual rows` differ by more than about an order of magnitude, the planner
  chose a join strategy or an index based on wrong information, and *that* is the
  bug. `ANALYZE` the table, or raise `default_statistics_target`.
* **`loops=…`** multiplies everything above it. A node estimated at 100 rows with
  `loops=5000` is five million rows, and reading only the `rows=` figure is how
  people misread nested loops for a week.

## Four plans and what each one means

Take one query and change one thing at a time. This is the exercise that teaches
the skill, and it is worth doing by hand once.

**1. No time bound.** Full scan, huge `read` count, seconds.

**2. A time bound.** Chunk exclusion kicks in, the plan names two chunks instead
of thirty, `read` drops by three orders of magnitude, sub-100 ms.

**3. Time bound plus `signal_id = …`.** Chunk exclusion still applies, plus an
index scan within each chunk. Faster again, and the *shape* changes — which is
the thing to notice, because the shape is what tells you which index earned its
keep.

**4. Time bound on `reading_1h` instead.** Same query, two orders of magnitude
fewer rows, and it is the same plan shape as (2). This is the clearest argument
for [03-01](03-01_continuous_aggregates.md) you will ever see: the aggregates are
not a convenience, they are the difference between a dashboard that works and one
that times out.

## The query that is fast and wrong

The most useful habit in this lesson is checking that the fast plan is the
*correct* plan:

<!-- check: skip -->
```sql
-- The plan says this touches 2 chunks and takes 40 ms. Does it return what you
-- think it does? Compare against the same query with a two-hour wider bound.
SELECT count(*) FROM reading
WHERE ts >= '2026-09-22 00:00:00+00' AND ts < '2026-09-22 01:00:00+00';
```

An empty result with a beautiful plan is a real outcome of chunk exclusion when
the range contains no data, and it is indistinguishable from "excluded everything"
unless you widen the bound and look.

This is the same shape as lesson [02-04](../02-intermediate/02-04_gaps.md) and
the `materialized_only` discussion in [03-01](03-01_continuous_aggregates.md): a
query that returns nothing successfully is the most dangerous kind of query, and
the cheapest defence is running a second one whose answer you can predict.

## When not to use `EXPLAIN ANALYZE`

* **On the gateway's write path.** `EXPLAIN ANALYZE` on an `INSERT` into
  `reading` *performs the insert*. Use plain `EXPLAIN` there.
* **To compare two queries by feel.** Times vary by a factor of three on a shared
  machine. Write the numbers down, as `make` targets and CI do, and compare the
  numbers.
* **When the query is fast enough.** If it returns in 20 ms, the plan is a
  curiosity. This lesson is for the ones that are not, and the ratio of time
  spent reading plans to time saved is strongly against reading plans for fast
  queries.

## Exercises

1. Take the query in this lesson and produce all four plans. Record the top-line
   `actual time` and total `Buffers: shared read` for each in a table. Which
   single change bought the most, and was it the one you would have guessed?
2. Find a query in stages 01 or 02 with no time bound. `EXPLAIN ANALYZE` it, then
   add a one-hour bound and do it again. Report the ratio of `read` counts and
   check it against the ratio of rows in range. Should they match?
3. `SELECT count(*) FROM reading WHERE signal_id = 'AERATION:AHU-1:DO'` with no
   time bound. Is chunk exclusion able to help? Why not, and what is the fastest
   rewrite that keeps the answer correct?
4. Find a place in this stage's lessons where a `Seq Scan` appears, and decide
   whether it is the right plan. Justify it from the row count and the `read`
   count rather than from the word "Seq".
5. `EXPLAIN (ANALYZE, BUFFERS)` the same query twice in a row, on a cold and
   then a warm cache. The `hit`/`read` split should move and the time should
   follow it. Which of the two numbers would you quote to a colleague, and why is
   the other one misleading?
