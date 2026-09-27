# 00-01 — A time-series table is not a normal table

**Next:** [00-02](00-02_identity_and_values.md) · [Back to the course](../README.md)

Almost everything interesting about querying a plant is a consequence of one
fact: **there is far more of it than fits in memory, and most of it is old.**

Four million readings in a week. Two hundred and twenty million in a year. A
`SELECT *` over any of those is not a slow query, it is a query that will be
cancelled and take the database's mood with it.

## The question

> How many readings is that, really — and what happens to the database when you
> ask?

## The count

```sql
SELECT count(*) FROM reading;
```

```
  count
---------
 4289810
```

Four and a quarter million. Now ask for one signal over one hour:

```sql
SELECT count(*)
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND ts >= '2026-09-26 00:00:00+00'
  AND ts <  '2026-09-26 01:00:00+00';
```

The deadband is doing its job, so an hour of settled plant is a few hundred rows,
not 3 600. Change the window and the number changes with it. That is fine, and it
is worth knowing before you start optimising anything.

## What `reading` actually is

<!-- check: skip -->
```sql
\d reading
```

```
                 Column |          Type          | Nullable
------------------------+-----------------------+---------
 ts                    | timestamp with time zone |    false
 signal_id             | text                  |    false
 value                 | double precision      |     true
 quality               | smallint              |    false
 source                | text                  |    false
------------------------+-----------------------+---------
Indexes:
  "reading_pkey" PRIMARY KEY, btree (ts, signal_id, source)
  "reading_signal_time_idx" btree (signal_id, ts DESC)
Foreign-key constraints:
  "reading_signal_id_fkey" FOREIGN KEY (signal_id) REFERENCES signal(id)
Check constraints:
  "reading_null_is_not_good" CHECK (value IS NOT NULL OR quality <> 0)
  "reading_quality_known" CHECK (quality IN (0, 1, 2))
  "reading_source_known" CHECK (source IN ('opcua','modbus','seed','rollup'))
```

Five columns, and every constraint on that listing is doing work. Hold the two
`CHECK`s and the foreign key in mind; [00-03](00-03_quality_is_data.md) is about
them.

## A hypertable is a table that lies about being one table

`reading` is not a normal table. It is a **hypertable**: one logical table that
TimescaleDB has physically split into *chunks*, one per time range.

```sql
SELECT chunk_name, range_start, range_end
FROM timescaledb_information.chunks
WHERE hypertable_name = 'reading'
ORDER BY range_start;
```

```
         chunk_name        |      range_start       |        range_end
--------------------------+------------------------+------------------------
 _hyper_7_84_chunk         | 2026-09-17 00:00:00+00 | 2026-09-24 00:00:00+00
 _hyper_7_85_chunk         | 2026-09-24 00:00:00+00 | 2026-10-01 00:00:00+00
```

Two chunks, one week each, aligned to midnight UTC. You did not choose those
boundaries and you should not try to — TimescaleDB picks them, and it will create
a new one as the data grows past the end of the last.

**Why it matters:** every query you write in this course is the *same query* it
would be against an ordinary table. A hypertable is not a special dialect. It is
an ordinary relation that the storage engine happens to have partitioned, and the
payoff is that a query for one hour of one signal touches one chunk rather than
four million rows.

You can see the payoff directly:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT ts, value
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND ts >= '2026-09-26 00:00:00+00'
  AND ts <  '2026-09-26 06:00:00+00'
ORDER BY ts;
```

Read the plan for `Chunks excluded during startup:`. That number is the whole
argument. A query with a timestamp range does not examine the rows outside it,
which on an ordinary table it would have to do before it could discard them.

`03-advanced` goes through the rest of this plan. For now, notice two things: the
index `reading_signal_time_idx` on `(signal_id, ts DESC)` is doing the work, and
the chunk exclusion is doing the rest.

## The one thing that is genuinely different

Not the storage — the *ordering*.

```sql
SELECT ts, value FROM reading ORDER BY ts LIMIT 5;
```

On this table, time is not just a column you can sort by. It is the primary axis:
the primary key starts with `ts`, the hypertable is partitioned on it, and
retention is a per-chunk operation you can run in milliseconds.

That is why every lesson in the beginner stage buckets by time before it
aggregates. It is not a style preference — it is that the shapes you already know
how to write are the shapes the storage is arranged to answer.

## Exercises

1. Run the chunk query again after a few hours of new data, or after
   `docker compose --profile demo run --rm seed --days 14`. How many chunks now?
   Roughly how large is each one, in rows?
2. `EXPLAIN` a query with **no** time filter — just
   `WHERE signal_id = 'AERATION:AHU-1:DO'`. Compare `Chunks excluded during
   startup` with the query above. What does the plan have to do that the other
   one did not?
3. The primary key is `(ts, signal_id, source)` and there is a separate index on
   `(signal_id, ts DESC)`. Work out which queries the primary key serves and which
   the second index serves, and what would happen if the second one did not exist.
   Then check your answer with `EXPLAIN` on
   `SELECT max(ts) FROM reading WHERE signal_id = 'AERATION:AHU-1:DO';`
4. The retention policy is 7 days on `reading` and *indefinite* on `reading_1h`.
   Give a reason a plant would want that asymmetry, and then give a reason it
   might be a *mistake*.
