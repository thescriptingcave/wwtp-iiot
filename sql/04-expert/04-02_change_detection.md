# 04-02 — Change detection, and the silence that means two things

**[Back to the course](../README.md)** · **Previous:** [04-01](04-01_time_weighted_averages.md) · **Next:** [04-03](04-03_fixing_a_slow_query.md)

Lesson 04-01 was about a wrong number that looks right. This one is about a
*missing* one, and it is the harder problem, because the absence of evidence and
the evidence of absence are the same shape.

## The question

> The pump current signal has not reported since 06:40. Is the pump broken, the
> cable cut, or the pump simply idle?

## The change detection everyone writes first

`lag` gives you the previous value, and comparing it to the current one tells you
whether anything moved:

```sql
SELECT
    count(*) FILTER (WHERE previous IS NOT NULL)  AS comparable,
    count(*) FILTER (WHERE value = previous)      AS unchanged,
    count(*) FILTER (WHERE value <> previous)     AS changed
FROM (
    SELECT
        value,
        lag(value) OVER (PARTITION BY signal_id ORDER BY ts) AS previous
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '6 hours'
) t;
```

```
 comparable | unchanged | changed
------------+-----------+--------
     199823 |         0 |   199823
```

**Zero.** Out of 199,823 comparable pairs, not one repeats the value before it.

That is not a bug and it is not a coincidence. This historian is
*change-triggered*: a row is written when a value differs from the last one, so
**every row is, by construction, a change.** `value = previous` is not an
interesting question about this database. It is a question the schema guarantees
the answer to.

Which means the query everyone reaches for cannot work here, and would not work on
any historian built the same way. Change detection is the wrong frame. The
question is not *what changed* — the storage format already answered that. It is:

> **What has stopped, and when?**

## Two silences that look identical

Here are two signals that both have something wrong with them, and which look the
same in every summary you would write:

```sql
SELECT
    signal_id,
    count(*)             AS points,
    count(DISTINCT value) AS distinct_values,
    max(gap)              AS longest_silence
FROM (
    SELECT
        signal_id,
        value,
        extract(epoch FROM ts - lag(ts)
                OVER (PARTITION BY signal_id ORDER BY ts)) AS gap
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '24 hours'
) t
GROUP BY signal_id
ORDER BY max(gap) DESC NULLS LAST
LIMIT 6;
```

```
             signal_id            | points | distinct_values | longest_silence_s
---------------------------------+--------+-----------------+-------------------
 AERATION:AHU-1:WASTE_RATE        |   4389 |            4389 |             31920
 INFLUENT:LIFT:CURRENT            |   4457 |              70 |             31920
 PRIMARY:PRI-CL-1:UNDERFLOW       |   4389 |               4 |             31920
 INFLUENT:FLOW:TURBIDITY          |     22 |              21 |             30891
 INFLUENT:FLOW:PH                 |     36 |              35 |             28923
 AERATION:AHU-1:DO                |    230 |             230 |             28508
```

**31,920 seconds — eight hours and fifty-two minutes** — is the longest silence for
all three of the top signals, and they are all plant measurements, not weather.

Notice `distinct_values` while you are here, because it is the same table telling
you something else entirely. `WASTE_RATE` has 4,389 points and 4,389 *distinct*
values: it never repeats, which is a fast-moving signal. `PRIMARY:PRI-CL-1:UNDERFLOW`
has 4,389 points and **4 distinct values** — it has been sitting on four numbers all
day. `INFLUENT:LIFT:CURRENT` sits on 70.

A lift current that reports 4,457 times and holds 70 values is a pump that ran for
part of the day and then stopped, which is what the silence says. A pump that
reports 4,389 times holding only *four* values is a pump whose reading is not
credible, and no threshold on the gap size will ever tell you that.

**And here is the trap: this is not the worst signal in the table.** Watch the
*typical* gap rather than the worst one, and the ranking inverts completely:

```sql
SELECT
    signal_id,
    count(*) AS points,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)::numeric, 1) AS typical_gap_s,
    round(percentile_cont(0.9) WITHIN GROUP (ORDER BY gap)::numeric, 1) AS p90_gap_s,
    max(gap)::int AS worst_gap_s
FROM (
    SELECT
        signal_id,
        extract(epoch FROM ts - lag(ts)
                OVER (PARTITION BY signal_id ORDER BY ts)) AS gap
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '24 hours'
) t
WHERE gap IS NOT NULL
GROUP BY signal_id
ORDER BY typical_gap_s DESC
LIMIT 6;
```

```
             signal_id            | points | typical_gap_s | p90_gap_s | worst_gap_s
---------------------------------+--------+----------------+-----------+-------------
 SLUDGE:DIG-1:ALKALINITY         |      9 |        9428.0  |  12650.2 |      20879
 INFLUENT:LIFT:RUNTIME           |     23 |        3601.0  |   3999.2 |       4241
 AERATION:AHU-1:NO3_OUT          |     18 |        1946.5  |   7715.2 |      26154
 SLUDGE:DIG-1:VFA_ALK_RATIO       |     16 |        1688.0  |  10554.5 |      12869
 INFLUENT:FLOW:TURBIDITY         |     21 |        1270.0  |  10531.0 |      31691
 INFLUENT:FLOW:PH                |     35 |         951.0  |   4541.1 |      28923
```

