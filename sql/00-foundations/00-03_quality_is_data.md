# 00-03 — A broken instrument is data, not a gap

**Previous:** [00-02](00-02_identity_and_values.md) ·
**Next:** [01-beginner](../01-beginner/) · [Back to the course](../README.md)

This is the most important idea in the course, and it is one line of schema:

<!-- check: skip -->
```sql
CONSTRAINT reading_null_is_not_good CHECK (value IS NOT NULL OR quality <> 0)
```

A reading with no value **cannot** claim to be Good. Not by convention. The
database refuses it.

## The three states, and why collapsing any two of them is a bug

A sensor can be in one of three conditions, and each of them has a *different
correct answer* to "what is the reading?":

| `quality` | Meaning | `value` |
|---|---|---|
| 0 | Good | a number |
| 1 | Uncertain | a number you should distrust |
| 2 | Bad — the instrument is not working | `NULL` |

The tempting shortcuts are all wrong in a way that matters:

* **Drop the bad rows.** Now a failed sensor is indistinguishable from a sensor
  that was never read. Your chart shows a gap, and "gap" and "broken" are
  different facts with different responses: one is a network question, the other
  is a maintenance one.
* **Store zero.** Zero is a *real* dissolved-oxygen concentration — it is what
  anaerobic sludge smells like — and a real flow rate, and a real alarm state. A
  bad sensor reading zero is a false alarm; a bad sensor reading nothing is
  information.
* **Store the last good value.** Now you cannot tell when the instrument died,
  which is the single thing you most need to know.
* **Store a string `"nan"`.** Not possible here, and worth knowing why: the column
  is `DOUBLE PRECISION`, and the database will not put text in it. That refusal is
  the feature.

## The scale

```sql
SELECT
    quality,
    CASE quality
        WHEN 0 THEN 'Good'
        WHEN 1 THEN 'Uncertain'
        WHEN 2 THEN 'Bad'
    END AS name,
    count(*) AS readings,
    count(value) AS with_a_value
FROM reading
GROUP BY quality
ORDER BY quality;
```

```
 quality |  name   | readings | with_a_value
---------+---------+----------+--------------
       0 | Good    |  4289810 |      4289810
```

Every seeded reading is Good, which is *itself* a finding worth pausing on. The
seeder replays the plant model through real fault scenarios, and a blower trip
takes out a signal's readings — so why is `quality` 2 nowhere in the data?

The answer is in the schema, and it is a real gap rather than a cosmetic one:
**the gateway's deadband drops a non-Good reading before it reaches the database.**

```python
# gateway/deadband.py
if quality != QUALITY_GOOD:
    return True      # publish it — do not filter a fault
```

That is the *correct* behaviour, and it is why `reading` is capable of holding
`quality = 2` rows. But the seeder and the fault engine do not route through the
gateway's deadband in the same way, so the seeded history contains faults visible
in the *values* — a blower that stops, ammonia that climbs hours later — and
almost none visible in the *quality column*.

The exercises at the bottom are about closing that gap by hand, which is the only
honest way to practise writing queries about bad instruments before the alarm
engine (Phase 4) starts producing them for real.

## Writing some

```sql
INSERT INTO reading (ts, signal_id, value, quality, source)
VALUES
    ('2026-09-27 09:00:00+00', 'AERATION:AHU-1:DO', 2.14, 0, 'opcua'),
    ('2026-09-27 09:00:01+00', 'AERATION:AHU-1:DO', 2.09, 1, 'opcua'),
    ('2026-09-27 09:00:02+00', 'AERATION:AHU-1:DO', NULL, 2, 'opcua');
```

Three readings of the same signal, one second apart, in three different states, in
one statement.

**The timestamps have to differ, and the reason is worth having.** Try it with
`now()` three times:

```
ERROR:  duplicate key value violates unique constraint "85_reading_pkey"
DETAIL:  Key (ts, signal_id, source)=(2026-09-27 03:00:08.203171+00,
        AERATION:AHU-1:DO, opcua) already exists.
```

The primary key is `(ts, signal_id, source)`, so *one signal, one instant, one
protocol, one reading*. That is not a limitation, it is a **definition**: a
reading is an observation of a signal at a moment by a source, and there is
exactly one such thing. The database will not let you have two opinions about the
same instant from the same instrument and call them two readings — you would have
to invent a `source` value, and `source` is constrained too.

(If a re-send genuinely happens, the gateway's `INSERT ... ON CONFLICT DO UPDATE`
handles it: the same reading arriving twice updates rather than duplicates. That
is the difference between the live path and the seeder's `COPY`, which cannot
upsert and will simply fail. Both halves of that sentence are load-bearing.)

Now try the thing the schema forbids:

<!-- check: skip -->
```sql
INSERT INTO reading (ts, signal_id, value, quality, source)
VALUES (now(), 'AERATION:AHU-1:DO', NULL, 0, 'opcua');
```

```
ERROR:  new row for relation "_hyper_7_85_chunk" violates check constraint
        "reading_null_is_not_good"
DETAIL:  Failing row contains (2026-09-27 03:04:11.482913+00,
         AERATION:AHU-1:DO, null, 0, opcua).
```

