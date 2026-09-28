# 04-04 — The dashboard query, and deciding what a bucket means

**[Back to the course](../README.md)** · **Previous:** [04-03](04-03_fixing_a_slow_query.md) · **[End of the course](../README.md)**

The last lesson takes everything above and puts it in the one place it matters:
a query the plant's operators actually look at.

It also finds a bug in it. That is not a contrived exercise — it is the query this
project ships, in `ui/grafana/generate_dashboards.py`, and the defect is the one
[04-01](04-01_time_weighted_averages.md) taught you to recognise.

## The query

<!-- verbatim from ui/grafana/generate_dashboards.py; a Python string, not a
     query the gate can run, so it is shown rather than executed -->
<!-- check: skip -->
```python
"SELECT time_bucket(INTERVAL '5 seconds', ts) AS time, "
"       avg(value) AS value "
"FROM reading "
f"WHERE signal_id = '{signal_id}' "
f"  AND ts >= $__timeFrom() AND ts <= $__timeTo() "
"  AND value IS NOT NULL "
f"GROUP BY time ORDER BY time"
```

Three decisions are hidden in four lines, and every one of them is defensible on
its own. Together they are a bug.

| | decision | right for |
|---|---|---|
| 1 | `time_bucket(5 seconds)` | the width of a trend panel |
| 2 | `avg(value)` | **nothing in particular — see below** |
| 3 | `AND value IS NOT NULL` | dropping bad readings — **in the wrong place** |

## Decision 2: what is a five-second bucket supposed to mean?

`avg(value)` inside a bucket is a different question from `avg(value)` over six
hours, and it is worth being clear about which one this is. A five-second trend
panel is showing the operator **what the signal was doing just now**. It wants the
value at the end of the bucket, not a mean of everything that happened inside it.

The current query returns a third thing — an unweighted mean of the samples that
landed in the bucket — which is neither. And because the historian is
change-triggered, that bucket does not even contain a representative slice of the
five seconds.

Here is the size of the difference, on a screen scraper sampled at about 1.2 s:

```sql
WITH windowed AS (
    SELECT
        time_bucket(INTERVAL '5 seconds', ts) AS bucket,
        value,
        ts,
        lead(ts) OVER (
            PARTITION BY time_bucket(INTERVAL '5 seconds', ts) ORDER BY ts
        ) - ts AS held_for
    FROM reading
    WHERE signal_id = 'PRIMARY:PRI-SCR-1:TORQUE'
      AND ts >= (SELECT max(ts) FROM reading) - interval '1 hour'
      AND value IS NOT NULL
),
per_bucket AS (
    SELECT
        bucket,
        avg(value) AS naive_avg,
        (sum(value * extract(epoch FROM held_for))
             FILTER (WHERE held_for IS NOT NULL)
         / NULLIF(sum(extract(epoch FROM held_for)), 0)) AS time_weighted,
        (array_agg(value ORDER BY ts DESC))[1] AS last_value
    FROM windowed
    GROUP BY bucket
)
SELECT
    count(*)                                            AS buckets,
    round(avg(abs(naive_avg - last_value))::numeric, 3)     AS avg_dev_from_last,
    round(max(abs(naive_avg - last_value))::numeric, 3)     AS worst_dev_from_last,
    round(avg(abs(time_weighted - last_value))::numeric, 3) AS weighted_dev_from_last
FROM per_bucket;
```

```
 buckets | avg_dev_from_last | worst_dev_from_last | weighted_dev_from_last
---------+-------------------+--------------------+------------------------
     721 |             1.910 |              6.713 |                  2.598
```

**Three points of average error on a scraper torque, and six point seven at the
worst.** Across a whole hour of a five-second trend that is a visible disagreement
with the instantaneous value — the number on the screen is not the number the
instrument was showing.

Now the interesting part. **The time-weighted mean is *further* from the last
value than the naive average is** — 2.598 against 1.910. The fix for this panel
is therefore not the fix from lesson 04-01.

That is worth sitting with, because it is the whole lesson:

> **`AVG` is not a slightly-wrong time-weighted mean. It is a different
> statistic, and which one is right depends on what the panel is for.**

For the six-hour compliance average in 04-01, the time-weighted mean was right,
because "average over the period" is what was asked. For a five-second trend, the
last value is right, because "what is it doing now" is what is being asked. The
dashboard used `avg` for both and was right in neither.

The corrected trend query:

