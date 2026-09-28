# The SQL course

Five stages, beginner to expert, all written, run against a week of data this
project generates. It is not an appendix. It is the reason the plant exists.

The last stage finds two real defects in this repository, and one of them is in the
query the plant's own dashboard runs.

Every lesson states **a question**, gives **a query**, and has its expected output
inline. Work them in order: the later stages assume the shapes the earlier ones
introduced.

## Before you start

```bash
docker compose up -d db                  # see docs/GETTING-STARTED.md
docker compose run --rm init-db          # schema + the contract
docker compose --profile demo run --rm seed   # a week of plant history
```

Without seeded data every query returns no rows and every chart is flat, and the
mistake everyone makes is concluding their SQL is wrong. Seed first.

### And stop the plant, if it is running

```bash
docker compose stop scada          # do this before working through the course
```

**It is the gateway, not the plant, that has to stop.** `scada` is the service
that reads the plant over Modbus and OPC UA and writes the rows; `softplc` is the
plant itself. The two courses want opposite things and you can have both:

| | SQL course | OPC UA course |
|---|---|---|
| `softplc` (plant, port 4840) | not needed | **needed** — lesson 01 talks to it |
| `scada` (gateway, writes rows) | **must be stopped** | not needed |

So the working combination for `make check` is both courses at once:
`softplc` up, `scada` down.

**A live plant writing into the database makes the course non-reproducible**, and
not in a subtle way. The lessons anchor to `(SELECT max(ts) FROM reading)`, so a
live writer moves that anchor, and:

- a query that was looking at seeded history now looks at whatever arrived since;
- "which signals have gone quiet" returns **nothing**, because a running plant
  reports all 57 within any recent window;
- expected outputs pasted into the lessons stop matching what you get.

`tools/check_sql.py` runs every query **three times** and fails any that return a
different number of rows twice, which is how this is caught rather than
discovered. It found two lessons that were silently non-deterministic before this
note existed — the fix was to bound those queries, and the prerequisite is here so
the next person does not rediscover it.

You do not need the plant stopped forever — only while you are working through the
lessons, and while running `make check`.

Then check you can see anything at all:

```sql
SELECT count(*) FROM reading;
```

If that returns 0, stop and fix the data before reading a word of the course. If
it returns about four million, you have what you need.

To run a query, use `psql` if you have it — it is the better tool:

```bash
docker compose exec db psql -U wwtp -d wwtp
```

or use the bundled runner, which needs nothing installed but Python:

```bash
uv run python tools/sqlrun.py "SELECT count(*) FROM reading"
```

## The stages

| Stage | What you learn | Why it comes when it does |
|---|---|---|
| [00-foundations](00-foundations/) | What a hypertable is; identity versus value and why they are different *tables*; why `value` can be NULL | Because every later stage assumes you know this, and because a time-series table is not a normal table with a timestamp column |
| [01-beginner](01-beginner/) | `SELECT`, `WHERE`, aggregation, your first `time_bucket`, `CASE`, `HAVING` | Because this is where "read one signal" becomes "answer a question about a signal" |
| [02-intermediate](02-intermediate/) | CTEs, window functions, joins, gaps, the quality scale as a filter | Because the questions get compositional, and a nested subquery stops being readable before it stops being possible |
| [03-advanced](03-advanced/) | Continuous aggregates, chunks, retention, `EXPLAIN` | Because the storage engine starts deciding what your query is allowed to do before it runs |
| [04-expert](04-expert/) | Time-weighted averages, change detection, reading a plan, writing the dashboard query | Because every earlier stage had a right answer to check against, and this is the stage where the obvious query returns a confident wrong one — **and finds two real defects in this repository** |

## How to work through a lesson

1. Read the question. Write down what you think the answer will be.
2. Run the query.
3. Compare with the output shown. If they differ, find out **why** before moving
   on.
4. Do the exercises at the bottom. They are not optional decoration; they are
   where the lesson actually lands.

