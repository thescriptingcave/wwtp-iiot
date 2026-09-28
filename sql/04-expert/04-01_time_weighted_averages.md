# 04-01 — Time-weighted averages, and why `AVG` is quietly wrong

**[Back to the course](../README.md)** · **Next:** [04-02](04-02_change_detection.md)

Every earlier stage assumed you wanted an average and taught you to write one. This
stage is about the questions where the obvious query returns a confident,
plausible, wrong number — and where the wrongness is invisible in the result.

This is the first of them, and it is the most common one in industrial data.

## The question

> What was the average dissolved oxygen over the last six hours?

## The answer you would write

```sql
SELECT avg(value)
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND ts >= (SELECT max(ts) FROM reading) - interval '6 hours';
```

```
       avg
------------------
 2.35810000000000
```

**2.3581 mg/L.** And here is the problem: that number is wrong, and nothing about
it looks wrong. It has four decimal places. It is not NULL. It is not an outlier.
It is a perfectly formed number computed from perfectly good readings, and it is
**2.9 % away from the correct answer** — on a signal measured in tenths of a
milligram per litre, where the permit is written in the same units.

## What `AVG` is actually asserting

`AVG` sums the values and divides by the count of values. That is only the average
of the *process over time* if every row represents an equal amount of time.

It does not. A historian stores a row when a value **changes**, not on a schedule.
This signal is dissolved oxygen in an aeration basin: it drifts slowly, and the
simulator's deadband suppresses storage when nothing meaningful moves. Six hours
produced this:

```sql
SELECT
    count(*)                                    AS points,
    round(avg(extract(epoch FROM gap))::numeric, 1) AS seconds_held,
    min(extract(epoch FROM gap))::int           AS shortest,
    max(extract(epoch FROM gap))::int           AS longest
FROM (
    SELECT ts - lag(ts) OVER (ORDER BY ts) AS gap
    FROM reading
    WHERE signal_id = 'AERATION:AHU-1:DO'
      AND ts >= (SELECT max(ts) FROM reading) - interval '6 hours'
) t
WHERE gap IS NOT NULL;
```

```
 points | seconds_held | shortest | longest
--------+---------------+----------+---------
     38 |          490.9 |       45 |    6358
```

**Thirty-eight gaps spanning six hours, and the longest is 6358 seconds — one hour
and forty-six minutes.** One row is standing in for nearly two hours of process
time while another represents forty-five seconds. `AVG` gives every row an equal
vote, so that one flat stretch counts the same as the rapid movement.

The first row is the seed's opening value, its `gap` is NULL, and it is excluded
above; so is the last row, which has no successor. Hence 38 gaps for 39 points.
Both exclusions are handled deliberately below.

## The three numbers

Here is the whole lesson in one query. For each signal: what `AVG` says, what the
time-weighted mean says, and how far apart they are.

```sql
WITH windowed AS (
    SELECT
        signal_id,
        value,
        lead(ts) OVER (PARTITION BY signal_id ORDER BY ts) - ts AS held_for
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '6 hours'
)
SELECT
    signal_id,
    count(value)  AS points,
    round(avg(value)::numeric, 4) AS naive,
    round((
        sum(value * extract(epoch FROM held_for)) FILTER (WHERE value IS NOT NULL)
        / sum(extract(epoch FROM held_for))
    )::numeric, 4) AS weighted,
    round((
        avg(value)
        - (sum(value * extract(epoch FROM held_for)) FILTER (WHERE value IS NOT NULL)
           / sum(extract(epoch FROM held_for)))
    )::numeric, 4) AS error
FROM windowed
WHERE signal_id IN ('UTILITY:SITE:PLANT_POWER',
                    'AERATION:AHU-1:BLOWER_RPM',
                    'AERATION:AHU-1:DO')
GROUP BY signal_id
ORDER BY points DESC;
```

```
             signal_id             | points |     naive |  weighted |   error
-----------------------------------+--------+-----------+-----------+---------
 UTILITY:SITE:PLANT_POWER           |  21601 | 547.4904  | 547.4937  | -0.0033
 AERATION:AHU-1:BLOWER_RPM          |  21511 | 939.1276  | 938.8556  |  0.2720
 AERATION:AHU-1:DO                  |     39 |   2.3581  |   2.2912  |  0.0669
```

**Read the `points` column against the `error` column.** They run in opposite
directions.

`PLANT_POWER` has 21,601 points, sampled roughly once a second, and `AVG` is off
by **0.001 %**. `DO` has 39 points and `AVG` is off by **2.9 %** — nearly three
thousand times worse, from a dataset four hundred and fifty times *smaller*.

