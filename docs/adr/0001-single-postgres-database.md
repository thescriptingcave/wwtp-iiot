# ADR 0001 — One PostgreSQL database, not InfluxDB plus Couchbase

* **Status:** accepted
* **Date:** 2026-09-26
* **Supersedes:** the implicit two-database arrangement; the reasoning is recorded
  in [`../DESIGN.md`](../DESIGN.md) §"What was rejected"
* **Affects:** `storage/`, `compose.yaml`, `sql/`, `docs/`

## Context

The project stored raw time series in InfluxDB 3 and metadata and events in
Couchbase Community. The stated justification, written into the README, was:

> "stores its own history in two databases chosen for two different jobs"

Time series and documents are genuinely different shapes, and the two engines each
did their own job competently. The problem was not the shape argument. It was that
the argument was a *consequence* of the first decision rather than a reason for
it, and that nobody checked.

## Decision

Replace both with one PostgreSQL 16 database using TimescaleDB 2.30.

* `reading` — hypertable, partitioned on `ts`
* `site`, `equipment`, `signal` — ordinary relational tables
* `event` — ordinary table with a `JSONB detail` column
* Two continuous aggregates, `reading_1m` and `reading_1h`, **both** aggregating
  from `reading`

## The measurements that decided it

**The document store held zero heterogeneity.** Three key-sets, one shape each,
80 documents. A document store's value is flexibility across heterogeneous
documents; there were none.

**The two stores could not be joined.** That was the only reason there were two,
and it was a storage limitation presented as an architectural one.

**Six SQL features did not exist**, all established by running the query and
reading the error: `CASE`, `IN`, `HAVING`, `INTERVAL`, scalar subqueries, and
`ORDER BY` on any column but `time`. Every lesson in the new
`sql/02-intermediate` needs at least one. That stage was unwritable before.

**A table's schema was fixed by its first write**, so the project's first design
rule — "tag by identity, field by value" — was reverse-engineered from a write
rejection and then written down as a principle about data modelling.

**Fifteen bugs**, found by running against the live databases. Six schema and
transport, four SDK, five compose; three of the compose bugs would each have
stopped the stack on a fresh machine.

**An Enterprise licence key was required** for the planner that made nine of
twenty-three course queries work. A fresh `docker compose up` began by failing
for a reason that had nothing to do with the project.

## Consequences

**Good**

* Referential integrity across metadata and time series. `reading.signal_id` is a
  foreign key, so a reading cannot name a signal that does not exist. There was no
  mechanism for that before. It found a real contract bug on first run.
* Four services instead of seven, and no licence key.
* The whole `sql/02` stage became writable, and `CASE` / `HAVING` / `INTERVAL` /
  subqueries are now used without comment.
* The weighted-average bug in the rollup worker is gone, because both aggregate
  tiers read from raw and there is no intermediate average to average.
* Constraints the database enforces, replacing conventions only the writer
  respected: `quality IN (0,1,2)`, and a `CHECK` that a valueless reading cannot
  claim to be Good.

**Bad**

* TimescaleDB is a dependency with its own versioning and its own failure modes.
  `CREATE EXTENSION timescaledb` is a hard requirement, and a plain Postgres
  cannot read a hypertable.
* The per-row storage cost went up, because the tag columns that were cheap to
  repeat are now a join.
* Two continuous aggregates cost a second pass over the data. That is the price of
  not having a weighting bug, and it was worth paying.

**Neutral**

* Roughly 5 900 lines were untouched: the process model, the contract loader, both
  protocol servers, the deadband, the spool, the fault engine. The coupled surface
  was about 1 800 lines behind a narrow interface.

## Alternatives considered and not chosen

**Keep InfluxDB, drop Couchbase only.** The document store's removal is
independently justified — three key-sets, one shape each. But the *time-series*
choice was the load-bearing one, and the 15 bugs and the six missing SQL features
are all downstream of it. Fixing half the problem would have left the
architectural claim ("two different jobs") still false.

**Keep both, add a join layer.** TimescaleDB's multi-node and cross-node query
support does not span InfluxDB and Couchbase. There is no join to have.

**Plain PostgreSQL, no TimescaleDB.** Workable — 4.3 million rows is small, and
BRIN indexes on `ts` plus a monthly `PARTITION BY RANGE` would cover this
workload. Rejected because the chunking, the rollups and the retention policies
would all be hand-written, and the hand-written versions are exactly what the
previous rollup worker was. The lesson is the *design* of the rollups — both tiers
from raw — not the absence of an extension.

## How to check this decision

```bash
docker compose up -d db
docker compose run --rm init-db
docker compose --profile demo run --rm seed     # 4.3 M readings, about 2 min
uv run python tools/check_sql.py sql/           # 64 queries, all passing
```

If a future change reinstates a second database, the question to ask is not
"are these different shapes" — they are, and that was always true. It is:

> **What does the split make impossible that a single relational schema would
> allow?**
