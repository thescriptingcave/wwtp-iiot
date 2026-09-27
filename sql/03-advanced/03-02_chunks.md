# 03-02 — Chunks: why a query over 4.3 million rows is fast

**Previous:** [03-01](03-01_continuous_aggregates.md) ·
**[Next: 03-03](03-03_retention.md)** · **[Back to the course](../README.md)**

`reading` has 4 290 000 rows and a query across all of them returns in
milliseconds. That is not Postgres being fast. It is a consequence of one
decision in `storage/postgres/schema.sql` that this lesson exists to make
legible, because you cannot reason about a hypertable's performance — or debug a
query on one — until you know it is not one table.

## The question

> A `SELECT count(*)` over the whole hypertable touches every row. Why is that not
> slow?

## The answer in one query

<!-- check: skip -->
```sql
-- Not a table. A parent with children.
SELECT chunk_schema, chunk_name, range_start, range_end,
       pg_size_pretty(total_bytes) AS size
FROM timescaledb_information.chunks
WHERE hypertable_name = 'reading'
ORDER BY range_start;
```

Thirty-odd rows. Each is a **real PostgreSQL table** with its own file on disk,
its own indexes, and its own constraints — and a chunk is a table, not a
materialised view or a partition in the loose sense. The next lesson's
constraint errors name a chunk, and that is not a bug: a constraint on a
hypertable lives on every chunk, because a chunk *is* a table.

## What chunk exclusion actually does

The planner is given the query and a list of chunks, and it removes every chunk
that cannot contain a matching row. This is not a hint and it is not a runtime
filter — it happens during planning, and the chunks never reach the executor.

```sql
-- One hour of one day. Which chunks can possibly hold it?
SELECT count(*) AS chunks_touched
FROM timescaledb_information.chunks
WHERE hypertable_name = 'reading'
  AND range_start <= '2026-09-22 14:00:00+00'
  AND range_end   >  '2026-09-22 13:00:00+00';
```

The answer is 1 or 2, not 30. `WHERE ts >= … AND ts < …` is a *ranged* condition
and every chunk has a `range_start`/`range_end`, so the exclusion is arithmetic on
the catalog rather than a scan of the data.

Which is exactly why the shape of your predicates matters:

<!-- check: skip -->
```sql
-- No chunk exclusion at all. `value` is in no chunk's range metadata, so the
-- planner has to keep every chunk in the plan and decide at runtime.
SELECT count(*) FROM reading WHERE value > 2.0;
```

That query is honest, correct, and the slowest thing in this course. A filter on
the *time* column is free; a filter on anything else is a full scan. Every
lesson in stages 01 and 02 filters on `ts` for this reason, and now you know
what it was buying.

## The subtlety that catches people: and non-ranged predicates

A query with a ranged predicate *and* another condition only gets exclusion from
the ranged part — but if the other condition is itself a function of `ts`, the
planner can still use it. This one excludes every chunk:

<!-- check: skip -->
```sql
-- `date_trunc` is a function of ts, so chunk exclusion still applies, because
-- the planner can prove the expression is monotonic in ts.
SELECT count(*) FROM reading
WHERE date_trunc('day', ts) = '2026-09-22';
```

This one does not:

<!-- check: skip -->
```sql
-- `extract` on a *value* column, not on ts. Nothing to exclude with.
SELECT count(*) FROM reading WHERE extract(hour FROM ts)::int % 6 = 0;
```

Both are the same shape to a reader. One scans a day; one scans a week. The
distinction is whether the predicate is a function of the partitioning column,
and that is worth checking whenever a query you expected to be fast is not.

## Confirming exclusion happened

The plan says so explicitly. Lesson [03-04](03-04_explain.md) is about reading
these in full; the line to look for here is the absence of chunks:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM reading
WHERE ts >= '2026-09-22 00:00:00+00' AND ts < '2026-09-22 01:00:00+00';
```

A plan that mentions `_timescaledb_internal._hyper_*` by name is showing you the
chunks it kept. A plan that mentions thirty of them where you expected two is
telling you your predicate is not ranged, and the fix is in the query rather
than in the index.

## What this means for the rest of the course

Three practical consequences, all of which are reasons for things earlier
lessons did without explaining:

1. **Never write a query with no time bound.** Not because it is slow — one-off
   and test queries are fine — but because it is a habit that becomes an outage
   when someone puts it in a dashboard.
2. **A missing time bound on a `GROUP BY signal_id` is a week-long scan per
   signal.** The exclusion is per chunk, and the grouping does not help.
3. **`ORDER BY ts LIMIT 10` is fast even with no time bound**, because TimescaleDB
   plans a chunk-aware walk and stops at the first chunk with rows. This is the
   one case where the absence of a bound is cheap, and it is worth knowing
   precisely because it is the exception.

## Exercises

1. Count the chunks, and sum `total_bytes`, for `reading`, `reading_1m` and
   `reading_1h`. Which has more chunks, and is the answer about *time* or about
   *data volume*? (`chunk_time_interval` is set in `schema.sql`; read it.)
2. Run `EXPLAIN (ANALYZE, BUFFERS)` on a one-hour count and a whole-table count
   of the same column. Record `actual time` and `Buffers: shared read` for each.
   The ratio of reads should be far smaller than the ratio of rows — explain why,
   and then explain what that means for a `min_count`-style check across all
   signals.
3. Compare `WHERE ts >= x AND ts < y` against `WHERE ts BETWEEN x AND y`. The
   second looks simpler and is *wider* at the boundary. Construct a row that the
   first excludes and the second includes, and say which one you want in a query
   whose upper bound is `now()`.
4. Create a temporary table, insert 10 000 rows with timestamps spread over a
   month, and `ANALYZE` it. Then `EXPLAIN` a `GROUP BY date_trunc('day', ts)`.
   TimescaleDB will not chunk it — it is not a hypertable. What does the plan
   look like, and what does that tell you about what the chunking is actually
   buying you?
5. Find a query in stages 01 or 02 that filters on something other than `ts`, or
   that has no time bound at all. Rewrite it to be ranged. Measure both with
   `EXPLAIN (ANALYZE, BUFFERS)`.
