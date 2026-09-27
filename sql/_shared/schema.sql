-- ============================================================================
-- wwtp-iiot — generated schema. DO NOT EDIT BY HAND.
--
--   uv run python sql/_shared/generate_schema.py
--
-- Generated from contracts/tags.yaml so that the course cannot drift from the
-- data. Every constraint below was measured against a real InfluxDB 3 instance;
-- see docs/LEARNING-LOG.md for the errors that established them.
-- ============================================================================


-- Raw tier: 1 s, 57 signals across 9 process stages.

-- aeration: 14 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."aeration" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- effluent: 9 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."effluent" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- influent: 6 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."influent" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- lift: 5 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."lift" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- primary: 5 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."primary" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- secondary: 4 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."secondary" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- site: 4 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."site" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- sludge: 9 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."sludge" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- utility: 1 signal(s)
CREATE TABLE IF NOT EXISTS "wwtp"."utility" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "value" DOUBLE,
  "quality" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);


-- ---------------------------------------------------------------------------
-- Rollup tiers
--
-- A rollup point is not a measurement: it has no `value`, it has `mean` and
-- `count`. Keeping them apart matters — a query that averaged `value` across
-- both tiers would be averaging a reading with a summary of readings, and would
-- return a number that means nothing.
--
-- `count` is the field that makes the tiers composable, and it is what makes the
-- weighted mean correct. Averaging the averages is wrong the moment two windows
-- hold different numbers of points, and they always do: the deadband filtered
-- some of them, one contains an outage, and the last window of a run is short.
-- The error is largest exactly when the data is most interesting.
--
-- Only two fields, for the same reason the raw tier has two.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS "bucket_1m"."aeration_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."effluent_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."influent_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."lift_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."primary_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."secondary_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."site_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."sludge_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1m"."utility_1m" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- 1m buckets hold time_bucket(INTERVAL '1 minute', time)

CREATE TABLE IF NOT EXISTS "bucket_1h"."aeration_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."effluent_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."influent_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."lift_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."primary_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."secondary_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."site_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."sludge_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

CREATE TABLE IF NOT EXISTS "bucket_1h"."utility_1h" (
  "time" TIMESTAMP NOT NULL,
  "area" TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal" TEXT NOT NULL,
  "eu" TEXT NOT NULL,
  "site" TEXT NOT NULL,
  "source" TEXT NOT NULL,
  "mean" DOUBLE,
  "count" BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);

-- 1h buckets hold time_bucket(INTERVAL '1 hour', time)

-- ---------------------------------------------------------------------------
-- Reading notes that the course keeps repeating
--
-- * ORDER BY accepts `time` and nothing else. `ORDER BY signal` is a parse
--   error that says "invalid ORDER BY, expected TIME column".
-- * `field` is a reserved word. So is `key`, and `AS key` is a parse error.
-- * A timestamp filter needs RFC 3339: `time >= '2026-09-26T00:00:00Z'`.
--   `time >= '1758844800000'` is rejected as "not a valid timestamp".
-- * `count()` is not available. Use `COUNT(*)`.
-- ---------------------------------------------------------------------------
