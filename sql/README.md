# The SQL course

Five stages, beginner to expert, run against data this project generates. It is
not an appendix. It is the reason the plant exists.

Every lesson states **a question**, gives **a query**, and has its expected output
in `_answers/`. Work them in order: the later stages assume the shapes the earlier
ones introduced.

## Before you start

```bash
docker compose up -d influxdb couchbase        # see docs/GETTING-STARTED.md
docker compose --profile demo run --rm seed    # a week of plant history
```

Without seeded data every query returns no rows and every chart is flat, and the
mistake everyone makes is concluding their SQL is wrong. Seed first.

Then check you can see anything at all:

```sql
SELECT COUNT(*) FROM "wwtp"."aeration";
```

If that returns 0, stop and fix the data before reading a word of the course.

## The stages

| Stage | What you learn | Why it comes when it does |
|---|---|---|
| [00-foundations](00-foundations/) | What a time-series table actually *is*; the six tag columns and the two fields; why `value` can be NULL | Because every later stage assumes you know this, and because a time-series table is not a normal table with a timestamp column |
| [01-beginner](01-beginner/) | `SELECT`, `WHERE`, aggregation, your first `time_bucket` | Because this is where "read one signal" becomes "answer a question about a signal" |
| `02-intermediate/` | CTEs, window functions, joins across the two databases | Unwritten |
| `03-advanced/` | Gap filling, continuous aggregates, retention, `INFORMATION_SCHEMA` | Unwritten |
| `04-expert/` | Window frames, time-weighted averages, change detection, query planning | Unwritten |

## How to work through a lesson

1. Read the question. Write down what you think the answer will be.
2. Run the query.
3. Compare with `_answers/`. If they differ, find out **why** before moving on.
4. Do the exercises at the bottom. They are not optional decoration; they are where
   the lesson actually lands.

The difference between reading a query and writing one is the difference between
recognising `mean(value)` and knowing that `AVG` over a table containing NULLs
ignores them, which changes your answer without telling you.

## Three things that will waste your time otherwise

Every one of these was measured against a live InfluxDB 3, and every one has an
error message that points somewhere else.

**`ORDER BY` accepts `time` and nothing else.**

```sql
-- invalid: ORDER BY accepts time and nothing else
SELECT signal, value FROM "wwtp"."aeration" ORDER BY signal;
-- error in InfluxQL statement: invalid ORDER BY, expected TIME column
```

Read [`_shared/DIALECT.md`](_shared/DIALECT.md) before lesson 01-01. It is the
list of things this dialect does *not* have, every one of them established by
running the query and reading the error — and three of them are in the SQL every
metrics tutorial teaches.

**`field` and `key` are reserved words.** That is why the tag is called `signal`
and why `AS key` is a parse error. You will meet both.

**Timestamp filters need RFC 3339, not epoch numbers.**

```sql
WHERE time >= '2026-09-26T00:00:00Z'   -- works
WHERE time >= '1758844800000'           -- "'1758844800000' is not a valid timestamp"
```

Writes carry integer timestamps; filters want a date string. The two forms are not
interchangeable and nothing says so.

## One more, which is a design decision rather than a quirk

**A broken instrument is stored as `value` NULL with `quality` = 2.** It is not
dropped, and it is not stored as zero or as a string.

That is the single most important idea in the course, and it is why `value` is
nullable. It means:

* a failed sensor is **distinguishable from no data at all** — the row exists;
* it is **distinguishable from a number that happens to be wrong** — `quality` says
  so;
* an aggregate that ignores NULLs (`mean`, `min`, `max`) is doing the *right*
  thing, and one that does not (`count(value)` versus `count(*)`) will quietly
  give you a different answer.

InfluxDB 2.x would have let you store a string `"nan"` in a float column, or a
float `NaN` that plots as a gap and reads as a process that stopped. InfluxDB 3
refuses both, because a column's type is fixed on its first write. The refusal is
the feature, and lesson 00-03 is about what that buys you.

## Checking the course

Every ````sql` block in this directory is executed against a real server:

```bash
INFLUX_TOKEN_TEST=… uv run python tools/check_sql.py
```

It runs each query **five times**, because "this query is broken" and "this
database is having a bad minute" look identical from the outside. A consistent
*parse* error is a real dialect violation and fails the run; anything intermittent
is reported and not failed.

That distinction earned its keep immediately. The first version of this course
used `INTERVAL '1 hour'`, `CASE WHEN`, `ORDER BY signal`, `IN (…)`, `HAVING` and a
scalar subquery — **none of which this dialect has.** Every one of them would have
looked like a database problem if the tool had reported only the first error it
saw. All of it is written up in [`_shared/DIALECT.md`](_shared/DIALECT.md), and
that file is the most useful thing in this directory.

At the time of writing, 14 of the 23 queries run and 9 are intermittently failing
in the InfluxDB 3 **Core** planner. That is a storage-engine stability problem, not
a course problem — see the recommendation in `docs/LEARNING-LOG.md`.

## If something in a lesson does not match your database

The schema is generated from `contracts/tags.yaml`:

```bash
uv run python sql/_shared/generate_schema.py
```

If the contract has moved on and `sql/_shared/schema.sql` has not, regenerate it.
A course that queries tables which do not exist is worse than no course, because
the student cannot tell whether they misunderstood the query or the data.
