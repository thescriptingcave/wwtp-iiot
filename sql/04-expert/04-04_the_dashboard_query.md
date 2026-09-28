# 04-04 — The dashboard query, and deciding what a bucket means

**[Back to the course](../README.md)** · **Previous:** [04-03](04-03_fixing_a_slow_query.md) · **[End of the course](../README.md)**

The last lesson takes everything above and puts it in the one place it matters: a
query the plant's operators actually look at.

It is also the one lesson where you cannot run the code and see the answer, because
this one is **already fixed**. The shape of the change and the reasoning behind it
are the lesson; the bug itself is in the git history.

## The question

> A panel says one number. It is averaging, and the historian is change-triggered.
> Is that number right, and what should have been in the query instead?

## What ships now

`ui/grafana/generate_dashboards.py`, `_raw_query`, verbatim:

<!-- check: skip -->
```python
"SELECT time_bucket(INTERVAL '5 seconds', ts) AS time, "
"       (array_agg(value ORDER BY ts DESC))[1] AS value, "
"       max(quality) AS quality, "
"       count(*) AS samples "
"FROM reading "
f"WHERE signal_id = '{signal_id}' "
f"  AND ts >= $__timeFrom() AND ts <= $__timeTo() "
f"GROUP BY time ORDER BY time"
```

The middle line used to be `avg(value) AS value`, and the `WHERE` clause used to
carry `AND value IS NOT NULL`. Both were defects, and the second one is the more
interesting of the two because it is the exact pitfall from
[04-01](04-01_time_weighted_averages.md) shipping inside a file that already has
a lesson about it.

Three clauses, and each answers a different question than the one it replaced:

| | was | now | why |
|---|---|---|---|
| 1 | `avg(value)` | `(array_agg(value ORDER BY ts DESC))[1]` | a trend shows the present, not a mean |
| 2 | `AND value IS NOT NULL` in `WHERE` | `max(quality)` in `SELECT` | the failure is evidence, not noise |
| 3 | — | `count(*)` | a bucket of one sample is not a bucket of five |

## Decision 1: `AVG` is not a slightly-wrong time-weighted mean

This is the part that is genuinely counter-intuitive, and it is worth doing
properly because the obvious conclusion from lesson 04-01 is wrong.

Lesson 04-01 established that `AVG` over a period is not the time-weighted mean.
The tempting next step is "so use the time-weighted mean everywhere". Here is why
that would have been wrong for this panel.

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
),
bucketed AS (
    SELECT
        bucket,
        count(*) AS samples,
        avg(value) AS naive_avg,
        (array_agg(value ORDER BY ts DESC))[1] AS last_value,
        (sum(value * extract(epoch FROM held_for))
             FILTER (WHERE held_for IS NOT NULL)
         / NULLIF(sum(extract(epoch FROM held_for)), 0)) AS time_weighted
    FROM windowed
    GROUP BY bucket
)
SELECT
    bucket,
    samples,
    round(naive_avg::numeric, 3)                  AS old_avg,
    round(last_value::numeric, 3)                 AS new_last,
    round(time_weighted::numeric, 3)              AS weighted,
    round(abs(naive_avg - last_value)::numeric, 3) AS old_was_off_by
FROM bucketed
WHERE naive_avg IS NOT NULL AND last_value IS NOT NULL
ORDER BY old_was_off_by DESC
LIMIT 5;
```

**Look at the `old_was_off_by` column and then at the `weighted` column.**

The old number was up to **5.2** away from what the instrument last reported. And
the time-weighted mean is *not* closer to that value — it sits at 40.874 when the
instrument was showing 45.379.

Because **none of the three is the answer to the question the panel is asking.**
A five-second trend is showing an operator what the signal is doing *now*. The
value at the end of the bucket is the answer. `avg` is a third statistic, and
`time_weighted` is a fourth, and both of them are answers to a different question
— "what was the average over this period" — which is a question no operator asks
of a live trend panel.

> **`AVG` is not a worse time-weighted mean. It is a different statistic, and which
> one is right depends on what the panel is for.**

That is the sentence worth taking out of this lesson. It also means the fix for
04-01's problem is *not* automatically the fix for this one, which is exactly the
trap when a lesson teaches you a rule.

## How wrong was it, and on which signals

The size of the error is not uniform, and that is the most useful thing to know
about it:

```sql
WITH windowed AS (
    SELECT
        signal_id,
        time_bucket(INTERVAL '5 seconds', ts) AS bucket,
        value,
        ts
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 hour'
      AND signal_id IN ('PRIMARY:PRI-SCR-1:TORQUE',
                        'UTILITY:SITE:PLANT_POWER',
                        'AERATION:AHU-1:BLOWER_VALVE',
                        'AERATION:AHU-1:DO',
                        'INFLUENT:FLOW:TURBIDITY',
                        'INFLUENT:FLOW:PH')
)
SELECT
    signal_id,
    count(*)                                              AS buckets,
    round(avg(abs(naive_avg - last_value))::numeric, 3)  AS mean_off_by,
    round(max(abs(naive_avg - last_value))::numeric, 3)  AS worst_off_by