> **The error in `AVG` has nothing to do with how much data you have. It is
> entirely about how irregularly you sampled.**

That is the sentence to carry out of this lesson. It inverts the intuition that
more rows mean a better answer, and it is why "we store a lot of points" is not a
data-quality argument. A fast-changing signal sampled every second is the easy
case. A slow, deadbanded, regulatory signal is the hard one, and it is usually the
one a report gets built on.

## Why the time-weighted mean is the right one

There is no clever trick here. The average of a function over an interval is
`∫f dt / ∫dt`, and for a step function that held constant between samples it
decomposes exactly:

```
        ∫value dt  =  Σ  value_i × held_i
        ∫dt         =  Σ  held_i
```

Which in SQL is the numerator and denominator above. `lead(ts) - ts` gives the
interval each value was held for; multiply, sum, divide.

The two details that make it correct rather than approximately correct:

**`lead(ts) - ts`, not `ts - lag(ts)`.** Either works — they are the same set of
intervals — but `lead` assigns each interval to the value that was *current*
during it, which is the one the numerator multiplies. `lag` assigns it to the
value that was current *before*, which reads more naturally and is wrong.

**The last row has no `held_for`, and that is not an oversight.** It is the value
held from the newest reading until now, and the course deliberately anchors to
`(SELECT max(ts) FROM reading)` rather than `now()`. So the denominator is the
time between the first and last reading and nothing more. The final interval is
unknown and is excluded. On a six-hour window that is seconds out of 21,317. On a
window that ends in the middle of a long flat period it would not be — which is
one more reason to anchor to the data.

## The pitfall: filter after the window, and time disappears

The course has one rule running through all of it: **a broken instrument is stored
as `value = NULL` with `quality = 2`.** So `value` really can be NULL — this
week's seed happens to contain no bad readings, which is exactly why a query that
handles them wrongly passes its tests here and fails in production.

Four rows, so you can check the arithmetic by hand. The instrument fails at
10:02 and the plant runs on for another three minutes:

```sql
WITH samples(ts, value) AS (VALUES
    (timestamptz '2026-09-28 10:00:00+00', 1.0),
    (timestamptz '2026-09-28 10:01:00+00', 2.0),
    (timestamptz '2026-09-28 10:02:00+00', NULL),   -- instrument failed
    (timestamptz '2026-09-28 10:04:00+00', 3.0),
    (timestamptz '2026-09-28 10:05:00+00', 4.0)
),
windowed AS (
    SELECT value, lead(ts) OVER (ORDER BY ts) - ts AS held_for
    FROM samples
)
SELECT
    round(avg(value)::numeric, 3) AS naive_avg,
    round((
        sum(value * extract(epoch FROM held_for))
        / sum(extract(epoch FROM held_for))
    )::numeric, 3) AS weighted_filtered_in_where,
    round((
        sum(value * extract(epoch FROM held_for))
            FILTER (WHERE value IS NOT NULL AND held_for IS NOT NULL)
        / sum(extract(epoch FROM held_for))
    )::numeric, 3) AS weighted_filtered_in_numerator
FROM windowed;
```

```
 naive_avg | weighted_filtered_in_where | weighted_filtered_in_numerator
-----------+----------------------------+-------------------------------
     2.500 |                      2.000 |                        1.200
```

**Three questions, three answers, and only one of them is right.** They differ
because of two NULLs that look identical and are not.

*First: `WHERE value IS NOT NULL`, the middle column.* The filter runs *after* the
window function, so the NULL row has already been used to compute the interval
before it — and then it is removed, taking 120 seconds of elapsed time with it.
The denominator becomes 180 instead of 300, and the average jumps to 2.000.
**The time during which the plant was unreadable is deleted from the average
rather than counted against it.** A sensor that drops out makes the surviving
data look *better* than it was, which is the opposite of what happened.

*Second: the last row, and the right-hand column.* `4.0` is the newest reading, so
`lead()` gives it no successor and its `held_for` is NULL. `4.0 * NULL` is NULL,
and `FILTER (WHERE value IS NOT NULL)` does not exclude it — the filter checks the
value, not the duration. So the most recent reading silently drops out of the
numerator while every other reading stays. Hence the second `AND held_for IS NOT
NULL`, and hence 1.200.

Check it by hand: the intervals are 60, 60, 120 and 60 seconds. Only three of
them have a value, and they are 1.0, 2.0 and 3.0, so

