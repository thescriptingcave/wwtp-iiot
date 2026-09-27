# 00-02 — Identity and value, and why they are different tables

**Previous:** [00-01](00-01_time_series_is_not_relational.md) ·
**Next:** [00-03](00-03_quality_is_data.md) · [Back to the course](../README.md)

Every schema for industrial telemetry makes the same first decision: *what is
identity and what is a measurement?* Get it right and the rest of the design
falls out. Get it wrong and you will spend a year querying a database that cannot
tell you what anything means.

This project's first design wrote the answer down as a slogan — **tag by identity,
field by value** — and then discovered the slogan was a workaround. This lesson
gives you the real answer, which is not a slogan.

## The question

> Every signal in this plant is read twice, once over Modbus TCP and once over
> OPC UA. Show me both readings of dissolved oxygen at the same moment, and tell
> me whether they agree.

## The schema, which is the answer

```sql
SELECT count(*) FROM signal;    -- 57
SELECT count(*) FROM equipment; -- 22
```

Fifty-seven signals. Twenty-two assets. One of each thing the plant *is*.

And then the table of things the plant *did*:

```sql
SELECT * FROM reading ORDER BY ts DESC LIMIT 5;
```

Five columns. `ts`, `signal_id`, `value`, `quality`, `source`.

**Notice what is not there.** No unit. No area. No equipment. No range. No normal
band. No deadband. Every one of those is in `signal`, and every one of them is the
same on every row for the life of the plant.

That is the whole lesson, and it is worth being precise about why it is a *design*
decision rather than a tidiness one.

## The rule, and where it actually comes from

> **Identity goes in one table. Measurement goes in another. A row in the
> measurement table names a row in the identity table.**

The previous design had this as six tag columns on every reading: `area`,
`equipment`, `signal`, `eu`, `site`, `source`. The rule was stated as *"a tag is
something that would still be true tomorrow"* — `area=AERATION` is true next week,
`value=2.03` is not.

That rule is not wrong. It is a *consequence*, and the previous project never
established what the cause was. Here is the cause, and it is worth having,
because it generalises:

**A thing that cannot change is not a property of a measurement. It is a property
of what is being measured.** Unit is a property of the signal, not of any
particular reading of it. Range is a property of the instrument. The deadband is a
property of the historian's opinion about the signal. Putting them on the reading
means either duplicating them four million times or breaking the row apart so they
can be looked up.

## What it costs, measured

The old design wrote six tag columns and two field columns into every single
point. A point was about 1.4 kB. This design's row is:

```sql
SELECT pg_column_size(t.*) AS bytes_per_row
FROM (SELECT ts, signal_id, value, quality, source FROM reading) t
LIMIT 1;
```

```
 pg_column_size
---------------
            71
```

Seventy-one bytes, tuple overhead included. The metadata is stored 57 times instead
of four million, and it is stored somewhere you can `UPDATE`.

That last part is the one people miss. In the old design, changing a signal's range
was not a query you could run — it was a data migration, because the range was
copied into every historical reading. And that is not merely expensive, it is
**wrong**: the reading taken in March was compliant with the range that applied in
March.

## The query: both protocol faces, at one instant

```sql
SELECT
    ts,
    max(value) FILTER (WHERE source = 'opcua') AS opcua,
    max(value) FILTER (WHERE source = 'modbus') AS modbus,
    max(value) FILTER (WHERE source = 'modbus')
      - max(value) FILTER (WHERE source = 'opcua') AS difference
FROM reading
WHERE signal_id = 'AERATION:AHU-1:DO'
  AND ts >= '2026-09-26 12:00:00+00'
  AND ts <  '2026-09-26 12:05:00+00'
GROUP BY ts
ORDER BY ts
LIMIT 5;
```

`FILTER (WHERE …)` is the readable way to pivot. `CASE WHEN … THEN … END` also
works and you will meet both; `FILTER` is used throughout this course because it
states the intent — *of these rows, the ones where source is modbus* — without the
`ELSE NULL` ceremony.

**The `difference` column is the point of the exercise.** The gateway reads Modbus
first and OPC UA second, so in normal operation only one source's row exists per
poll. Both appear only when you ask a question that makes both faces record, which
is what this query does by writing both sources at the same timestamp.

## And now the reason `source` is in the primary key

```sql
SELECT
    conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'reading'::regclass AND contype = 'p';
```

```
   conname    |              pg_get_constraintdef
--------------+---------------------------------------------------
 reading_pkey | PRIMARY KEY (ts, signal_id, source)
```

`source` is part of the identity of a row. That is a deliberate decision, and it
buys the single most useful check in the system:

> **The same physical quantity, observed twice, independently.**

Dissolved oxygen in the aeration basin is one number in the world. It is measured
by two instruments, over two protocols, by two pieces of software. If they agree,
you have evidence. If they disagree, one of them is lying, and the disagreement is
a two-row answer to a `SELECT`.

### Why this is not a nicety

Read the next query and then decide for yourself whether you would rather have it:

```sql
-- The wrong value, which this project has actually produced
SELECT 2.3e-41 AS decoded_low_word_first_float;
```

A 32-bit float whose two words arrive **low-word-first**, decoded as though they
arrived **high-word-first**, gives about `2.3e-41`.

Now put that through every check this system could possibly have:

* is it finite? **yes**
* is it in the signal's range of 0–20 mg/L? **yes**
* is it a plausible dissolved-oxygen concentration? **no — it is a flat line at
  effectively zero**
* would a deadband notice? **yes** — and that is the only reason it was ever
  caught, and it is a reason that would not survive a plant that was actually
  running

Nothing about the *number* is wrong. There is no validation you can write against
it. The only defence is to compare two independent observations of the same
physical quantity — which means you have to store both, which means `source` has
to be part of the row's identity.

This is the project's signature bug class, and it is worth noticing the shape of
it: **the failures that no amount of validation catches are the ones where the
wrong value is a perfectly well-formed value.** Every one of the bugs recorded in
[`docs/LEARNING-LOG.md`](../../docs/LEARNING-LOG.md) is like this.

## The join, and why there is only one database

```sql
SELECT
    s.id, s.unit, s.range_min, s.range_max, s.normal_low, s.normal_high,
    e.id AS equipment, e.type, s.modbus_address
FROM signal s
LEFT JOIN equipment e ON e.id = s.equipment_id
WHERE s.id LIKE 'AERATION%'
ORDER BY s.id;
```

`24` of the 57 signals have `equipment_id = NULL`, and that is correct. They
belong to a grouping, not to an asset: `INFLUENT:FLOW:FLOW` measures the influent
flow, which is not a pump. `INFLUENT:LIFT:RUNTIME` belongs to the lift station
rather than to any one of its three pumps.

This is not a hypothetical design choice. **A foreign key found it.** The
`equipment` field in the contract had been quietly doing two jobs — "the asset id"
*and* "whatever the middle of the signal id happens to be" — and fifteen signals
were naming a holder that was not an asset. Nothing had complained, because in the
previous storage engine nothing *could*: a tag said `equipment=FLOW` and there was
no constraint anywhere that could say there is no such asset.

They are now two things. `signal.equipment_id` is a column, and it is the asset or
`NULL`. The holder is *derived*, and there is deliberately no `holder` column:

```sql
SELECT
    split_part(id, ':', 2) AS holder,
    equipment_id,
    count(*) AS signals
FROM signal
GROUP BY 1, 2
ORDER BY 1;
```

```sql
SELECT split_part(id, ':', 1) AS area, count(*) FROM signal GROUP BY 1 ORDER BY 1;
```

A derived column is a column that can be wrong. If `holder` were stored, it would
be a second copy of a fact already encoded in `id`, and every query that joined
the two would have to decide what to do when they disagreed — which is a question
nobody should have to answer about an identifier. The derivation is one function
call and it cannot drift.

(The OPC UA server builds its address-space folders from the same derivation, in
Python. Two implementations of one rule in two languages is a real smell, and it
is accepted here because one of them is a *writer* of the contract and the other
is a *reader* of it, with a test asserting they agree.)

A constraint found a modelling error that a year of querying had not.

## Exercises

1. `reading` has five columns. `signal` has sixteen. For each column of `signal`,
   give a one-sentence answer to: *if this were on `reading` instead, what would go
   wrong?* Several of them have more than one answer.
2. The primary key is `(ts, signal_id, source)`. Suppose it were
   `(ts, signal_id)`. What query from this lesson becomes impossible, and what
   breaks in the gateway?
3. Run the both-faces query above over an hour. `difference` is not always exactly
   zero. Find the largest one, identify the signal, and work out whether you
   expect the two faces to agree exactly. (Hint: the two are not reading the same
   instant.)
4. Find the 24 signals with `equipment_id IS NULL` and their distinct
   `split_part(id, ':', 2)` values — there are four. Then argue for or against
   each being an `equipment` row instead of a NULL.
5. `signal.modbus_address` is nullable, and only 14 of 57 signals have one. Look at
   the registers in `contracts/tags.yaml` and account for the other 5. (They are
   not measurements, which is itself the interesting part.)
