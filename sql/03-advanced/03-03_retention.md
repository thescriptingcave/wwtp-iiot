# 03-03 — Retention: what is about to be deleted, and can you see it coming

**Previous:** [03-02](03-02_chunks.md) · **[Next: 03-04](03-04_explain.md)** ·
**[Back to the course](../README.md)**

Retention is the reason the tiers exist, and it is the only part of this database
that **deletes data on its own, without asking**. That makes it the part with the
most ways to lose something you did not mean to lose, and the part where knowing
the mechanism matters more than knowing the syntax.

## The question

> What will be deleted in the next hour, and how would I know if I got the
> interval wrong?

## What is actually configured

<!-- check: skip -->
```sql
SELECT application_name, schedule_interval, config
FROM timescaledb_information.jobs
WHERE proc_name = 'policy_retention'
ORDER BY application_name;
```

`config` is a JSON blob holding the interval, and the interval is the whole
policy. Read it as a *policy*, not as a schedule: a 7-day retention on `reading`
means "drop chunks whose **entire** range is older than 7 days", not "delete rows
older than 7 days".

## Why it drops chunks and not rows

**A chunk is dropped whole, or not at all.** A retention policy never issues a
`DELETE`.

This is the single most important thing to know about it, and it has two
consequences that surprise people in opposite directions.

**It cannot delete part of a chunk.** So a chunk spanning the retention boundary
is kept *entirely*, and the effective retention is between your interval and your
interval plus one chunk width. For `reading` at a 7-day policy and a 1-day chunk
interval, the oldest surviving data is between 7 and 8 days old. If you need an
exact figure, that is the answer and no query will give you a better one.

**It does not need to delete part of a chunk either.** Dropping a file is a
`unlink`; deleting a million rows one at a time is a transaction. Retention that
dropped rows would be catastrophically slow and would bloat the WAL. So the
design is the fast one, and the cost is that your interval is a range rather
than a number.

## The failure mode: an interval that is too short

Set `RETENTION_RAW_DAYS=1` and the plant loses a week of history overnight. No
error, no warning, no row count anomaly — the table simply has less in it, and
every query still returns sensible answers about a plant that no longer exists.

Which is why the honest version of this question is the next one.

## What will be deleted, and when

You can compute it exactly, and it is worth having as a standing query:

```sql
WITH bounds AS (
    SELECT max(ts) AS newest FROM reading
),
policies AS (
    SELECT
        'reading'::text  AS relation, 7::int AS keep_days,
        interval '1 day' AS chunk_width
    UNION ALL
    SELECT 'reading_1m', 90, interval '1 day'
)
SELECT
    p.relation,
    p.keep_days,
    b.newest - make_interval(days => p.keep_days) AS cutoff,
    date_trunc('day', b.newest - make_interval(days => p.keep_days))
        - p.chunk_width                       AS whole_chunks_go,
    date_trunc('day', min(r.ts))               AS oldest_surviving,
    b.newest - date_trunc('day', min(r.ts))    AS actual_age
FROM policies p
CROSS JOIN bounds b
JOIN reading r ON true
GROUP BY p.relation, p.keep_days, b.newest, p.chunk_width;
```

Three numbers that should be read together:

* **cutoff** — the exact instant below which rows are past policy.
* **whole_chunks_go** — the earliest chunk that can be dropped. Between `cutoff`
  and this, chunks are straddling the boundary and are kept.
* **oldest_surviving** — what a query can actually see. Between `whole_chunks_go`
  and this is the chunk-width slack described above.

If `actual_age` is comfortably inside `keep_days`, the policy is doing what you
think. If it is more than `keep_days` by a lot, something is wrong with the
policy; if it is *less*, you are looking at a database that has not been
populated for `keep_days` yet, which looks identical to a policy that is too
aggressive and is not.

## Detaching a policy before you regret it

The useful emergency control, and the reason to read this lesson before you need
it:

<!-- check: skip -->
```sql
-- Not destructive. Removes the *policy*; the data stays until you drop it.
SELECT remove_retention_policy('reading', if_exists => TRUE);
```

`add_retention_policy` and `remove_retention_policy` are the pair. A policy you
have detached is inert, and a policy you have attached again starts from `now()`
with a fresh window — so reattaching is *not* equivalent to never detaching, and
a database that was detached for a week loses only a week.

`storage/postgres/schema.py` calls `add_retention_policy(..., if_not_exists =>
TRUE)` on every start, which is why a detached policy comes back on the next
`docker compose up`. That is deliberate and it is worth knowing before you try
to keep it off.

## Retention and the aggregates interact, and in an order

`reading_1m` is retained for 90 days and `reading_1h` indefinitely, so the
hourly tier is the only one that survives a long outage of the raw data. That is
the design: retention decides how far back you can go, not how far back you
*can*.

But the aggregates have their own refresh policies, and **a chunk dropped from
`reading` is not automatically un-refreshed in `reading_1h`.** The hourly tier
keeps its buckets because nothing tells it to drop them. That is the right
default — a rollup that loses history when the raw data does is worse than
useless — and it means the hourly table is the archive of record for anything
older than 7 days.

Which is also why [03-01](03-01_continuous_aggregates.md) matters more the longer
this project runs: a tier that is cheap to query *and* survives retention is the
only reason a year of history is possible at all.

## Exercises

1. Run the "what will be deleted" query. Read off `cutoff`,
   `whole_chunks_go` and `oldest_surviving`, and confirm the gap between the
   first two is one chunk width. Then check `chunk_time_interval` for `reading`
   in `schema.sql` and confirm it matches `information_schema` rather than
   assuming.
2. `SELECT remove_retention_policy('reading_1m', if_exists => TRUE)`, then
   `docker compose up -d`. Which brings it back, and what does that tell you
   about where retention is configured — in the database, or in the code that
   starts the database?
3. How many rows would a naive `DELETE FROM reading WHERE ts < now() - interval
   '7 days'` remove, versus how many a chunk drop removes? Do not run it. Read
   the chunk list from lesson [03-02](03-02_chunks.md) and reason about which
   rows fall in the slack between the cutoff and the first whole chunk.
4. `reading_1h` is retained indefinitely. A year of hourly buckets for 57 signals
   is about 490 000 rows. Compare that with a week of `reading` — 4 290 000 rows
   — and with a year of `reading` at the same sample rate. What does the ratio
   tell you about which tier a compliance archive should be built from?
5. The seeder attaches retention *after* seeding. The comment says attaching it
   first "would make the policy's own refresh window the thing being measured".
   Read `storage/seed/main.py` and work out what would actually happen if the
   order were reversed with `RETENTION_RAW_DAYS=1`.
