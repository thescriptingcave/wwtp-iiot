# InfluxQL in this project — the dialect, as measured

Every rule here was established by running the query against a live InfluxDB 3 and
reading the error. None of it is in the InfluxDB 1.x/2.x documentation, and most of
it contradicts what a tutorial will tell you.

There is a companion script that checks the whole course against a real server:

```bash
INFLUX_TOKEN_TEST=… uv run python tools/check_sql.py
```

## The rules

### 1. Durations are Go literals, not `INTERVAL`

```sql
time_bucket(1h, time)                  -- works
time_bucket(INTERVAL '1 hour', time)   -- parsing error
now() - 1h                              -- works
now() - INTERVAL '1 hour'               -- parsing error
```

`INTERVAL` is SQL-standard and is what every tutorial uses. InfluxQL does not have
it, and the error names no alternative.

**Minutes, hours, seconds:** `10s`, `5m`, `1h`, `7d`. **Months and years are not
supported** — use `1mo` and `1y` only if you have checked, and prefer computing a
month boundary in the application.

### 2. There is no `CASE` expression

```sql
-- invalid: this dialect has no CASE expression
SELECT CASE WHEN quality = 0 THEN 'Good' END FROM "wwtp"."aeration";
-- parsing error
```

This is the big one, and it removes the pivot idiom that every SQL-for-metrics
tutorial is built on. **You cannot put two signals side by side in one row.** See
"long format is the format" below for what to do instead.

### 3. `ORDER BY` accepts `time` and nothing else

```sql
ORDER BY time          -- works
ORDER BY time DESC     -- works
ORDER BY signal        -- parsing error: invalid ORDER BY, expected TIME column
ORDER BY 1             -- parsing error: expected ASC, DESC or TIME
ORDER BY mean(value)   -- parsing error
```

If you need to order by an aggregate, you cannot. Add the aggregate to the select
list, order by `time`, and sort client-side, or aggregate to a single row where
order does not matter.

### 4. Time bucketing is `GROUP BY time(<duration>)`, not `time_bucket` in `GROUP BY`

```sql
SELECT time, signal, mean(value)
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
GROUP BY time(1h), signal
ORDER BY time;
```

Notes on the form:

* `time(1h)` — the duration is **required**. `time()` is a parse error.
* A **tag** must appear in the `GROUP BY` as well. `GROUP BY time(1h)` alone
  parses and then fails at planning.
* The alias is not usable in `GROUP BY`. Repeat the expression.
* `ORDER BY time` — required, and the only valid order.

`time_bucket()` exists as a *function*, but it cannot be grouped by on this build.

### 5. Long format is the format

Because there is no `CASE` and no pivot, a query that wants several signals side by
side returns them stacked:

```
time                 signal        mean
2026-09-26T07:00:00Z do_mg_l        2.03
2026-09-26T07:00:00Z air_flow_m3h   7100.0
2026-09-26T08:00:00Z do_mg_l        2.11
2026-09-26T08:00:00Z air_flow_m3h   7400.0
```

This is not a limitation to work around; it is how the data is stored, and the
aggregates you actually want — per-signal means, min, max, counts — are all
computed in this shape without any reshaping. Reshape in the application, or in
`02-intermediate` where the window functions live.

### 6. `count(*)` and `count(value)` are different numbers

```sql
SELECT count(*) AS readings, count(value) AS usable FROM "wwtp"."aeration";
```

`value` is nullable, and a `NULL` means a failed instrument. `count(*)` counts what
happened; `count(value)` counts what you can use. `mean`, `min` and `max` ignore
NULLs, which is the correct behaviour — and which means **a filter is not needed to
exclude them from an average**, though it *is* needed to exclude them from a count.

### 7. `NULLIF` for anything that divides

```sql
-- invalid: no CASE, so there is no conditional count
SELECT count(CASE WHEN quality = 2 THEN 1 END) FROM "wwtp"."aeration";

-- valid: filter, then divide, and guard the zero
SELECT count(*) * 1.0 / NULLIF(count(value), 0) AS fraction_bad
FROM "wwtp"."aeration" WHERE quality = 2;
```

Aggregate functions skip NULLs, so `count(*)` is never NULL and never zero in a
table with rows — but a window or a filtered aggregate can be, and a division by
zero returns NULL rather than an error. `NULLIF` makes the intent explicit.

## The one that is not a dialect rule

**`GROUP BY time(...)` is unreliable on InfluxDB 3 Core.** The query parses, and
then planning fails intermittently:

```
error while planning query: rewriting statement caused by
gather information about select statement
```

The same statement, unchanged, returns rows on one call and an error on the next.
That is a stability problem in the storage engine, not a syntax problem, and it is
why:

* `tools/check_sql.py` classifies a failure as *dialect* or *flaky* by running each
  query five times, and only fails the course on a consistent parse error;
* `docs/LEARNING-LOG.md` recommends InfluxDB 3 **Enterprise** for exactly this
  reason, and the `sql/` course is written against the dialect above, which is
  unchanged between the editions.

If you are reading this and your `GROUP BY time(1h)` query is failing, check
whether it fails *every* time (a real problem, probably a tag missing from the
`GROUP BY`) or *sometimes* (this).
