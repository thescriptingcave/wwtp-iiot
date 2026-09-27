-- ============================================================================
-- wwtp-iiot — schema
--
-- Applied by storage/postgres/schema.py. Hand-written on purpose: it is five
-- tables and stable, so a generator would be ceremony. What *is* generated is
-- the row data, from contracts/tags.yaml — and tests/test_postgres_schema.py
-- asserts the contract fits this file, which is where drift would actually show
-- up.
--
-- This replaces an earlier design that stored readings in InfluxDB 3 with six
-- tag columns and two fields. The reasons, all measured, are in
-- docs/DESIGN.md; the short version is that the tag model was standing in for a
-- relation the database could have enforced.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─── metadata ────────────────────────────────────────────────────────────────
--
-- Relational, because the data is relational. The InfluxDB version stored these
-- as documents in Couchbase, and every document type turned out to have exactly
-- one key-set: 22 equipment documents with identical keys, 57 tag documents
-- with identical keys, zero heterogeneity. A document store's value proposition is
-- schema flexibility across heterogeneous documents, and there was none to be
-- flexible about.
--
-- The thing this buys is not elegance, it is *constraints the database enforces*.

CREATE TABLE IF NOT EXISTS site (
    id          TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    -- Permit limits live here rather than on the signals, because they are a
    -- property of the licence and not of any instrument. A limit that changed on
    -- reissue would otherwise need rewriting on every historical reading — which
    -- is expensive, and wrong: the reading was compliant with the limit that
    -- applied at the time.
    permit      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    design      JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS equipment (
    id          TEXT PRIMARY KEY,
    site_id     TEXT        NOT NULL REFERENCES site (id),
    area        TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    type        TEXT        NOT NULL,
    rated_kw    DOUBLE PRECISION,
    duty        TEXT,
    fail_modes  TEXT[]      NOT NULL DEFAULT '{}'
    -- No `state` column. Equipment state changes every scan; a row that changes
    -- every scan is write amplification wearing a metadata costume. State is a
    -- measurement, and it lives in `reading`.
);

CREATE TABLE IF NOT EXISTS signal (
    id                 TEXT PRIMARY KEY,
    -- Nullable, and deliberately so. A signal whose holder is a grouping
    -- (INFLUENT:FLOW, SITE:WEATHER) has no single asset. Declaring that here
    -- rather than inventing a placeholder equipment row is the difference
    -- between a schema that tells the truth and one that does not.
    equipment_id       TEXT             REFERENCES equipment (id),
    area               TEXT             NOT NULL,
    measurement        TEXT             NOT NULL,
    field              TEXT             NOT NULL,
    unit               TEXT             NOT NULL,
    range_min          DOUBLE PRECISION NOT NULL,
    range_max          DOUBLE PRECISION NOT NULL,
    normal_low         DOUBLE PRECISION NOT NULL,
    normal_high        DOUBLE PRECISION NOT NULL,
    deadband           DOUBLE PRECISION NOT NULL,
    deadband_mode      TEXT             NOT NULL DEFAULT 'absolute',
    sample_ms          INTEGER          NOT NULL,
    writable           BOOLEAN          NOT NULL DEFAULT FALSE,
    -- Which Modbus register exposes this signal, if any. Nullable: five of the
    -- nineteen registers are not measurements, and a heartbeat has no signal.
    modbus_address     INTEGER,
    modbus_word_order  TEXT,
    CONSTRAINT signal_range_ascending   CHECK (range_min < range_max),
    CONSTRAINT signal_normal_in_range   CHECK (normal_low >= range_min
                                           AND normal_high <= range_max),
    CONSTRAINT signal_deadband_positive CHECK (deadband > 0),
    CONSTRAINT signal_deadband_mode     CHECK (deadband_mode IN
                                               ('absolute', 'relative', 'always')),
    CONSTRAINT signal_word_order        CHECK (modbus_word_order IS NULL
                                           OR modbus_word_order IN ('big', 'little'))
);

-- The word-order trap, as a database constraint rather than a comment in a
-- config file. The InfluxDB version carried this as a YAML field that a client
-- had to remember to read; here it is part of the schema, so a signal claiming a
-- float32 register cannot claim high-word-first by accident.
CREATE INDEX IF NOT EXISTS signal_modbus_idx
    ON signal (modbus_address) WHERE modbus_address IS NOT NULL;

-- ─── readings ────────────────────────────────────────────────────────────────
--
-- The hypertable. Three decisions, each of which cost something in the InfluxDB
-- version.
--
-- 1. `value` is NULLABLE and `quality` is NOT NULL. A failed instrument is a row
--    that exists with value NULL and quality 2 — distinguishable from "no data"
--    and from "a wrong number". InfluxDB 3 arrived at the same representation,
--    but only because it refused every alternative; here it is a design choice
--    the database then enforces.
--
-- 2. `quality` has a CHECK constraint. InfluxQL had no constraints at all, so
--    Good/Uncertain/Bad was a convention that only the writer respected.
--
-- 3. `source` is in the primary key, so both protocol faces can record the same
--    signal at the same instant. That is what makes "Modbus and OPC UA disagree"
--    a query rather than a hunch — and it is the check that catches the
--    low-word-first float bug in the field.
CREATE TABLE IF NOT EXISTS reading (
    ts          TIMESTAMPTZ      NOT NULL,
    signal_id   TEXT             NOT NULL REFERENCES signal (id),
    value       DOUBLE PRECISION,
    quality     SMALLINT         NOT NULL DEFAULT 0,
    source      TEXT             NOT NULL,
    PRIMARY KEY (ts, signal_id, source),
    CONSTRAINT reading_quality_known  CHECK (quality IN (0, 1, 2)),
    CONSTRAINT reading_source_known  CHECK (source IN
                                            ('opcua', 'modbus', 'seed', 'rollup')),
    -- A reading with no value cannot claim to be Good. Without this a NULL
    -- value with quality 0 would be indistinguishable from a reading of zero,
    -- and zero is a real dissolved-oxygen concentration, a real flow rate, and a
    -- real alarm state. This is the invariant the whole quality scale exists to
    -- protect, expressed as something the database refuses to violate.
    CONSTRAINT reading_null_is_not_good CHECK (value IS NOT NULL OR quality <> 0)
);

SELECT create_hypertable('reading', by_range('ts'),
                         if_not_exists => TRUE,
                         migrate_data => TRUE);

-- The query in every dashboard: one signal, newest first. Timescale's
-- segment-by-unique-value keeps each signal's points contiguous, so this is an
-- index seek per signal rather than a scan over the whole retention window.
CREATE INDEX IF NOT EXISTS reading_signal_time_idx
    ON reading (signal_id, ts DESC);

-- ─── events ──────────────────────────────────────────────────────────────────
--
-- Alarms, fault injections, state changes. The one genuinely document-shaped
-- thing in the system, and `detail` is where that flexibility lives.
--
-- This is strictly better than the Couchbase version: `kind` and `severity` are
-- real, constrained, indexable columns, so "everything critical this week" is a
-- fast typed query, while the varying payload stays flexible. A document store
-- gives you the flexibility and takes away the constraints — the wrong way round
-- for the fields you actually query on.
CREATE TABLE IF NOT EXISTS event (
    id          BIGSERIAL   PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT        NOT NULL,
    -- A word rather than a number, because it is queried by meaning. A numeric
    -- scale nobody remembers the order of gets queried backwards.
    severity    TEXT        NOT NULL,
    message     TEXT        NOT NULL,
    signal_id   TEXT             REFERENCES signal (id),
    equipment_id TEXT            REFERENCES equipment (id),
    detail      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT event_severity_known CHECK (severity IN
                                            ('info', 'warning', 'critical'))
);

CREATE INDEX IF NOT EXISTS event_ts_idx        ON event (ts DESC);
CREATE INDEX IF NOT EXISTS event_severity_idx  ON event (severity, ts DESC);
CREATE INDEX IF NOT EXISTS event_detail_idx    ON event USING GIN (detail);

-- ─── continuous aggregates ───────────────────────────────────────────────────
--
-- These replace the hand-rolled rollup worker, and the important decision is
-- what they aggregate *from*.
--
-- Each tier aggregates from `reading`, the raw table. NOT from the tier below.
--
-- That is the whole trick, and it eliminates a bug the previous design
-- documented at length. Rolling 1 minute up into 1 hour by averaging the 1-minute
-- averages is wrong the moment two minutes hold different numbers of points —
-- and they always do, because the deadband filtered some of them and the last
-- window of a run is short. The error is largest exactly when the data is most
-- interesting, which is during an event.
--
-- Aggregating both tiers from raw costs a second pass over the data and removes
-- the weighting problem entirely: `avg` is always a true average over raw points
-- and `count` is always exact. The lesson survives in docs/LEARNING-LOG.md, and
-- the bug does not survive here.
--
-- `count(value)` rather than `count(*)`: the first counts readings you can use
-- and the second counts readings that happened. They are different numbers, and
-- conflating them is how a "fraction of readings that were bad" quietly measures
-- the wrong thing.

CREATE MATERIALIZED VIEW IF NOT EXISTS reading_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 minute', ts) AS bucket,
    signal_id,
    source,
    avg(value)   AS mean,
    min(value)   AS min,
    max(value)   AS max,
    count(value) AS n
FROM reading
GROUP BY bucket, signal_id, source
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS reading_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    signal_id,
    source,
    avg(value)   AS mean,
    min(value)   AS min,
    max(value)   AS max,
    count(value) AS n
FROM reading
GROUP BY bucket, signal_id, source
WITH NO DATA;

-- materialised_only = true: a continuous aggregate is a rollup, and a rollup
-- that answers for the last incomplete window teaches the reader to trust a
-- number that is about to change. The trade is that the newest minute is not
-- available through `reading_1m`; query `reading` for "right now".
ALTER MATERIALIZED VIEW reading_1m
    SET (timescaledb.materialized_only = true);
ALTER MATERIALIZED VIEW reading_1h
    SET (timescaledb.materialized_only = true);

-- ─── retention ───────────────────────────────────────────────────────────────
--
-- Applied by the seed/rollup step rather than at CREATE time, because the
-- intervals come from the environment and a schema file that hardcodes an
-- operational parameter is a schema file that has to be edited to change it.
--
--   1 s raw, 7 days · 1 min, 90 days · 1 h, indefinitely
--
-- Retention is the reason the tiers exist. Keeping every 1 Hz reading for two
-- years is expensive *and* less useful, because a query over 63 million points is
-- a query nobody runs.

-- ─── roles ───────────────────────────────────────────────────────────────────
--
-- Not in the DDL, deliberately, and the reasoning is the interesting part.
--
-- `init-db` runs this file as the database *owner*, and it is the only thing that
-- ever should. A role's grants to *other* roles are cluster state, not schema
-- state: they survive a dump of this file, they are not restored by a
-- `pg_dump --schema-only`, and a migration that creates a table and forgets to
-- grant on it produces a runtime permission error rather than a schema error.
-- So they are applied separately, by `storage/postgres/roles.py`, and the
-- check below is what makes the separation visible.
--
-- Why they exist at all is in `docs/SECURITY.md`. The short version: the
-- previous stack had a bucket-scoped application user that provably could not
-- administer the cluster, and the migration replaced it with one shared
-- password that owns the database. That is a regression, and the three roles
-- here are the fix:
--
--   wwtp_owner   owns the schema. init-db only. Never given to a long-running
--                process, because it can DROP TABLE and a historian that can
--                forget is worse than one that stops.
--   wwtp_writer  INSERT on reading, INSERT on event, SELECT on signal and
--                equipment. The gateway. Notably *no DELETE* — a compromised
--                gateway should be able to lie by omission but not by erasure.
--   wwtp_reader  SELECT only. Grafana and the dashboard.