```sql
SELECT
    time_bucket(INTERVAL '5 seconds', ts) AS time,
    (array_agg(value ORDER BY ts DESC))[1] AS value
FROM reading
WHERE signal_id = 'PRIMARY:PRI-SCR-1:TORQUE'
  AND ts >= (SELECT max(ts) FROM reading) - interval '1 hour'
  AND value IS NOT NULL
GROUP BY time
ORDER BY time;
```

`array_agg(...)[1]` is the idiomatic last-value-per-group in Postgres, and it is
both cheaper and clearer than an aggregate that has to be explained.

**One caveat, honestly stated:** taking the last value means a bucket containing
one bad reading *and* the good reading before it shows the good one. For a trend
that is what you want. If a panel must never hide a bad reading, add
`max(quality)` alongside it and let the renderer decide — which is the same rule as
04-01's coverage figure: **the aggregation and the caveat are separate outputs.**

## Decision 3: the NULL filter is in the wrong place

<!-- a fragment lifted out of the query above, not a statement -->
<!-- check: skip -->
```sql
AND value IS NOT NULL
```

This is the 04-01 pitfall, in production. The filter runs before the bucket
aggregate, so any interval a failed instrument covered simply vanishes from the
bucket instead of being counted as a bucket with no data.

Two consequences, and the second is the nastier one:

- **The gap is invisible.** A bucket where the sensor was dead for the whole five
  seconds produces no row at all, exactly like a bucket outside the query range.
  Grafana draws a gap for both. The operator cannot tell "no data" from "no
  change".
- **A bucket that *partly* contains a failure is silently averaged over the
  survivors.** The value that gets plotted is not the value the instrument last
  reported before the failure.

The fix is to keep the bucket and carry the quality through, so the renderer can
decide:

```sql
SELECT
    time_bucket(INTERVAL '5 seconds', ts) AS time,
    (array_agg(value ORDER BY ts DESC))[1] AS value,
    max(quality)                          AS worst_quality
FROM reading
WHERE signal_id = 'PRIMARY:PRI-SCR-1:TORQUE'
  AND ts >= (SELECT max(ts) FROM reading) - interval '1 hour'
GROUP BY time
ORDER BY time;
```

Now a bucket that is entirely bad has `value IS NULL` and `worst_quality = 2`, and
the panel can grey it. A bucket that is partly bad tells you so.

## Decision 1: the bucket width is not the interesting choice

`INTERVAL '5 seconds'` is fine, and the reason it is worth mentioning at all is
that it interacts with the other two decisions. At 5 seconds a sparse signal like
dissolved oxygen produces **at most one sample per bucket** — 39 buckets and 39
samples over six hours — so the aggregation question never arises and the NULL
question never arises. At 5 seconds the bug is invisible on that signal. It only
appears on the fast ones, where it is worst.

So the bug is not on every panel. **A defect that only shows up on some of your
charts is the kind that survives for years**, because nobody ever looks at the
panel where it does not appear. That is an argument for writing the query once,
correctly, and generating every panel from it — which is what
`ui/grafana/generate_dashboards.py` already does, and is the one part of this
story that is right.

## The finished query

Everything the stage produced, in one statement:

```sql
SELECT
    time_bucket(INTERVAL '5 seconds', ts)  AS time,
    (array_agg(value ORDER BY ts DESC))[1] AS value,     -- 04-04: last, not avg
    max(quality)                           AS quality,   -- 04-01: the caveat travels
    count(*)                               AS samples    -- 04-02: how much was there
FROM reading
WHERE signal_id = 'PRIMARY:PRI-SCR-1:TORQUE'
  AND ts >= (SELECT max(ts) FROM reading) - interval '1 hour'  -- 04-03: chunk exclusion
  AND ts <= (SELECT max(ts) FROM reading)
GROUP BY time
ORDER BY time
LIMIT 5;
```

```
          time          |      value  | quality | samples
-----------------------+--------------+---------+---------
 2026-09-28 19:05:50+00 | 40.885227787 |       0 |      3
 2026-09-28 19:05:55+00 | 37.270565520 |       0 |      4
 2026-09-28 19:06:00+00 | 40.718758229 |       0 |      4
 2026-09-28 19:06:05+00 | 37.252342676 |       0 |      3
 2026-09-28 19:06:10+00 | 39.677927726 |       0 |      4
```

