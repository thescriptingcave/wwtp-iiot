# 02-02 — Joins, and the rows that have no partner

**Previous:** [02-01](02-01_ctes.md) · **Next:** [02-03](02-03_window_functions.md)

A join matches rows from two tables. That is the easy part.

The hard part is that **the absence of a match is a result**, and every join type
is a different opinion about what that absence means. Get it wrong and your query
does not error — it quietly returns fewer rows, and the missing ones are exactly
the ones you wanted.

## The question

> Which signals have no data at all today?

Which sounds trivial and is not, because "no data" has three quite different
causes that all look identical in the table.

## The join that gets it wrong

<!-- check: skip -->
```sql
SELECT s.id, s.unit
FROM signal s
JOIN reading r ON r.signal_id = s.id
WHERE r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
  AND r.ts <  (SELECT max(ts) FROM reading);
```

That finds signals that *have* a reading, and then the `WHERE` throws away the
readings outside today. A signal with data yesterday and none today is silently
absent from the result — which is the opposite of what was asked.

The mistake is worth naming precisely: **`JOIN` is `INNER JOIN`, and an inner
join keeps only rows that matched on both sides.** A signal with no reading today
has no matching row, so it does not appear. The query is not wrong; it is
answering "which signals have data today", which is a different question.

## `LEFT JOIN`, which is the one you want

```sql
SELECT
    s.id AS signal_id,
    s.unit,
    count(r.signal_id) AS readings_today,
    max(r.ts)         AS last_seen
FROM signal s
LEFT JOIN reading r
       ON r.signal_id = s.id
      AND r.ts >= timestamptz '2026-09-28 14:05:50+00'
      AND r.ts <  timestamptz '2026-09-28 20:05:50+00'
GROUP BY s.id, s.unit
HAVING count(r.signal_id) = 0
ORDER BY s.id;
```

```
             signal_id           |  unit  | readings_today | last_seen
---------------------------------+--------+----------------+------------
 AERATION:AHU-1:MLSS               | mg/L   |              0 |     NULL
 AERATION:AHU-1:SETPOINT_DO        | mg/L   |              0 |     NULL
 AERATION:AHU-1:SRT                | d      |              0 |     NULL
 EFFLUENT:FLOW:CONDUCTIVITY        | uS/cm  |              0 |     NULL
 INFLUENT:FLOW:CONDUCTIVITY        | uS/cm  |              0 |     NULL
 PRIMARY:PRI-CL-1:BLANKET          | m      |              0 |     NULL
 PRIMARY:PRI-CL-1:TEMP              | Cel    |              0 |     NULL
 SECONDARY:SEC-CL-1:BLANKET        | m      |              0 |     NULL
 SITE:WEATHER:BARO                 | hPa    |              0 |     NULL
 SITE:WEATHER:RAIN                 | mm/h   |              0 |     NULL
 SITE:WEATHER:STORM                | {Boolean} |            0 |     NULL
 SLUDGE:DIG-1:GAS_FLOW             | m3/h   |              0 |     NULL
 SLUDGE:DIG-1:GAS_PRESSURE         | mbar   |              0 |     NULL
 SLUDGE:DIG-1:TEMP                 | Cel    |              0 |     NULL
 SLUDGE:DIG-1:VFA_ALK_RATIO         | 1      |              0 |     NULL
 SLUDGE:GAS-BLR:BOILER_DUTY         | kW     |              0 |     NULL
 SLUDGE:THK-1:TS                   | %      |              0 |     NULL
```

**Note the literal timestamps, which are the exception in this course.** Every
other lesson anchors to `(SELECT max(ts) FROM reading)`, and so should almost
yours. This one needs the seeded week to be the *newest* data, because a running
plant reports all 57 signals within any recent window and this query then returns
nothing at all. `docker compose --profile demo run --rm seed` on its own gives
that; `docker compose up softplc` as well does not. The lesson needs a dataset it
can name, and the price is that this window stops being the last six hours the
moment somebody reseeds — which is why the timestamp is a literal and not a
subtraction.

**Widen the window to a full day and the same query returns no rows at all**, over
the seeded data: every signal has produced something in twenty-four hours, so
there is nothing for the `LEFT JOIN` to preserve. The seventeen above are the
signals that went quiet across those six hours — the weather station, the digester
and the conductivity probes, which in this simulator only report when their value
actually moves.