**A digester alkalinity probe, a lift runtime counter, a nitrate monitor.** The
alkalinity probe produced **nine readings in twenty-four hours** and typically
goes 9,428 seconds — two and a half hours — between them. It also went silent for
20,879 seconds at its worst, which sounds alarming and is roughly twice its normal
behaviour. If you page an operator at 3am for that, they will tell you the digester
alkalinity probe is slow, and they will be right.

And a lift current that stops for nine hours when it normally reports every second
is a serious fault.

**The same worst-case silence — around 8.5 to 9 hours — is unremarkable for one of
these signals and a serious fault for the other. No threshold on the gap size can
tell them apart**, because the threshold would have to be simultaneously tighter
than 2.6 hours for the alkalinity probe and looser than 8 hours for the lift, and
those bounds do not overlap.

## What actually distinguishes them

Not the gap. **The ratio of the gap to what that signal normally does.** A signal's
own recent history is the baseline, because the baseline differs per signal by
three orders of magnitude in this table alone.

```sql
WITH gaps AS (
    SELECT
        signal_id,
        extract(epoch FROM ts - lag(ts)
                OVER (PARTITION BY signal_id ORDER BY ts)) AS gap
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '7 days'
),
baseline AS (
    SELECT
        signal_id,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS typical_gap
    FROM gaps
    WHERE gap IS NOT NULL
    GROUP BY signal_id
)
SELECT
    g.signal_id,
    round(b.typical_gap::numeric, 1)   AS typical_s,
    max(g.gap)::int                    AS worst_s,
    round((max(g.gap) / b.typical_gap)::numeric, 0) AS times_normal
FROM gaps g
JOIN baseline b USING (signal_id)
WHERE b.typical_gap >= 60          -- only signals that are not sampled per second
GROUP BY g.signal_id, b.typical_gap
ORDER BY times_normal DESC
LIMIT 8;
```

```
             signal_id            | typical_s | worst_s | times_normal
---------------------------------+-----------+---------+--------------
 SLUDGE:DIG-1:CH4                  |     114.0 |  344359 |        3021
 PRIMARY:PRI-CL-1:TEMP             |     234.0 |  458888 |        1961
 PRIMARY:PRI-CL-1:BLANKET          |     234.0 |  458888 |        1961
 AERATION:AHU-1:MLSS               |     234.0 |  458888 |        1961
 AERATION:AHU-1:SRT                |     234.0 |  458888 |        1961
 EFFLUENT:FLOW:CONDUCTIVITY        |     234.0 |  458888 |        1961
 INFLUENT:FLOW:CONDUCTIVITY        |     234.0 |  458888 |        1961
 SLUDGE:DIG-1:TEMP                 |     234.0 |  458888 |        1961
```

**1961 times normal.** That is the number that means something. It says: this
signal normally reports every 234 seconds, and on this occasion it went 458,888
seconds — five days and seven hours — without saying anything. Whether that is a
fault, a shutdown or a seeder artefact is a question about the plant; the *query*
has isolated it correctly, and a fixed threshold never would have.

Note what `times_normal` does that `worst_s` cannot: **it is dimensionless.**
The digester methane monitor at 3021× and a clarifier blanket at 1961× are
comparable numbers, and a threshold in seconds could not compare them.

### Why the `typical_gap >= 60` filter is not a convenience

Drop it and the table is swamped. `INFLUENT:LIFT:CURRENT` reports about once a
second, so its median gap is 1.0 s, so *ten times its median* is ten seconds — and
a signal sampled every second breaches that on ordinary jitter, thousands of times
a day. Measured over the same week, that filter would have hidden every quiet
signal on this list behind a wall of false alarms from the fast ones.

That is not a tuning annoyance. **A ratio threshold only means something relative
to a distribution you can see.** For a once-per-second signal the interesting
events are in the tail of a distribution you have to look at, not above a small
multiple of the middle. The two populations need different tools, and the honest
conclusion is that a "is this signal broken" query is per-signal configuration
rather than a constant you can ship once.

## Three details that decide whether this works

**`percentile_cont(0.5)`, not `avg(gap)`.** The median is what you want for a
baseline, and the reason is the bug you are hunting: a five-day silence inside
your baseline window drags an average upward, which raises the threshold, which
hides the next fault. The median barely moves. Use the 50th percentile and the
threshold is set by what the signal does *normally*.

**Filter in `WHERE` on the baseline, not on the gaps.** The `typical_gap >= 60`
predicate belongs to the `baseline` CTE and is applied when joining. Had it been
written against `gaps`, it would filter the very rows whose `max()` you are
reading — the same trap as [02-02](../02-intermediate/02-02_joins.md) and
[04-01](04-01_time_weighted_averages.md), and it would return an empty table with
no error.

**Anchor to `(SELECT max(ts) FROM reading)`, always.** The seven-day baseline
window has to end where the data ends, not at `now()`. Anchor to the clock and
this week — where the newest reading is hours old — computes a baseline for a
window containing hours of nothing, and every signal looks broken.

