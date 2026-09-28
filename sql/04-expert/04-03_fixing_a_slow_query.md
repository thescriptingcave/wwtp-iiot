# 04-03 — Reading a plan, and acting on it

**[Back to the course](../README.md)** · **Previous:** [04-02](04-02_change_detection.md) · **Next:** [04-04](04-04_the_dashboard_query.md)

[03-04](../03-advanced/03-04_explain.md) taught you to read a plan. This lesson is
the other half: having read it, deciding what to do — and knowing which of the
numbers in the plan you are allowed to trust.

It is the shortest lesson in the stage, because the answer turns out to be one
clause.

## The question

> This query takes too long. Is the index broken, is the table too big, or is the
> query asking for more than it needs?

## Two versions of one query

```sql
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)
SELECT avg(value) FROM reading WHERE signal_id = 'AERATION:AHU-1:DO';
```

```
 Finalize Aggregate  (cost=975.31..975.32 rows=1 width=8) (actual rows=1 loops=1)
   Buffers: shared hit=1664
   ->  Append  (cost=214.19..975.30 rows=2 width=32) (actual rows=2 loops=1)
         Buffers: shared hit=1664
         ->  Partial Aggregate  (cost=214.19..214.20 rows=1 width=32) (actual rows=1 loops=1)
               Buffers: shared hit=509
               ->  Index Scan using _hyper_1_7_chunk_reading_signal_time_idx on _hyper_1_7_chunk
                     Index Cond: (signal_id = 'AERATION:AHU-1:DO'::text)
                     Buffers: shared hit=509
         ->  Partial Aggregate  (cost=761.09..761.10 rows=1 width=32) (actual rows=1 loops=1)
               Buffers: shared hit=1155
               ->  Index Scan using _hyper_1_8_chunk_reading_signal_time_idx on _hyper_1_8_chunk
                     Index Cond: (signal_id = 'AERATION:AHU-1:DO'::text)
                     Buffers: shared hit=1155
 Execution Time: 4.633 ms
```

**Look at the good news first: the index is being used.** Both scans are
`Index Scan using ..._reading_signal_time_idx`, and the `Index Cond` is exactly
the predicate you wrote. Nothing is wrong with the index. That rules out the most
common guess and it took four lines.

Now the part that matters. There are **two** scans — one per chunk — each doing its
own `Partial Aggregate`, joined by an `Append`. The hypertable is answering
"average DO over all of history" by aggregating every row this signal has ever
produced, in both chunks, and combining them.

## Add the clause you should have written

```sql
EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)
SELECT avg(value)
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND ts >= (SELECT max(ts) FROM reading) - interval '6 hours';
```

```
 Finalize Aggregate  (cost=330.65..330.66 rows=1 width=8) (actual rows=1 loops=1)
   Buffers: shared hit=163
   InitPlan 2 (returns $1)
     ->  Result  (cost=1.12..1.13 rows=1 width=8) (actual rows=1 loops=1)
   InitPlan 1 (returns $0)
     ->  Limit  (cost=1.10..1.12 rows=1 width=8) (actual rows=1 loops=1)
           ->  Custom Scan (DeferredChunkAppend) on reading reading_1
                 Order: ts DESC
                 Chunks Visited: 1
                 ->  Limit  (cost=0.43..0.45 rows=1 width=8) (actual rows=1 loops=1)
                       ->  Index Only Scan using _hyper_1_7_chunk_reading_ts_idx
                             Index Cond: (ts IS NOT NULL)
                             Heap Fetches: 1
   ->  Custom Scan (ChunkAppend) on reading  (cost=0.56..329.51 rows=2 width=32)
         Chunks excluded during startup: 0
         Chunks excluded during runtime: 1
 Execution Time: 0.805 ms
```

**`Chunks excluded during runtime: 1`.** One line, and it is the whole lesson. The
time predicate let the storage engine skip an entire chunk without reading a
single row of it — "runtime" because the bound that enabled the exclusion was
itself discovered at run time, which is what the `InitPlan` below is for.

The engine did that because the predicate's lower bound is itself a subquery, and
subqueries in a `WHERE` clause are evaluated **once**, before the scan begins.
That is what the two `InitPlan` nodes are. Without the subquery — a literal
`ts >= '2026-09-26'`, say — the engine would need to know the range at plan time
to exclude a chunk, and a hypertable cannot do chunk exclusion on a bound it only
discovers mid-scan. The `InitPlan` is not a detail. It is the mechanism.