```
        (1.0 x 60) + (2.0 x 60) + (3.0 x 60)
        ---------------------------------  =  360 / 300  =  1.2
              60 + 60 + 120 + 60
```

**That is the right answer, and note which way it moved: 1.2, not 2.0.** The
interval the failed instrument covered is the *longest* one in the window, so
counting it honestly drags the average down. The plant genuinely ran through that
time with no measurement of it, and the average says so.

**The general rule:** filter NULLs out of the *numerator* with `FILTER`, never out
of the `WHERE` clause, and filter on both `value` and `held_for` — because they
can each be NULL for entirely different reasons.

## Report coverage, or you have not finished

A time-weighted average with a gap in it is not an answer on its own. The reader
needs to know how much of the window was actually measured:

```sql
SELECT
    round((
        sum(value * extract(epoch FROM held_for))
            FILTER (WHERE value IS NOT NULL AND held_for IS NOT NULL)
        / sum(extract(epoch FROM held_for))
    )::numeric, 3) AS average,
    round(100.0 * count(held_for) / count(*), 1) AS rows_with_a_duration
FROM (
    SELECT value, lead(ts) OVER (ORDER BY ts) - ts AS held_for
    FROM reading
    WHERE signal_id = 'AERATION:AHU-1:DO'
      AND ts >= (SELECT max(ts) FROM reading) - interval '6 hours'
) t;
```

```
 average | rows_with_a_duration
---------+-----------------------
   2.291 |                  97.4
```

**2.291 mg/L, and 97.4 % of the rows carry a duration.** The two missing
percentages are the newest reading, which has no successor to measure against.
That is not a fault and it is not noise — it is the one interval whose length is
genuinely unknown, and the course anchors to the last reading rather than to
`now()` precisely so that "unknown" stays a small, bounded, explainable quantity
instead of becoming however long you have not run the report.

That is the shape to report: **an average, and the coverage that makes it
interpretable.** An average without its coverage is the same mistake as a value
without its status, one level up — and on this signal the naive `AVG` differs by
2.9 %, so "2.36 on its own" is a number nobody should act on.

## When `AVG` is fine

It is worth being explicit, because "time-weight everything" is as bad advice as
"never use `AVG`".

`AVG` is correct whenever sampling is regular — that is, when every row stands for
the same amount of time. `PLANT_POWER` above is sampled once a second to within a
millisecond, and `AVG` is off by 0.001 %. On that signal the simple query is right,
and the time-weighted one is the more expensive way to get the same answer.

So the rule is not "always time-weight". It is:

> **Time-weight when the sampling interval varies. Check whether it varies before
> you decide.**

Which is a one-line check, and it is the query above — `min(gap)`, `max(gap)`.
Run it once per signal, put the result in the signal's metadata, and the decision
is made.

## What to take away

- **`AVG` assumes every row represents the same amount of time.** On a
  change-triggered historian, that is false, and the error scales with the
  irregularity rather than with the volume.
- **More data does not mean a better average.** `PLANT_POWER` had 21,601 points
  and 0.001 % error; `DO` had 39 points and 2.9 % error.
- **`lead(ts) - ts` assigns each interval to the value that was current during
  it.** `lag` reads more naturally and is wrong.
- **Filter `value IS NOT NULL` in the numerator, never in the `WHERE` clause**, or
  the time during which the instrument was down is deleted from the average rather
  than counted against it.
- **Report coverage with the average.** 2.291 with its coverage is an answer;
  2.291 alone is a claim.

## Exercises

1. **The bias grows with the window.** Recompute `DO` over 1 hour, 6 hours and 1
   day. Predict the direction before you run it: a longer window contains a longer
   flat period, so it should make the naive average *worse*. Check whether it does.
2. **A constant signal.** For a signal that never moves, `AVG` and the
   time-weighted mean must agree exactly. Verify that, then explain why this is a
   useful test of a query rather than a trivial one.
3. **Break it on purpose.** Take the correct query and move the `NOT NULL` filter
   into the `WHERE` clause. Recreate a NULL reading (a row with `quality = 2`),
   then explain the exact interval that went missing and in which direction the
   answer moved.
4. **Which is the permit number?** The compliance average for ammonia is what
   goes on the report. Look up its signal in `sql/` and work out whether
   time-weighting changes the number enough to matter, and say why the answer
   depends on the signal rather than on the query.

---

**A note on this data.** These figures come from a seeded week on one machine and
the absolute values will differ on yours. The *relative* ordering — the
fast-sampled signal accurate to a thousandth, the slow one wrong by percent — is
structural and holds on any change-triggered historian, because it follows from
the sampling intervals and not from this dataset.