## The pitfall: a stuck sensor and a working one are the same row

The remaining failure is the mirror image, and it is worse because it produces
data rather than an absence of it.

A sensor that is stuck at a plausible value keeps reporting it. In a
change-triggered historian that means it reports it *once* and then never again —
indistinguishable, in the raw rows, from a sensor that is genuinely constant.

```sql
SELECT
    s.id AS signal_id,
    s.normal_low,
    s.normal_high,
    count(r.value) AS points,
    min(r.value)  AS lowest,
    max(r.value)  AS highest
FROM signal s
LEFT JOIN reading r
       ON r.signal_id = s.id
      AND r.ts >= (SELECT max(ts) FROM reading) - interval '24 hours'
GROUP BY s.id, s.normal_low, s.normal_high
HAVING count(r.value) > 0
   AND max(r.value) = min(r.value)
   AND min(r.value) BETWEEN s.normal_low AND s.normal_high
ORDER BY s.id;
```

```
             signal_id            | normal_low | normal_high | points | lowest | highest
---------------------------------+------------+-------------+--------+--------+--------
 AERATION:AHU-1:MLSS              |     1500.0 |      5000.0 |      1 | 2999.98 | 2999.98
 AERATION:AHU-1:SETPOINT_DO       |        2.0 |         2.0 |      1 |    2.00 |    2.00
 AERATION:AHU-1:SRT               |        8.0 |        25.0 |      1 |   15.00 |   15.00
 EFFLUENT:FLOW:CONDUCTIVITY       |      200.0 |       900.0 |      1 |  520.00 |  520.00
 INFLUENT:FLOW:CONDUCTIVITY       |      200.0 |       900.0 |      1 |  520.00 |  520.00
 PRIMARY:PRI-CL-1:BLANKET         |        0.2 |         1.0 |      1 |    0.50 |    0.50
```

**Sixteen signals match that shape**, and every one of them is inside its normal
operating range. A mixed liquor suspended solids probe reporting exactly
2999.98 mg/L for a day, on a scale running 1500 to 5000, passes every range check
this project has. So does a clarifier blanket at 0.50 m on a 0.2–1.0 m scale.

Note also that `BETWEEN normal_low AND normal_high` did its job and still told you
nothing, because a stuck sensor is stuck at a *plausible* number. That is the whole
point: the range is there to catch impossible values, and a stuck sensor is not
impossible — it is just not real.

This is lesson 04's recurring lesson in its purest form: **the value is fine and
the question is about the value.** A stuck sensor produces numbers that are in
range, have a good status code, and are wrong. The only evidence is that the
number did not move, and "did not move" is *exactly* what a healthy constant
signal also does.

Which is why there is no single query that finds this. The two failure modes are
opposites — one signal moves too little, one goes too quiet — and separating them
needs something outside the data:

- **a per-signal expectation** (a probe should vary; a setpoint may not)
- **a sibling signal** (if the clarifier blanket and its temperature are both
  frozen, the *gateway* is the suspect, not the probe)
- **time-of-day behaviour** (a night-shift quiet period is expected; a lunch-time
  one is not)

Write the check you can defend, and say in the comment which of the three it is
relying on. A "stuck sensor" query with no stated assumption is a guess.

## What to take away

- **In a change-triggered historian, `value = previous` is always false.** The
  storage format already told you what changed; asking again is not change
  detection.
- **A gap is not a fault.** A nine-hour silence on a rain gauge and on a lift
  current are the same number and opposite events.
- **Compare each gap to that signal's own median, not to a constant.** The
  dimensionless ratio — 1961× normal — is what separates them, and a fixed
  threshold never will.
- **Use the median for the baseline, not the mean**, or the fault you are hunting
  raises the bar that catches it.
- **Filter in `HAVING`, never in `WHERE`**, when the predicate decides which rows
  get counted.
- **A stuck sensor produces in-range values with good status codes.** Detecting it
  needs an expectation from outside the data, and the query must state which one
  it assumes.

## Exercises

1. **The seeder artefact.** Five signals have a 458,888-second gap — five days and
   seven hours. Find the two readings that bound it and work out what the seeder
   did. Is this a plant fault or a generation artefact, and how would you tell the
   difference from the data alone?
2. **The boundary case.** Find a signal whose `typical_gap` is between 60 and 300
   seconds and whose `p90` is more than twice its median. That is a signal that is
   usually regular and occasionally not. Is a `10×` threshold right for it?
3. **Write the stuck-sensor check for one specific signal** and state, in a
   comment, which of the three assumptions from the last section it relies on.
   Then find a signal where the check gives a false positive and explain it.
4. **Time of day.** Restrict the baseline to daytime hours and re-run the first
   query. Does the night-time data inflate anyone's threshold, and does fixing it
   change which signals you would call broken?

---

**A note on this data.** The five-day gaps in the last two queries are an artefact
of how the seed was generated, not a simulated plant fault — which is why
exercise 1 asks you to identify them. That is deliberate: a course that only
contains clean faults teaches you to expect faults to be obvious, and this lesson
is about the ones that are not.