That is worth sitting with. **A query that finds nothing is not a query that found
no problems.** The same SQL, against a different window, is either a useful
instrument or an empty one, and nothing in the result distinguishes the two.

### The rule that is worth memorising

> **A predicate about the right-hand table in a `LEFT JOIN` belongs in the `ON`
> clause. In the `WHERE` clause, it silently converts the join back to an inner
> one.**

These two are identical to read and produce different results:

<!-- check: skip -->
```sql
-- Correct: keeps the signal, with NULL for the reading
FROM signal s
LEFT JOIN reading r ON r.signal_id = s.id AND r.ts >= '2026-09-26 00:00+00'
WHERE …

-- Wrong: the WHERE discards the NULL-extended rows, so it is an inner join
FROM signal s
LEFT JOIN reading r ON r.signal_id = s.id
WHERE r.ts >= '2026-09-26 00:00+00'
```

The reason is the `WHERE`. For a signal with no matching reading, the outer join
produces `r.ts = NULL`, and `NULL >= '2026-09-26'` is `NULL` — not true — so the
row is dropped. The outer join did its work and then the `WHERE` threw it away.

**Use `WHERE` for conditions on the left table, `ON` for conditions on the right.**
It is a rule about which table the condition is *about*, and it is the single most
common `LEFT JOIN` bug.

## The three kinds of "no data", and telling them apart

`count(r.signal_id) = 0` is one answer. It does not say *why*, and the three
reasons need three different responses:

```sql
WITH today AS (
    SELECT max(ts) AS data_end FROM reading
),
status AS (
    SELECT
        s.id                                   AS signal_id,
        s.unit,
        s.sample_ms,
        count(r.signal_id) AS readings_today
    FROM signal s
    CROSS JOIN today t
    LEFT JOIN reading r
           ON r.signal_id = s.id
          AND r.ts >= t.data_end - interval '1 day'
          AND r.ts <  t.data_end
    GROUP BY s.id, s.unit, s.sample_ms
)
SELECT
    signal_id,
    unit,
    sample_ms,
    readings_today,
    CASE
        WHEN sample_ms >= 3600000 AND readings_today <= 24 THEN
            'sampled hourly or slower: few rows is expected'
        WHEN readings_today = 0 THEN
            'NO DATA: either the plant was steady, or the instrument is gone'
        WHEN readings_today < 10 THEN
            'suspiciously few readings'
        ELSE 'reporting normally'
    END AS diagnosis
FROM status
WHERE readings_today < 10
ORDER BY readings_today, signal_id;
```

The first branch is the interesting one. `sample_ms` is the instrument's
configured period in milliseconds, so `sample_ms >= 3600000` means "this is
polled hourly or slower", and a signal like `AERATION:AHU-1:SRT` — sludge retention
time, `sample_ms = 3600000` — genuinely produces a handful of rows a day.
Reporting it as "NO DATA" would be an alarm for a correctly working instrument.

**A note on the branch this query does *not* have.** The obvious thing to reach
for is the deadband: *"if the deadband is wide, silence is expected."* That
comparison is meaningless. `deadband` is in the signal's own units — 5 mg/L, 1 pH,
20 m³/h — and it is not a duration, so there is no arithmetic that turns it into
one. An earlier draft of this lesson wrote `deadband * 1000 > 86400000`, which
compares milligrams per litre to milliseconds and is true or false by accident.

`sample_ms` is the right column because it *is* a duration. The deadband's effect
on row count is real but unpredictable, and "unpredictable" is exactly the kind of
thing a threshold should not be built on.

**This is the thing the deadband makes hard, and it is not a SQL problem.** From
[01-01](../01-beginner/01-01_ask_a_question.md): a row exists when the value moved.
So "no rows" means "the value did not move", and it does *not* mean "the
instrument is broken" — but you cannot tell those apart from row counts alone.

The only way to tell them apart is to compare against a **sample rate** you
expected, and the contract has one:

```sql
-- How long since this signal last produced anything at all?
SELECT
    s.id AS signal_id,
    s.unit,
    s.sample_ms,
    (SELECT max(ts) FROM reading) - max(r.ts) AS silence
FROM signal s
LEFT JOIN reading r ON r.signal_id = s.id
GROUP BY s.id, s.unit, s.sample_ms
HAVING max(r.ts) IS NOT NULL
ORDER BY silence DESC
LIMIT 8;
```