FROM (
    SELECT
        signal_id,
        bucket,
        avg(value) AS naive_avg,
        (array_agg(value ORDER BY ts DESC))[1] AS last_value
    FROM windowed
    GROUP BY signal_id, bucket
    HAVING count(value) > 0
) AS bucketed
WHERE naive_avg IS NOT NULL AND last_value IS NOT NULL
GROUP BY signal_id
ORDER BY worst_off_by DESC
LIMIT 6;
```

```
             signal_id            | buckets | mean_off_by | worst_off_by
---------------------------------+---------+-------------+--------------
 UTILITY:SITE:PLANT_POWER          |     721 |       5.623 |       22.408
 PRIMARY:PRI-SCR-1:TORQUE          |     721 |       1.925 |        6.718
 AERATION:AHU-1:BLOWER_VALVE        |     721 |       0.121 |        0.270
 AERATION:AHU-1:DO                  |       1 |       0.000 |        0.000
 INFLUENT:FLOW:PH                   |       1 |       0.000 |        0.000
 INFLUENT:FLOW:TURBIDITY            |       1 |       0.000 |        0.000
```

**Three signals with an error of 0.000, exactly.** Not approximately zero — the
`avg` of a bucket containing one sample *is* that sample. Dissolved oxygen, influent
pH and influent turbidity each produced a single reading in the whole hour, so
there was nothing to aggregate and the old query was right on them.

**A plant power meter with a worst error of 22.4 kW** — on a signal whose typical
value is around 550. That is four per cent, every bucket, for an hour, on a panel
an operator glances at to confirm the plant is running normally.

Look at the `buckets` column too. The three correct signals have **one bucket**
between them; the plant power meter and the blower valve have all 721, and only
those two with real traffic show any error at all. **The signals where the defect
bites are the ones that report most often** — the exact opposite of what a reviewer
skimming a panel list would predict, and the reason nobody reported it.

> **A defect that is exactly zero on half your panels and large on the rest is the
> kind that survives for years.** Nobody looks at the panel where it does not
> appear, so nobody reports it, so it is never fixed.

This is the argument for fixing it in the generator rather than per panel — which
is what the dashboards here are for. Every panel is generated from one function,
so a fix lands in all of them at once, and `tests/test_grafana_dashboards.py`
executes every generated query against a live database. A dashboard JSON file that
is never run is a screenshot of somebody's guess.

## Decision 2: the NULL filter was deleting the evidence

The old `WHERE` clause had `AND value IS NOT NULL`. That is 04-01's pitfall, in
production, and it fails in two distinct ways.

**It hides a bucket that should be drawn as empty.** A filter in the `WHERE`
clause runs before the aggregate, so a five-second bucket that the instrument
spent entirely dead produces no row at all — identical to a bucket outside the
query's time range. The panel draws a gap for both, and an operator cannot tell
"the sensor failed" from "nothing happened".

**It averages over the survivors.** A bucket that *partly* contains a failure is
not dropped; it is silently computed from the readings that did survive. The value
plotted is not the value the instrument last reported.

The fix inverts the responsibility. `value` may be NULL, `quality` travels with it,
and the renderer decides:

```sql
SELECT
    time_bucket(INTERVAL '5 seconds', ts)  AS time,
    (array_agg(value ORDER BY ts DESC))[1] AS value,
    max(quality)                           AS quality,
    count(*)                               AS samples
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND ts >= (SELECT max(ts) FROM reading) - interval '6 hours'
  AND ts <= (SELECT max(ts) FROM reading)
