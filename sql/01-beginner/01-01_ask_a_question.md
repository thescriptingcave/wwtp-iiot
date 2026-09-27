# 01-01 — Ask a question

**Previous:** [README](README.md) · **Next:** [01-02](01-02_filtering.md)

The first thing to get right is not clever. It is *specific*.

## The question

> What is dissolved oxygen in the aeration basin doing right now?

## The vague version

```sql
SELECT * FROM reading;
```

Four million rows. You have learned nothing and possibly crashed something.

## The specific version

```sql
SELECT ts, value, quality, source
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
ORDER BY ts DESC
LIMIT 10;
```

```
            ts             |      value       | quality | source
----------------------------+------------------+---------+--------
 2026-09-27 02:07:43.886867 | 2.027180593      |       0 | opcua
 2026-09-27 00:21:45.886867 | 2.047185509      |       0 | opcua
 2026-09-27 00:04:34.886867 | 2.067260395      |       0 | opcua
 2026-09-26 23:47:23.886867 | 2.086335181      |       0 | opcua
 2026-09-26 23:30:12.886867 | 2.105410457      |       0 | opcua
```

Every part of that query is load-bearing.

**`WHERE signal_id = …`** — without it you are averaging dissolved oxygen in mg/L
with blower speed in rpm. This is the first mistake everyone makes, and it is
worth doing once deliberately: drop the filter and look at what comes back.

**`ORDER BY ts DESC`** — you asked what it is doing *now*, so newest first.
`LIMIT 10` then means "the last ten readings", not "ten arbitrary readings".
Note the ordering works on an ordinary column here; this is not a special
property of time-series storage, it is `ORDER BY`.

**Explicit columns, never `*`.** `value` and `quality` and `source` are the three
you want. `*` gives you all five and you spend the rest of the query working out
which one you meant. The habit is worth forming before it is painful.

**`LIMIT`** — a `LIMIT` without an `ORDER BY` is a lie. Postgres will give you ten
rows and make no promise about which.

## Why the timestamps are far apart, and what that means

Look at the gaps: 02:07, 00:21, 00:04, 23:47. That is not a 1 Hz signal.

**This is the deadband doing its job.** Every signal in `contracts/tags.yaml`
declares one, and the gateway refuses to write a reading that has not moved far
enough to be worth recording:

```
AERATION:AHU-1:DO   deadband 0.02 mg/L
```

Dissolved oxygen in a settled basin drifts by hundredths of a mg/L over minutes.
At 1 Hz that is 3 600 readings an hour saying almost nothing, and a historian
storing all of them is mostly storing noise.

So `reading` is not "one row per sample". It is **one row per sample that
changed enough to be worth keeping.** That is a design decision with consequences
you will meet repeatedly:

* row counts are not a sample rate;
* a `count(*)` over an hour is a measure of *how much the plant moved*, not of how
  long it ran;
* a gap in the data means the signal was steady, not that the historian was down.

```sql
-- How much did each signal actually move over the last day?
SELECT
    signal_id,
    count(*)                    AS readings,
    max(value) - min(value)     AS range_moved
FROM reading
WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY signal_id
ORDER BY readings DESC
LIMIT 8;
```

A tight deadband on a noisy signal produces tens of thousands of rows a day; a wide
one on a stepped signal produces single digits. Both are working correctly, and
neither number is a sample rate.

## The one query worth memorising

```sql
SELECT
    r.ts,
    r.value,
    r.quality,
    s.unit,
    s.range_min,
    s.range_max,
    s.normal_low,
    s.normal_high
FROM reading r
JOIN signal s ON s.id = r.signal_id
WHERE r.signal_id = 'AERATION:AHU-1:DO'
ORDER BY r.ts DESC
LIMIT 5;
```

You did not have to know the unit, the range, or the normal band, because you did
not have to memorise them either. You asked the database.

**That join is the difference between this schema and the one it replaced.** In
the previous design every one of those facts was a column *on the reading*, so a
query that wanted the unit had to filter on it:

<!-- check: skip -->
```sql
-- previous design: the unit was duplicated into every row
WHERE signal = 'do_mg_l' AND eu = 'mg/L'
```

And if the unit ever changed — a probe replaced with one reporting in µg/L, which
is a real and expensive event — the change had to be written into four million
historical rows, or the history would lie about what the old numbers meant.

Here the join is free, always correct, and updating the unit is one `UPDATE`.

## When there is no data at all

```sql
SELECT ts, value FROM reading WHERE signal_id = 'SLUDGE:DIG-1:ALKALINITY'
ORDER BY ts DESC LIMIT 4;
```

```
            ts             |      value       | quality | source
----------------------------+------------------+---------+--------
 2026-09-27 01:36:11.886867 | 3574.969329      |       0 | seed
 2026-09-26 22:59:03.886867 | 3579.969423      |       0 | seed
 2026-09-26 20:42:16.886867 | 3584.972896      |       0 | seed
```

**Nine readings in the whole day.** Not a query problem, and not a broken
historian. Alkalinity in the digester moves slowly and in small steps, so with a
deadband of 5 mg/L on a range of 0–8 000 it takes a real change before anything is
recorded.

Contrast the busiest signal in the same window:

```sql
SELECT r.signal_id, count(*) AS n, s.deadband, s.unit
FROM reading r JOIN signal s ON s.id = r.signal_id
WHERE r.ts >= (SELECT max(ts) FROM reading) - interval '1 day'
GROUP BY 1, 3, 4
ORDER BY n DESC
LIMIT 5;
```

```
         signal_id          |    n   | deadband |  unit
----------------------------+---------+----------+---------
 PRIMARY:PRI-SCR-1:TORQUE    |   67122 |    0.5   | N.m
 UTILITY:SITE:PLANT_POWER    |   64652 |    1     | kW
 AERATION:AHU-1:BLOWER_RPM   |   63799 |    2     | rev/min
```

Four orders of magnitude between the noisiest and the quietest signal in one
plant, in one day. That spread *is* the deadband, and it is why the seeder exists:
**an empty result is the most common first experience with this database and it is
almost never the query's fault.**

## Exercises

1. Run the vague query with a `LIMIT 100`. How many *distinct* `signal_id` values
   appear? How many distinct units? Now write the one-line fix.
2. `AERATION:AHU-1:DO` has a deadband of 0.02 mg/L. Find three signals with a much
   wider one and three with a much tighter one, using only `signal`. Predict
   roughly how many readings each will have per day, then check.
3. A signal is missing entirely from `reading`. Write **two** queries that would
   find it — one using `signal`, one using `reading` — and say which is faster on
   a four-million-row table and why.
4. The join above gives you the unit for free. Write the query that gives you the
   *equipment* the signal belongs to, and explain what the row looks like for one
   of the 24 grouping signals from [00-02](../00-foundations/00-02_identity_and_values.md).
5. `ORDER BY ts DESC LIMIT 10` and `LIMIT 10` return different row counts from a
   four-million-row table. Find out why, and write a query that proves it.
