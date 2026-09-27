# 03 — Advanced

Four lessons about the *storage engine* rather than the query language. Stages 01
and 02 assumed a table you could scan; this stage is about a table split across
disk, where the shape of the data changes what the database is allowed to do
before your query runs at all.

| Lesson | The question |
|---|---|
| [03-01](03-01_continuous_aggregates.md) | Which of the three tables should this query read? |
| [03-02](03-02_chunks.md) | Why is this query fast when it scans 4 million rows? |
| [03-03](03-03_retention.md) | What is about to be deleted, and can you see it coming? |
| [03-04](03-04_explain.md) | How do you read what the planner actually did? |

## What changed, and why this stage could not exist before

Two reasons, and the second is the interesting one.

**The dialect had no rollups and no plans to read.** The previous storage engine
had a single bucket, a single key, and a query language with six documented gaps.
There was nothing to choose between, because there was only one thing. The
question "which tier should this query read?" has no answer when there is one
table.

**And there were two databases, so there was no such thing as a plan.** A query
that needed a reading *and* the name of the instrument it came from required two
connections and a join performed in Python. `EXPLAIN` was not available for the
part that mattered, because the part that mattered was not in the database.

## Before you start

```bash
docker compose --profile demo run --rm seed
```

This stage's lessons are the first that genuinely need the full week. 03-01
compares the cost of three tables and is meaningless without enough rows for the
comparison to have a result.

## The habit from stages 01 and 02, now with a reason

Every lesson still anchors to the data rather than the clock:

<!-- check: skip -->
```sql
WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 day'
```

The reason has changed, though. In stages 01 and 02 it was about *correctness* —
you get the same answer whatever time it is. In this stage it is also about
*cost*: a query anchored to `now()` against a database whose newest reading is a
week old asks the planner to scan a week of nothing, and on a hypertable the
planner may not be able to prove that cheaply.

## The one idea the whole stage rests on

**A continuous aggregate is not a view with `GROUP BY` in it. It is a
materialised table, incrementally maintained, that the planner is allowed to
ignore.**

That last clause is the one that surprises people, and lesson
[03-01](03-01_continuous_aggregates.md) is mostly about it. Both aggregates here
are declared `materialized_only = true`, which means a query against them **does
not silently fall back to computing the answer from `reading`** — it returns only
what has been materialised. That is a deliberate choice in this project and
lesson 03-01 explains what it buys and what it costs.

## A warning about the timings in these lessons

Every number quoted here was measured on one machine, on one dataset, and will
differ on yours. The *ratios* are the point and the ratios hold; the absolute
figures do not. Where a lesson gives a number it says which machine, because a
benchmark without its conditions is a number someone will quote.