| | no time filter | with time filter |
|---|---|---|
| chunks touched | 2 | **1** |
| buffers | 1664 | **163** |
| execution | 4.6 ms | **0.8 ms** |

**Ten times fewer buffers, six times faster** — from a clause that says what you
meant rather than what you defaulted to.

## The number you are allowed to trust

I ran the first query twice. Here is what it printed:

```
 Execution Time: 4.633 ms
 Execution Time: 4.581 ms
 Execution Time: 4.702 ms
```

**Three runs, a 2.6 % spread — and on a busy machine that spread has been an order
of magnitude larger.** If that is your measurement, you do not have a measurement.
You have a number that moves, and "it got slower" is not a conclusion you can draw
from it.

`Buffers: shared hit=1664` appeared in all three. That number is a count of pages
read from the buffer cache — a property of the work the plan did, not of how
quickly the machine happened to do it. **It is reproducible, and it is the number
to reason with.**

This is the single most useful habit in this lesson:

> **Reason about buffers. Report wall-clock only as a sanity check.**

`shared hit` means the page was already in memory — good, and it is why the
`Buffers` line matters more than the milliseconds. `shared read` would mean it went
to disk. A query that is slow in `hit` is CPU- or logic-bound and a faster disk
will not help it; a query that is slow in `read` is I/O-bound and one will. Those
have opposite fixes, and the `hit`/`read` split is what tells them apart.

## `rows=` is a guess, and it is the first thing to check

Every node carries two counts:

```
 Partial Aggregate  (cost=867.36..867.37 rows=1 width=32) (actual rows=1 loops=1)
```

`rows=1` is the planner's **estimate**. `actual rows=1` is what happened. When
they agree, the planner chose well. When they are far apart, the planner chose
based on a wrong picture, and no amount of index tuning fixes a bad estimate.

On a hypertable this matters more than usual, because the planner's statistics do
not see across chunks the way they see a plain table, and because the time
distribution of your data is rarely uniform. A dashboard query that is fast in
testing and slow in production is very often a planner whose estimate was right
about Tuesday and wrong about Sunday.

When the two disagree, the fix is `ANALYZE` and better statistics — not a new
index. Reaching for an index when the problem is an estimate is the most common
wasted afternoon in database work.

## What to take away

- **Check whether the index is used before assuming it is broken.** Four lines of
  plan answer it, and they usually say the index is fine and the query is asking
  for too much.
- **`Chunks Visited: N` is the hypertable's own evidence.** It tells you whether
  the engine skipped work, and it is why chunk exclusion — not indexes — is the
  main lever on a time series.
- **Chunk exclusion needs a bound known at plan time.** That is what
  `(SELECT max(ts) FROM reading)` buys you via the `InitPlan`; a literal cannot do
  it and `now()` would exclude everything.
- **Reason about `Buffers`, not milliseconds.** The same query measured 4.633,
  4.581 and 4.702 ms on unchanged data. Buffer counts reproduce; timings do not.
- **`hit` versus `read` decides the fix.** CPU-bound and I/O-bound look the same
  in milliseconds and have opposite remedies.
- **When `rows=` and `actual rows=` disagree, fix the estimate, not the index.**

## Exercises

1. **Break the exclusion.** Replace `(SELECT max(ts) FROM reading)` with a literal
   timestamp and re-run. Predict whether `Chunks Visited` changes before you do,
   then explain the difference using the `InitPlan` nodes in both plans.
2. **Make it slow on purpose.** Add `AND source = 'modbus'` to the filtered query
   — a value that exists in the schema but not in the data. Watch the estimate
   versus the actual, and say which one sent the planner the wrong way.
3. **Find a query the planner gets wrong.** Take a query from
   [02-03](../02-intermediate/02-03_window_functions.md) and compare `rows=` to
   `actual rows=` at the top node. Then `ANALYZE` and re-run it. Did the estimate
   improve, and did the query get faster?
4. **Find a `read` instead of a `hit`.** Every number in this lesson came from the
   buffer cache because the dataset fits in memory on this machine. Write down
   what you would expect to change on a database where it does not, and which of
   the two numbers in the table above would move.

---

**A note on these timings.** The buffer counts are properties of the data and the
plan and will reproduce. The milliseconds will not, which is the point of the
lesson rather than a caveat bolted onto it — the ratio between the two queries
holds, the individual figures do not, and the reason is above.