The difference between reading a query and writing one is the difference between
recognising `avg(value)` and knowing that `AVG` over a table containing NULLs
ignores them, which changes your answer without telling you.

## The one idea the whole course rests on

**A broken instrument is stored as `value = NULL` with `quality = 2`.** It is not
dropped, and it is not stored as zero or as a string.

That single representation is what makes three things distinguishable that are
otherwise identical:

* a failed sensor is **distinguishable from no data at all** — the row exists;
* it is **distinguishable from a number that happens to be wrong** — `quality`
  says so;
* an aggregate that ignores NULLs (`avg`, `min`, `max`) is doing the *right*
  thing, and one that does not (`count(value)` against `count(*)`) will quietly
  give you a different answer.

And it is enforced, not merely documented. The schema has:

<!-- check: skip -->
```sql
CONSTRAINT reading_null_is_not_good CHECK (value IS NOT NULL OR quality <> 0)
```

You cannot record a Good reading that has no value — because zero is a real
dissolved-oxygen concentration, a real flow rate, and a real alarm state. That
constraint is the whole argument for a relational time-series database in one
line, and [00-03](00-foundations/00-03_quality_is_data.md) is about what it buys
you.

## The other idea, which is a plant lesson rather than a SQL one

**`source` is in the primary key, so both protocol faces can record the same
signal at the same instant.** That is what makes

> *do Modbus and OPC UA agree about dissolved oxygen?*

a query rather than a hunch, and it matters more than it looks, because the
failure it catches is **finite, in range, and wrong**.

This project's signature bug is reading a 32-bit float whose words arrive
low-word-first as though they arrived high-word-first. The result is about
`2.3e-41`. It passes every range check in the system. It plots as a flat line at
zero. No amount of validation on the value itself will ever catch it, because
there is nothing wrong with the number *as a number*. The only defence is to
compare the two independent observations of the same physical quantity — which
means storing both, which means `source` has to be part of the identity of a
row.

[00-02](00-foundations/00-02_identity_and_values.md) develops this properly.

## Checking the course

Every ````sql` block in this directory is executed against a real database:

```bash
docker compose --profile demo run --rm seed     # if not already seeded
uv run python tools/check_sql.py
```

It runs each query **three times** and fails on any error, on any answer that
changes between runs, and on any query that returns zero rows.

The repeat is not superstition. The property it verifies is the one that actually
matters for a lesson:

> **A query whose answer changes between runs is not a lesson.**

A reader who runs a query twice and gets two different numbers has learned
something about the query and nothing about the plant, and cannot tell which.
Queries that depend on `now()` are exempt from the comparison — their row count
legitimately grows as data arrives — and are reported as `volatile` instead.

The empty-result check is on by default because in a seeded database it almost
always means the lesson is querying a signal id that does not exist, which is
exactly the sort of error a student would otherwise spend an hour on. A lesson
*about* an empty result is legitimate; pass `--allow-empty` for those.

## If something in a lesson does not match your database

The schema lives in [`storage/postgres/schema.sql`](../storage/postgres/schema.sql)
and is applied by `docker compose run --rm init-db`, which also loads the 57
signals and 22 assets from [`contracts/tags.yaml`](../contracts/tags.yaml). The
signal ids in the lessons are real ids from that file.

A course that queries tables which do not exist is worse than no course, because
the student cannot tell whether they misunderstood the query or the data.


---

## The queries as `.sql` files

[`TablePlus/`](TablePlus/README.md) holds every runnable query from these lessons
as a standalone file, for opening in a SQL client:

```bash
make tableplus          # regenerate
make tableplus-check    # report drift (also in CI)
```

**Generated from the lessons, which stay `.md` and always will be** — a lesson is
prose that teaches something with a query inside it, and the explanation is the
course. What was missing is that the queries were not individually addressable:
you could not `psql -f` one, link to one, or diff one change.

64 of the 117 fenced blocks are extracted. The other 53 are deliberately not
runnable — fragments, `<placeholder>` values, or queries *meant* to come back
empty — and a folder of 53 files that fail when opened would be worse than none.