**Three or four samples in a five-second bucket, a value, a quality, and a count.**
The `samples` column is the one that makes the rest readable: it says how much of
that five seconds this row actually represents, and a bucket showing `samples = 1`
is telling you something the value alone cannot.

Look also at the values — 40.9, 37.3, 40.7, 37.3, 39.7. The scraper is genuinely
moving that fast. That is why the `avg` question in this lesson is not academic:
the samples inside one bucket disagree, so which one you pick changes what the
operator sees.

Four clauses, and each one is a lesson:

- **`ts >= (SELECT max(ts) FROM reading) - $window`** — the subquery is evaluated
  once, as an `InitPlan`, which is what lets the engine exclude whole chunks. A
  literal cannot, and `now()` would exclude everything.
- **`(array_agg(value ORDER BY ts DESC))[1]`** — the aggregation states what the
  panel is for. Last value for a trend; a time-weighted mean for a period
  average; both are defensible, `avg` is not.
- **`max(quality)`** — the status travels with the value instead of being filtered
  away, so "no data" and "bad data" remain distinguishable.
- **`count(*)`** — the coverage. A bucket with one sample out of a possible five
  is a different statement from one with five, and only the count says so.

**None of these is exotic.** Every clause is a plain, readable piece of Postgres
that this course has already met. The value of the expert stage is not that the
SQL got harder — it is that you now know which of four defensible-looking choices
is the wrong one, and why.

## What to take away

- **Decide what an aggregate means before choosing it.** `AVG` is not a
  slightly-wrong time-weighted mean; it is a different statistic. A trend wants
  the last value, a period average wants the time-weighted mean, and `avg` is
  right for neither.
- **`AND value IS NOT NULL` in the `WHERE` clause deletes your evidence of a
  failure.** Carry `quality` out and let the renderer decide.
- **The same bug can be invisible on half your panels and glaring on the rest.**
  Generate every panel from one correct query rather than fixing them one at a
  time.
- **The subquery anchor is load-bearing.** It is what makes chunk exclusion
  possible, and it is why a report is reproducible against a database whose newest
  reading is a week old.
- **Ship the coverage with the value.** `count(*)` next to a bucketed reading is
  the difference between a chart and an answer.

## The bug is still in the code, on purpose

Everything above is a criticism of a query that `ui/grafana/generate_dashboards.py`
still ships. That is deliberate, and there are two reasons.

**The reasoning is the lesson.** A lesson that says "here is the fix" and hands you
a corrected query teaches you one thing. A lesson that shows you a live defect,
measures it, and makes *you* write the fix teaches you how to notice the next one —
and the next one is in a file you have never opened.

**The guard will tell you when you are wrong.** `tests/test_readme_claims.py` has a
test asserting that `_raw_query` still contains both defects and that this lesson
still names it. The moment you fix the generator, that test fails and tells you
this lesson needs rewriting. It is a one-directional check on purpose: it cannot
stop this lesson from becoming wrong, but it will stop the repository from quietly
having a lesson about a bug that no longer exists.

So exercise 1 is not homework. It is the fix, and the dashboard tests in
`tests/test_grafana_dashboards.py` assert on the query text, so part of it is
working out what they are actually checking.

## Exercises

1. **Fix the generator.** Change `_raw_query` in
   `ui/grafana/generate_dashboards.py` to take the last value and carry
   `max(quality)` and `count(*)`. Regenerate the dashboards and check that the
   panel tests in `tests/test_grafana_dashboards.py` still hold — several of them
   assert on the query text, and you will need to read what they are actually
   asserting.
2. **Find the worst panel.** For every signal, compute the mean absolute
   difference between `avg(value)` and the last value per 5-second bucket over one
   hour. Which signals would an operator have complained about, and in what units
   is the error significant?
3. **Time-weight a trend anyway.** Build the 04-01 time-weighted mean *per bucket*
   and compare all three aggregations on a signal where the signal changes fast
   within a bucket. Under what circumstances would a trend panel genuinely want
   the mean rather than the last value?
4. **Prove the gap is invisible.** Using only the broken query, count the buckets
   in a one-hour window, then count them again with the `value IS NOT NULL` filter
   removed. For a signal that failed mid-hour, the difference is the number of
   buckets the old query was hiding. Find such a signal or construct one.

---

**Next:** nothing — this is the end of the course. `sql/README.md` lists what is
here and what each stage assumes, and the
[OPC UA course](../../courses/opcua/README.md) is the other half of the same
plant.
