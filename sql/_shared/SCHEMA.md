# The schema this course runs against

Five tables. Four of them are ordinary relational tables you already know; one is
a **hypertable**, which is a Postgres table that TimescaleDB has learned to
partition by time.

The authoritative definition is [`storage/postgres/schema.sql`](../../storage/postgres/schema.sql)
— read that, not this file, if the two ever disagree. This file exists to explain
*why* each table is shaped the way it is, which a `CREATE TABLE` cannot.

## The shape in one picture

```
site ──1──n──► equipment ──1──n──► signal ──1──n──► reading   (hypertable)
                                                        
                                          event  (alarms, faults, state changes)
```

Five tables, three of them metadata, and the arrows are foreign keys the database
enforces. That is the whole design, and it is unremarkable — which is the point.
The previous version of this project stored the same information as tag columns
in a time-series database and documents in a document store, and needed fifteen
bugs and two services to make up for it.

## `site`, `equipment`, `signal` — identity

| Table | One row per | Holds |
|---|---|---|
| `site` | plant | name, and the permit limits as `JSONB` |
| `equipment` | asset — 22 of them | area, type, rated power, duty, fail modes |
| `signal` | measurement — 57 of them | unit, range, normal band, deadband, sample rate, which Modbus register exposes it |

These three are the *identity* of the plant: what exists, what it is called, what
it can physically do. It changes when someone re-commissions a pump, and not
otherwise.

`signal.equipment_id` is **nullable, and that is correct.** 24 of the 57 signals
belong to a grouping rather than to an asset — `INFLUENT:FLOW:FLOW` is a
measurement of the influent flow, which is not a pump, and `INFLUENT:LIFT:RUNTIME`
belongs to the lift station rather than to any one of its three pumps. Those
signals have `equipment_id = NULL` and a separate `signal.holder` naming the
grouping. This split was forced by a foreign key complaining, which is a story
worth reading in [`docs/LEARNING-LOG.md`](../../docs/LEARNING-LOG.md).

Permit limits live on `site`, not on the signals, and that is a decision with a
consequence: a limit that changed on reissue does **not** rewrite history. A
reading taken in March was compliant with the limit that applied in March. Storing
the limit on every reading — which the tag-column design did, since it wrote every
column of metadata into every point — would mean a permit reissue either lied
about the past or required rewriting four million rows.

## `reading` — the hypertable

<!-- check: skip -->
```sql
CREATE TABLE reading (
    ts          TIMESTAMPTZ      NOT NULL,
    signal_id   TEXT             NOT NULL REFERENCES signal (id),
    value       DOUBLE PRECISION,               -- NULL = no value; see below
    quality     SMALLINT         NOT NULL DEFAULT 0,
    source      TEXT             NOT NULL,
    PRIMARY KEY (ts, signal_id, source),
    CONSTRAINT reading_quality_known      CHECK (quality IN (0, 1, 2)),
    CONSTRAINT reading_source_known      CHECK (source IN
                        ('opcua', 'modbus', 'seed', 'rollup')),
    CONSTRAINT reading_null_is_not_good   CHECK (value IS NOT NULL OR quality <> 0)
);
SELECT create_hypertable('reading', by_range('ts'));
```

Five columns. Every one of them earns its place, and the reasoning is in the
comments in the file. Three are worth stating here because the course depends on
them.

**`value` is nullable and `quality` is not.** A failed instrument is a row that
exists, with `value = NULL` and `quality = 2`. It is not dropped, and it is not
stored as zero. The `CHECK` constraint `reading_null_is_not_good` makes that
enforced rather than conventional: you cannot record a Good reading that has no
value, because zero is a real dissolved-oxygen concentration, a real flow rate,
and a real alarm state.

**`source` is in the primary key.** Both protocol faces can record the same signal
at the same instant. That is deliberate, and
[00-02]](../00-foundations/00-02_identity_and_values.md) is about why.

**`signal_id` is a foreign key.** This is the single largest correctness gain in
the schema, and the reason to read [00-02]](../00-foundations/00-02_identity_and_values.md) before
writing any query. A reading cannot name a signal that does not exist.

## `event` — the one genuinely document-shaped thing

<!-- check: skip -->
```sql
CREATE TABLE event (
    id           BIGSERIAL   PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind         TEXT        NOT NULL,
    severity     TEXT        NOT NULL CHECK (severity IN ('info','warning','critical')),
    message      TEXT        NOT NULL,
    signal_id    TEXT             REFERENCES signal (id),
    equipment_id TEXT             REFERENCES equipment (id),
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb
);
```

Alarms, fault injections, state changes. The `detail` column is where the
flexibility lives, and it is `JSONB` so it can be indexed:

<!-- check: skip -->
```sql
SELECT ts, kind, detail->>'reason' FROM event WHERE kind = 'blower_trip';
```

Note what this table used to be. It was a document store, and `kind` and
`severity` were keys inside a document, so "everything critical this week" was a
full scan with a `JSON` filter. Now `severity` is a constrained, indexable column
and the flexible part is the part that genuinely varies. **A document store gives
you the flexibility and takes away the constraints — the wrong way round for the
fields you actually query on.**

## The two continuous aggregates

<!-- check: skip -->
```sql
SELECT time_bucket('1 minute', ts), signal_id, source,
       avg(value), min(value), max(value), count(value)
FROM reading GROUP BY 1, 2, 3;
```

`reading_1m` and `reading_1h`, with `materialized_only = true`.

**Both aggregate from `reading`, not from each other.** This is the single most
important decision in the schema, and it is easy to get wrong. Rolling 1 minute up
into 1 hour by averaging the 1-minute averages gives a wrong answer as soon as
two minutes hold different numbers of points — and they always do, because the
deadband filtered some of them and the last window of a run is short. The error is
largest exactly when the data is most interesting, which is during an event.

Aggregating both tiers from raw costs a second pass over the data and removes the
problem completely: `avg` is always a true average over raw points, and `count` is
always exact. `03-advanced` measures it.

**`count(value)`, not `count(*)`.** The first counts readings you can use; the
second counts readings that happened. They are different numbers, and conflating
them is how a "fraction of readings that were bad" quietly measures the wrong
thing.

**`materialized_only = true`** means the aggregate does not answer for the last
incomplete window. A rollup that answers for a window that is still changing
teaches a reader to trust a number that is about to move. The price is that
"right now" is a query on `reading`, which is the correct place to ask it.

## Retention

| Tier | Resolution | Kept |
|---|---|---|
| `reading` | 1 s | 7 days |
| `reading_1m` | 1 min | 90 days |
| `reading_1h` | 1 h | indefinitely |

Retention is the reason the tiers exist. Keeping every 1 Hz reading for two years
is expensive *and* less useful, because a query over 63 million points is a query
nobody runs.

## Running a query

```bash
uv run python tools/sqlrun.py "SELECT count(*) FROM reading"
```

or use `psql` if you have it — it is the better tool. `sqlrun` is there for when
you do not.