GROUP BY time
ORDER BY time
LIMIT 5;
```

```
          time          |     value  | quality | samples
-----------------------+------------+---------+---------
 2026-09-28 16:29:55+00 | 2.39987023 |       0 |      1
 2026-09-28 16:31:35+00 | 2.41994068 |       0 |      1
 2026-09-28 16:33:25+00 | 2.44002807 |       0 |      1
 2026-09-28 16:35:40+00 | 2.46007850 |       0 |      1
 2026-09-28 16:38:10+00 | 2.48038969 |       0 |      1
```

**One sample per bucket, and one bucket every 1 min 40 s.** The panel now shows
that dissolved oxygen reports about thirty-six times an hour rather than implying
continuous coverage — and `samples = 1` says so explicitly, rather than leaving an
operator to infer it from a line that is not quite continuous.

Read the values too: 2.400, 2.420, 2.440, 2.460, 2.480. The basin is climbing and
every one of these is a reading somebody might have to act on. The old query
returned exactly the same numbers here, because a bucket of one sample has nothing
to average — and that coincidence is precisely why the defect survived review.

Had a reading been bad, this row would have `value` NULL and `quality` 2, and the
panel could grey it. That is the whole change: **the aggregation and the caveat
are separate outputs, and the renderer is what decides how to draw them.**

## What keeps it fixed

The fix is held in place by a test that asserts the *output* of the generator, not
its source:

<!-- check: skip -->
```python
raw = gen._raw_query(get_contract(), "AERATION:AHU-1:DO")

assert "(array_agg(value ORDER BY ts DESC))[1] AS value" in raw
assert "max(quality) AS quality" in raw
assert "count(*) AS samples" in raw
assert "value IS NOT NULL" not in raw
```

Checking the output rather than the source is deliberate, and it is the third time
in this project that detail has mattered: **the docstring quotes the old broken
query to explain why it was broken**, so a test that greps the source reads its own
explanation as the code still being wrong. That mistake was made, caught, and the
test rewritten to call the function.

Both regressions have been tried against it — putting `avg(value)` back, and
putting the NULL filter back in the `WHERE` clause — and each fails with a message
naming the lesson it came from.

## What to take away

- **Decide what an aggregate means before choosing it.** A trend wants the last
  value; a period average wants the time-weighted mean; `avg` is right for neither.
- **A rule taught in one lesson is not automatically the rule in the next.** The
  04-01 fix was wrong here, and the reason is that the two panels ask different
  questions.
- **Check which signals a defect affects, not just how large it is.** Exactly zero
  on half the signals is what let this one survive.
- **`WHERE value IS NOT NULL` deletes your evidence of a failure.** Carry the
  quality out and let the renderer decide.
- **Test a generator's output, not its source**, when the source documents the
  mistake it is guarding against.

## Exercises

1. **Make the old query visible.** Add `avg(value) AS old_avg` alongside the
   current columns and render both as two series. On which signals would an
   operator have noticed the difference within a day, and on which would they
   never have?
2. **The rollup tier has the same shape.** `_rollup_query` reads `reading_1h` and
   selects `mean`. Is `mean` the right statistic for a long-range trend panel, or
   does it have the same problem `avg` had? Check what `reading_1h` actually
   stores before answering — the module docstring in `generate_dashboards.py`
   records a past version of that query failing loudly on a missing column.
3. **Prove the gap is still invisible somewhere.** The `WHERE` clause is fixed for
   the value, but what happens to a bucket that contains *no rows at all* because
   the instrument reported nothing for those five seconds? Construct the case and
   decide whether `count(*)` can be made to say something about it.
4. **Check the other panels.** The module docstring claims "no panel here has a
   line that simply stops". Find the panel that comes closest to that claim being
   false, and decide whether the `quality` column should reach it too.

---

**Next:** nothing — this is the end of the course. `sql/README.md` lists what is
here and what each stage assumes, and the
[OPC UA course](../../courses/opcua/README.md) is the other half of the same
plant.