`sample_ms` is the instrument's configured rate. It is on `signal`, it is joined
for free, and comparing "how long since the last row" against "how long *should*
there have been rows" is the only honest test.

```
              signal_id            |  unit  | sample_ms |     silence
-----------------------------------+--------+-----------+------------------
 SECONDARY:SEC-CL-1:BLANKET         | m      |      2000 | 6 days, 23:59:59
 EFFLUENT:FLOW:CONDUCTIVITY         | uS/cm  |      5000 | 6 days, 23:59:59
 PRIMARY:PRI-CL-1:TEMP              | Cel    |     10000 | 6 days, 23:59:59
```

**Six days, 23:59:59** — these signals produced rows at the very start of the
seeded week and then nothing at all. Given a `sample_ms` of 2 000, a healthy
instrument should have produced around 300 000 rows. It produced some, at the
start, and then stopped.

So: is that a broken instrument, or a settled one? The deadband says a settled
one, and the arithmetic supports it — a `PRIMARY:PRI-CL-1:BLANKET` reading with a
deadband of 0.005 m either moves by 5 mm or is not recorded, and a gravity
thickener's blanket does not move by 5 mm very often.

**You cannot settle it from row counts, and that is the honest answer.** What you
*can* do is ask a question that distinguishes them: has this signal *ever* moved
by more than its deadband in the last week? Exercise 3 is that question.

## The join that is not a join: `EXISTS`

Sometimes you do not want the second table's columns, only the question *does a
row exist*. `EXISTS` says exactly that and stops:

<!-- check: skip -->
```sql
-- Signals that have never been recorded
SELECT s.id, s.unit
FROM signal s
WHERE NOT EXISTS (
    SELECT 1 FROM reading r WHERE r.signal_id = s.id
);
```

**Use `EXISTS`, not `JOIN`, whenever you only need to know whether a match
exists.** It short-circuits on the first match, it cannot duplicate the left-hand
rows, and it says what you mean. A `JOIN` where you wanted `EXISTS` is a common
and quiet source of duplicated rows.

And here is where `NOT IN` earns its warning from
[01-02](../01-beginner/01-02_filtering.md):

<!-- check: skip -->
```sql
-- DANGEROUS. Returns nothing if ANY subquery result is NULL.
SELECT id FROM signal
WHERE id NOT IN (SELECT signal_id FROM reading WHERE signal_id IS NULL);
```

`NOT IN` is implemented as "compare against every candidate". One `NULL`
candidate makes every comparison `NULL`, so the whole predicate is never true and
the query returns zero rows — **without an error**. `NOT EXISTS` does not have
this problem, because it asks a question rather than comparing values.

This schema currently cannot produce that `NULL` (`signal_id` is `NOT NULL`), so
the query above is safe *today*. That is exactly why it is dangerous: it is safe
because of a constraint in a different table, and the day somebody relaxes that
constraint this query silently returns nothing. `NOT EXISTS` is correct
regardless.

## Exercises

1. Take the first query in this lesson and add `OR s.id = 'SLUDGE:DIG-1:PH'`.
   Predict whether the signal appears. Then move the timestamp condition from
   `ON` to `WHERE` and predict again. Run both and explain the difference using
   three-valued logic.
2. Find every signal with **no readings in the whole database**, not just today.
   Then find every signal whose last reading is more than an hour old. Are the two
   sets the same size? Should they be?
3. Using only `signal`, write a query that lists signals you would *expect* to be
   quiet today, and explain the arithmetic. Then cross-check your list against the
   `LEFT JOIN` result from the top of the lesson. How many of the "no data" signals
   did you predict?
4. `LEFT JOIN` from `signal` to `reading` on a four-million-row table. `EXPLAIN`
   it. Then `EXPLAIN` the `NOT EXISTS` version. Which is faster, and what happens
   to the plan if you add a `LIMIT 10`?
5. Write a query that finds signals whose *only* readings are `quality = 2` — an
   instrument that has been reporting failure and nothing else for a week. What
   would you want an operator to do about each row it returns?