**Read the error carefully — it names the chunk, not the table.** A constraint on
a hypertable lives on every chunk it has, because a chunk *is* a table. So the
name in the message is `_hyper_7_85_chunk` and the name you wrote is
`reading_null_is_not_good`. Both are correct and neither is a bug; it is simply
what a distributed table looks like from the inside, and it will confuse you once.

## The two aggregates, and the difference between them

This is where the representation pays for itself.

```sql
SELECT
    count(*)                AS readings_that_happened,
    count(value)            AS readings_you_can_use,
    count(*) - count(value) AS readings_with_no_value,
    avg(value)              AS mean_of_what_you_can_use
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO';
```

* `count(*)` counts rows. It counts the broken instrument.
* `count(value)` counts rows **with a value**. It skips the `NULL`s.
* `avg(value)` ignores `NULL`s — so it is a mean over the readings you can use,
  which is the right thing, and it is right *by accident of SQL's null handling*
  rather than by anything you wrote.

That last point deserves emphasis, because it is the sort of thing that works
until you do something reasonable to the query. If you `coalesce` the bad readings
to zero to "fill the gap", the mean now includes a fabricated value, and it drops.
You will do this. It is a reasonable thing to want. Do it deliberately.

```sql
-- A question you should be suspicious of
SELECT
    signal_id,
    avg(coalesce(value, 0)) AS mean_including_fabricated_zeros
FROM reading
GROUP BY signal_id
ORDER BY mean_including_fabricated_zeros
LIMIT 5;
```

Run it. Then run the honest version and compare:

<!-- check: skip -->
```sql
SELECT
    signal_id,
    count(*) - count(value) AS unusable,
    avg(value)               AS honest_mean
FROM reading
WHERE ts >= now() - interval '2 days'
GROUP BY signal_id
HAVING count(*) - count(value) > 0
ORDER BY unusable DESC;
```

That second query returns nothing in the seeded data, for the reason given above.
Which is fine — it is a query you will write many times, and knowing that it is
*correct and empty* is different from knowing it is broken.

## Quality as a filter, and the decision hiding in it

```sql
-- "Only the readings I trust"
SELECT ts, value
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND quality = 0
  AND ts >= (SELECT max(ts) FROM reading) - interval '6 hours'
ORDER BY ts;
```

**Why `(SELECT max(ts) FROM reading)` and not `now()`.** It was `now()` here
until the pre-push review ran `tools/check_sql.py` against a database seeded six
hours earlier and this query came back **empty** — with the runner's own warning,
`an empty result in a seeded database is usually a wrong signal id`, which is
right and was not the problem.

The problem is that `now()` is the wrong anchor for a retrospective question. A
seeder writes history *up to the moment it ran*, so a seeded database's newest
reading is as old as the seed; six hours later "the last six hours" contains
nothing, and the lesson silently becomes an empty result set that looks like a
broken signal. The pattern is already taught in
[01-beginner](../01-beginner/README.md) — `now() - interval '24 hours'` for
"the last day", and this for "the last day *of the data*".

The distinction is not pedantry. **In a historian, `now()` and "the end of the
data" are different questions**, and a query that silently conflates them will
return an empty set rather than an error on any database that is not being
written to right now. Every retrospective query in a historian has to choose, and
this is the lesson that says so.

Two things about this query that are worth more than the query:

**The `quality = 0` is redundant for `avg`, and that redundancy is the point.**
Drop it and `avg(value)` gives the *same number*, because `avg` skips NULLs. But
drop the *rows* with `quality = 1` — the Uncertain ones, which do have values —
and you get a different number. Two decisions that look identical in the query
and are not the same decision.

You want both, usually, and it is worth being able to say which you meant:

```sql
-- Uncertain readings excluded, broken ones were already excluded by the NULL
SELECT avg(value) AS mean_of_good_and_uncertain FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO' AND quality <> 2;

-- Bad readings treated as "no reading at all", which is what avg already does
SELECT avg(value) AS mean_of_everything_with_a_value FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO';
```

**Never filter on `value IS NULL` to find bad instruments.** Filter on `quality`.
They are related — the constraint guarantees a `NULL` value is never Good — but
they are not the same question, and the moment you have Uncertain readings with
values the two diverge.

## Exercises

1. Insert a `quality = 1` reading with a value, and one with `NULL`. The second
   must fail. Now work out, from the constraint, what the database is guaranteeing
   about the *pair* (`value`, `quality`) — and whether it is a complete
   characterisation or only half of one.
2. Write a query that reports, per signal, the fraction of readings that were
   unusable, using `count(value)` and not `count(*)`. Why does the order of the
   arithmetic matter for a fraction?
3. The deadband publishes non-Good readings but filters Good ones that have not
   moved. Suppose someone changes that so a *Bad* reading is dropped instead.
   Using only the schema, work out what a chart of dissolved oxygen would then
   look like during a blower trip, and what question you would no longer be able
   to answer.
4. `quality` is `SMALLINT` with a `CHECK (quality IN (0,1,2))`. Suppose a
   maintenance mode is added later, with readings that are valid but should be
   excluded from process statistics. What are the two ways to add it, what does
   each cost, and which is the one that will still be correct in three years?
5. `source` is also `CHECK`-constrained. Find a fourth value it might reasonably
   need, and argue for whether it belongs in the constraint or beside it.
