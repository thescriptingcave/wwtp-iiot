# WWTP-IIOT — End-to-End Test Plan

**Target repo:** `/Users/dev/Developer/wwtp-iiot`
**Scope:** SQL layer, Grafana, Node-RED, web dashboard, notebooks, ML workshops, install/upgrade, cross-cutting
**Environments:** macOS + Linux, local Docker Compose, CI
**Roles served:** wastewater engineer, data engineer, data scientist, analyst, ops

---

## How to use this plan

### Prioritisation

Not everything here is equal. Run in this order; stop at the first red gate unless told otherwise.

| Priority | What | Section | Est. time | Gate |
|---|---|---|---|---|
| **P0 — Smoke (blocking)** | Stack comes up, data lands, all 4 surfaces answer | A1, A2, B1, B3, C1, E1–E3 | 45 min | `make up` green |
| **P0 — Data integrity (blocking)** | Schema, roles, retention, aggregates, data quality | A3–A9 | 90 min | `make sql`, `make roles` |
| **P1 — Correctness** | Panels, flows, alarm rules, notebook outputs | A10–A12, B2, B4–B6, C2–C6, D2–D5 | 4 h | `make check`, `make notebooks` |
| **P2 — Resilience** | Failure injection, drift, rollback | D6, E5–E7, F3 | 3 h | manual |
| **P3 — Security / hygiene** | Docs, secrets, least-privilege | F1, F2, F4, F5 | 90 min | `make lint-debt` |

### Time budget

- **Full pass:** ~12 hours across 3–4 sessions.
- **P0 only (release smoke):** ~2 h 15 min.
- **CI-equivalent:** `make check && make integration && make notebooks && make workshop-notebooks` ≈ 45 min + 8 min + 40 min.

### Reporting

For every test case record one row. Copy this header:

```
| ID | Title | Result (PASS/FAIL/BLOCKED) | Commit | Env | Evidence link | Notes |
```

Rules:
- **Do not fix a gate to make it pass.** `docs/CLEAN-MACHINE-TEST-PLAN.md` states this explicitly. A red gate is a finding; changing the assertion is a second finding.
- Attach evidence to every FAIL. A FAIL with no evidence is not actionable.
- `BLOCKED` is a legitimate result and is expected for P2/P3 items when a single host is all you have.
- Record the exact commit (`git rev-parse --short HEAD`) and the exact command line you ran. Many expected values in this plan are commit-pinned.

### Naming convention for evidence

```
evidence/
  <test-id>/
    cmd.txt            # the exact command and its full output
    query.txt          # SQL + psql output (use \pset format aligned)
    screenshot.png     # dashboard / notebook / Grafana UI
    log.txt            # docker compose logs, tracebacks
    verdict.md         # one paragraph: what happened, why, next step
```

### Before you start — the four facts that will waste your time if you don't know them

1. **DB port on this host is 55433, not 5432.** `tools/env.sh` sources `.env` *after* the inherited environment, so **make wins** over your environment. `POSTGRES_PORT=5999 make notebooks-open` still targets 55433. The classic symptom is `dsn()` falling back to 5432 and reporting a **password** error when the real fault is the **port**.
2. **`.env` must exist before almost anything.** `make setup` creates it from `.env.example` and never overwrites. `tools/env.sh` *warns* rather than fails when it is missing, because CI deliberately has none.
3. **Committed `.ipynb` files carry no outputs — by design.** `outputs` in a notebook is a record of the last run and rots silently. If you open one in JupyterLab and it dirties the tree, run `make notebooks-reset`. Do not commit the diff.
4. **The gateway writes; the gates read.** `make sql`, `make notebooks` and `make test_scada_contract.py` all stop the gateway first (`make db-still`) because lessons anchor to `(SELECT max(ts) FROM reading)`. If you see non-deterministic gate failures, check whether the gateway is running.

---

## 1. System architecture as understood from the code

### 1.1 The single source of truth

`contracts/` is the only place meaning is declared, in **two** files (not one — this is load-bearing and was previously documented wrongly):

- **`contracts/tags.yaml`** — 22 `equipment`, 8 `areas`, and a `measurements` block yielding **57 signals**. Each signal carries unit, engineering range, normal band, deadband + mode, sample rate, writable flag, Modbus address and word order.
- **`contracts/fault-scenarios.yaml`** — **12 faults** and **6 scenarios**, deliberately separate ("a fault is a test instrument, not a description of the plant").

Everything downstream is *derived*: PLC physics, both protocol servers, the gateway, the DDL, the seeder, the alarm rules, the Node-RED flows, both Grafana dashboards, the web dashboard's read model, and the tests. There is a whole class of drift gate (§F5) whose only job is to prove nothing was hand-edited.

Signal identity is `AREA:HOLDER:MEASUREMENT`; the value lives in a column, never in the name. 24 of the 57 signals name a grouping (`FLOW`, `LIFT`, `SITE`, `WEATHER`) rather than an asset, so `signal.equipment_id` is **nullable** and `holder` is derived from the id. There is no `signal.holder` column. *(See Open Question Q1 — a doc still claims there is.)*

### 1.2 Data flow — one scan, end to end

```
process model ──► scan loop ──► function blocks ──► scan image
      │                                                   │
       ┌───────────────────────────────────────────────────┴─────────────┐
       ▼                                                                 ▼
  Modbus TCP :5020 (19 regs)                                  OPC UA :4840 (generated tree)
  17 big-word-first, 2 little                                  AREA.HOLDER.FIELD, browse names verbatim
       │                                                                 │
       └────────────────────────────────┬────────────────────────────────┘
                                        ▼
                        gateway: deadband → spool (JSONL) → Postgres/Timescale
                                         │                    reading (hypertable)
                                         └────────────► event
```

Per-scan, in order:

1. **Process model** — `softplc/process/units.py`, ~1600 lines of hand-written Monod kinetics, mass balances, digester VFA/alkalinity, breakpoint chlorination, DO control loop, chlorine dose controller. Steps every 20 ms. Returns 57 values + quality per signal. Knows nothing about a database. Only runtime dependency in the whole project is PyYAML.
2. **Scan image** — `softplc/scanloop.py`, the register file. Blocks read/write in fixed order on one thread; a block reading a value written earlier *in the same scan* sees the new value. Ordering is asserted by tests.
3. **Two protocol faces** — Modbus 19 registers, only 14 linked to signals (5 are deliberately not: heartbeat, fault code, state bitfield, two halves of a 32-bit counter). **17 big-word-first, 2 little** — `AERATION_BLOWER_VALVE` @40108 and `AERATION_WASTE_RATE` @40304; the `little` one is the *fifth* in a run of `big`, not adjacent. Wire offset = `contract address − 40000`. Wrong order decodes to **~2.3e-41**, which passes every range check.
4. **Deadband** — `gateway/deadband.py`. Publishes when: first reading since writer start, **or** the value moved past the signal's deadband (absolute / relative-to-span / always), **or** quality is not Good (faults are never filtered). Non-finite values always publish (a NaN comparison is always False, so a NaN would otherwise suppress a reading forever). **Consequence: a row exists only when the value moved, so "last known value" and "how long ago" are different facts.** 13 of 57 signals produce exactly **one** reading in a seeded week.
5. **Spool** — `gateway/spool/store.py`, JSON Lines on named volume `gateway-spool`, rotated every **60 s** (not hourly — see Open Question Q3), cap 512 MB. Delivery is **at-least-once**: `pending()` → `read()` → `flush()` → then `acknowledge()` deletes. Crash between write and delete re-delivers rather than loses.
6. **Postgres** — 5 tables (§A3). Writer batches **2000 rows or 5 seconds**, whichever first; both bounds are needed.

### 1.3 The eight compose services

| Service | Image | Profile | Host port | Purpose |
|---|---|---|---|---|
| `db` | `timescale/timescaledb:2.30.1-pg16` | — | `${HOST_BIND}:5432` | Postgres 16 + TimescaleDB 2.30.1 |
| `softplc` | project image | — | `5020`, `4840` | the simulated plant, both protocols |
| `init-db` | project image | — | — | one-shot: schema, metadata, retention, roles |
| `gateway` | project image | — | none | read both protocols, deadband, spool, write |
| `scada` | `scada/nodered` | `scada` | `127.0.0.1:18880` | Node-RED, admin root `/scada` |
| `web` | `ui/web` (Next.js 15.1.6) | `ui` | `${HOST_BIND}:3001` | custom dashboard, server-rendered, read-only role |
| `grafana` | `grafana/grafana:11.5.1` | `observability` | `${HOST_BIND}:3000` | provisioned dashboards |
| `seed` | project image | `demo` | — | one-shot: a week of history |

One Python image, parameterised by command. `depends_on` uses `service_healthy` for `db`/`softplc` and `service_completed_successfully` for `init-db`. Hardening: non-root (`10001` Python, `10002` web, `1000` node-red), `cap_drop: [ALL]` on Python services, `no-new-privileges`, `read_only: true` on `web`, log rotation 10 MB × 3. **No `deploy.resources` limits anywhere** — see F2/T-F2-3.

### 1.4 Databases

| Name | Owner | Used by |
|---|---|---|
| `wwtp` | — | the plant, seeded relative to `now()` |
| `wwtp_notebooks` | — | notebooks; **pinned** fixture, 7 days ending `2026-09-29T00:00:00Z` |
| `wwtp_test` | — | `make integration` throwaway |
| `wwtp_ml` | — | workshop, 3-week default |
| `wwtp_ml25` | — | `make workshop-long`, deliberately separate so it cannot overwrite the panel |

### 1.5 Storage tiers

| Tier | Resolution | Retention | Source |
|---|---|---|---|
| `reading` (hypertable) | 1 s | **7 days** (`RETENTION_RAW_DAYS`) | gateway / seed |
| `reading_1m` (cagg) | 1 min | **90 days** (`RETENTION_MINUTE_DAYS`) | rolls up **from `reading`** |
| `reading_1h` (cagg) | 1 h | indefinite | rolls up **from `reading`** |

Both aggregates roll up from raw, **not from each other** — rolling 1m into 1h by averaging means is wrong wherever deadband left unequal counts. Both are `materialized_only = true` and created `WITH NO DATA`, so **nothing is in them until a refresh runs.** That exact gap once produced a 2-row `reading_1h` from 4.29 M seeded readings.

### 1.6 What is deliberately absent

- **No MQTT.** Excluded so protocol behaviour stays visible. Excluding it and then *generating* the OPC UA tree hid the protocol just as effectively — hence `courses/opcua/`, which makes a reader walk the generated tree by hand and says what is wrong with it.
- **No authentication in the demo stack.** Threat model in `docs/SECURITY.md`, which names **three** gaps (the README says two — see Q4).
- **No ORM.** Every query was chosen.
- **No message bus** between gateway and database. The spool file is the durability mechanism.
- **No pip-installable CLI.** `[project.scripts]` is absent. Everything is `python -m <module>`. See Q5.
- **No Grafana alerting.** Zero alert rules, zero contact points. Panel threshold bands are *not* alarms, and `ui/grafana/generate_dashboards.py:18-34` says so explicitly. See B5.

---

# A. SQL Layer

## A0. Area-level notes

- **There are no `.sql` migration files and no migrations runner.** `sql/00-foundations .. 04-expert` are a *Markdown course* of 20 numbered lessons, not migrations. Ordering is enforced by the call sequence in `storage/postgres/schema.py:505-559`: `schema.sql` → `seed_metadata()` → `apply_retention()` → `apply_roles()`. Rationale is in the docstring at `schema.py:82-88`: exactly one schema file, so a migration framework would have one migration in it.
- **There are no stored procedures, functions or views.** The only server-side objects are the two continuous aggregates plus Timescale built-ins (`create_hypertable`, `add_retention_policy`, `add_continuous_aggregate_policy`, `CALL refresh_continuous_aggregate`). If the brief expects procedures, this is a **finding**, not a gap in the plan — see Q6.
- **The authoritative DDL is `storage/postgres/schema.sql` (267 lines).** Test against that, not against `sql/`, which is teaching material.
- Connection: `make psql` → `docker compose exec db psql -U wwtp -d wwtp`. For scratch queries prefer `make query SQL="..."`.

---

## A1. Fresh schema application is idempotent

**Objective** — `init-db` applies a clean schema from nothing, and re-running it changes nothing.

**Pre-conditions**
```bash
cd /Users/dev/Developer/wwtp-iiot
make setup            # creates .env from .env.example if absent
docker compose down -v
docker compose up -d db init-db
```

**Steps**
1. Wait for completion: `docker compose wait init-db` (or poll `docker compose ps init-db` until `Exited (0)`).
2. Capture the init log:
   ```bash
   docker compose logs init-db | tee evidence/A1/init-db.log
   ```
3. Confirm exit code 0 and inspect the full log for `ERROR`, `WARNING`, `already exists`:
   ```bash
   docker compose logs init-db | grep -inE 'error|warning|already exists' | tee evidence/A1/init-db-warnings.txt
   ```
4. Re-run init-db a second time to prove idempotency:
   ```bash
   docker compose up -d --force-recreate init-db
   docker compose logs init-db | grep -icE 'error|already exists'   # expect 0
   ```
5. Verify the five tables exist and nothing extra did:
   ```sql
   \dt
   -- expect exactly: site, equipment, signal, reading, event,
   --                  reading_1m, reading_1h  (7 relations)
   ```
6. Verify the hypertable conversion:
   ```sql
   SELECT hypertable_name FROM timescaledb_information.hypertables;
   -- expect exactly one row: reading
   ```

**Expected result**
- Both runs exit 0. `IF NOT EXISTS` is used throughout, so the second run is a silent no-op.
- No `WARNING` of class `duplicate_table` / `duplicate_object`.
- `reading` is the only hypertable.

**Pass/fail criteria**
- PASS: both runs exit 0, 7 relations, 1 hypertable, zero `ERROR` lines.
- FAIL: any `ERROR`; any relation beyond the 7 expected; `reading` not a hypertable.
- Note: this does **not** yet prove the schema is *correct* — that is A3.

**Evidence** — `init-db.log`, `init-db-warnings.txt`, psql `\dt` output, `hypertable_name` query.

**Risks / caveats**
- A missing C compiler (`cc`) makes the Postgres image build fail with a *shared library* error naming neither cause. If init-db never starts, check `cc --version` before anything else.

---

## A2. Contract → schema fidelity

**Objective** — every signal and asset in `contracts/tags.yaml` reaches exactly one row, with metadata intact.

**Pre-conditions** — A1 complete.

**Steps**
1. Establish the contract's own counts (do not take them from a doc):
   ```bash
   make contract | head -20
   make contract | wc -l        # expect 57, one line per signal
   ```
2. Compare against the database:
   ```sql
   SELECT (SELECT count(*) FROM signal)    AS signals,
          (SELECT count(*) FROM equipment) AS equipment,
          (SELECT count(DISTINCT area) FROM signal) AS areas,
          (SELECT count(*) FROM site)     AS sites;
   -- expect: 57 | 22 | 8 | 1
   ```
3. Prove **no signal was invented by the seeder** and none dropped:
   ```sql
   -- expect 0 rows
   SELECT s.id FROM signal s
   LEFT JOIN equipment e ON e.id = s.equipment_id
   WHERE s.equipment_id IS NOT NULL AND e.id IS NULL;

   -- expect 57 rows, the 24 grouping signals + 33 asset signals
   SELECT count(*) FILTER (WHERE equipment_id IS NULL)     AS grouping_signals,
          count(*) FILTER (WHERE equipment_id IS NOT NULL) AS asset_signals
   FROM signal;
   -- expect: 24 | 33
   ```
4. Spot-check one signal's full metadata against `contracts/tags.yaml` (pick `AERATION:AHU-1:DO`):
   ```sql
   SELECT * FROM signal WHERE id = 'AERATION:AHU-1:DO';
   ```
   Expect `area='AERATION'`, `measurement='DO'`, `field` non-null, `unit='mg/L'`, `range_min < range_max`, `normal_low < normal_high`, `deadband > 0`, `deadband_mode IN ('absolute','relative','always')`, `sample_ms = 20000`.
5. Verify permit limits live on `site`, not on `signal`:
   ```sql
   SELECT jsonb_pretty(permit) FROM site;
   ```
   Expect keys for ammonia (10 mg/L), TSS (30 mg/L), pH min/max, coliform geometric mean.

**Expected result** — 57 / 22 / 8 / 1, 24 grouping signals with NULL `equipment_id`, zero orphan FKs, permit in `site.permit`.

**Pass/fail criteria**
- PASS: counts match exactly; 0 orphan rows; permit JSON contains all five parameters.
- FAIL: any count differs; a grouping signal has a non-NULL `equipment_id`; an asset signal has NULL; `permit` is `{}`.

**Evidence** — `make contract` output, the four-row counts query, `SELECT * FROM signal`, `permit` JSON.

**Risks / caveats**
- **Do not treat a count mismatch as "the doc is wrong" without checking `git rev-parse HEAD`.** Several docs carry stale counts (§F5). The contract is authoritative.

---

## A3. Schema shape — columns, types, constraints, indexes

**Objective** — the physical schema matches the reviewed design and every CHECK constraint is meaningful.

**Pre-conditions** — A1, A2.

**Steps**
1. Dump the schema and read it:
   ```bash
   make query SQL="SELECT column_name, data_type, is_nullable, column_default
                   FROM information_schema.columns
                   WHERE table_name='reading' ORDER BY ordinal_position"
   ```
   Expect, in order: `ts TIMESTAMPTZ not null`, `signal_id TEXT not null`,
   `value DOUBLE PRECISION` **nullable**, `quality SMALLINT not null default 0`,
   `source TEXT not null`.

2. **The nullable-`value` design is deliberate.** `value` is NULL by design; the constraint
   `reading_null_is_not_good CHECK (value IS NOT NULL OR quality <> 0)` means NULL is *only*
   legal when quality is non-Good. Verify it bites:
   ```sql
   -- expect ERROR: violates check constraint "reading_null_is_not_good"
   INSERT INTO reading (ts, signal_id, value, quality, source)
   VALUES (now(), 'AERATION:AHU-1:DO', NULL, 0, 'opcua');

   -- expect ERROR: violates check constraint "reading_quality_known"
   INSERT INTO reading (ts, signal_id, value, quality, source)
   VALUES (now(), 'AERATION:AHU-1:DO', 2.0, 7, 'opcua');

   -- expect ERROR: violates check constraint "reading_source_known"
   INSERT INTO reading (ts, signal_id, value, quality, source)
   VALUES (now(), 'AERATION:AHU-1:DO', 2.0, 0, 'carrier-pigeon');
   ```
   Each of these must fail. Run inside a transaction you roll back.

3. Verify **no CHECK constraint is a tautology**. `tests/test_postgres_schema.py` sweeps the
   *file* for `OR true`; now check the *live* catalog for a constraint that can never fail:
   ```sql
   SELECT conname, pg_get_constraintdef(oid)
   FROM pg_constraint
   WHERE conrelid IN ('reading'::regclass,'signal'::regclass,'event'::regclass)
     AND contype = 'c'
   ORDER BY conrelid::text, conname;
   ```
   Expect `signal_range_ascending`, `signal_normal_in_range`, `signal_deadband_positive`,
   `signal_deadband_mode`, `signal_word_order`, `reading_quality_known`,
   `reading_source_known`, `reading_null_is_not_good`, `event_severity_known`.
   **None** may contain `OR true`.

4. Verify the primary key is the **triple** — this is the whole cross-protocol defence:
   ```sql
   SELECT a.attname FROM pg_index i
   JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
   WHERE i.indrelid = 'reading'::regclass AND i.indisprimary;
   -- expect, in order: ts, signal_id, source
   ```
5. Confirm the same signal at the same instant from two sources is legal:
   ```sql
   BEGIN;
   INSERT INTO reading (ts, signal_id, value, quality, source)
   SELECT now(), 'AERATION:AHU-1:DO', 2.0, 0, 'opcua';
   INSERT INTO reading (ts, signal_id, value, quality, source)
   SELECT now(), 'AERATION:AHU-1:DO', 9.9e-42, 0, 'modbus';
   SELECT count(*) FROM reading
    WHERE signal_id='AERATION:AHU-1:DO' AND ts = (SELECT max(ts) FROM reading
                                                  WHERE signal_id='AERATION:AHU-1:DO');
   -- expect 2
   ROLLBACK;
   ```
6. Verify indexes exist:
   ```sql
   SELECT indexname FROM pg_indexes WHERE tablename IN ('signal','reading','event') ORDER BY 1;
   -- expect: signal_modbus_idx (partial, WHERE modbus_address IS NOT NULL)
   --         reading_signal_time_idx (signal_id, ts DESC)
   --         event_ts_idx (ts DESC), event_severity_idx (severity, ts DESC)
   --         event_detail_idx (GIN on detail)
   --         plus the two cagg-backed indexes on reading_1m / reading_1h
   ```
7. Confirm `reading` has **no** `DELETE`/`TRUNCATE`-friendly design compromise and that
   `signal_modbus_idx` is genuinely partial:
   ```sql
   SELECT indexdef FROM pg_indexes WHERE indexname='signal_modbus_idx';
   -- expect the definition to contain: WHERE (modbus_address IS NOT NULL)
   ```

**Expected result** — all 9 CHECK constraints present, none tautological; PK is
`(ts, signal_id, source)`; two sources may coexist at one instant; all 5 indexes present with
`signal_modbus_idx` partial.

**Pass/fail criteria**
- PASS: every one of the 3 negative inserts is rejected by the *named* constraint; PK is the
  triple; the dual-source insert yields 2 rows; no `OR true`.
- FAIL: any negative insert succeeds; PK is `(ts, signal_id)` only; a missing or
  non-partial `signal_modbus_idx`.
- Note: `tests/test_postgres_schema.py` also pins `signal` to an **exact 16-column set**. A 17th
  column is a FAIL, and adding `holder` would fail it.

**Evidence** — constraint catalog dump, the 3 rejection errors verbatim, PK column list, index
list.

**Risks / caveats**
- `signal_normal_in_range` and friends are only checked at INSERT/UPDATE. There is no
  statement-level cross-row validation, so a contract edit that produces an out-of-band normal
  will fail loudly at seed time — which is the desired behaviour, but means A2 failures often
  surface as A3 failures first.

---

## A4. Seed / ETL job correctness and volume

**Objective** — the seeder produces the exact pinned fixture, quickly, and only into the named database.

**Pre-conditions**
```bash
docker compose up -d db init-db
make db-up          # idempotent; polls up to 60 s
```

**Steps**
1. Seed and time it:
   ```bash
   time make seed | tee evidence/A4/seed.log
   ```
2. Read the seeder log for the throughput claim:
   ```bash
   grep -E 'rows|readings|/s|per second|second' evidence/A4/seed.log | tail -20
   ```
   Docs claim "about 4.3 million readings in about two minutes, ~29 000/second".
3. Confirm the exact row count:
   ```sql
   SELECT count(*) FROM reading;                 -- expect 4239284
   SELECT count(DISTINCT signal_id) FROM reading; -- expect 57
   SELECT DISTINCT source FROM reading;           -- expect: opcua, seed  (see caveat)
   ```
4. **The fingerprint.** This is the strongest single check in the SQL layer — four integer
   aggregates, chosen because float `sum` is not order-independent:
   ```sql
   SELECT count(*)                                AS rows,
          sum(round(value * 1000)::bigint)        AS value_milli_sum,
          sum(quality::bigint)                    AS quality_sum,
          sum(epoch FROM ts)::bigint)             AS epoch_sum
   FROM reading;
   ```
   **Expected, exactly:**
   | rows | value_milli_sum | quality_sum | epoch_sum |
   |---|---|---|---|
   | 4 239 284 | 5 625 358 748 306 | 38 | 7 589 771 859 816 991 |
5. Confirm the row counts of the aggregates match notebook 01's claims:
   ```sql
   SELECT 'reading' t, count(*) FROM reading
   UNION ALL SELECT 'reading_1m', count(*) FROM reading_1m
   UNION ALL SELECT 'reading_1h', count(*) FROM reading_1h
   UNION ALL SELECT 'signal', count(*) FROM signal
   UNION ALL SELECT 'equipment', count(*) FROM equipment
   UNION ALL SELECT 'site', count(*) FROM site
   UNION ALL SELECT 'event', count(*) FROM event
   ORDER BY 1;
   -- expect: event 0 | equipment 22 | reading 4239284 | reading_1h 5882
   --         reading_1m 185455 | signal 57 | site 1
   ```
6. Confirm the week is exactly 7 days and check the storm is present:
   ```sql
   SELECT min(ts), max(ts), max(ts) - min(ts) FROM reading;
   -- expect span = 7 days
   ```
7. Confirm the injected faults are actually in the data:
   ```sql
   -- sensor_dead is the ONLY source of value IS NULL with quality=2 in the project
   SELECT count(*) FROM reading WHERE value IS NULL AND quality = 2;
   -- expect >= 1 (notebook 09 ground truth: INFLUENT:LIFT:CURRENT
   --              2026-09-27 11:02 -> 12:02, 60 minutes)
   SELECT quality, count(*) FROM reading GROUP BY 1 ORDER BY 1;
   -- expect 0 (Good) to dominate, 1 and 2 present
   ```

**Expected result** — 4 239 284 rows, fingerprint matches to the digit, aggregates populated
(185 455 / 5 882), storm window present, at least one NULL-bad reading.

**Pass/fail criteria**
- PASS: all four fingerprint values match exactly; aggregate counts match; seed completes
  under ~5 minutes; `reading_1h` has ~5 882 rows (**not** 2 — see caveat).
- FAIL: any fingerprint digit differs; `reading_1h` near-empty; `source` contains neither
  `seed` nor `opcua`; `event` ≠ 0 (seeding should write no events).
- If the fingerprint matches but `reading_1m`/`reading_1h` are empty → the continuous
  aggregate refresh failed, not the seed. Go to A5.

**Evidence** — seed log with timing, fingerprint row, union counts, quality histogram.

**Risks / caveats**
- **Re-seeding an overlapping window fails loudly.** `COPY` cannot upsert, so a duplicate PK
  raises rather than silently overwriting. That is correct; do not "fix" it with `--reset`
  unless you intend to lose the fixture.
- **`--reset` without `--database` is refused** (`storage/seed/main.py:453-471`) — "the
  database lets you reset it", i.e. `POSTGRES_DB` in a shell must not be mistaken for an
  explicit instruction. Verify the refusal (A4b below).
- `SEED_DAYS` in `.env` and `--days` on the command line both control the window. The
  fingerprint above is valid for **7 days only** (`SEED_DAYS=7`, `--end` pinned).
- On a host where `.env` sets `POSTGRES_PORT=55433`, use `make seed` rather than
  `docker compose run` so the port override survives.

---

## A4b. Seeder target-safety and failure modes

**Objective** — the seeder cannot destroy the wrong database, and aborts on repeated failure.

**Pre-conditions** — A4 complete on `wwtp`.

**Steps**
1. Prove `--reset` is refused without an explicit database:
   ```bash
   docker compose run --rm seed --reset 2>&1 | tee evidence/A4b/reset-refused.txt
   ```
   Expect a refusal naming `--database`, exit non-zero, and **`reading` row count unchanged**.
2. Prove the explicit form works and only touches the named database:
   ```bash
   docker compose run --rm seed --database wwtp_scratch --days 1
   make query SQL="SELECT count(*) FROM reading"                  # 4239284, unchanged
   make query SQL="SELECT count(*) FROM reading" # inside wwtp_scratch
   ```
3. Prove a bad target fails before any write (the `-q` config check):
   ```bash
   docker compose --profile demo run --rm seed --database no_such_db 2>&1 | tee ...
   ```
   Expect a clear "database does not exist" error and no partial write.
4. Confirm 3 consecutive failed COPY batches abort. Point the seeder at a host that goes away
   mid-run:
   ```bash
   POSTGRES_PORT=5599 docker compose --profile demo run --rm seed --database wwtp_scratch --days 1 2>&1 | tail -20
   ```
   Expect `SystemExit`, not a hang. (Note: `POSTGRES_PORT` may be overridden by `.env` — see
   How-to-use §1. If so, edit `.env` temporarily instead.)

**Expected result** — 1: refusal; 2: isolation; 3: fail-before-write; 4: bounded abort.

**Pass/fail criteria**
- PASS: `wwtp` row count is **still 4 239 284** after every step; the seeder exits non-zero on
  all three error paths within a bounded time.
- FAIL: `wwtp` row count changes; a bad target produces a partial seed; the seeder hangs.

**Evidence** — refusal message, before/after `reading` counts, abort traceback.

**Risks / caveats**
- 19 tests exist for this (`tests/test_seed_target_database.py`, `tests/test_seed_schedule.py`).
  Run them too — this case covers the CLI path, they cover the code path:
  ```bash
  uv run pytest tests/test_seed_target_database.py tests/test_seed_schedule.py -q
  ```

---

## A5. Roles, grants and least privilege

**Objective** — the three group roles and the two login roles hold exactly the documented grants, and the withheld privileges really are withheld.

**Pre-conditions** — A1 complete (`init-db` applied roles).

**Steps**
1. Ask the database what it thinks:
   ```bash
   make roles | tee evidence/A5/roles.txt
   ```
2. Verify the three group roles exist and are `NOLOGIN`:
   ```sql
   SELECT rolname, rolcanlogin FROM pg_roles
   WHERE rolname IN ('wwtp_owner','wwtp_writer','wwtp_reader',
                     'wwtp_gateway','wwtp_ui')
   ORDER BY rolname;
   -- expect: wwtp_gateway=false? NO -- gateway is a LOGIN role.
   -- group roles wwtp_owner/wwtp_writer/wwtp_reader: rolcanlogin = false
   -- login roles wwtp_gateway/wwtp_ui: rolcanlogin = true
   ```
3. Verify grants *and* the withheld set. `storage/postgres/roles.py` keeps a
   `DELIBERATELY_WITHHELD` list — read it and assert each entry is genuinely absent:
   ```sql
   SELECT grantee, table_name, privilege_type
   FROM information_schema.role_table_grants
   WHERE grantee IN ('wwtp_writer','wwtp_reader')
   ORDER BY grantee, table_name, privilege_type;
   ```
   **Expected, exactly:**
   | grantee | table | privilege |
   |---|---|---|
   | `wwtp_writer` | `event` | INSERT, SELECT |
   | `wwtp_writer` | `equipment` | SELECT |
   | `wwtp_writer` | `reading` | INSERT, SELECT, UPDATE |
   | `wwtp_reader` | `equipment` | SELECT |
   | `wwtp_reader` | `event` | SELECT |
   | `wwtp_reader` | `reading` | SELECT |
   | `wwtp_reader` | `reading_1h` | SELECT |
   | `wwtp_reader` | `reading_1m` | SELECT |
   | `wwtp_reader` | `signal` | SELECT |
   | `wwtp_reader` | `site` | SELECT |

   **Must be absent:** any `DELETE` on `reading` or `event`; any `TRUNCATE`; any `UPDATE` on
   `signal`; any `INSERT` on `reading` for the reader.
4. Verify sequence usage (the writer needs `USAGE` on `event_id_seq` or every event insert fails):
   ```sql
   SELECT grantee, sequence_name, privilege_type
   FROM information_schema.role_usage_grants
   WHERE grantee = 'wwtp_writer';
   -- expect: event_id_seq / USAGE
   ```
5. **Prove the refusals with the real credentials**, not just by reading the catalog. This is
   the gateway's own compose healthcheck, so it runs every time:
   ```bash
   docker compose exec gateway python -c "
   import os, psycopg
   with psycopg.connect(host='db', dbname='wwtp', user='wwtp_gateway',
                        password=os.environ['POSTGRES_PASSWORD']) as c:
       print('signals:', c.execute('SELECT count(*) FROM signal').fetchone()[0])
       try:
           c.execute('TRUNCATE reading'); print('LEAK: gateway could TRUNCATE')
       except psycopg.errors.InsufficientPrivilege:
           print('ok: gateway cannot erase history')
   " | tee evidence/A5/gateway-refusal.txt
   ```
   Expect `signals: 57` and `ok: gateway cannot erase history`.
6. Prove the writer role *can* write, and that its `UPDATE` is needed (it backs an upsert):
   ```bash
   docker compose run --rm --entrypoint sh gateway -c "true"  # no-op, confirms entry
   ```
   Instead use the gateway smoke path with a short run:
   ```bash
   uv run python -m gateway.main --iterations 10 --no-postgres   # spool-only, no DB
   ```
7. Prove `wwtp_ui` (the web dashboard role) is read-only across all four verbs:
   ```bash
   docker compose --profile ui run --rm --entrypoint sh web -c "true"
   # or, from psql with WEB_DB_PASSWORD from .env:
   # for v in INSERT UPDATE DELETE TRUNCATE; do ... done — all four must raise
   ```
   Concretely, in `psql` as `wwtp_ui`:
   ```sql
   DELETE FROM reading;      -- must ERROR: permission denied
   TRUNCATE reading;         -- must ERROR: permission denied
   UPDATE reading SET value=0; -- must ERROR: permission denied
   INSERT INTO event (kind,severity,message) VALUES ('x','info','x'); -- must ERROR
   SELECT count(*) FROM reading;  -- must SUCCEED
   ```
8. Verify roles **are not** in `schema.sql` (they are cluster state, not schema state, and are
   not restored by `pg_dump --schema-only`):
   ```bash
   grep -inE 'create role|grant ' storage/postgres/schema.sql   # expect no output
   grep -inE 'create role|grant ' storage/postgres/roles.py | head
   ```
9. Run the gate:
   ```bash
   uv run python -m storage.postgres.roles --check | tee evidence/A5/roles-check.txt
   ```

**Expected result** — the 9-row grant table exactly; sequence usage present; gateway can read the contract and is refused `TRUNCATE`; `wwtp_ui` is refused all four write verbs; `schema.sql` contains no DDL for roles.

**Pass/fail criteria**
- PASS: grants match the table exactly (no extras, no omissions); all refusal paths raise
  `InsufficientPrivilege`; `roles --check` exits 0.
- FAIL: any withheld privilege is present; the gateway healthcheck is not running (it is
  compose's liveness signal — if it is absent, the gateway container is unhealthy and every
  later test is suspect); `schema.sql` contains role DDL.

**Evidence** — `make roles` output, the role-table-grants dump, gateway refusal transcript,
`wwtp_ui` four-verb transcript, `roles --check` exit code.

**Risks / caveats**
- **The gateway's refusal check has bitten before.** `compose.yaml` records a fix for an
  "unreadable condition and a stderr redirect that made a real connection failure look like a
  pass". If the healthcheck ever prints success while the database is unreachable, that is a
  **P0 finding** — log it as such, do not work around it.
- `wwtp_owner` is a group role; the *login* user is `POSTGRES_USER` (default `wwtp`). Do not
  confuse the two in your evidence.

---

## A6. Retention policies and continuous aggregates

**Objective** — raw/1m/1h retention matches configuration, and the aggregates are actually populated and refreshable.

**Pre-conditions** — A4 complete.

**Steps**
1. Read the configured retention. `RETENTION_RAW_DAYS` and `RETENTION_MINUTE_DAYS` come from `.env`:
   ```bash
   grep -E 'RETENTION_(RAW|MINUTE)_DAYS' .env
   # expect RETENTION_RAW_DAYS=7, RETENTION_MINUTE_DAYS=90
   ```
2. Confirm the intervals are **not** hardcoded in SQL — they are bound parameters:
   ```bash
   grep -c 'add_retention_policy' storage/postgres/schema.sql   # expect 0
   grep -n 'add_retention_policy' storage/postgres/schema.py     # expect present, with INTERVAL '1 day' * %s
   ```
3. Confirm the policies exist with the right intervals:
   ```sql
   SELECT view_name, schedule_interval FROM timescaledb_information.jobs
   WHERE proc_name LIKE '%retention%' ORDER BY view_name;
   ```
   Adjust to the configured values: raw ≈ `1 day`, 1m ≈ `1 day` (90-day span expressed as a
   multiple), 1h should have **no** retention job (indefinite).
4. Confirm the aggregate refresh policies:
   ```sql
   SELECT view_name, schedule_interval, config
   FROM timescaledb_information.jobs
   WHERE proc_name LIKE '%policy%' AND view_name LIKE 'reading%';
   ```
   Expect two rows (one per aggregate) with sane `start_offset` / `end_offset`.
5. **Prove `materialized_only` is on and the tables are populated.** This is the exact bug
   class that once produced a 2-row `reading_1h` from 4.29 M readings:
   ```sql
   SELECT view_name, materialized_only
   FROM timescaledb_information.continuous_aggregates ORDER BY view_name;
   -- expect: reading_1h true, reading_1m true
   ```
   ```sql
   SELECT count(*) FROM reading_1m;   -- ~185455
   SELECT count(*) FROM reading_1h;   -- ~5882, NOT 2
   ```
6. **Prove both aggregates roll up from raw, not from each other.** Inspect the definitions:
   ```sql
   SELECT view_name, view_definition FROM timescaledb_information.continuous_aggregates
   WHERE view_name IN ('reading_1m','reading_1h');
   ```
   Both must read from `reading`. A `reading_1h` defined over `reading_1m` is a **FAIL** — it
   averages means over unequal counts wherever deadband left holes.
7. **Prove both use `count(value)`, not `count(*)`.** A NULL value is a row without a
   measurement, and `count(*)` would report it as a sample:
   ```bash
   make query SQL="SELECT view_definition FROM timescaledb_information.continuous_aggregates WHERE view_name='reading_1h'"
   ```
   Expect `count(value)` in the definition.
8. Force a manual refresh and prove it is idempotent and bounded:
   ```sql
   CALL refresh_continuous_aggregate('reading_1h', now() - interval '7 days', now());
   -- expect: completes, no rows returned, no error
   SELECT count(*) FROM reading_1h;   -- ~5882, unchanged
   ```
   Note: `CALL` **cannot** take bind parameters (`IndeterminateDatatype`) — that is why
   `schema.py:394-399` uses `psycopg.sql.SQL(...).format(Literal(...))`. Not a defect; do not
   report it as one.
9. Confirm `n` in the aggregates is non-zero (proves the columns are populated):
   ```sql
   SELECT count(*) FILTER (WHERE n = 0) AS zero_n, count(*) AS total FROM reading_1h;
   -- expect zero_n = 0
   ```

**Expected result** — retention 7d raw / 90d 1m / none for 1h; refresh policies present; both aggregates `materialized_only = true`, both defined over `reading`, both using `count(value)`; ~185 455 and ~5 882 rows; a manual refresh changes nothing.

**Pass/fail criteria**
- PASS: as above. Specifically FAIL if `reading_1h` < 5 000 rows, if either aggregate is defined over the other, if `materialized_only` is false, or if `n = 0` rows exist.
- FAIL if `add_retention_policy` appears in `schema.sql`.

**Evidence** — `.env` grep, job catalog dump, continuous-aggregate definitions, row counts before/after manual refresh.

**Risks / caveats**
- Retention job scheduling can be slow to fire. Do not wait 7 days to test it; the *declaration*
  plus the manual `CALL` above is the correct assertion. If you need to test actual deletion,
  clone with `--days 12` into a scratch DB and set raw retention to 2 days.
- `add_retention_policy` with `if_not_exists => TRUE` means re-running is safe; there is also a
  `remove_retention_policy(..., if_exists => TRUE)` path — exercised in the same code, not
  separately tested here.

---

## A7. Data quality checks

**Objective** — the seeded week satisfies every invariant the project claims, and violations are detectable.

**Pre-conditions** — A4 complete.

**Steps**
1. **Nulls.** `value` is nullable by design; a NULL must always carry non-Good quality:
   ```sql
   SELECT count(*) AS null_with_good_quality
   FROM reading WHERE value IS NULL AND quality = 0;
   -- expect 0 (this is the CHECK constraint restated as a data query)
   ```
   ```sql
   SELECT count(*) AS total, count(value) AS with_value,
          count(*) FILTER (WHERE quality <> 0) AS non_good
   FROM reading;
   ```
   Cross-check: `non_good` must be small (fingerprint says `sum(quality) = 38`, so the vast
   majority are 0 with a handful at 1 or 2).
2. **Uniqueness.** The PK guarantees it, but prove it against the whole table:
   ```sql
   SELECT count(*) FROM (
     SELECT ts, signal_id, source FROM reading
     GROUP BY 1,2,3 HAVING count(*) > 1
   ) dupes;
   -- expect 0
   ```
3. **Referential integrity.** Signals in `reading` with no `signal` row, and assets with no `site`:
   ```sql
   SELECT count(*) FROM reading r LEFT JOIN signal s ON s.id = r.signal_id
   WHERE s.id IS NULL;                          -- expect 0
   SELECT count(*) FROM equipment e LEFT JOIN site t ON t.id = e.site_id
   WHERE t.id IS NULL;                          -- expect 0
   SELECT count(*) FROM signal s WHERE s.equipment_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM equipment e WHERE e.id = s.equipment_id);  -- expect 0
   ```
4. **Range conformance.** Every reading should sit inside its signal's engineering range,
   *except* deliberately injected faults. This is a heuristic, so report the outliers rather
   than asserting zero:
   ```sql
   SELECT s.id, s.unit, count(*) AS n,
          min(r.value) AS min_v, max(r.value) AS max_v,
          s.range_min, s.range_max
   FROM reading r JOIN signal s ON s.id = r.signal_id
   WHERE r.value IS NOT NULL
     AND (r.value < s.range_min OR r.value > s.range_max)
   GROUP BY 1,2,5,6 ORDER BY n DESC LIMIT 20;
   ```
   Expect a **small** number of rows, each explainable as an armed instrument fault
   (`bad_instrument`, `sensor_stuck_high`, `do_sensor_drift`). A large count means the contract
   range or the process model is wrong. Cross-reference `contracts/fault-scenarios.yaml`.
5. **The word-order signature — the highest-value quality check in this plan.** A float read
   low-word-first decodes to ~2.3e-41 and passes every range check. Query for it:
   ```sql
   SELECT signal_id, count(*) AS word_order_bugs, min(value), max(value)
   FROM reading
   WHERE value IS NOT NULL AND value <> 0 AND abs(value) < 1e-30
   GROUP BY 1 ORDER BY 2 DESC;
   -- expect 0 rows. Any row here is a live word-order bug.
   ```
   The two `little`-word-order signals are `AERATION_BLOWER_VALVE` @40108 and
   `AERATION_WASTE_RATE` @40304. If either shows up here, the gateway is misreading it.
6. **Quality distribution per signal**, to spot a signal that is mostly degraded:
   ```sql
   SELECT signal_id, quality, count(*) FROM reading
   GROUP BY 1,2 HAVING count(*) > 100 ORDER BY 3 DESC LIMIT 20;
   ```
   Expect few or no rows.
7. **Gap behaviour.** Deadband means irregular sampling, so gaps are expected — but a gap of a
   whole hour for a *fast* signal is worth knowing about:
   ```sql
   WITH b AS (
     SELECT signal_id, time_bucket('1 hour', ts) AS h, count(*) AS n
     FROM reading GROUP BY 1,2
   )
   SELECT signal_id, count(*) FILTER (WHERE n = 0) AS empty_hours, count(*) AS hours
   FROM generate_series(DISTINCT signal_id) s(signal_id)
   CROSS JOIN generate_series(
     date_trunc('hour', (SELECT min(ts) FROM reading)),
     date_trunc('hour', (SELECT max(ts) FROM reading)), '1 hour') g
   LEFT JOIN b ON b.signal_id = s.signal_id AND b.h = g
   GROUP BY 1 ORDER BY 2 DESC LIMIT 15;
   -- sanity, not a pass/fail: expect empty_hours > 0 for many signals (deadband)
   ```
8. **The 13-quiet-signals invariant.** 13 of 57 signals produce exactly one reading in a seeded
   week (deadband never trips). This is *expected*, and Grafana has a panel for it:
   ```sql
   SELECT count(*) FROM (
     SELECT signal_id FROM reading WHERE source = 'seed'
     GROUP BY signal_id HAVING count(*) = 1
   ) q;
   -- expect 13
   ```
9. **Event table is empty after seeding:**
   ```sql
   SELECT count(*) FROM event;   -- expect 0
   SELECT kind, severity, count(*) FROM event GROUP BY 1,2;  -- expect no rows
   ```

**Expected result** — 0 nulls-with-Good, 0 duplicates, 0 FK orphans, 0 word-order signatures,
0 rows with `n=0`, `event` = 0, 13 single-reading signals.

**Pass/fail criteria**
- **Hard FAIL:** any duplicate PK; any NULL with quality 0; any FK orphan; **any row with
  `abs(value) < 1e-30`**; `event` non-empty after a pure seed.
- **Soft FAIL (investigate):** range outliers not attributable to `fault-scenarios.yaml`;
  the 13-signal count differing from 13 (it is a *derived* number — if it differs, either
  deadband config or the contract changed, and A2/A4 should be re-run).
- Everything else is informational.

**Evidence** — each query with `\pset format aligned`, plus the word-order query result
explicitly marked PASS-with-0-rows.

**Risks / caveats**
- Check 5 is the one to run first. It is cheap and it finds the bug class the project is
  actually about.
- `bad_instrument` deliberately does **not** damage the process — it publishes `Uncertain` and
  the basin stays healthy. So "instrument fault visible in data" and "plant degraded" are
  different things; do not conflate them in your verdict.
- Check 7 is a cross join that can be slow on some hosts. If it takes > 60 s, rewrite it with
  a lateral or skip it — it is informational.

---

## A8. Core queries — the SQL course gate

**Objective** — every hand-written query in the course executes, returns rows, and is deterministic.

**Pre-conditions** — A4 complete **and the gateway stopped**, because lessons anchor to
`(SELECT max(ts) FROM reading)`:
```bash
make db-still
```

**Steps**
1. Run the gate:
   ```bash
   make sql 2>&1 | tee evidence/A8/sql-course.log
   ```
   Docs claim **78 queries across 26 files**. `tools/check_sql.py` executes each fenced
   ```sql``` block, `--runs 3` by default.
2. Confirm the query/file counts match the docs:
   ```bash
   grep -cE '^```sql' sql/*/*.md | awk -F: '{s+=$2} END {print s}'   # ~117 fenced blocks
   tail -5 evidence/A8/sql-course.log                                 # expect the query/file summary
   ```
3. Understand the two failure modes it checks, then confirm neither fired:
   - **Zero rows** fails. That is how an unanchored lesson query is caught.
   - **Differing row counts between the 3 runs** fails. Queries containing a volatile marker
     (e.g. `now()`) are reported as `volatile` and exempted — confirm the exemption list is
     only what you expect.
   ```bash
   grep -iE 'volatile|zero rows|mismatch|error' evidence/A8/sql-course.log
   ```
4. Run the extracted-query corpus too (`sql/TablePlus/`, 64 generated files):
   ```bash
   uv run python tools/check_sql.py sql/ --verbose 2>&1 | tail -30
   ```
5. Verify the extraction is not stale:
   ```bash
   make tableplus-check    # expect: no drift
   ```
6. Spot-run the heaviest-looking lesson by hand to build confidence in the gate. The
   dashboard query is stage 04:
   ```bash
   make query SQL="$(awk '/^```sql/{f=1;next}/^```/{f=0}f' sql/04-expert/04-04_the_dashboard_query.md | head -30)"
   ```

**Expected result** — all 78 queries execute 3× with identical row counts each time; zero errors; zero zero-row results; `tableplus-check` reports no drift.

**Pass/fail criteria**
- PASS: gate exits 0; query count ≥ 78; no unexempted volatility; no drift.
- FAIL: any query errors; any zero-row result; any count instability; `tableplus-check` drift.

**Evidence** — the gate log with its summary line, the volatile list, `tableplus-check` output.

**Risks / caveats**
- **The gateway must be stopped.** If you see sporadic failures, check `docker compose ps gateway`.
- Stage 04 is documented as finding "two real defects in this repository". If your run is clean,
  verify you are actually on a commit where those were fixed, and read what they were before
  assuming the gate is weak.
- `--allow-empty` exists for queries that legitimately return no rows. Do not use it to make
  the gate pass.

---

## A9. Performance on representative heavy queries

**Objective** — the dashboard query, the aggregates, and the retention-scheduled refreshes are fast enough for the 30 s dashboard refresh.

**Pre-conditions** — A4 complete (4.29 M rows).

**Steps**
1. Establish the environment so results are comparable:
   ```sql
   SELECT version();
   SELECT extname, extversion FROM pg_extension;
   -- expect: postgresql 16.x, timescaledb 2.30.1
   ```
   ```bash
   docker stats --no-stream wwtp-iiot-db-1
   ```
2. **The dashboard query** — this is the one behind the 30 s Grafana refresh and the web
   sparklines. Time it as the dashboard actually runs it, with `$__timeFrom`/`$__timeTo`
   substituted for a 6 h window:
   ```sql
   EXPLAIN (ANALYZE, BUFFERS)
   SELECT time_bucket(INTERVAL '5 seconds', ts) AS time,
          (array_agg(value ORDER BY ts DESC))[1] AS value,
          max(quality) AS quality, count(*) AS samples
   FROM reading
   WHERE signal_id = 'AERATION:AHU-1:DO'
     AND ts >= now() - interval '6 hours' AND ts <= now()
   GROUP BY time ORDER BY time;
   ```
   **Expect:** single scan using `reading_signal_time_idx`, execution time well under 1 s.
   Record the actual time; it is your baseline.
3. **The 30-day permit query** (5-way `UNION ALL` over `reading_1h`, behind
   `wwtp-permit`):
   ```sql
   EXPLAIN (ANALYZE, BUFFERS)
   WITH span AS (SELECT now() - interval '30 days' AS since)
   SELECT 'nh4_30d_mean' AS parameter, avg(h.mean) AS value
   FROM reading_1h, span WHERE signal_id='EFFLUENT:FLOW:NH4' AND bucket >= span.since
   UNION ALL
   SELECT 'tss_30d_mean', avg(h.mean)
   FROM reading_1h, span WHERE signal_id='EFFLUENT:FLOW:TSS' AND bucket >= span.since
   UNION ALL
   SELECT 'ph_min', min(h.min) FROM reading_1h, span
   WHERE signal_id='EFFLUENT:FLOW:PH' AND bucket >= span.since
   UNION ALL
   SELECT 'ph_max', max(h.max) FROM reading_1h, span
   WHERE signal_id='EFFLUENT:FLOW:PH' AND bucket >= span.since
   UNION ALL
   SELECT 'bacti_geomean', exp(avg(ln(h.mean))) FROM reading_1h, span
   WHERE signal_id='EFFLUENT:DIS-CL-2:BACTI' AND bucket >= span.since;
   ```
   **Expect:** all five branches served by the aggregate, total under ~2 s.
   Record actual.
4. **A query that must not scan raw.** If any of the five above touches the `reading`
   hypertable directly, the aggregate is not covering the dashboard and the 30 s refresh will
   not hold. That is a **FAIL**.
5. **The "what has stopped reporting" table** (6-panel table on `wwtp-overview`):
   ```sql
   EXPLAIN (ANALYZE, BUFFERS)
   SELECT s.id, s.area, s.unit, max(r.ts) AS last_reading,
          now() - max(r.ts) AS silence
   FROM signal s LEFT JOIN reading r ON r.signal_id = s.id
   GROUP BY s.id, s.area, s.unit
   HAVING max(r.ts) IS NULL OR now() - max(r.ts) > interval '1 hour'
   ORDER BY silence DESC NULLS FIRST LIMIT 30;
   ```
   This touches all 4.29 M rows. **Expect:** under ~10 s on a laptop, and note it is the
   slowest panel on the dashboard. If it exceeds the 30 s refresh interval, that is a
   legitimate finding to raise (not a defect you should work around).
6. **Aggregate refresh cost** — what the scheduled policy actually pays:
   ```sql
   EXPLAIN (ANALYZE) SELECT count(*) FROM reading_1h WHERE bucket > now() - interval '1 day';
   ```
7. **Time-weighted average** (stage 04 lesson 1) — the heaviest teaching query. Extract and
   time it:
   ```bash
   make query SQL="$(awk '/^```sql/{f=1;next}/^```/{f=0}f' sql/04-expert/04-01_time_weighted_averages.md)"
   ```
8. **Storage footprint**, for the record:
   ```sql
   SELECT hypertable_name, pg_size_pretty(hypertable_size('reading')) FROM timescaledb_information.hypertables;
   ```
   Docs warn these byte counts are machine-specific (there is an automated rule,
   `check_portable_numbers`, that forbids asserting on them — so record, do not assert).
9. Confirm the **web dashboard's** sparkline query shape is also indexed:
   ```sql
   EXPLAIN (ANALYZE)
   SELECT gs.bucket, a.mean, a.min, a.max
   FROM generate_series(date_trunc('minute', now() - interval '6 hours'),
                        date_trunc('minute', now()), '1 minute') gs(bucket)
   LEFT JOIN reading_1m a ON a.bucket = gs.bucket AND a.signal_id = 'AERATION:AHU-1:DO'
   ORDER BY gs.bucket;
   -- expect under 1 s
   ```

**Expected result** — dashboard query < 1 s; permit query < 2 s, entirely off the hypertable;
"stopped reporting" < 10 s; all queries served by the expected indexes.

**Pass/fail criteria**
- PASS: every query completes under its budget; no panel query scans raw `reading` when an
  aggregate should serve it; no sequential scan on the hypertable for a single-signal query.
- FAIL: permit query touches `reading`; any single-signal query plans as a seq scan; any query
  exceeds the 30 s dashboard refresh; the "stopped reporting" query exceeds ~30 s.
- Record absolute times even on PASS — the baseline is what makes a regression visible.

**Evidence** — `EXPLAIN (ANALYZE, BUFFERS)` output per query, `docker stats`, and a summary
table of measured-vs-budget.

**Risks / caveats**
- Absolute timings depend on hardware. Report hardware alongside any FAIL.
- `pg_size_pretty` / `hypertable_size` are explicitly excluded from portable assertions by
  `tools/check_notebooks.py::check_portable_numbers`. Do not fail a build on a byte count.
- The advisory-lock contention between the scheduled refresh policy and a manual `CALL` is
  handled by `REFRESH_LOCK_ATTEMPTS = 10` with linear backoff. If you run A6 and A9
  concurrently, expect retries in the log — not a bug.

---

# B. Grafana, Node-RED and the Web Dashboard

## B0. Area-level notes

Three separate operator surfaces, all read-only over the same database, each with a different
trust posture:

| Surface | URL | DB identity | Can write? |
|---|---|---|---|
| Grafana | `http://127.0.0.1:${GRAFANA_PORT:-3000}` | **owner** (`wwtp`) | yes (Grafana is not read-only) |
| Node-RED | `http://127.0.0.1:${SCADA_PORT:-18880}/scada` | **owner** (`wwtp`) | yes (can `DELETE`) |
| Web dashboard | `http://127.0.0.1:${WEB_PORT:-3001}` | `wwtp_ui` / `wwtp_reader` | no |

**Both Grafana and Node-RED authenticate as the database owner.** This is a documented open
thread (`compose.yaml:402-410`, `scada/README.md:118-126`, `docs/SECURITY.md` gap 3), not an
oversight to be discovered by QA. Your job is to *verify the gap is as described* (B1.4, B3.7),
not to close it.

**Grafana has no alerting.** No alert rules, no contact points, no notification policies, no
silences, no mute timings, no `ui/grafana/provisioning/alerting/` directory. Panel threshold
bands are `fieldConfig.defaults.thresholds` steps on the same axis — they are *not* alarms.
`ui/grafana/generate_dashboards.py:18-34` says so explicitly. B5 verifies this absence and
verifies the alarm system that *does* exist (`alarms/`) works. Do not write B5 as "test Grafana
alert rules"; that would be testing something the project deliberately does not have.

---

## B1. Grafana datasource provisioning and connectivity

**Objective** — the datasource is provisioned from environment variables, connects, and executes both query shapes.

**Pre-conditions**
```bash
make up
make grafana          # docker compose --profile observability up -d grafana
```

**Steps**
1. Confirm the container is up and the port binds to loopback only:
   ```bash
   docker compose --profile observability ps grafana
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:${GRAFANA_PORT:-3000}/login   # expect 200
   ```
2. Read the provisioning file and check **every** value comes from an env var:
   ```bash
   cat ui/grafana/provisioning/datasources/postgres.yaml | tee evidence/B1/datasource.yaml
   ```
   Expect: `name: TimescaleDB`, `uid: wwtp-postgres`, `type: postgres` (built-in, **no
   plugin**), `access: proxy`, `url: ${POSTGRES_HOST}` → `db:5432`, `database: ${POSTGRES_DB}`
   → `wwtp`, `user: ${POSTGRES_USER}` → `wwtp`, `secureJsonData.password:
   ${POSTGRES_PASSWORD}`, `jsonData.sslmode: disable`, `jsonData.postgresVersion: 1600`,
   `jsonData.timescaledb: true`, `jsonData.maxOpenConns: 10`,
   `postgresExtraOptions: ["application_name=grafana"]`, `isDefault: true`, `editable: false`.
3. Verify the healthcheck endpoint — the one a QA engineer should actually use:
   ```bash
   docker compose exec grafana curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
     'http://localhost:3000/api/datasources/uid/wwtp-postgres/health' | python3 -m json.tool
   ```
   Expect `"status": "OK"` and `"message": "Database Connection OK"`.
4. Prove the datasource is the only one and is marked default:
   ```bash
   docker compose exec grafana curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
     'http://localhost:3000/api/datasources' | python3 -m json.tool | grep -E '"(id|uid|name|type|isDefault|url)"'
   ```
   Expect exactly one entry, `isDefault: true`.
5. **Check for provisioned-but-broken.** Grafana prints `starting to provision dashboards` and
   `finished to provision dashboards` *whether or not it inserted anything* — per-file insert
   lines are `level=debug` and off by default. So log greps alone prove nothing:
   ```bash
   docker compose exec grafana curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
     'http://localhost:3000/api/search?type=dash-db' | python3 -m json.tool
   ```
   Expect exactly two dashboards: `WWTP — overview` and `WWTP — discharge permit`.
6. Turn on debug logging to see per-file inserts (temporary):
   ```bash
   # add to .env: GF_LOG_LEVEL=debug, then
   docker compose --profile observability up -d --force-recreate grafana
   docker compose logs grafana | grep -i 'inserting dashboard'   # expect 2 lines
   # then remove GF_LOG_LEVEL and recreate
   ```
7. Confirm the mounts are read-only and the provisioning dir is the source:
   ```bash
   docker compose --profile observability config | grep -A6 'grafana:'
   ```
   Expect three mounts: `./ui/grafana/provisioning:/etc/grafana/provisioning:ro`,
   `./ui/grafana/dashboards:/var/lib/grafana/dashboards:ro`, `grafana-data:/var/lib/grafana`.
8. If the admin password is wrong/stale, recover it and record that you had to:
   ```bash
   docker compose exec grafana grafana cli admin reset-admin-password admin
   ```
9. Verify sign-up is disabled (this is a hardening item with a test behind it):
   ```bash
   docker compose --profile observability config | grep GF_USERS_ALLOW_SIGN_UP   # expect "false"
   docker compose --profile observability config | grep GF_INSTALL_PLUGINS      # expect "" (empty)
   ```

**Expected result** — one datasource `wwtp-postgres` health `OK`; exactly two dashboards in the API; three read-only mounts; sign-up off; no plugins.

**Pass/fail criteria**
- PASS: health `OK`; `/api/search?type=dash-db` returns exactly 2 entries with the expected
  titles; mounts `:ro`; `GF_USERS_ALLOW_SIGN_UP: "false"`.
- FAIL: health not OK; 0 or >2 dashboards; a writable mount; sign-up enabled.
- **Special case:** if the dashboard count is 0 while the log says provisioning finished, that
  is a **FAIL with high diagnostic value** — it means the dashboard JSON failed to parse or the
  mount is empty. Capture `docker compose logs grafana` at debug level.

**Evidence** — datasource YAML, health JSON, `/api/search` JSON, rendered compose config.

**Risks / caveats**
- `${GRAFANA_ADMIN_PASSWORD:?...}` is **required**. With no `.env`, compose refuses to
  interpolate and Grafana never starts. Set it or expect a compose error naming the variable.
- The datasource connects as **owner**, so a datasource test passing does *not* prove
  least-privilege. That is B1.4/F1.
- If `.env` sets a non-default `GRAFANA_PORT` (this host uses 3002), use the variable, not 3000.

---

## B2. Dashboard existence, identity and drift

**Objective** — both dashboards are generated from the contract, are up to date, and appear in the `WWTP` folder.

**Pre-conditions** — B1.

**Steps**
1. Confirm the two files exist and are byte-identical to a fresh generation:
   ```bash
   ls -la ui/grafana/dashboards/
   make dashboards-check 2>&1 | tee evidence/B2/dashboards-check.log
   ```
   Expect **no drift**. `--check` is the drift gate; `make dashboards` regenerates.
2. If drift is reported, inspect the diff before regenerating — a drift here means someone
   hand-edited a generated file, which is a **finding**:
   ```bash
   uv run python -m ui.grafana.generate_dashboards   # regenerates
   git diff --stat ui/grafana/dashboards/            # shows what the hand-edit was
   ```
3. Verify identity fields in the JSON:
   ```bash
   python3 -c "
   import json,glob
   for f in sorted(glob.glob('ui/grafana/dashboards/*.json')):
       d=json.load(open(f))
       print(f.split('/')[-1], '|', d['title'], '|', d['uid'], '|',
             d['schemaVersion'], '| refresh', d['refresh'], '|',
             d['time']['from'], '->', d['time']['to'], '|',
             d['timezone'], '| editable', d['editable'], '| tags', d['tags'],
             '| panels', len(d['panels']), '| tooltips', d['graphTooltip'],
             '| templating', d['templating']['list'],
             '| annotations', d['annotations']['list'])
   "
   ```
   **Expect exactly:**
   | file | title | uid | schemaVersion | refresh | range | timezone | editable | tags | panels |
   |---|---|---|---|---|---|---|---|---|---|
   | `wwtp-overview.json` | `WWTP — overview` | `wwtp-overview` | 39 | `30s` | `now-6h`→`now` | `utc` | `false` | `["wwtp","generated"]` | 6 |
   | `wwtp-permit.json` | `WWTP — discharge permit` | `wwtp-permit` | 39 | `30s` | `now-30d`→`now` | `utc` | `false` | `["wwtp","generated"]` | 3 |

   Note the titles use an **em dash** (U+2014) and both uids equal their filename.
4. Confirm both appear in the `WWTP` folder in the UI (port 3000 → folder **WWTP** → two
   items). Screenshot.
5. Confirm URLs resolve by uid (slug == uid here):
   - `http://127.0.0.1:3000/d/wwtp-overview/wwtp-overview`
   - `http://127.0.0.1:3000/d/wwtp-permit/wwtp-permit`
   Both must render, not 404.
6. Verify the dashboards are read-only in the UI: no edit (pencil) affordance should be
   available, because `editable: false` and `allowUiUpdates: false`. Screenshot the absence.
7. **Doc-vs-reality check.** `docs/VERIFYING.md:598` tells the reader to expect dashboards named
   **"Plant overview"** and **"Discharge permit"**. The actual titles are **`WWTP — overview`**
   and **`WWTP — discharge permit`**. Verify the actual, and log the doc as **stale** (see F4):
   ```bash
   grep -n 'Plant overview\|Discharge permit' docs/VERIFYING.md
   ```

**Expected result** — no drift; exactly the two rows in the table; both render in the `WWTP` folder; read-only.

**Pass/fail criteria**
- PASS: `dashboards-check` clean; identity fields match the table exactly; both URLs render;
  titles are `WWTP — …`.
- FAIL: any drift; wrong schemaVersion/refresh/timezone; more or fewer than 6/3 panels;
  an editable pencil affordance.
- Record separately: **FINDING** — `docs/VERIFYING.md:598` names the wrong dashboard titles.

**Evidence** — drift log, the identity-field dump, screenshots of the folder and both dashboards, the VERIFYING.md grep.

**Risks / caveats**
- `em dash` vs `hyphen`: if you grep for `"WWTP - overview"` you will not find it. Copy from
  the JSON, do not retype.
- `dashboards-check` needs no database, so it is safe to run before `make up`.

---

## B3. Panel-level correctness — `WWTP — overview`

**Objective** — each of the 6 panels queries the right signals, returns data, and renders the contract band correctly.

**Pre-conditions** — B1, B2, plus a seeded database (A4) and the gateway running so there is
recent data (`make up`).

**Steps**
1. Confirm live data is arriving (else panels will be empty and you cannot tell a bug from a
   quiet database):
   ```sql
   SELECT max(ts) AS newest, now() - max(ts) AS age FROM reading;
   -- age should be seconds, not hours
   ```
2. Open `http://127.0.0.1:3000/d/wwtp-overview/wwtp-overview`, set time range to **Last 6 hours**.
   Then work panel by panel against this expected map:

   | # | Panel title | Type | Signals | Unit | Grid (x,y,w,h) | Band? |
   |---|---|---|---|---|---|---|
   | 1 | `Influent and effluent flow` | timeseries | `INFLUENT:FLOW:FLOW`, `EFFLUENT:FLOW:FLOW` | `m3/h` | 0,0,8,8 | no |
   | 2 | `Dissolved oxygen` | timeseries | `AERATION:AHU-1:DO` | `mg/L` | 8,0,8,8 | **yes** (1.5–3.0) |
   | 3 | `Blower speed and air flow` | timeseries | `AERATION:AHU-1:BLOWER_RPM`, `AERATION:AHU-1:AIR_FLOW` | none | 16,0,8,8 | no |
   | 4 | `Effluent quality` | timeseries | `EFFLUENT:FLOW:NH4`, `EFFLUENT:FLOW:TSS` | none | 0,8,12,8 | no, **`rollup=true`** |
   | 5 | `Sludge and digester` | timeseries | `SECONDARY:SEC-CL-1:BLANKET`, `SLUDGE:DIG-1:PH` | none | 12,8,12,8 | no |
   | 6 | `What has stopped reporting` | **table** | (see SQL below) | — | 0,16,24,10 | — |

   Band is drawn **only** when `show_band` and there is exactly one signal on the panel — hence
   only panel 2. Panels 3 and 5 carry two signals of *different* units on one axis, so no band
   (and no unit) is drawn. That is correct behaviour; do not report it as missing.

3. For each panel, compare the rendered series against the database. Panel 1 first:
   ```sql
   SELECT round(avg(value)::numeric,1) AS mean_m3h, count(*) AS n
   FROM reading WHERE signal_id='INFLUENT:FLOW:FLOW'
     AND ts >= now() - interval '6 hours' AND value IS NOT NULL;
   ```
   Expect influent mean in the ballpark of **~1798 m³/h** (notebook 01's measured figure) with
   thousands of rows. The panel's value at any point must match the DB at that timestamp.
4. Panel 2 — verify the band renders at exactly the contract's `normal_low`/`normal_high`:
   ```sql
   SELECT normal_low, normal_high, unit FROM signal WHERE id='AERATION:AHU-1:DO';
   -- expect 1.5 | 3.0 | mg/L
   ```
   Confirm the green band in the screenshot sits between 1.5 and 3.0 and that the DO line sits
   **inside** it during healthy operation. Notebook 01 measured DO median **2.18 mg/L**.
5. Panel 4 — verify it uses the **hourly tier**, not raw:
   ```bash
   python3 -c "
   import json; d=json.load(open('ui/grafana/dashboards/wwtp-overview.json'))
   for p in d['panels']:
       if p['title']=='Effluent quality':
           for t in p['targets']: print(t['refId'], t['rawQuery']); print(t['rawSql'])
   "
   # expect rawSql to read FROM reading_1h and project bucket AS time, mean AS value
   ```
   Then confirm the panel is *smooth* at 6 h zoom (hourly resolution) while panel 1 is dense
   (5 s buckets). If panel 4 looks identical in resolution to panel 1, the rollup wiring is
   broken.
6. Panel 6 — run its exact SQL and confirm the table matches:
   ```bash
   make query SQL="SELECT s.id, s.area, s.unit, max(r.ts) AS last_reading,
                          now() - max(r.ts) AS silence
                   FROM signal s LEFT JOIN reading r ON r.signal_id = s.id
                   GROUP BY s.id, s.area, s.unit
                   HAVING max(r.ts) IS NULL OR now() - max(r.ts) > interval '1 hour'
                   ORDER BY silence DESC NULLS FIRST LIMIT 30"
   ```
   Expect **13 rows** when the database holds only seeded history (the 13 single-reading signals
   from A7.8) plus any signal the live gateway is not reporting. Note `NULLS FIRST` puts
   never-reported signals at the top — verify that ordering is visible in the table.
7. Verify the raw-tier query template is the one documented (5 s bucket, newest value via
   `array_agg(... ORDER BY ts DESC)[1]`, plus `max(quality)` and `count(*)`):
   ```bash
   python3 -c "
   import json; d=json.load(open('ui/grafana/dashboards/wwtp-overview.json'))
   print(d['panels'][0]['targets'][0]['rawSql'])"
   ```
8. Confirm panel behaviour on a **gap**. Take one signal and confirm the line breaks rather
   than interpolating (this is the deadband semantic from §1.2):
   ```sql
   SELECT signal_id, max(gap) FROM (
     SELECT signal_id, extract(epoch FROM ts - lag(ts) OVER (PARTITION BY signal_id ORDER BY ts)) AS gap
     FROM reading WHERE signal_id='INFLUENT:LIFT:CURRENT' ORDER BY ts) t
   GROUP BY 1;
   ```
   Then screenshot the panel — expect a visible break, not a straight line across the gap.
9. Screenshot all 6 panels. This is the primary evidence for B3.

**Expected result** — 6 panels render; panel 2 shows a green 1.5–3.0 mg/L band with DO inside it; panel 4 visibly coarser than panel 1; panel 6 lists the silent signals with NULLS FIRST ordering; gaps break the line.

**Pass/fail criteria**
- PASS: all 6 render with data; titles/signals/units/grid positions match the map; band at
  1.5–3.0 only on panel 2; panel 4 reads `reading_1h`; panel 6 row count and ordering match
  the DB; gaps break.
- FAIL: a panel empty while `age` is seconds; band drawn on a two-signal panel; panel 4
  querying raw; panel 6 count differing from the DB query run at the same moment (measure both
  within the same minute, or the `silence > 1 hour` predicate will disagree).
- Any panel title differing from the table is a **generator drift finding**.

**Evidence** — six screenshots (full dashboard + each panel), the panel-2 band values, the
panel-4 `rawSql` dump, the panel-6 SQL and its row count, the gap query.

**Risks / caveats**
- Panel 6's `HAVING` is time-relative. Running the DB query and looking at the panel five
  minutes apart can legitimately disagree as signals cross the 1-hour boundary. Record the
  timestamp of both.
- If the gateway is stopped (`make db-still`) the raw panels go stale but the rollup panels do
  not. That difference is itself a useful check — do it deliberately as step 10.

---

## B4. Panel-level correctness — `WWTP — discharge permit`

**Objective** — the three panels and the 5-way compliance table return correct numbers against the contract's permit limits.

**Pre-conditions** — B2, seeded 30+ days of data. **Important:** the dashboard range is
`now-30d`. A 7-day seed means the older two-thirds of the range is empty. Either accept partial
coverage or seed a longer window (see risks).

**Steps**
1. Open `http://127.0.0.1:3000/d/wwtp-permit/wwtp-permit`. Confirm range `Last 30 days`,
   refresh `30s`. Screenshot.
2. Panel 1 — `Ammonia — permit 10 mg/L 30-day mean`, `EFFLUENT:FLOW:NH4`, grid (0,0,12,9),
   unit `mg/L`, `rollup=true`:
   ```sql
   SELECT avg(mean) FROM reading_1h
   WHERE signal_id='EFFLUENT:FLOW:NH4' AND bucket >= now() - interval '30 days';
   ```
   Expect a value comfortably **below 10 mg/L** — notebook 01 measured a 7-day mean of
   **7.83 mg/L** (p05 4.02, p95 9.32). If the panel mean exceeds 10, the plant is genuinely
   non-compliant and you should verify with the raw data before calling it a bug.
3. Panel 2 — `Suspended solids — permit 30 mg/L`, `EFFLUENT:FLOW:TSS`, grid (12,0,12,9),
   `rollup=true`. Run the same query for TSS and compare. Expect below 30 mg/L.
4. Panel 3 — `Permit compliance, from the contract`, **table**, grid (0,9,24,8). This is the
   only place in the project that computes a compliance number. Verify its SQL:
   ```bash
   python3 -c "
   import json; d=json.load(open('ui/grafana/dashboards/wwtp-permit.json'))
   for p in d['panels']:
       print('==', p['title']); print(p['targets'][0]['rawSql'])"
   ```
   Expect a `WITH span AS (SELECT now() - interval '30 days' AS since)` followed by a
   **5-way `UNION ALL`** over `reading_1h`:
   | # | Parameter | Signal | Statistic | Permit limit (from `site.permit`) |
   |---|---|---|---|---|
   | 1 | ammonia 30-day mean | `EFFLUENT:FLOW:NH4` | `avg(mean)` | **10 mg/L** |
   | 2 | TSS 30-day mean | `EFFLUENT:FLOW:TSS` | `avg(mean)` | **30 mg/L** |
   | 3 | pH min | `EFFLUENT:FLOW:PH` | `min(min)` | `eff_ph_min` |
   | 4 | pH max | `EFFLUENT:FLOW:PH` | `max(max)` | `eff_ph_max` |
   | 5 | coliform geometric mean | `EFFLUENT:DIS-CL-2:BACTI` | `exp(avg(ln(mean)))` | `dis_bacti_geomean` |
5. **Verify the geometric mean is a geometric mean.** `exp(avg(ln(mean)))` is correct and is
   not interchangeable with `avg(mean)`. Prove they differ:
   ```sql
   SELECT round(exp(avg(ln(mean)))::numeric,3) AS geomean,
          round(avg(mean)::numeric,3)           AS arithmetic,
          round(exp(avg(ln(mean))) - avg(mean)::numeric,3) AS difference
   FROM reading_1h
   WHERE signal_id='EFFLUENT:DIS-CL-2:BACTI' AND bucket >= now() - interval '30 days';
   ```
   Expect `difference` to be **large and negative** — notebook data has coliform p50 ≈ 49.8
   and p95 ≈ 864, so a geometric mean is far below the arithmetic one. If the panel shows the
   arithmetic mean, that is a **FAIL**.
   Also confirm no row of that branch is NULL, since `ln(NULL)` is NULL and `avg` skips it:
   ```sql
   SELECT count(*) FILTER (WHERE mean IS NULL) AS null_means, count(*) AS total
   FROM reading_1h WHERE signal_id='EFFLUENT:DIS-CL-2:BACTI';
   -- expect null_means = 0
   ```
6. Cross-check every limit in the table against the contract, not against a doc:
   ```sql
   SELECT jsonb_pretty(permit) FROM site;
   ```
   Confirm the five keys and values match what the panels declare. If a limit in the panel
   disagrees with `site.permit`, the generator hardcoded it and that is a **FAIL** — the
   dashboard title says "from the contract".
7. Confirm all five branches are served by the aggregate (re-use A9.3's `EXPLAIN`). Any branch
   touching raw `reading` is a FAIL.
8. Screenshot all three panels plus the compliance table.

**Expected result** — 3 panels; ammonia mean ≈ 7.83 mg/L (< 10); TSS < 30; coliform shown as a geometric mean well below the arithmetic mean; all five limits traceable to `site.permit`.

**Pass/fail criteria**
- PASS: five table rows; every limit equals the contract value; coliform uses
  `exp(avg(ln(...)))`; ammonia and TSS means under their permits; no raw scan.
- FAIL: a limit hardcoded and differing from `site.permit`; coliform as an arithmetic mean; a
  NULL-silent geometric mean (i.e. `null_means > 0` and the panel still shows a number);
  any branch scanning raw.
- Record: ammonia/TSS means, and whether the 30-day window is fully covered.

**Evidence** — three screenshots, the compliance table screenshot, the panel-3 `rawSql` dump,
the permit JSON, the geomean-vs-arithmetic query, the `EXPLAIN`.

**Risks / caveats**
- **30-day range vs 7-day seed is a coverage gap, not a bug.** Panels 1–2 will simply show 7
  days of data in a 30-day frame. To test properly, seed longer:
  ```bash
  docker compose --profile demo run --rm seed --database wwtp --days 35 --reset
  ```
  Expect ~21 M rows and ~15 minutes; budget disk accordingly. Record which mode you tested in.
- Permit limits live on `site` so a reissue does not rewrite history. That means a **stale
  dashboard will still show old limits** if `site.permit` changed without a dashboard regen —
  run `make dashboards-check` after any contract change.

---

## B5. Alarms — the actual alerting system

**Objective** — verify the alarm engine raises, latches, acknowledges and clears correctly, and that the three independent implementations of "outstanding alarm" agree.

> Grafana has no alerting. This case tests `alarms/` instead, which is where the alarm system
> actually lives. The panel-band-is-not-an-alarm distinction is verified in B5.5.

**Pre-conditions**
```bash
make up
make roles
```

**Steps**
1. Confirm the rule inventory against the code, not a doc:
   ```bash
   uv run python -c "
   from alarms.rules import RULES
   print(len(RULES), 'rules over', len({r.signal_id for r in RULES}), 'signals')"
   ```
   **Expect 16 rules over 11 signals.** Note `alarms/rules.py:1` and `:170` say "Fourteen
   rules" / "fourteen rules over ten signals" and `rules.py:24-27` says "The three rules that are
   not thresholds" (there are four). Log as **FINDING** — stale docstring. `docs/ALARMS.md:3`
   has it right.
2. Print the full rule table and check each against the contract for sane thresholds:
   ```bash
   uv run python -c "
   from alarms.rules import RULES
   for r in RULES:
       print(f'{r.rule_id:38s} {r.signal_id:32s} {r.detector:24s} {r.severity:8s} for={r.for_s:<7} clear={r.clear_s:<7} {r.params}')"
   ```
   Verify the 16 rows and their parameters:
   | rule_id | detector | sev | for_s | clear_s | params |
   |---|---|---|---|---|---|
   | `aeration_blower_speed_drop` | rate_of_change | critical | 0.0 | 120.0 | per_hour 20000.0 rev/min |
   | `aeration_do_sagging` | trend | critical | 600.0 | 1800.0 | per_hour 0.10, down, min_points 4, min_span_s 21600 |
   | `aeration_do_low` | single_point_threshold | warning | 300.0 | 900.0 | limit 1.5, low, hyst 0.2 |
   | `aeration_ammonia_rising` | trend | critical | 1800.0 | 3600.0 | per_hour 2.0, up, min_points 3, min_span_s 10800 |
   | `influent_flow_surge` | trend | warning | 900.0 | 1800.0 | per_hour 400.0 m³/h/h, up, min_points 4, min_span_s 3600 |
   | `influent_flow_ceiling` | single_point_threshold | warning | 600.0 | 1200.0 | limit 2000.0, hyst 150.0 |
   | `digester_vfa_high` | single_point_threshold | critical | 1800.0 | 3600.0 | limit 0.6, hyst 0.05 |
   | `digester_ph_low` | single_point_threshold | critical | 900.0 | 1800.0 | limit 6.4, low, hyst 0.1 |
   | `secondary_blanket_stuck` | flatline_detection | warning | 3600.0 | 3600.0 | max_silence_s 1800, stuck_s 7200 |
   | `influent_lift_current_anomaly` | deviation_from_baseline | warning | 900.0 | 1800.0 | tolerance 0.05, high |
   | `effluent_tss_coverage` | expected_sample_count | warning | 1800.0 | 1800.0 | min_count 3, horizon_s 3600 |
   | `lift_current_silence` | quality_flag | critical | 60.0 | 120.0 | min_quality 2 |
   | `lift_pump_flow_lost` | single_point_threshold | critical | 180.0 | 300.0 | limit 50.0, low, hyst 25.0 |
   | `secondary_scrape_torque_high` | single_point_threshold | warning | 900.0 | 1800.0 | limit 50.0 N·m, hyst 4.0 |
   | `aeration_do_xvalidation` | cross_validation | critical | 300.0 | 900.0 | tolerance 0.05 |
   | `influent_flow_xvalidation` | cross_validation | critical | 300.0 | 900.0 | tolerance 0.02 |
3. Confirm detector dispatch covers all ten plus the registered alias:
   ```bash
   uv run python -c "
   from alarms.detectors import _DETECTORS
   print(len(_DETECTORS)); print(sorted(_DETECTORS))"
   ```
   Expect 11 keys: `single_point_threshold`, `deviation_from_baseline`, `trend`,
   `rate_of_change`, `state_change`, `flatline_detection`, `expected_sample_count`,
   `quality_flag`, `cross_validation`, `model_based`, `ratio_derived_alarm`.
   Note `ratio_derived` is registered under the name `ratio_derived_alarm`. If a rule ever
   references `ratio_derived`, dispatch will `KeyError` — check no rule does.
4. **Exercise the engine end to end** against the live historian, in replay mode (default):
   ```bash
   make alarms 2>&1 | tee evidence/B5/alarms-serve.log &
   ALARM_PID=$!
   sleep 120
   # raise a fault in the plant so there is something to detect
   docker compose exec softplc true   # no-op; instead use replay over the seeded week:
   python -m alarms.replay --lookback-hours 24 2>&1 | tee evidence/B5/replay.log
   kill $ALARM_PID
   ```
   `alarms.main serve` flags: `--interval 30.0`, `--report-every 300.0`,
   `--stale-seconds 900.0`, `--replay/--no-replay` (default replay), `--replay-hours 24.0`.
5. **Verify the latch-and-acknowledge lifecycle in the database.** A `critical` alarm **never
   writes `alarm_cleared`** — acknowledgement is what takes it off the list. Confirm the event
   kinds present and the shape:
   ```sql
   SELECT kind, severity, count(*) FROM event
   GROUP BY 1,2 ORDER BY 3 DESC;
   -- expect: alarm_raised / alarm_cleared / alarm_acknowledged only
   ```
   ```sql
   SELECT ts, kind, severity, message, signal_id, detail->>'rule' AS rule, detail
   FROM event ORDER BY ts DESC LIMIT 20;
   ```
   Check `detail` carries at least `rule`, and that timestamps are monotonic.
6. **Verify the three implementations of "outstanding alarm" agree.** The predicate is
   duplicated in `alarms/replay.py`, `ui/web/lib/queries.ts` and `scada/build_flows.py`:
   ```sql
   SELECT r.id, r.ts, r.severity, r.message, r.signal_id,
          r.detail->>'rule' AS rule, now() - r.ts AS age_s
   FROM event r
   LEFT JOIN LATERAL (
     SELECT ack.id FROM event ack
     WHERE ack.kind='alarm_acknowledged'
       AND ack.detail->>'rule' = r.detail->>'rule'
       AND ack.ts > r.ts
     ORDER BY ack.ts LIMIT 1) a ON TRUE
   WHERE r.kind='alarm_raised' AND r.severity='critical'
     AND r.ts >= now() - interval '24 hours'
     AND a.id IS NULL
     AND NOT EXISTS (SELECT 1 FROM event c
                     WHERE c.kind='alarm_cleared'
                       AND c.detail->>'rule' = r.detail->>'rule' AND c.ts > r.ts)
   ORDER BY r.ts DESC LIMIT 50;
   ```
   Run this, then open `/alarms` on the web dashboard (B4.6 URL) and then the Node-RED
   annunciator (B3). All three must list **the same rows in the same order**.
7. **Verify a threshold boundary.** Pick `aeration_do_low` (limit 1.5, hysteresis 0.2). Its
   effective clear threshold is 1.7. Prove hysteresis with a direct engine test rather than
   waiting for the plant:
   ```bash
   uv run pytest tests/test_alarm_detectors.py -q -k hysteresis
   uv run pytest tests/test_alarm_rules.py tests/test_alarm_engine.py -q
   ```
   Expect all pass. Then confirm `for_s=300` means a 5-minute dwell before raising — a DO of
   1.0 mg/L for 30 s must **not** raise. There is a test; name it in your evidence.
8. **Verify the coverage audit** — the fault × rule matrix, which is the project's own answer
   to "did we write a rule that can never fire":
   ```bash
   time make coverage 2>&1 | tee evidence/B5/coverage.log
   ```
   Expect ~8 minutes. Report the matrix: which of the 12 faults raise which of the 16 rules, and
   the `blind_spots` list. Four rules are known never to fire (recorded in
   `docs/VERIFYING.md`) — confirm they appear as blind spots rather than claiming they fire.
   ```bash
   make coverage-json   # > coverage.json, machine-readable
   python3 -c "import json;d=json.load(open('coverage.json'));print(json.dumps(d,indent=2))"
   ```
9. **Verify the tuning tool** exists and is runnable:
   ```bash
   python -m alarms.tune --rule aeration_do_sagging 2>&1 | tee evidence/B5/tune.log
   python -m alarms.tune --json | head -40
   ```
10. **Confirm Grafana really has no alerting** — the negative assertion:
    ```bash
    find ui/grafana/provisioning -type d -name alerting   # expect: no output
    grep -rilE 'contact_point|notification_policy|mute_timing|grafana_alert|unified_alerting' ui/grafana/   # expect: no output
    docker compose --profile observability exec grafana \
      curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" 'http://localhost:3000/api/v1/provisioning/alert-rules' \
      | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"   # expect 0
    ```
11. Run the alarm test suites as a cross-check:
    ```bash
    uv run pytest tests/test_alarm_counts.py tests/test_alarm_engine.py tests/test_alarm_replay.py \
                    tests/test_alarm_rules.py tests/test_alarm_detectors.py -q
    ```

**Expected result** — 16 rules over 11 signals; 11 detector keys; `alarm_raised`/`alarm_cleared`/`alarm_acknowledged` only; the three outstanding-alarm views agree row-for-row; the coverage matrix reports its blind spots; Grafana alert-rule count is 0.

**Pass/fail criteria**
- PASS: rule inventory 16/11; detector dispatch complete; hysteresis tests pass; coverage runs
  and names its blind spots; **zero** Grafana alert rules; the three views agree.
- FAIL: a rule count other than 16/11; a rule referencing an unregistered detector name;
  `event` containing an unexpected `kind`; the three views disagreeing (this is a real defect
  — the duplication is intentional, so disagreement means the predicate drifted);
  `critical` alarms writing `alarm_cleared` (that breaks the latch contract).
- **FINDING (expected, log it):** `alarms/rules.py` docstrings say "Fourteen rules" / "three
  rules that are not thresholds".

**Evidence** — rule table dump, detector key list, `event` queries, the three-way comparison
table, coverage matrix + `coverage.json`, tune output, the Grafana zero-alert proof, pytest summary.

**Risks / caveats**
- **Known first open thread: a factor-of-7 disagreement in the alarm harness.** This undermines
  existing alarm work and is recorded in `docs/VERIFYING.md`. If your coverage matrix does not
  reconcile with `docs/ALARMS.md`, **do not treat it as a new defect** — cite the known issue
  and quantify the disagreement.
- `make coverage` takes ~8 min and `--hours 8` replays 8 hours of simulated plant per fault.
- `alarms.main serve` was never started in CI (`docs/CI.md` says so). So B5 is genuinely
  **ungated** — a failure here is high-value.
- `model_based` and `ratio_derived_alarm` detectors exist but no shipped rule uses them
  (`model_based` is the forward path for the ML workbooks). That is expected; note it.

---

## B6. Node-RED — flow assembly, deploy, and behaviour

**Objective** — the three flows assemble from the generated files, deploy without error, and do what they claim.

**Pre-conditions**
```bash
make scada-flows      # generate_tags + build_flows
make scada-check      # drift gate — run this FIRST
make scada            # docker compose --profile scada up -d scada
```

**Steps**
1. **Drift gate before anything else:**
   ```bash
   make scada-check 2>&1 | tee evidence/B6/scada-check.log
   ```
   Expect no drift. Drift means a hand-edit to a generated file, which the next rebuild
   silently reverts.
2. Confirm the generated artefacts:
   ```bash
   ls -la scada/flows/
   # expect: 01-mimic.json 02-annunciator.json 03-control.json tags.json
   python3 -c "import json;d=json.load(open('scada/flows/tags.json'));print(d['areas'],'areas')"
   # expect: 8
   ```
3. **Flow assembly.** `scada/nodered/entrypoint.sh` concatenates `/flows/*.json` — excluding
   `tags.json`, which is a tag list, not a flow — into a single `flows.json` using `node`, not
   `sed`, then renames atomically. Verify:
   ```bash
   docker compose logs scada | grep -E 'assembled|nodes'
   # expect exactly: assembled /data/flows.json from 3 file(s), 41 nodes
   ```
   Count independently:
   ```bash
   docker compose exec scada node -e \
     "console.log(JSON.parse(require('fs').readFileSync('/data/flows.json')).length)"
   # expect 41  (12 mimic + 14 annunciator + 15 control)
   ```
   Also confirm `tags.json` was **excluded** (3 files, not 4).
4. **No errors after deploy** — the documented check:
   ```bash
   docker compose logs scada | grep -ci error    # expect 0
   docker compose logs scada | grep -iE 'error|cannot|failed' -A3   # expect empty
   ```
5. **Verify the editor is bound to loopback.** Node-RED has **no setting that
   disables its editor** — `NODE_RED_EDITOR` is not a Node-RED option and is read by
   nothing in this repo. Do not grep for it; the grep returns nothing and reads like a
   pass. What actually protects the generated flows is the port bind, so that is what
   to check:
   ```bash
   docker compose --profile scada config | grep -A3 'published:' | grep -E '127.0.0.1|SERVER_PORT'
   # expect the scada published port to be bound to 127.0.0.1, not 0.0.0.0
   ```
   Then confirm it is *un*reachable off-host:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://$(hostname -I | awk '{print $1}'):${SCADA_PORT:-18880}/scada/
   # expect 000 (connection refused), NOT 200
   ```
   Open `http://127.0.0.1:${SCADA_PORT:-18880}/scada` **on the loopback address**. The
   editor is fully present and fully editable — screenshot it as such. The flows are
   generated, so a hand edit here is silently reverted by the next
   `python -m scada.build_flows`; that is the reason for the bind, not a read-only UI.
6. **Flow 1 — `01 — Plant mimic (read-only)`.** Expect 12 nodes. Its `inject` has
   `repeat: 5` (every 5 s); its `postgresql` node runs
   `SELECT DISTINCT ON (signal_id) signal_id, value, quality, source, ts, now() - ts AS age FROM reading WHERE signal_id IN (...) ORDER BY signal_id, ts DESC`
   over `MIMIC_TAGS` = `INFLUENT:FLOW:FLOW`, `AERATION:AHU-1:DO`, `AERATION:AHU-1:AIR_FLOW`,
   `AERATION:AHU-1:BLOWER_RPM`, `PRIMARY:PRI-CL-1:BLANKET`, `SECONDARY:SEC-CL-1:BLANKET`,
   `SLUDGE:DIG-1:PH`, `EFFLUENT:FLOW:NH4`, `EFFLUENT:FLOW:TSS` (9 signals).
   Steps:
   - Open the sidebar debug pane, expand the `mimic status` debug node.
   - Expect a JSON object with 9 keys appearing every 5 s.
   - Cross-check one value against the DB **immediately**:
     ```sql
     SELECT signal_id, value, quality, source, ts FROM reading
     WHERE signal_id='AERATION:AHU-1:DO' ORDER BY ts DESC LIMIT 1;
     ```
   - Confirm the `annotate against the normal band` debug node flags out-of-band values.
   - `mimic status` is `active: true` in the flow and needs no enabling. It used to ship
     `active: false`, so every tap in the project was silent; FAIL if the pane is empty
     rather than telling the tester to switch something on.
7. **Flow 2 — `02 — Alarm annunciator`.** 14 nodes. Its `refresh the panel` inject is
   `once: true` with `repeat: ""` — event-driven, deliberately not polled. Click its button.
   - Expect the `annunciator panel` debug node to list unacknowledged criticals with
     `rule`, `raised_at`, `severity`, `message`, `acknowledged`, `age_s`.
   - Acknowledge with the **`operator acknowledges an alarm`** inject — a separate node
     from the panel, and deliberately so: a refresh must never be able to acknowledge
     anything. It is `manual` (`once: false`, `repeat: ""`) so it can never fire by itself,
     and its `payloadType` is `json`: edit the payload to
     `{"rule": "<rule>", "orig": {"message": "<message>", "signal_id": "<id>", "equipment_id": "<real id>"}}`
     and press the button.
     - **The acknowledge path used to be wired off the `annunciator panel` debug node**,
       which registers `outputs: 0`, so the wire pointed at a port that does not exist and
       this path had never run. A tap that displays is not a control surface. FAIL if you
       find a wire leaving a `debug` node.
     - `equipment_id` is a foreign key. Use a real `equipment.id`; an invented one is
       rejected with `violates foreign key constraint "event_equipment_id_fkey"`, which
       is the database working, not a flow defect.
   - Verify the acknowledgement landed:
     ```sql
     SELECT ts, kind, message, detail FROM event
     WHERE kind='alarm_acknowledged' ORDER BY ts DESC LIMIT 5;
     ```
   - Confirm the row disappears from the panel (this is the B5.6 latch/acknowledge path).
8. **Flow 3 — `03 — Operator control (setpoint)`.** 15 nodes, 2 outputs from
   `check against the permit range`.
   - With an **in-range** setpoint (contract write range for `AERATION:AHU-1:SETPOINT_DO` is
     **0.5 to 6.0 mg/L**), inject a value such as `2.5`:
     ```sql
     SELECT value, ts FROM reading WHERE signal_id='AERATION:AHU-1:SETPOINT_DO' ORDER BY ts DESC LIMIT 1;
     ```
     Expect `control: written` debug output returning `{"id": N}` (the `event` row),
     and an `audit trail` INSERT into `event`. The PostgreSQL config node is named
     `wwtp-db`; **`wwtp-softplc-modbus` is the `modbus-client` config node**, not the
     database.
     **Now assert the held value changed, and that it stays changed.** The write
     takes: `ModbusTcpServer._accept_write` queues it, and `SoftPlc._apply_pending_writes`
     applies it to both `plant.aeration.setpoint_do_mg_l` and
     `aeration_control.setpoint_mg_l` before the physics runs. Read PDU 102 at leisure:
     ```bash
     uv run python -c "
     import struct
     from pymodbus.client import ModbusTcpClient
     c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
     w = c.read_holding_registers(102, count=2, slave=1).registers
     print(struct.unpack('>f', struct.pack('>HH', *w))[0])
     c.close()"
     ```
     Expect `2.5`, and expect it again 30 seconds later. **This section previously
     said the opposite** — that the value reverted to the model's own 2.0 within one
     scan and could only be caught by polling inside the same 20 ms window. That was
     accurate when written; it described the missing write-back path, which has since
     been built. A revert now is a regression, so re-run this read after a delay
     rather than racing the scan. `adr` is the **PDU** offset (40102 − 40000 = 102), not
     the 4xxxx address: `adr: 40102` returns Modbus exception 2 and writes nothing.
   - **Confirm the control loop actually responded.** The setpoint is applied twice on
     purpose, because `AerationControl` receives its setpoint by value at construction.
     Reading PDU 102 back is *not* sufficient evidence: the register publishes the
     plant's field, so it would read correctly even if the PI loop were still
     integrating against the startup value. Inject a setpoint far from the current DO
     (`5.5`) and check that `AERATION:AHU-1:DO` and the blower duty register
     (`40108`, PDU 108) climb:
     ```sql
     SELECT value, ts FROM reading
     WHERE signal_id IN ('AERATION:AHU-1:DO','AERATION:AHU-1:BLOWER_VALVE')
     ORDER BY ts DESC LIMIT 8;
     ```
     Duty rising while the setpoint sits high is the finding that matters.
   - **The inject payload is a bare number, not JSON.** The permit check does
     `Number(msg.payload)`, so `{"setpoint": 3.4}` is `NaN` and the flow refuses it
     as "not a number". That refusal is correct behaviour and looks exactly like a
     range refusal, so it is easy to misread. Inject `3.4`:
     ```bash
     curl -s -X POST http://127.0.0.1:18880/scada/inject/a6f5e510057a9 \
       -H 'Content-Type: application/json' \
       -d '{"__user_inject_props__":[{"p":"payload","v":"3.4","vt":"num"}]}'
     ```
     The node id is `enter a setpoint` in tab `03 — Operator control (setpoint)`;
     the path is `/scada/inject/<id>`, not `/inject/<id>` — the admin root is
     `/scada`, and a request to the bare path returns `Cannot POST`.
   - **Expect no `ERROR` lines from the write node.** An empty `control: written`
     with a red `modbus-write` means the PLC refused the write. Read the PLC's own
     reason, which names the register and why:
     ```bash
     docker compose logs softplc --since 2m | grep -E 'write accepted|write refused'
     ```
     Two messages it can print, and what each means:
     ```
     modbus write accepted: AERATION_SETPOINT_DO = 3.4 -> AERATION:AHU-1:SETPOINT_DO
     modbus write refused: AERATION_SETPOINT_DO is 2 register(s) of float32; got 1
     ```
     **The second one is a flow defect, not a plant refusal, and it is the single
     most important line in this section.** It means the flow sent FC6 (write single
     register) instead of FC16, so only the high word of the float32 arrived. The
     flow's `dataType` was `HoldingRegister`, which
     `node-red-contrib-modbus` maps to **function code 6** — and FC6 writes one
     register and ignores `quantity` entirely. The correct value is
     `MHoldingRegisters` (function code 16). It is now correct and asserted by
     `tests/test_scada_contract.py::test_a_multi_register_write_uses_the_multi_register_function_code`,
     but the failure was invisible for as long as it existed: the node reported
     success, the audit row was written, the historian recorded the setpoint, the
     mimic showed it, and the only thing that never happened was the plant
     changing. If this message appears, the flow has been edited by hand or
     regenerated from a stale generator — regenerate with
     `uv run python -m scada.build_flows` and `docker compose restart scada`.
   - **Verify the audit trail reflects what actually happened.** The `event` row is
     written *downstream* of the write node, so a refused write must leave no row:
     ```sql
     SELECT count(*) FROM event WHERE kind='setpoint_written';
     ```
     Count before and after an out-of-range inject; the two numbers must be equal.
     A row for a write the plant refused is an audit trail that lies.
   - With an **out-of-range** setpoint (`0.1` and `9.0`):
     Expect `control: refused` debug output and **no** change to the DB value and **no**
     `event` row. The flow **refuses**; it does not clamp. That distinction is the whole point
     of the two-output function.
     The PLC refuses these independently: `_accept_write` range-checks on the wire and
     answers with Modbus exception 3. Node-RED's permit check is the *client's* courtesy,
     so a tester bypassing the flow should still be refused — worth confirming once with
     the `python` one-liner above, using `9.0`, and confirming the register still holds
     the last accepted value afterwards.
   - **Bypass the flow entirely once.** A Modbus tool with no notion of a permit
     range is the real test of whether the *server* enforces anything, because
     Node-RED's check is only a courtesy:
     ```bash
     uv run python -c "
     import struct
     from pymodbus.client import ModbusTcpClient
     c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
     for v in (9.0, 0.1, 99.0):
         b = struct.pack('>f', v); hi, lo = struct.unpack('>HH', b)
         r = c.write_registers(102, [hi, lo], slave=1)
         print(v, '->', 'REFUSED' if r.isError() else 'ACCEPTED')
     c.close()"
     ```
     All three must print `REFUSED`. Also try a **read-only** register —
     `AERATION_DO` at PDU 100, and the two that *used* to be writable,
     `FAULT_CODE` (40350) and `STORM_FLAG` — each must be refused too. Those two
     are the interesting case: they were `writable: true` in the contract while
     nothing applied a write to either, so a client was told it succeeded and the
     value was discarded on the next scan. They are read-only now.
   - **A half-width write must be refused too**, which is the FC6/FC16 check above
     seen from the other side:
     ```bash
     uv run python -c "
     from pymodbus.client import ModbusTcpClient
     c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
     r = c.write_register(102, 1, slave=1)
     print('FC6 ->', 'REFUSED' if r.isError() else 'ACCEPTED')
     c.close()"
     ```
     Must print `REFUSED`. Accepting it would store a torn float — a fresh high
     word on a stale low word — which is a plausible but wrong setpoint, and that
     is worse than an error.
   - Verify the audit trail exists for accepted writes:
     ```sql
     SELECT ts, kind, severity, message, detail FROM event
     WHERE kind <> 'alarm_raised' ORDER BY ts DESC LIMIT 10;
     ```
     The expected message is the operator's own number and the range it was
     checked against, e.g.
     `setpoint AERATION:AHU-1:SETPOINT_DO set to 3.4 mg/L (range 0.5-6 mg/L)`.
9. **Node count and structure sanity:**
   ```bash
   python3 -c "
   import json, glob, collections
   tot=collections.Counter()
   for f in sorted(glob.glob('scada/flows/0*.json')):
       fl=json.load(open(f)); c=collections.Counter(n['type'] for n in fl)
       print(f.split('/')[-1], len(fl), 'nodes', dict(c)); tot.update(c)
   print('TOTAL', sum(tot.values()), dict(tot))"
   ```
   Expect `inject`/`function`/`debug` triples in each flow; node types drawn only from
   `inject, function, debug, tab, comment, postgresql, modbus-write, postgreSQLConfig,
   modbus-client, file in`. There is no `trigger` node: it has no startup path, so it never
   fired, and the tag loader uses `inject(once=True)`.
   **Expect zero `http in` / `http response` nodes** — the flows emit only to the debug
   sidebar, and there are no flow HTTP or websocket endpoints. If you find an `http in` node,
   the flows have been hand-edited. Do not expect to `curl` a flow endpoint.
10. **Flow SQL is gated** even though the flow JavaScript is never executed:
    ```bash
    uv run pytest tests/test_scada_contract.py -q
    ```
    Expect 49 tests pass (2 skipped without a database and without `.env`). This is the
    only automated check on flow contents — see risks.
11. **Credentials.** Two credential **ids** are referenced, not secrets:
    `wwtp-db` (postgresdb) and `wwtp-softplc-modbus` (modbus-client, `serverType: tcp`,
    `reconnectDelay: 1000`, `reconnectTries: 10`). The real values are written by
    `entrypoint.sh` to `/data/flows_cred.json` (mode 600) **only if absent**, plus
    `/data/.credentialSecret` (32 random bytes, mode 600). Verify:
    ```bash
    docker compose exec scada ls -la /data/flows_cred.json /data/.credentialSecret
    # expect both mode 600
    ```
    And verify the required-password guard:
    ```bash
    docker compose --profile scada run --rm -e POSTGRES_PASSWORD= scada 2>&1 | tail -3
    # expect: POSTGRES_PASSWORD is required to build the Node-RED database credential
    ```
12. **Node packages resolved** (mirrors the CI `images` job):
    ```bash
    docker compose exec scada node -e \
      "require.resolve('node-red-contrib-postgresql'); require.resolve('node-red-contrib-modbus'); console.log('ok')"
    ```
    Expect `ok`.
13. **Rebuild-and-reload** works end to end:
    ```bash
    make scada-flows && docker compose restart scada
    docker compose logs scada --since 1m | grep assembled   # expect 3 file(s), 41 nodes
    docker compose logs scada --since 1m | grep -ci error  # expect 0
    ```

**Expected result** — no drift; `assembled /data/flows.json from 3 file(s), 41 nodes`; 0 errors; editor reachable on loopback and unreachable off it; flow 1 emits 9 values every 5 s matching the DB; flow 2 lists and acknowledges alarms; flow 3 writes in-range and refuses out-of-range; credentials mode 600.

**Pass/fail criteria**
- PASS: every assertion above. Specifically FAIL if the assembled node count is not 41, if
  `grep -ci error` is non-zero, if flow 3 **clamps** instead of refusing, if an out-of-range
  write produces an `event` row, if any `http in` node exists, or if either credential file is
  group/world-readable.
- FAIL `test_scada_contract.py` is a hard FAIL — it is the only gate on flow SQL.

**Evidence** — drift log, assembly line, node counts, four screenshots (mimic, annunciator,
control-accepted, control-refused), the DB cross-checks, the credential `ls -la`, the
required-password guard output, the node package resolution, pytest summary.

**Risks / caveats**
- **The flows' JavaScript is never executed by any automated gate** (`docs/CI.md` says so).
  Every B6 behavioural check is manual and, as far as the repo is concerned, unverified.
  A failure here is high-value — capture the debug node payload verbatim.
- Node-RED's admin auth (`httpAdminAuth`) is set by the editor on first run and is gitignored.
  On a fresh volume you may be asked to create credentials. Record that as install friction.
- **Flow 3's write is the only control path in the project.** It writes Modbus FC6 to the
  soft PLC with no authentication and no authorization beyond a range check (SECURITY gap 2).
  Verify the range check rejects, and report the absence of auth as a finding, not a test bug.
- `scada` binds **hard-coded** to `127.0.0.1` (unlike `${HOST_BIND}` used elsewhere). Confirm
  with `docker compose --profile scada config`; a FAIL here is a real finding.

---

## B7. Web dashboard — routes, rendering, and credential containment

**Objective** — all four routes render correct data, `/api/health` is a real check, and no credential reaches the browser.

**Pre-conditions**
```bash
make page-check      # drift gate on ui/web/lib/contract.json — run first
make web             # docker compose --profile ui up -d web
```

**Steps**
1. **Drift gate.** `ui/web/lib/contract.json` is generated from `contracts/tags.yaml` by
   `ui/web/generate_page.py`; `lib/types.ts` is hand-written so a contract change becomes a
   **type error**.
   ```bash
   make page-check 2>&1 | tee evidence/B7/page-check.log   # expect no drift
   ```
   Confirm the read model has all 57 signals:
   ```bash
   python3 -c "import json;d=json.load(open('ui/web/lib/contract.json'));print(len(d['signals']),'signals')"
   ```
2. Container health — `/api/health` is the compose healthcheck:
   ```bash
   docker compose --profile ui ps web    # expect (healthy)
   ```
3. Confirm all four routes return 200:
   ```bash
   for p in / /permit /alarms /api/health; do
     printf '%s -> %s\n' "$p" \
       "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${WEB_PORT:-3001}$p)"
  
   # expect: / -> 200, /permit -> 200, /alarms -> 200, /api/health -> 200
   ```
4. `/api/health` body and failure mode:
   ```bash
   curl -s http://127.0.0.1:${WEB_PORT:-3001}/api/health | python3 -m json.tool
   # expect: {"ok":true,"database":"reachable","ms":<small integer>}
   ```
   Prove it is a *real* check by stopping the DB:
   ```bash
   docker compose stop db
   sleep 20
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:${WEB_PORT:-3001}/api/health
   # expect 503
   curl -s http://127.0.0.1:${WEB_PORT:-3001}/api/health | python3 -m json.tool
   # expect ok:false
   docker compose start db && sleep 30
   ```
   A 200 with `ok:true` while the DB is down means the healthcheck is fake — **P0 finding**.
5. **`/` — Plant overview.** Expect every signal grouped by area, each rendered as a
   `Panel` (value, age, trend sparkline).
   - Verify the **single-query** optimisation: one `latest(allSignalIds)` call for all 57
     signals, not 57 calls. Confirm in the DB log or by timing — 57 panels must render in
     well under 1 s:
     ```sql
     SELECT count(*) FROM pg_stat_activity
     WHERE datname = current_database() AND usename = 'wwtp_ui';
     -- expect <= 4 (the pool max is 4)
     ```
   - Cross-check one panel against the DB:
     ```sql
     SELECT r.signal_id, r.value, r.ts,
            extract(epoch FROM (now() - r.ts)) AS age_s
     FROM reading r WHERE r.signal_id='AERATION:AHU-1:DO'
     ORDER BY r.ts DESC LIMIT 1;
     ```
   - **Verify the gap handling.** `trend()` LEFT JOINs `generate_series` to `reading_1m`, so
     absent hours are **real `null`s**, and `Sparkline.tsx` breaks the path on `null` rather
     than interpolating. Screenshot a quiet signal (one of the 13 single-reading signals from
     A7.8) and confirm the sparkline is **empty/broken**, not a flat line.
   - **Verify the staleness warning.** When the newest reading is older than the trend window,
     `page.tsx` must render an explicit `staleData` warning rather than silently sliding the
     window back. Test it deterministically by stopping the gateway:
     ```bash
     docker compose stop gateway
     sleep 120
     ```
     Screenshot `/`. Expect a visible staleness notice. Restart the gateway afterwards.
6. **`/permit` — Discharge permit.** Expect the five permit parameters.
   ```sql
   SELECT jsonb_pretty(permit) FROM site;
   ```
   Confirm the page's five numbers trace to these limits (ammonia 10, TSS 30, pH min, pH max,
   coliform geomean). Cross-check the ammonia value against `avg(mean)` over `reading_1h`.
7. **`/alarms` — Outstanding alarms.** Expect the same rows as the B5.6 SQL and the Node-RED
   annunciator (24-hour window, `LIMIT 50`). All three must agree.
8. **`ui/web/lib/queries.ts` contains exactly four queries.** Assert no SQL was hand-added:
   ```bash
   grep -cE 'SELECT|WITH ' ui/web/lib/queries.ts   # expect 4
   ```
   The four are `latest`, `trend`, `outstandingAlarms`, `dataSpan`.
9. **Least privilege in practice.** `web` uses `wwtp_ui` (LOGIN in `wwtp_reader` only). Verify
   the container's identity and that it cannot write:
   ```bash
   docker compose --profile ui exec web printenv POSTGRES_USER   # expect: wwtp_ui
   docker compose --profile ui exec web id -u                   # expect: 10002 (non-root)
   ```
   The write refusals are in A5.7.
10. **Credential containment — the highest-value check in this case.** `POSTGRES_PASSWORD`,
    `POSTGRES_HOST`, `POSTGRES_USER` and `POSTGRES_DB` must not reach the client bundle. The
    two `NEXT_PUBLIC_*` build args were deliberately removed so no credential can:
    ```bash
    docker compose --profile ui exec web sh -c \
      "grep -rl 'POSTGRES_PASSWORD\|POSTGRES_HOST\|NEXT_PUBLIC' /app/.next/static | wc -l"
    # expect 0
    ```
    ```bash
    docker compose --profile ui exec web sh -c \
      "grep -rl 'POSTGRES' /app/.next/server 2>/dev/null | head -5"
    # server-side files MAY reference it; client bundles may NOT
    ```
    Also confirm the browser-side HTML carries no DSN:
    ```bash
    curl -s http://127.0.0.1:${WEB_PORT:-3001}/ | grep -ciE 'postgres://|postgresql://|password'  # expect 0
    ```
    And that `lib/db.ts` **throws** rather than defaulting to `localhost`:
    ```bash
    docker compose --profile ui exec web sh -c "grep -n 'localhost' /app/.next/server/app/page.js | head" || echo "no localhost default"
    ```
11. **Hardening flags.** `read_only: true`, tmpfs `/tmp:size=32m` and
    `/app/.next/cache:size=64m`, `no-new-privileges`:
    ```bash
    docker compose --profile ui config | grep -E 'read_only|tmpfs|no-new-privileges|user:'
    ```
    Then prove `read_only` actually bites: the container must not write outside its tmpfs.
    ```bash
    docker compose --profile ui exec web sh -c "touch /app/should-fail" 2>&1 | tail -1
    # expect: Read-only file system
    ```
12. Screenshot all three pages.
13. There are 22 tests for this surface but no TS runner, so they are Python-side:
    ```bash
    uv run pytest tests/test_web_page.py -q    # expect 22 tests
    ```

**Expected result** — no drift; 4 routes 200; `/api/health` `{"ok":true,"database":"reachable","ms":N}` and **503** with the DB down; 57 panels in one query with `pg_stat_activity ≤ 4`; sparklines break on gaps; staleness warning appears with the gateway stopped; `grep -rl POSTGRES_PASSWORD /app/.next/static | wc -l` = **0**; `read_only` bites.

**Pass/fail criteria**
- PASS: as above. Specifically FAIL if any of `POSTGRES_PASSWORD`/`POSTGRES_HOST`/`NEXT_PUBLIC`
  appears in `/app/.next/static`; if `/api/health` returns 200 with the DB stopped; if the
  client HTML contains a DSN; if the container runs as root; if `touch /app/...` succeeds;
  if the sparkline interpolates across a gap; if no staleness warning appears with the gateway
  stopped.
- FAIL `test_web_page.py` (22 tests) is a hard FAIL.
- **There is no TypeScript test runner** (`docs/VERIFYING.md` known issue). The React
  components are effectively untested — note this as a coverage gap.

**Evidence** — drift log, four status codes, health JSON both states, the `pg_stat_activity`
count, sparkline screenshots (normal + quiet signal), staleness screenshot, three page
screenshots, the static-bundle grep counts, the `touch` refusal, rendered compose config,
pytest summary.

**Risks / caveats**
- Step 4 and step 5 involve stopping the database and the gateway. **Run them after** B1–B6,
  and bring the stack back before continuing. Leave the stack in the state you found it.
- `dataSpan()` is described as free on a hypertable (a TimescaleDB metadata op). If your
  TimescaleDB version returns different wording, that is a version difference, not a bug.
- `max: 4` on the pool means `pg_stat_activity ≤ 4` for `wwtp_ui`. Check for connection leaks
  by running the count twice, a minute apart, after several page loads.
- The `alarms` page and the Node-RED annunciator use *identical* predicates, so agreement
  between them is expected by construction. Disagreement is the signal; agreement proves only
  that neither drifted from `alarms/replay.py`.

---

# C. Notebooks

## C0. Area-level notes

- **The `.ipynb` files are generated.** `notebooks/src/*.md` is the source of truth;
  `notebooks/*.ipynb` is built output. Edit the markdown, never the notebook.
- **Committed notebooks carry no outputs, by design.** `outputs` is a record of the last run
  and rots silently; a fenced ` ```output ` block in `src/*.md` is *a claim a machine can
  check*. A JupyterLab session dirties the tree even if you changed nothing — a saved session
  once produced **1,399 insertions across all eleven files**. Run `make notebooks-reset`.
- **The notebooks read a pinned database, not the live plant.** `NOTEBOOK_DB` is hardcoded to
  `wwtp_notebooks` in `notebooks/_data.py`; `SEED_END = "2026-09-29T00:00:00Z"`, `SEED_DAYS = 7`,
  `STORM_AFTER_H = 36`, `STORM_HOURS = 2`. `dsn()` takes `storage.postgres.schema.dsn()` and
  replaces **only** `dbname`. So host/port/credentials come from `.env`, database name does
  not. If `dsn()` reports a *password* error, suspect the **port** (55433 vs 5432).
- **`notebooks/07-what-are-you-estimating.md` has a `.md` extension and is therefore never
  built** — the builder globs `src/*.md`, and this file sits in `notebooks/` root. There is no
  `07-what-are-you-estimating.ipynb`. See Q7. Expect 10 built notebooks, not 11, unless this is
  fixed. `docs/TESTING.md` and the CI job name claim 11.
- **Kernel must be the venv's `python3`.** `pymodbus`/`psycopg`/`asyncua` are only in `.venv`.
  A `jupyter` earlier on `PATH` (anaconda, pyenv, system Python) runs everything under the
  wrong interpreter — and `tools/notebook_read.py` exists precisely because nbconvert once
  resolved `python3` to anaconda.

### The eleven notebook sources

| Source in `notebooks/src/` | Teaches | Reads |
|---|---|---|
| `01-meet-the-plant.ipynb` | what the plant is; a row is not a sample | `reading`, `reading_1m`, `reading_1h`, `signal`, `equipment`, `site`, `event` |
| `02-three-kinds-of-nothing.ipynb` | no data vs bad data vs no change | `reading`, `signal` |
| `03-choosing-a-tier.ipynb` | raw vs 1m vs 1h and what each costs | `reading`, `reading_1m`, `reading_1h` |
| `04-resampling-buys-you-nothing.ipynb` | rollups are a different estimator | `reading`, `reading_1h` |
| `05-autocorrelation.ipynb` | how many independent hours; block bootstrap | `reading`, `reading_1m` |
| `06-detrending-and-differencing.ipynb` | stationarity; hand-rolled ADF | `reading_1h` |
| `07-what-are-you-estimating.md` **(not built — Q7)** | time- vs flow-weighted means | `reading` |
| `08-rolling-origin-validation.ipynb` | random split vs rolling origin | `reading_1m` |
| `09-anomaly-detection.ipynb` | 4 detectors vs injected ground truth | `reading_1m`, `signal`, `reading` |
| `10-change-points.ipynb` | CUSUM on standardised residual | `reading_1m`, `signal`, `reading` |
| `11-dose-or-flow.ipynb` | dose (air) vs flow, with counterfactual | `reading_1m` |

Figures land in `notebooks/figures/*.png` (gitignored), rendered HTML in `notebooks/read/*.html`
(gitignored). **Never commit a committed PNG** — the convention is that it is a picture of a
database that no longer exists.

---

## C1. Environment, kernel and secrets

**Objective** — the kernel is the venv's Python 3.13, dependencies resolve, and the notebooks reach the right database on the right port.

**Pre-conditions**
```bash
make setup
make sync            # uv sync --all-extras
```

**Steps**
1. Confirm the venv and interpreter:
   ```bash
   uv run python -c "import sys; print(sys.executable, sys.version)"
   # expect: .../wwtp-iiot/.venv/bin/python, 3.13.x
   ```
2. **Verify every critical import resolves under the venv**, not a system Python:
   ```bash
   uv run python -c "
   import pandas, numpy, matplotlib, seaborn, psycopg, nbformat
   print('pandas', pandas.__version__); print('numpy', numpy.__version__)
   print('matplotlib', matplotlib.__version__); print('seaborn', seaborn.__version__)
   print('psycopg', psycopg.__version__)"
   ```
   Also confirm `statsmodels` is **absent** by design (notebook 06 hand-rolls OLS + ADF):
   ```bash
   uv run python -c "import statsmodels" 2>&1 | tail -1   # expect ModuleNotFoundError
   ```
3. Confirm the kernel spec name and which interpreter nbclient will use:
   ```bash
   uv run python -m ipykernel --version
   uv run python -c "import ipykernel; print(ipykernel.__file__)"
   uv run jupyter kernelspec list
   ```
   Expect a `python3` kernelspec. Check the JupyterLab status bar names `.venv`.
4. **Prove the gate uses nbclient with `kernel_name="python3"`, not nbconvert.** Both
   `tools/check_notebooks.py` and `tools/notebook_read.py` must do this, and it is asserted by
   tests:
   ```bash
   uv run pytest tests/test_notebook_kernels.py -q     # expect 7 tests
   ```
   Read `test_nothing_shells_out_to_nbconvert_with_execute` — it parses Makefile recipes and
   Python string literals via `ast`, so a module may *quote* the bug without tripping it.
5. Confirm extras are cumulative. `make sync` always uses `--all-extras` because
   `uv sync --extra analysis` alone **removes** asyncua/pymodbus/fastapi:
   ```bash
   grep -n 'EXTRA' Makefile | head -3
   # expect: --extra protocols --extra storage --extra analysis --extra serve
   uv run python -c "import asyncua, pymodbus; print('protocols ok')"
   ```
6. **Determine the notebook DB endpoint actually in use** — this is the #1 time-waster:
   ```bash
   grep -E 'POSTGRES_(HOST|PORT|USER|DB)' .env
   uv run python -c "
   import sys; sys.path.insert(0,'notebooks')
   import _data; d=_data.dsn()
   import psycopg; print({k:v for k,v in d.items() if k!='password'})"
   ```
   Expect `dbname=wwtp_notebooks` and `port` matching `.env` (**55433 on this host**). If you
   see `5432`, your `.env` was not sourced — run through `make`, not a bare `uv run`.
7. Confirm connectivity and that it is the *notebook* database, not the plant:
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'notebooks')
   import _data
   with _data.connect() as c:
       print('db:', c.execute('SELECT current_database()').fetchone()[0])
       print('rows:', c.execute('SELECT count(*) FROM reading').fetchone()[0])
       print('span:', c.execute('SELECT min(ts), max(ts) FROM reading').fetchone())"
   ```
   Expect `dbname = wwtp_notebooks`, `rows = 4239284`, span ending `2026-09-29T00:00:00Z`.
8. **Prove no secret is needed to run them beyond `.env`.** `NOTEBOOK_DB` is hardcoded;
   `POSTGRES_HOST/PORT/USER/PASSWORD/DB` come from `.env`. Confirm nothing else is required:
   ```bash
   grep -rhn 'os.environ' notebooks/*.py notebooks/src/*.md | sort -u
   ```
   Expect only `NOTEBOOK_DB` (and `DISPLAY`/`BROWSER` for the launcher).
9. Jupyter URL/token mechanics — a token is minted per-launch and written to a gitignored file:
   ```bash
   make notebooks-url          # prints http://127.0.0.1:8899/lab?token=<12 hex chars>
   cat notebooks/.jupyter-url
   git check-ignore -v notebooks/.jupyter-url    # prove it is ignored, do not assume
   ```
   The Makefile uses `uuidgen | tr 'A-Z' 'a-z' | cut -c1-12`. Port **8899** (8888 was taken);
   workshop uses **8898**.
10. Confirm the launcher waits for the port and opens the real URL, because JupyterLab 4 masks
    its own token in the printed URL:
    ```bash
    make notebooks-url-open
    # expect: a browser tab at /lab?token=... that actually loads
    make notebooks-stop
    ```

**Expected result** — venv Python 3.13; all imports resolve; `statsmodels` absent; kernel `python3`; 7 kernel tests pass; `dbname=wwtp_notebooks`, port 55433, 4 239 284 rows; `.jupyter-url` gitignored and holds a 12-char token.

**Pass/fail criteria**
- PASS: as above. Specifically FAIL if `dsn()` reports 5432, if `statsmodels` imports
  (meaning the env is not the venv), if any notebook connects to `dbname=wwtp` rather than
  `wwtp_notebooks`, if the row count is not 4 239 284, or if `notebooks/.jupyter-url` is not
  gitignored.
- FAIL: `tests/test_notebook_kernels.py` (7 tests) is a hard FAIL.

**Evidence** — interpreter/version prints, import versions, kernelspec list, pytest summary,
the `dsn()` dict (password redacted), the `current_database()`/`count(*)` output, `git check-ignore` output, the printed Jupyter URL.

**Risks / caveats**
- `.env` sets `POSTGRES_PORT=55433` on this checkout. Compose maps `${POSTGRES_PORT}`, so the
  DB is reachable on **55433 on the host**, not 5432. If `make db-up` reported 5432, you are
  looking at a different `.env`.
- `tools/env.sh` **warns** rather than fails when `.env` is missing, because CI deliberately
  has none. So a missing `.env` is a warning in your output — treat it as a failure anyway
  for a local environment.
- The notebook token is written to `notebooks/.jupyter-url`, which holds a **live credential**.
  Never paste it into a shared evidence bundle.

---

## C2. Notebook build, drift, and the four automated checks

**Objective** — notebooks regenerate deterministically and pass all four gate checks.

**Pre-conditions** — C1.

**Steps**
1. Confirm the build is clean **before** running anything:
   ```bash
   make notebooks-build
   git status --short notebooks/   # expect: empty (build is deterministic)
   git diff --stat -- 'notebooks/*.ipynb'   # expect: no output
   ```
   Any diff here is a build-determinism finding.
2. Run the full gate. It seeds `wwtp_notebooks` on first use (~2.5 min) then executes:
   ```bash
   time make notebooks 2>&1 | tee evidence/C2/notebooks.log
   ```
   Docs claim ~90 s of execution once the database exists; `Makefile:191` says ~2.5 min seed
   plus ~6 min execution. Budget accordingly. The CI job has `timeout-minutes: 30` and the
   per-cell timeout is `timeout=600`.
3. **Verify the four per-notebook checks all ran.** From the log, confirm for each notebook:
   | check | what it proves | failure mode |
   |---|---|---|
   | **1. drift** | `.ipynb` byte-equals a fresh build from `src/*.md` | file was hand-edited |
   | **2. executes** | `NotebookClient(timeout=600, kernel_name="python3", allow_errors=False)` | a cell raised, or hit the 600 s timeout |
   | **3. output fences** | every non-elision line of each ` ```output ` block appears in printed text | a claimed number is not produced |
   | **4. prose numbers** | every number inside `**…**` in prose matches something printed, at the prose's stated precision (tolerance `0.5·10^-decimals + 1e-9`, with a ×100 fraction→percent allowance) | a bolded claim is unbacked |
   ```bash
   grep -cE '^(ok|PASS|checked|✓)' evidence/C2/notebooks.log
   grep -iE 'drift|FAIL|error|Traceback|zero rows' evidence/C2/notebooks.log
   ```
   Expect 10 notebooks × 4 checks = 40 passes (see Q7 on the 10-vs-11 count).
4. **Verify the seed fingerprint gate ran.** For `DATABACKED_TRACKS` the gate first requires
   `SELECT 1` to succeed *and* `tools.notebook_data.status()` to contain
   `"the pinned seed"`, else it exits 2:
   ```bash
   uv run python -m tools.notebook_data status 2>&1 | tee evidence/C2/seed-status.log
   # expect a line containing "the pinned seed"
   ```
   ```bash
   grep -i 'fingerprint\|4239284\|5625358748306' evidence/C2/notebooks.log | head
   ```
   The four fingerprint values from `notebooks/_data.py::EXPECTED`:
   | rows | value_milli_sum | quality_sum | epoch_sum |
   |---|---|---|---|
   | 4 239 284 | 5 625 358 748 306 | 38 | 7 589 771 859 816 991 |
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'notebooks')
   import _data
   with _data.connect() as c:
       got = c.execute(_data.FINGERPRINT_SQL).fetchone()
   for k,(g,e) in zip(_data.EXPECTED, zip(got, _data.EXPECTED.values())): pass
   print('got     ', got); print('expected', tuple(_data.EXPECTED.values()))"
   ```
   Expect an exact match. Integer aggregates only — float `sum` is not order-independent.
5. **Verify the SQL-fence gate.** Each ` ```sql ` block runs inside
   `conn.transaction(force_rollback=True)` with `SET TRANSACTION READ ONLY`, and **both an
   error and a zero-row result fail**:
   ```bash
   grep -icE 'sql fence|zero rows' evidence/C2/notebooks.log
   ```
   Prove the read-only enforcement is real by trying to write through a notebook connection:
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'notebooks')
   import _data, psycopg
   with _data.connect() as c:
       try:
           with c.transaction(force_rollback=True):
               c.execute('SET TRANSACTION READ ONLY')
               c.execute('DELETE FROM reading LIMIT 1')
           print('LEAK: write succeeded under READ ONLY')
       except psycopg.Error as e:
           print('ok:', type(e).__name__)"
   ```
   Expect a `psycopg` read-only error.
6. **Verify the portability rule.** `check_portable_numbers()` scans the ` ```python ` fences
   of every source against a `NOT_PORTABLE` regex covering `hypertable_size`,
   `pg_total_relation_size`, `pg_size_pretty`, `time.monotonic`/`perf_counter`,
   `datetime.now`/`today`, `pd.Timestamp.now`, `os.getpid`, `platform.*`, `gethostname`,
   `secrets.`. It exists because notebook 03's `hypertable_size()` megabytes matched locally
   and not on a runner while its *row counts* matched to the digit:
   ```bash
   uv run pytest tests/test_readme_claims.py -q -k portable
   grep -nE 'hypertable_size|pg_size_pretty|pg_total_relation_size|pg_relation_size' notebooks/src/*.md
   ```
   Expect no matches in prose-comment-excluded code fences. A match is a FAIL — a
   machine-dependent number in a gate that runs on CI hardware.
7. Confirm the built notebooks have **no** stored outputs:
   ```bash
   uv run python -c "
   import json, glob
   for f in sorted(glob.glob('notebooks/*.ipynb')):
       nb=json.load(open(f))
       code=[c for c in nb['cells'] if c['cell_type']=='code']
       print(f.split('/')[-1],
             'code', len(code),
             'exec_count', {c.get('execution_count') for c in code},
             'outputs', sum(len(c.get('outputs',[])) for c in code))"
   ```
   Expect every notebook: `exec_count {None}`, `outputs 0`.
8. `make notebooks-reset` behaviour — it must report the diff **before** rebuilding, so a
   silent revert is distinguishable from one that ate something:
   ```bash
   # dirty a notebook the way a Jupyter session would
   uv run python -c "
   import json
   nb=json.load(open('notebooks/01-meet-the-plant.ipynb'))
   for c in nb['cells']:
       if c['cell_type']=='code': c['execution_count']=7; c['outputs']=[{'output_type':'stream','name':'stdout','text':['dirty']}]
   json.dump(nb, open('notebooks/01-meet-the-plant.ipynb','w'), indent=1)"
   make notebooks-reset 2>&1 | tee evidence/C2/reset.log
   git status --short notebooks/   # expect: empty after reset
   ```
   ```bash
   uv run pytest tests/test_notebook_reset.py -q    # expect 4 tests
   ```
9. Confirm the `update_notebook_outputs` helper works — it re-executes and proposes what the
   run *actually* printed, replacing **only** `output` fences:
   ```bash
   uv run python -m tools.update_notebook_outputs --help
   uv run python -m tools.update_notebook_outputs notebooks/src/01-meet-the-plant.md | head -40
   ```
   Expect a unified diff. `--write` rewrites. Never use it to "fix" a failing gate without
   reading the diff.

**Expected result** — build is byte-deterministic; 40 checks pass (10 notebooks × 4); fingerprint matches exactly; read-only transaction enforced; no portable-unsafe numbers; all notebooks carry `execution_count: None` and zero outputs; `notebooks-reset` reports then restores.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** on: any drift; any cell error; any unbacked `output` fence
  line; any bolded prose number with no printed match; fingerprint mismatch; zero-row SQL
  fence; read-only write succeeding; a `NOT_PORTABLE` match; a notebook with stored outputs.
- FAIL `tests/test_notebook_reset.py` and `tests/test_notebook_kernels.py` are hard FAILs.
- The fingerprint mismatch deserves a specific note: `value_milli_sum` uses
  `sum(round(value * 1000)::bigint)`. A mismatch means the seed data changed, **not** a
  notebook bug. Cross-check A4 before investigating the notebook.

**Evidence** — build determinism (`git status` empty), the gate log with per-check lines, `notebook_data status`, the fingerprint got-vs-expected table, the read-only transcript, the `NOT_PORTABLE` grep, the per-notebook output census, the reset log, the `update_notebook_outputs` diff, pytest summaries.

**Risks / caveats**
- `make check` deliberately does **not** include notebooks — cost, not omission.
- Per-cell timeout is **600 s**. A cell that exceeds it reports as a check-2 failure, not a
  crash; read the log carefully to distinguish timeout from exception.
- A failure in check 3 or 4 may be a stale claim rather than broken code. Read
  `tools/update_notebook_outputs.py`'s guidance: "A *count* that moves is a finding. A *mean*
  that moves is a re-seed."
- Notebook 03's portability rule means **do not** assert on byte counts anywhere in C.

---

## C3. Notebook execution, outputs and expected numbers

**Objective** — each notebook executes under the venv, produces its figures, and the key printed numbers match the pinned fixture.

**Pre-conditions** — C2 passed.

**Steps**
1. Run all notebooks to HTML so you can read them without a browser:
   ```bash
   time make notebooks-read 2>&1 | tee evidence/C3/notebooks-read.log
   ls -la notebooks/read/    # expect one .html per built notebook
   ```
   `tools/notebook_read.py` uses `HTMLExporter(template_name="lab")` and `kernel_name="python3"`.
   Note it exists because nbconvert once resolved `python3` to anaconda.
2. Confirm figures were written to the right directory — the path is anchored to `__file__`
   because a relative default once produced a duplicate `notebooks/notebooks/figures/` tree
   that got committed:
   ```bash
   ls -la notebooks/figures/ | head -20
   test -d notebooks/notebooks && echo "BUG: duplicate tree exists" || echo "ok: no duplicate tree"
   ```
   Expected figures: `01_influent`, `01_diurnal`, `02_three_kinds`, `03_storm_tiers`,
   `04_valve_hourly_mean`, `04_valve_counts`, `05_acf`, `05_block_bootstrap`, `06_transforms`,
   `08_random_vs_rolling`, `08_storm_day`, `09_events`, `10_change_points`, `11_excess`.
3. Open each rendered HTML and walk the expected numbers. **Notebook 01 is the reference:**

   | Claim | Expected |
   |---|---|
   | total readings | **4,239,284** across **57** signals |
   | table row counts | `reading 4239284`, `reading_1m 185455`, `reading_1h 5882`, `signal 57`, `equipment 22`, `site 1`, `event 0` |
   | sampling | 9,576 signal-hours; **61.4 %** wrote something; median **43** rows; max **3,600**; span **3,600×** |
   | influent | **1798** m³/h mean vs effluent **1708** (a **5.0 %** deficit) |
   | ammonia | **21.8 → 7.9** mg/L, **64 %** removed |
   | air flow | **6188** m³/h |
   | power | **697** kW mean, **2032** peak |
   | storm | **2026-09-27 12:00 → 14:00**; **31.1 %** of the meter's week in **1.2 %** of the time |
   | diurnal | **1.74×** amplitude, peak at **07:00** |

   Verify the storm window independently:
   ```sql
   SELECT date_trunc('hour', ts) AS h, count(*)
   FROM reading WHERE signal_id='INFLUENT:FLOW:FLOW'
     AND ts BETWEEN '2026-09-27 12:00+00' AND '2026-09-27 14:00+00'
   GROUP BY 1 ORDER BY 1;
   ```
   Expect the hourly counts to spike at 12:00 and 13:00.
4. **Notebook 09 — ground truth and detector scoring.** This is the notebook with the most
   checkable numbers:
   ```sql
   -- do_sensor_drift / AERATION:AHU-1:DO / 2026-09-24 02:24 -> 03:54 (90 min)
   -- effluent_tss_stuck / EFFLUENT:FLOW:TSS / 2026-09-25 20:24 -> 22:24 (120)
   -- sensor_dead / INFLUENT:LIFT:CURRENT / 2026-09-27 11:02 -> 12:02 (60)
   -- wet_weather_storm / INFLUENT:LIFT:FLOW / 2026-09-27 12:00 -> 14:00 (120)
   ```
   Expected alarm-minutes per detector:
   `{'threshold': 105170, 'residual': 27613, 'silence': 3105, 'quality flag': 37}`
5. **Notebook 10 — CUSUM.** Expected error rates:
   - drift: alarm delay **6.0 min**, onset error **1.0 min**
   - TSS stuck and storm: **0.0 / 0.0**
6. **Notebook 11 — dose or flow attribution.** Expected:
   - **168** hourly means of 6 signals
   - correlation with TSS: raw `flow 0.901`, `air -0.117`; clock-removed `flow 0.986`, `air -0.092`
   - flow↔air `-0.330`; **VIF 1.12**
   - `np.linalg.lstsq` on 18 rows: flow **13.51** mg/L per 1000 m³/h, air **-6.426**
   - flow-only model: **14.62**
   - first-hour split: `24.5` flow + `2.7` air + `0.9` unexplained = `28.2` observed
   - counterfactual: **675** m³/h-hours extra air, **176** kWh, **16 of 16** hours low NH4
7. **Notebook 08 — rolling origin.** Expected: 1008 ten-minute steps, **18** empty, **864**
   usable rows, 60-min horizon. Random-fold MAE `[m3/h]`: yesterday **40.5**, neighbour in time
   **61.7**, linear **97.0**, persistence **255.1**.
8. **Notebook 05 — autocorrelation.** Expected measured lag-1 autocorrelation: **+0.92** on
   `PRIMARY:PRI-SCR-1:TORQUE`, **+0.999** on `UTILITY:SITE:PLANT_POWER`. The block bootstrap
   must use `np.random.default_rng(seed)` over a seed loop.
9. **Notebook 06 — stationarity.** The ADF test is hand-rolled OLS + F via `np.linalg.lstsq`
   (**no statsmodels**). Confirm:
   ```bash
   grep -cE 'statsmodels' notebooks/src/06-*.md    # expect 0
   ```
   Confirm `figure` for `06_transforms` exists and shows the differenced series.
10. **Verify notebook-internal assertions are structural, never exact-value.** This is a
    deliberate design rule — `assert acf1 > 0.5`, not `== 0.912`:
    ```bash
    grep -rhnE 'assert .*==\s*[0-9]' notebooks/src/*.md | head
    ```
    Expect only counts the generator guarantees (e.g. "exactly one row is Bad"). A new
    exact-value assertion on a floating-point measurement is a FAIL.
11. **Confirm `sns.lineplot` is never used.** It aggregates repeated x into mean + 95 % band,
    which erases the irregular sampling and implies inference over near-redundant samples:
    ```bash
    grep -rn 'lineplot' notebooks/src/ workshops/ml/src/    # expect: no output
    ```
    Also confirm `notebooks/_style.py` exposes the intended helpers:
    ```bash
    uv run python -c "import sys; sys.path.insert(0,'notebooks'); import _style; print(_style.__all__)"
    # expect: FIGURES, apply_style, describe, figure, format_time_axis, save, signal_meta, stamp, trend
    ```
12. Read at least one notebook interactively to prove the manual path works, then clean up:
    ```bash
    make notebooks-open
    # open 01-meet-the-plant.ipynb, Run All, confirm the last cell's figure renders
    # confirm the status bar names the .venv kernel
    make notebooks-stop
    git status --short notebooks/    # expect: DIRTY — the session wrote back
    make notebooks-reset && git status --short notebooks/    # expect: clean
    ```

**Expected result** — all notebooks execute; 14 figures; every listed number matches; no `statsmodels`; no `lineplot`; structural assertions only; a Jupyter session dirties the tree and `notebooks-reset` restores it.

**Pass/fail criteria**
- PASS: all numbers in steps 3–8 match; figures present in `notebooks/figures/` with no
  duplicate tree; notebook 03 contains no machine-dependent numbers; notebook 06 contains no
  `statsmodels` import; no `lineplot`; `notebooks-reset` returns the tree to clean.
- **Hard FAIL** if any notebook cell errors; if a figure is missing; if `notebooks/notebooks/`
  exists; if a committed notebook has stored outputs.
- **Tolerate ±1 %** on continuous figures (means, correlations) — they are computed from the
  pinned seed so they should match tightly, but rounding in prose is checked separately by
  check 4. If a value is off by more than 1 %, investigate rather than adjusting the plan.
- `sns.lineplot` present is a **FAIL**, and a *design* failure: it would misrepresent the
  sampling.

**Evidence** — `notebooks-read` log, the figures listing, the duplicate-tree check, per-notebook rendered-HTML screenshots of the key cell, the storm SQL, the four fingerprint-adjacent verifications, the `statsmodels`/`lineplot` greps, the `_style.__all__` output, the post-session `git status` (dirty) and post-reset (clean).

**Risks / caveats**
- `make notebooks-read` executes every notebook again — roughly another 6 minutes. Total for
  C2+C3 ≈ 15 min.
- Figure paths are absolute and `__file__`-anchored by design; do not "fix" them to relative.
- If a figure is missing but the notebook passed, check `_style.save()` wrote to
  `notebooks/figures/` and not to a stale duplicate tree.
- `notebooks/read/*.html` and `notebooks/figures/*.png` are gitignored. If `git status` shows
  them as untracked, `.gitignore` has drifted — a finding.

---

# D. ML Workbooks (training / inference pipelines)

## D0. Area-level notes

**There is no model registry and no persisted model anywhere.** No `joblib`, no `pickle`, no
`.pkl`/`.joblib`, no model artifact directory. Every model is constructed, fitted and
discarded inside a notebook cell or a measurement script. `torch` is deliberately **not** a
dependency (~200 MB for an unwritten day-2 tier).

So the brief's "model registration" and "real-time inference" sections map to nothing that
exists. This plan tests what is actually there:

| Brief asks for | Reality | Tested by |
|---|---|---|
| Training jobs | 6 workshop notebooks + 2 measurement scripts, real sklearn estimators | D2 |
| Model evaluation | accuracy/recall/precision/F1/MAE, noise-to-effect ratios | D3 |
| Model registration | **does not exist** — see Q8 | D7 (asserts absence) |
| Inference | batch-only, in-notebook `predict` | D4 |
| Drift detection | `IsolationForest`, PCA reconstruction error, `base_24`/`n_over_base` | D5 |
| Resource usage | documented scaling curves (2→3→18 wk) | D6 |

Two datasets, no database:

- **`workshops/ml/dataset.csv`** — 3-week default, **28,728 rows** (= 57 × 504 hours),
  **22 positives**, 3.3 MB, <2 min, ~1 GB peak, 854 MB DB.
- **`workshops/ml/dataset-18wk.csv`** — 172,368 rows, **419** positives, 19.6 MB, 46 min, ~19 GB.

Feature list (`workshops/ml/build_dataset.py::FEATURES`, 9 features):
`mean, min, max, n, row_written, value_is_null, base_24, n_over_base, missing_streak`.
Labels (`LABELS`): `is_fault, fault_type, is_storm, week`.
Row states (`ROW_STATES`): stored NULL vs absent hour — distinguished by `row_written` (0 vs 1),
with `value_is_null` 1 in both cases.

`base_24` uses `shift(1).rolling(window_h, min_periods=6).median()` — **past only**. That is the
whole point, and it is enforced by `tests/test_workshop_dataset.py`.

---

## D1. Workshop environment, seed and panel build

**Objective** — the workshop database seeds deterministically, the panel is built, and its shape is exactly right.

**Pre-conditions**
```bash
make setup && make sync
uv sync --extra workshop     # scikit-learn >= 1.5
```

**Steps**
1. Confirm the ML extra resolves:
   ```bash
   uv run --extra workshop python -c "import sklearn; print(sklearn.__version__)"
   uv run --extra workshop python -c "import torch" 2>&1 | tail -1   # expect ModuleNotFoundError
   ```
2. **Seed and build in one command:**
   ```bash
   time make workshop 2>&1 | tee evidence/D1/workshop.log
   ```
   This is `workshop-seed` + `workshop-dataset`. Variables: `WORKSHOP_DB=wwtp_ml`,
   `WORKSHOP_WEEKS=3`, `WORKSHOP_HOURS=36`, `WORKSHOP_END=2026-09-29T00:00:00Z`,
   `WORKSHOP_SAMPLE_INTERVAL=1`, `WORKSHOP_PANEL=workshops/ml/dataset.csv`.
3. **Verify the seed command passes `--database` as an argument, never as
   `POSTGRES_DB=` in the environment.** This is a real hazard: `tools/py.sh` sources `.env`
   *after* the inherited environment, so a prefix assignment is overwritten by `.env`'s `wwtp`
   and the command would **seed the plant's database with `--reset`**. Confirm:
   ```bash
   grep -n 'workshop-seed:' -A6 Makefile | grep -E 'seed.main|--database|POSTGRES_DB'
   # expect --database $(WORKSHOP_DB); expect NO "POSTGRES_DB=" prefix
   ```
   Then prove the plant database is untouched:
   ```sql
   SELECT count(*) FROM reading;   -- in wwtp: must still be 4239284 (or your A4 value)
   ```
4. **Verify the panel shape** — 28,728 = 57 × 504 hours:
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   p=_data.load_panel()
   print(p.shape)
   print('signals', p.signal.nunique(), 'hours', p.bucket.nunique())
   print('positives', int(p.is_fault.sum()))
   print('bytes', __import__('os').path.getsize('workshops/ml/dataset.csv'))"
   ```
   **Expect:** `(28728, …)`, 57 signals, 504 hours, **22** positives, ~3.3 MB.
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data; _data.summarise(_data.load_panel())" | tee evidence/D1/summarise.txt
   ```
   Expect a **"majority-class accuracy"** line and per-class row states (the test asserts this
   key exists by name).
5. **Verify the panel is dense.** Every `(signal, hour)` pair must be present even when nothing
   was stored — this is what makes "no data" distinguishable from "bad data":
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data, pandas as pd
   p=_data.load_panel()
   full=57*504
   print('rows', len(p), 'expected', full, 'dupe pairs', int(p.duplicated(['signal','bucket']).sum()))"
   # expect: rows 28728, expected 28728, dupe pairs 0
   ```
6. **Distinguish an absent hour from a stored NULL:**
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   p=_data.load_panel()
   print(p.groupby(['row_written'])['value_is_null'].agg(['count','sum']))"
   # expect: row_written=0 -> value_is_null all 1; row_written=1 -> mostly 0
   ```
   `row_written 0` and `row_written 1 with value_is_null 1` are the two distinct states.
7. **Verify `base_24` uses only the past.** Structural proof — mutate hour *t* and confirm
   `base_24` at hour *t* does not move:
   ```bash
   uv run --extra workshop python -m workshops.ml.measure_long_window --help | head -20
   uv run pytest tests/test_measure_long_window.py -q    # expect 17 tests
   ```
   The test asserts `base_24` at hour *t* is invariant to hour *t*, and that the source
   contains `shift(1).rolling` and, separately, `shift(-1).rolling` with `base_24_fwd` in
   `FORWARD`. Read the diff directly:
   ```bash
   grep -n 'shift(1).rolling\|shift(-1).rolling\|base_24_fwd\|FORWARD' workshops/ml/measure_long_window.py
   ```
   Note the test uses a **6-hour** window with 4 of 6 values changed — a 24-hour window cannot
   fail this test, because a median of 24 sits between the 12th and 13th value. So `base_24` is
   a *name*, not the window size.
8. **Verify fault windows are half-open `[start, end)`** and the storm is not a fault:
   ```bash
   uv run pytest tests/test_workshop_dataset.py -q    # expect 15 tests, no database needed
   ```
   The tests also assert `FEATURES` **cannot see the answer** — `is_fault`, `fault_type`,
   `is_storm`, `week`, `bucket` are all excluded:
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   from build_dataset import FEATURES; import _data
   print(sorted(FEATURES)); print(sorted(_data.LABELS))
   print('leak:', set(FEATURES) & set(_data.LABELS) | {'bucket'})"
   # expect: leak: empty set
   ```
9. **Verify the panel paths are protected structurally.** `workshop-long` writes its own
   database *and* its own CSV, so it cannot overwrite the panel every number in `TRAINER.md`
   was measured on:
   ```bash
   uv run pytest tests/test_workshop_panel_paths.py -q    # expect 18 tests
   grep -n 'WORKSHOP_PANEL\|WORKSHOP_LONG_WEEKS\|LONG_DB\|LONG_PANEL' Makefile | head
   ```
   Expect `WORKSHOP_LONG_WEEKS ?= 8`, `LONG_DB ?= wwtp_ml25`,
   `LONG_PANEL = workshops/ml/dataset-$(WORKSHOP_LONG_WEEKS)wk.csv`.
10. **Confirm `TRAINER.md` records the measured numbers** the tests assert:
    ```bash
    grep -nE '419|0\.615|\+0\.152' workshops/ml/TRAINER.md
    grep -nE 'not a power law|interrupt' workshops/ml/README.md
    ```
    Expect `419`, `0.615`, `+0.152` present; the three rows-per-day figures present together
    with `"not a power law"`; and the `23 GB` figure inside a block containing "interrupt".

**Expected result** — sklearn present, torch absent; panel `(28728, …)`, 57 × 504, 22 positives, 3.3 MB; dense with no duplicate `(signal, bucket)`; `base_24` past-only; no label leakage; `wwtp` untouched; 15 + 17 + 18 tests pass.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if the panel is not 28,728 rows, if `is_fault` or `week`
  appears in `FEATURES`, if `wwtp`'s reading count changed, or if a `shift(-1)` baseline leaks
  into `base_24`.
- **Hard FAIL** `tests/test_workshop_dataset.py`, `tests/test_measure_long_window.py` or
  `tests/test_workshop_panel_paths.py`.
- If positives ≠ 22, check `WORKSHOP_HOURS=36` and `WORKSHOP_END` before assuming a bug.

**Evidence** — `workshop.log` with timing, the panel shape/size/positives output, the
`summarise` output, the density and duplicate-pair check, the `row_written` × `value_is_null`
crosstab, the 45 pytest results, the leakage intersection, the `wwtp` count before/after, the
`TRAINER.md`/`README.md` greps.

**Risks / caveats**
- Peak memory ~1 GB for the 3-week build; the 18-week build needs ~19 GB disk. Check free space
  before D6.
- `dataset*.csv` are gitignored. A `git status` showing them as untracked means `.gitignore`
  drifted.
- `--storm-after` arms **one** storm at the end of the window, so the final week is
  contaminated. That is a *known, recorded* limitation — the fix is to hold out a week from
  the *middle*. Do not report it as a new defect, and do use it when interpreting D3's
  held-out numbers.

---

## D2. Training execution — every estimator, every parameter

**Objective** — all six workshop notebooks train with the documented hyperparameters and complete within the CI timeout.

**Pre-conditions** — D1.

**Steps**
1. Confirm the estimators and hyperparameters from source before running:
   ```bash
   for f in workshops/ml/src/0*.md; do
     echo "== $f"; grep -nE 'RandomForest|IsolationForest|PCA|DummyClassifier|LinearRegression|n_estimators|random_state|n_jobs|contamination|n_components|quantile' "$f"
   done
   ```
   **Expected inventory:**
   | Notebook | Estimator | Hyperparameters |
   |---|---|---|
   | `01` | `DummyClassifier` | `strategy="most_frequent"` |
   | `01` | `RandomForestClassifier` | `n_estimators=200, random_state=0, n_jobs=-1` |
   | `03` | `RandomForestClassifier` | `n_estimators=300, random_state=0, n_jobs=-1` (held-out week) |
   | `03` | `RandomForestClassifier` | `n_estimators=200, random_state=0, n_jobs=-1` (20-fold sweep) |
   | `04` | `RandomForestClassifier` | `n_estimators=300, random_state=0, n_jobs=-1` |
   | `05` | `IsolationForest` | `contamination=0.001` and `0.01`, `n_estimators=300, random_state=0, n_jobs=-1` |
   | `05` | `PCA` | `n_components=3`, reconstruction error at quantile 0.999 / 0.99 |
   | `06` | `LinearRegression`, `RandomForestRegressor` | `n_estimators=200, random_state=0, n_jobs=-1` |
2. **Verify every estimator is seeded.** Unseeded training would break the reproducibility
   claim:
   ```bash
   grep -rn 'random_state' workshops/ml/src/ | grep -v 'random_state=0'   # expect: no output
   grep -rnE 'RandomForest|IsolationForest|PCA|DummyClassifier|LinearRegression' workshops/ml/src/ \
     | grep -v 'random_state' | grep -vE 'import|^\s*#'                  # review each hit
   ```
   Also confirm `np.random.default_rng(...)` seeds:
   ```bash
   grep -rn 'default_rng' workshops/ml/src/ notebooks/src/ | head
   # expect: seed loops in 03, np.random.default_rng(20260929) in 08
   ```
3. Run the gate:
   ```bash
   time make workshop-notebooks 2>&1 | tee evidence/D2/workshop-notebooks.log
   ```
   The CI job is `workshop-notebooks` with `timeout-minutes: 40`. Expect 6 notebooks × 3
   checks (drift, executes, output fences) — the workshop track is **not** DB-backed, so no
   fingerprint check and no SQL-fence check.
   ```bash
   grep -icE 'FAIL|error|Traceback' evidence/D2/workshop-notebooks.log   # expect 0
   ```
4. **Confirm the workshop track really is CSV-backed and the analyst track is not** — asserted
   by a test:
   ```bash
   uv run python -c "
   from tools.check_notebooks import DATABACKED_TRACKS
   print('notebooks' in DATABACKED_TRACKS, 'workshop' in DATABACKED_TRACKS)"
   # expect: True False
   ```
5. Verify an unknown track refuses rather than silently building nothing:
   ```bash
   uv run python -m tools.build_notebooks --track nonsense 2>&1 | tail -2
   # expect SystemExit: unknown notebook track
   uv run pytest tests/test_notebook_kernels.py -q -k track    # covers this
   ```
6. **Record per-notebook runtime** and peak memory, since D6 depends on it:
   ```bash
   /usr/bin/time -l uv run python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   from nbclient import NotebookClient
   import nbformat
   nb=nbformat.read('workshops/ml/05-unsupervised.ipynb', as_version=4)
   NotebookClient(nb, timeout=1800, kernel_name='python3', allow_errors=False).execute()"
   ```
   Repeat per notebook. Record wall-clock and `maximum resident set size`.
7. Screenshot each notebook's headline result in `make workshop-read`-style HTML, or open
   interactively:
   ```bash
   make workshop-open       # JupyterLab on port 8898, --notebook-dir=workshops/ml
   make workshop-url        # prints the URL with token
   make workshop-stop
   ```
   Port **8898**, not 8899.

**Expected result** — all 6 notebooks train and pass 3 checks each; every estimator has `random_state=0`; runtime within the 40-min CI budget; `DATABACKED_TRACKS` excludes `workshop`; unknown track raises `SystemExit`.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** on any cell error; on any unseeded estimator; on any check
  failing; on total runtime exceeding 40 minutes (the CI budget).
- **Record but do not fail** if a single notebook takes > 20 min — flag it as a CI budget
  risk.
- The `--track nonsense` case must raise, not build an empty set.

**Evidence** — the hyperparameter grep, the seeded-estimator check, `workshop-notebooks.log` with timing, the `DATABACKED_TRACKS` print, the `SystemExit` message, a per-notebook runtime/memory table, six result screenshots.

**Risks / caveats**
- `n_jobs=-1` uses all cores. On a shared or container-limited runner this can cause
  timeouts or thrash. Record core count: `sysctl -n hw.ncpu` / `nproc`.
- A separate CI job exists for workshop vs analyst notebooks specifically so a workshop failure
  does not turn the analyst series red. Do not merge them locally when measuring time.

---

## D3. Model evaluation — metrics, thresholds, confusion

**Objective** — the six notebooks' headline metrics match `TRAINER.md`, and the base rates are as extreme as claimed.

**Pre-conditions** — D2.

**Steps**
1. Establish the base rate first. **Accuracy is never the point in this project** — it moves
   0.9993 → 0.833 across the six notebooks:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   p=_data.load_panel()
   print('rows', len(p), 'positives', int(p.is_fault.sum()))
   print('majority-class accuracy', round(1 - p.is_fault.sum()/len(p), 4))"
   # expect: rows 28728, positives 22, majority-class accuracy ~0.9992
   ```
2. Walk the six notebooks' expected results:
   | # | Notebook | Expected |
   |---|---|---|
   | 01 | `the-metric-turn` | `DummyClassifier` accuracy **0.9993**; forest **0.9995** with recall **0.4286**, precision **0.7500**, F1 **0.5455** |
   | 02 | `the-dense-panel` | a query keeps **10 of 22** fault hours |
   | 03 | `the-baseline-and-the-noise-floor` | split noise **42×** the effect (separation 29×: 596 rows vs 20) |
   | 04 | `the-forward-window` | F1 **0.444 → 0.833** with a forward window |
   | 05 | `unsupervised` | **0 of 22** fault hours; **285 of 288** flags are `INFLUENT:LIFT:STARTS` |
   | 06 | `predictive` | a random split makes the model **5.3×** better (MAE **0.0627** vs **0.3304**) |
3. **The single most important result** — the baseline earns its keep:
   | | naive | with baseline | delta |
   |---|---|---|---|
   | F1 | **0.463** | **0.615** | **+0.152** |
   | separation | 29.3× | 25.6× | |
   | held-out final week | 0.438 | 0.579 | |
   ```bash
   grep -nE '0\.463|0\.615|0\.152|0\.438|0\.579|29\.3|25\.6' workshops/ml/TRAINER.md
   ```
   All six must be present.
4. **Verify `row_written` erodes under the longer recurrence** — a genuinely subtle
   measurement, and the reason the long window exists:
   | fault | `row_written` at 12 h recurrence |
   |---|---|
   | `effluent_tss_stuck` | 0 % → **5 %** |
   | `sensor_dead` | 0 % → **7 %** |
   ```bash
   grep -nE 'row_written|5 ?%|7 ?%' workshops/ml/TRAINER.md | head
   ```
5. **Verify the confusion structure for notebook 01.** Compute it independently:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data, numpy as np
   from sklearn.ensemble import RandomForestClassifier
   from sklearn.dummy import DummyClassifier
   from sklearn.metrics import accuracy_score, recall_score, precision_score, f1_score
   from build_dataset import FEATURES
   p=_data.load_panel()
   tr, te = _data.held_out(p, last=1)
   Xtr, ytr = tr[list(FEATURES)], tr['is_fault'].astype(int)
   Xte, yte = te[list(FEATURES)], te['is_fault'].astype(int)
   d=DummyClassifier(strategy='most_frequent').fit(Xtr,ytr)
   r=RandomForestClassifier(n_estimators=200, random_state=0, n_jobs=-1).fit(Xtr,ytr)
   for name,m in (('dummy',d),('forest',r)):
       yp=m.predict(Xte)
       print(name, 'acc', round(accuracy_score(yte,yp),4),
             'rec', round(recall_score(yte,yp,zero_division=0),4),
             'prec', round(precision_score(yte,yp,zero_division=0),4),
             'f1', round(f1_score(yte,yp,zero_division=0),4))"
   ```
   Expect `forest` F1 ≈ 0.5455 and recall ≈ 0.4286, i.e. **3 of 7** held-out positives. Note
   `held_out(p, last=1)` holds out the **final week**, which is storm-contaminated by design
   (D1 risk) — so a small discrepancy from `TRAINER.md` may be explained by that, not by a bug.
6. **Verify notebook 05's failure is real, not a broken run.** `0 of 22` fault hours with 285
   of 288 flags on one signal is the *point* — unsupervised methods find the high-variance
   signal, not the fault. Confirm:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   p=_data.load_panel()
   print(p.groupby('signal').is_fault.sum().sort_values(ascending=False).head(5))"
   # expect INFLUENT:LIFT:STARTS to dominate
   ```
7. **Verify the 20-fold sweep's split definition.** The measurement must compare `naive_scores`
   against `added_scores` and must **not** fold in the forward window — doing so yields 49×
   instead of notebook 03's 42×. The tests pin the column lists `NAIVE`/`ADDED`:
   ```bash
   grep -nE '^NAIVE|^ADDED|naive_scores|added_scores|42|49' workshops/ml/measure_long_window.py | head
   uv run pytest tests/test_measure_long_window.py -q -k noise
   ```
8. **Verify the verdict bands** are parametrised at 1.0 / 2.0 / 5.0 / 10.0 / 42.0, with
   `POWER_BELOW == 2.0` and `NO_POWER_ABOVE == 10.0`:
   ```bash
   uv run python -c "
   from workshops.ml.measure_long_window import POWER_BELOW, NO_POWER_ABOVE, verdict
   print(POWER_BELOW, NO_POWER_ABOVE); print(verdict(13.0)[:40]); print(verdict(1.0)[:40])"
   # expect: 2.0 10.0 ; "NO POWER ..." ; "POWER ..." something
   ```
9. Confirm the labels are disjoint where they should be — but **not** between storm and fault,
   because one fault deliberately coincides with the storm:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   p=_data.load_panel()
   print('storm & fault rows:', int(((p.is_storm)&(p.is_fault)).sum()))
   print('fault_type values:', sorted(p.fault_type.unique()))"
   ```
   `do_sensor_drift` on `AERATION:AHU-1:DO` 2026-09-27 12:00–13:00 sits inside the storm's
   12:00–14:00, so a non-zero overlap is **expected**, not a bug.
10. **Three of the six beats are corrections to the original plan.** If your run reproduces the
    documented numbers, note this; a run where a "win" comes out as a loss is a strong signal
    to investigate.

**Expected result** — all six notebooks' metrics match; the base rate is ~0.999; the baseline lifts F1 by +0.152; notebook 05's single-signal collapse is reproduced; the noise ratio is 42× not 49×; storm/fault overlap is non-zero by design.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if the naive F1 is not ≈ 0.463 or the baseline F1 not ≈ 0.615;
  if the split-noise ratio is 49× (that means the forward window leaked into the comparison);
  if `accuracy` is used as the headline metric anywhere; if notebook 05 finds fault hours
  (that would mean the label or feature set is wrong).
- Tolerate small F1 drift (±0.02) attributable to the storm-contaminated held-out week; record
  it rather than adjusting the plan.

**Evidence** — the base-rate printout, `TRAINER.md` grep for all six numbers, the `row_written` table, the independent confusion-matrix computation, the signal-dominance check, the `NAIVE`/`ADDED` diff, the verdict-band print, the storm/fault overlap count, a table of metric-vs-expected.

**Risks / caveats**
- `held_out(p, last=1)` and the storm contamination interact. Document which week you held out.
- `accuracy_score`, `recall_score`, `precision_score`, `f1_score` are all called with
  `zero_division=0`. On the analyst side, `mean_absolute_error` has no such guard — a NaN MAE
  is a real finding.
- The 3-week window has only **22 positives**, and 4 of them land in a 20 % fold. That is 1–2
  positives per fold — an extremely thin basis for any fold statistic. Do not read stability
  into fold-to-fold variation.

---

## D4. Inference — batch only, input and output schema

**Objective** — the models produce usable predictions on the documented feature schema, and the failure modes are understood.

**Pre-conditions** — D2, D3.

**Steps**
1. **Establish that inference is batch-only and in-process.** There is no serving endpoint, no
   model file, no `predict` API. Confirm nothing exists:
   ```bash
   find . -name '*.pkl' -o -name '*.joblib' -o -name '*.onnx' -o -name 'model*' -not -path './.git/*' | grep -v node_modules | head
   # expect: no model artifacts
   grep -rnE 'pickle|joblib|mlflow|bentoml|torch.save' --include='*.py' --include='*.md' . | grep -v '.venv' | head
   # expect: no output
   ```
   Also confirm no serving extras:
   ```bash
   uv run python -c "import importlib.util as u; print('mlflow', u.find_spec('mlflow')); print('bentoml', u.find_spec('bentoml'))"
   # expect: None None
   ```
2. **Verify the input schema is enforced and versioned.** `FEATURES` is the contract; the
   labels must not be present:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   from build_dataset import FEATURES, COLUMNS, META
   print('FEATURES', list(FEATURES)); print('n', len(FEATURES))
   print('COLUMNS', list(COLUMNS)); print('META', list(META))"
   ```
   Expect 9 features: `mean, min, max, n, row_written, value_is_null, base_24, n_over_base,
   missing_streak`.
3. **Verify a feature-missing prediction fails loudly** rather than silently imputing:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   from sklearn.dummy import DummyClassifier
   from build_dataset import FEATURES
   p=_data.load_panel(); tr,te=_data.held_out(p,last=1)
   m=DummyClassifier(strategy='most_frequent').fit(tr[list(FEATURES)], tr['is_fault'].astype(int))
   try:
       m.predict(te[[c for c in FEATURES if c!='base_24']])
       print('FAIL: missing column accepted silently')
   except Exception as e:
       print('ok:', type(e).__name__, str(e)[:80])"
   ```
   Expect a sklearn `KeyError`/`ValueError`, not a silent drop.
4. **Verify NaN handling is explicit, not accidental.** `base_24` is NaN for the first
   `min_periods` rows and `n_over_base` is NaN (not `inf`) when `base_24 == 0`:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data, numpy as np
   p=_data.load_panel()
   print('base_24 NaN', int(p.base_24.isna().sum()))
   print('n_over_base inf', int(np.isinf(p.n_over_base).sum()))
   print('first 6 base_24 all NaN:', bool(p.groupby('signal').base_24.head(6).isna().all()))"
   ```
   **Expect `n_over_base inf == 0`.** `inf` there is a documented failure the test guards
   against — an instrument coming back online must not produce an infinite ratio.
5. **Verify a wholly-empty signal behaves.** `base_24 == 0`, first 6 rows NaN,
   `n_over_base` NaN:
   ```bash
   uv run pytest tests/test_workshop_dataset.py -q -k empty
   ```
6. **Batch inference cost and output shape:**
   ```bash
   uv run --extra workshop python -c "
   import sys, time; sys.path.insert(0,'workshops/ml')
   import _data
   from sklearn.ensemble import RandomForestClassifier
   from build_dataset import FEATURES
   p=_data.load_panel(); tr,te=_data.held_out(p,last=1)
   m=RandomForestClassifier(n_estimators=200,random_state=0,n_jobs=-1).fit(tr[list(FEATURES)],tr['is_fault'].astype(int))
   t=time.time(); yp=m.predict(te[list(FEATURES)]); el=time.time()-t
   print('n', len(yp), 'positives predicted', int(yp.sum()), 'seconds', round(el,3))"
   ```
   Record `n` (the held-out week ≈ 57 × 168 = 9,576 rows) and elapsed time.
7. **Missing-streak semantics.** `missing_streak` is a *within-signal* run length that resets:
   ```bash
   uv run pytest tests/test_workshop_dataset.py -q -k streak
   ```
8. **Confirm there is no real-time or streaming inference path:**
   ```bash
   grep -rniE 'kafka|redis|mqtt|websocket|celery|fastapi.*model|/predict' workshops/ml/ | grep -v Binary | head
   # expect: no output
   ```

**Expected result** — no model artifacts or registry; 9-feature input schema; labels excluded; missing column raises; `n_over_base inf == 0`; batch predict over ~9,576 rows in well under 1 s; no streaming path.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if any `.pkl`/`.joblib` exists (that would contradict the stated
  design); if a missing feature column is silently imputed; if `n_over_base` contains `inf`;
  if any label column is in `FEATURES`.
- The "no artifact" assertions are as important as the positive ones — if someone has added a
  registry, the brief's model-registration requirements become testable and this plan needs
  extending (Q8).

**Evidence** — the artifact `find` and `grep` outputs, the `FEATURES`/`COLUMNS`/`META` dump, the missing-column transcript, the NaN/inf census, the batch timing, the empty-signal and streak pytest results, the streaming grep.

**Risks / caveats**
- "Inference" here means `predict()` inside a notebook cell on a `reading_1m`-derived panel.
  Nothing is persisted, so nothing to serve. Do not write pass/fail criteria against a serving
  layer that does not exist.
- If you add a registry as part of this engagement, D7 must be rewritten, not just extended.

---

## D5. Drift and quality monitoring

**Objective** — the drift machinery detects what it claims, and the known-eroding feature is correctly understood.

**Pre-conditions** — D2. For the long-window test, D6.

**Steps**
1. **The panel's own drift features.** `base_24`, `n_over_base` and `missing_streak` are the
   drift monitors. `n_over_base` is the ratio of recent sample count to the 24 h baseline:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   p=_data.load_panel()
   print(p.n_over_base.describe())"
   ```
   A signal trending to `n_over_base ≈ 0` is going silent — that is the drift signal.
2. **Verify `n_over_base` collapses for the silent faults.** This is the quantified finding:
   `row_written` for the silent faults goes 0 % → 5 % (`effluent_tss_stuck`) and 0 % → 7 %
   (`sensor_dead`) at 12 h recurrence — "eroded by the recurrence that bought the sample size".
   ```bash
   grep -nE 'row_written|0 ?%.*5 ?%|0 ?%.*7 ?%' workshops/ml/TRAINER.md
   uv run pytest tests/test_workshop_dataset.py -q
   ```
3. **Verify the unsupervised detectors and their known failure.** Notebook 05 uses
   `IsolationForest` at `contamination=0.001` and `0.01`, and `PCA(n_components=3)` with
   reconstruction error at quantile 0.999 / 0.99:
   ```bash
   grep -nE 'contamination|n_components|quantile|IsolationForest|PCA' workshops/ml/src/05-unsupervised.md
   ```
   Expected outcome: **0 of 22** fault hours detected; **285 of 288** flags are
   `INFLUENT:LIFT:STARTS`. Reproduce:
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data
   from sklearn.ensemble import IsolationForest
   from build_dataset import FEATURES
   p=_data.load_panel()
   X=p[list(FEATURES)].fillna(0)
   m=IsolationForest(contamination=0.001, n_estimators=300, random_state=0, n_jobs=-1).fit(X)
   flags=(m.predict(X)==-1)
   print('flags', int(flags.sum()))
   print(p[flags].signal.value_counts().head(3))"
   ```
   Expect ~288 flags with `INFLUENT:LIFT:STARTS` dominating. **A detector that finds the
   faults here would contradict `TRAINER.md`** — the recorded conclusion is that unsupervised
   detection does not work on this panel, and reproducing the failure is the pass condition.
4. **Verify PCA reconstruction error:**
   ```bash
   uv run --extra workshop python -c "
   import sys; sys.path.insert(0,'workshops/ml')
   import _data, numpy as np
   from sklearn.decomposition import PCA
   from build_dataset import FEATURES
   p=_data.load_panel()
   X=p[list(FEATURES)].fillna(0).values
   Xs=(X-X.mean(0))/X.std(0)
   z=PCA(n_components=3, random_state=0).fit_transform(Xs)
   err=((Xs-z@PCA(n_components=3, random_state=0).fit(Xs).components_.T)**2).sum(1)**0.5
   print('q0.999', np.quantile(err,0.999), 'q0.99', np.quantile(err,0.99), 'max', err.max())"
   ```
   Record the quantiles; the notebook prints its own.
5. **Cross-check with the SQL-layer detectors.** Notebook 09's four detectors scored against
   injected ground truth (from C3.4): `threshold 105170`, `residual 27613`,
   `silence 3105`, `quality flag 37` alarm-minutes. The `silence` and `quality flag` detectors
   are the drift/quality monitors on the analyst side. Confirm the notebook's ground truth
   matches the database:
   ```sql
   SELECT signal_id, min(ts), max(ts), count(*)
   FROM reading WHERE quality = 2 AND value IS NULL GROUP BY 1;
   -- expect INFLUENT:LIFT:CURRENT around 2026-09-27 11:02 -> 12:02
   ```
6. **Verify the alarm engine's quality-flag rule sees the same condition.** Rule 12,
   `lift_current_silence`, `quality_flag`, `min_quality=2`, `for_s=60`:
   ```bash
   uv run pytest tests/test_alarm_detectors.py -q -k quality
   ```
7. **Verify no production drift monitor exists** — drift is measured in notebooks, not
   continuously:
   ```bash
   grep -rniE 'drift|psi|population stability' --include='*.py' --include='*.yaml' . \
     | grep -v '.venv\|node_modules\|.git' | head
   ```
   If a monitoring service exists that the exploration missed, this is a positive finding —
   record it.

**Expected result** — drift features present; `n_over_base` collapses for silent faults; `IsolationForest` flags ~288 rows all on `INFLUENT:LIFT:STARTS` and finds **0** fault hours; PCA quantiles match the notebook; notebook 09's silence/quality detector alarm-minutes match the database; no continuous drift monitor.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if the unsupervised detectors *do* find the fault hours
  (inconsistent with `TRAINER.md` — investigate whether `FEATURES` leaked), if
  `n_over_base` contains `inf`, or if notebook 09's alarm-minutes disagree with C3.4.
- The "0 of 22" result is a **pass**, not a failure. This is the most commonly misread number
  in the project; state it explicitly in your verdict so it is not "fixed" later.

**Evidence** — `n_over_base` describe(), the `TRAINER.md` `row_written` figures, the IsolationForest flag count and top signals, the PCA quantiles, the SQL quality-2 query, the quality-detector pytest results, the drift grep.

**Risks / caveats**
- `fillna(0)` in a drift context is a modelling choice, not a correctness one. Record whatever
  the notebook actually does rather than assuming.
- Notebook 09's ground truth has **four** events; two of them (drift, storm) coincide or
  overlap. Small-sample interpretation only.

---

## D6. Resource usage and scaling behaviour

**Objective** — training and seeding resource costs match the documented curves, and the power verdict changes as designed.

**Pre-conditions** — D1. **Check disk before starting: the 18-week build needs ~19 GB.**

**Steps**
1. Record host capacity as the baseline for any comparison:
   ```bash
   sysctl -n hw.ncpu hw.memsize 2>/dev/null || nproc && free -h
   df -h . | tail -1
   ```
2. **Time and measure the 3-week build** (already done in D1, but re-measure with resources):
   ```bash
   /usr/bin/time -l make workshop 2>&1 | tee evidence/D6/workshop-3wk.log
   ```
   Expect: 28,728 panel rows, 22 positives, 3.3 MB CSV, **< 2 min**, **~1 GB** peak RSS,
   **854 MB** database.
3. **Build the 8-week window** and confirm the documented curve:
   ```bash
   time make workshop-long WORKSHOP_LONG_WEEKS=8 WORKSHOP_HOURS=36 2>&1 | tee evidence/D6/workshop-8wk.log
   ```
   Expect: 33,843,069 readings, 76,608 panel rows, 62 positives, 12 in a 20 % fold,
   **13×** noise/effect, 8.7 MB CSV, **~15 min**, **~8 GB** peak, 1,040 MB database.
4. **Optional, expensive: the 18-week window.** ~46 min and ~19 GB:
   ```bash
   time make workshop-long WORKSHOP_LONG_WEEKS=18 WORKSHOP_HOURS=12 2>&1 | tee evidence/D6/workshop-18wk.log
   ```
   Expect: 76,170,570 readings, 172,368 rows, **419** positives, 84 in fold,
   **1×** noise/effect, 19.6 MB CSV, **46 min**, **~19 GB** peak.
   ```bash
   grep -nE '23 ?GB|interrupt|killed' workshops/ml/README.md
   ```
   The 23 GB figure must sit inside a block saying "interrupt"/"killed" — a test asserts it.
5. **Run the measurement over the long window** and confirm the power verdict:
   ```bash
   uv run --extra workshop python -m workshops.ml.measure_long_window \
     --panel workshops/ml/dataset.csv --panel workshops/ml/dataset-18wk.csv 2>&1 \
     | tee evidence/D6/measure-long.log
   ```
   Expect the verdict to change from `42x` / `NO POWER` (3-week) to `1x` / `POWER`
   (18-week), with powered F1 **0.615** vs naive **0.463**.
6. **Verify the rows-per-day figures and the explicit "not a power law" note.** The three
   measurements are 142,654 → 604,341 → 557,522 rows/day, and the README states explicitly
   that this is **"not a power law"** because two points had been read as "superlinear and
   unexplained":
   ```bash
   grep -nE '142,?654|604,?341|557,?522|not a power law' workshops/ml/README.md
   uv run pytest tests/test_workshop_panel_paths.py -q
   ```
   All four strings must be present. A doc that omits the caveat is a FAIL — the caveat is
   what stops the next reader re-deriving the same false conclusion.
7. **Verify `storage.seed.schedule.minimum_interval_s() == 7200.0`** — the 12 h recurrence
   used in the long windows is a floor, not a preference:
   ```bash
   uv run python -c "from storage.seed.schedule import minimum_interval_s; print(minimum_interval_s())"
   # expect: 7200.0
   ```
8. **Clean up the long window** — it writes its own database and CSV deliberately:
   ```sql
   DROP DATABASE IF EXISTS wwtp_ml25;
   ```
   ```bash
   docker builder prune -af
   rm -f workshops/ml/dataset-18wk.csv workshops/ml/dataset-8wk.csv
   docker volume ls | grep wwtp
   ```
   Confirm `workshops/ml/dataset.csv` (the 3-week panel) is **still present** — `workshop-long`
   must not have overwritten it. A missing `dataset.csv` after step 3/4 is a **FAIL** of the
   path protection in D1.9.
9. **Record disk reclamation on macOS.** The Postgres volume lives in a sparse `Docker.raw`
   that does not shrink; `docker builder prune -af` then Docker Desktop → Resources →
   Reclaim disk space. Verify the volume is gone:
   ```bash
   docker volume ls | grep wwtp   # expect: no wwtp-iiot_db-data after make clean
   ```

**Expected result** — 3wk < 2 min / ~1 GB / 854 MB DB; 8wk ~15 min / ~8 GB / 1,040 MB; 18wk ~46 min / ~19 GB; rows/day 142,654 → 604,341 → 557,522 with the "not a power law" caveat; verdict flips `42x NO POWER` → `1x POWER`; `dataset.csv` survives; long DB dropped.

**Pass/fail criteria**
- PASS: timings within ~2× the documented figures **on comparable hardware**; all row/positivity
  counts exact; the verdict flips as documented; `dataset.csv` intact; `wwtp_ml25` dropped.
- FAIL: `dataset.csv` overwritten (path-protection regression); the `23 GB` figure missing its
  interrupt caveat; `minimum_interval_s()` ≠ 7200.0; a rows-per-day figure missing.
- Record hardware alongside any timing FAIL. Absolute times are not portable and
  `check_portable_numbers` explicitly forbids asserting on them.

**Evidence** — host capacity, the three build logs with timings and peak RSS, the three panel summaries, the `measure_long_window` log with both verdicts, the `README.md` grep for all four strings, the `minimum_interval_s()` print, the `dataset.csv` existence check, `docker volume ls` before/after.

**Risks / caveats**
- Step 4 is optional and resource-hungry (46 min, 19 GB). Skip it if disk < 30 GB free and
  record `BLOCKED` with the reason.
- **`make workshop-long` deliberately writes `wwtp_ml25` and `dataset-Nwk.csv`** so building
  the long window cannot overwrite the panel every number in `TRAINER.md` was measured on. This
  is the single most important invariant in the workshop section.
- `DROP DATABASE IF EXISTS wwtp_ml25` requires being connected to *another* database.
- The 23 GB figure is an **interrupted** build. Do not "correct" it to 19 GB without
  reproducing — the test asserts the interrupt caveat is present.

---

## D7. Absence checks — model registration and real-time inference

**Objective** — prove the documented absences, so the gap is explicit rather than assumed.

**Pre-conditions** — none. Cheap; run early to scope D.

**Steps**
1. No registry infrastructure:
   ```bash
   grep -rniE 'mlflow|model_registry|model-registry|bentoml|dvc|vertex|sagemaker|databricks|wandb|neptune' \
     --include='*.py' --include='*.toml' --include='*.yaml' --include='*.md' . \
     | grep -v '.venv\|node_modules\|.git\|LEARNING-LOG' | head
   # expect: no output
   ```
2. No persisted models (repeat of D4.1, but as a standalone assertion):
   ```bash
   git ls-files | grep -iE '\.pkl$|\.joblib$|\.onnx$|\.pt$|\.pth$|\.h5$|model' | head
   # expect: no output
   ```
3. No versioned-model directory:
   ```bash
   ls -d models/ model_registry/ artifacts/ checkpoints/ 2>/dev/null; echo "exit=$?"
   # expect: no such directories
   ```
4. No serving layer or API:
   ```bash
   grep -rniE 'FastAPI|@app\.(post|get).*predict|uvicorn' --include='*.py' . \
     | grep -v '.venv\|node_modules\|storage/\|ui/' | head
   # expect: no output (fastapi/uvicorn exist only under the unused `storage` extra)
   ```
5. No real-time/streaming inference:
   ```bash
   grep -rniE 'kafka|redis|mqtt|websocket|celery|async.*predict|stream.*infer' \
     --include='*.py' --include='*.md' workshops/ notebooks/ | head
   # expect: no output
   ```
6. Record what *does* exist as the "artifact" story:
   ```bash
   ls -la notebooks/figures/ workshops/ml/*.csv
   git check-ignore -v notebooks/figures/01_influent.png
   git check-ignore -v workshops/ml/dataset.csv
   ```
   Expect both gitignored, with the PNG comment: "a committed PNG is a picture of a
   database that no longer exists".
7. Note the deliberate `torch` omission:
   ```bash
   grep -n 'torch' pyproject.toml
   # expect a comment explaining it is excluded (~200 MB, day-2 tier unwritten)
   ```

**Expected result** — all greps empty; no artifact files tracked; `models/` etc. absent; figures and CSVs gitignored; `torch` deliberately excluded with a rationale.

**Pass/fail criteria**
- PASS: every absence confirmed. Then D4's and the brief's model-registration/inference
  sections are correctly scoped as **not applicable**, and Q8 is answered.
- **FAIL (positive finding):** any of these greps returns output. Then model registration or
  serving *does* exist, the exploration was incomplete, and D4/D8 need new cases.

**Evidence** — each grep's output (empty or not), `git ls-files`, the `git check-ignore` results, the `pyproject.toml` torch comment.

**Risks / caveats**
- This case is cheap insurance. Running it first prevents writing elaborate tests against a
  registry that does not exist.
- If you *are* asked to add a registry, Q8 becomes a requirements question, not a
  documentation gap.

---

# E. Installation & Deployment

## E0. Area-level notes

- **Prerequisites:** Docker with Compose v2, `uv` (or Python 3.13), ~2 GB disk for a seeded
  week, and a **C compiler**. `docs/CLEAN-MACHINE-TEST-PLAN.md` step 0 asks for git 2.x,
  **docker 24+**, **GNU make 3.81+**, and `cc`. The C compiler requirement appears *only* there,
  and its absence produces a Postgres build error naming a missing **shared library**, not a
  missing compiler.
- **There is no pip-installable CLI.** `pyproject.toml` has no `[project.scripts]`. Everything
  is `python -m <module>`: `softplc.main`, `gateway.main`, `storage.postgres.schema`,
  `storage.seed.main`, `alarms.main`, `scada.build_flows`, `ui.grafana.generate_dashboards`,
  `ui.web.generate_page`, `tools.check_sql`, `tools.opcua_browser.py`, etc. See Q5.
- **Four required env vars.** Compose uses `${VAR:?...}` with no default and refuses to
  interpolate without them: `POSTGRES_PASSWORD`, `GATEWAY_DB_PASSWORD`, `WEB_DB_PASSWORD`,
  `GRAFANA_ADMIN_PASSWORD`. Everything else is `${VAR:-default}`.
- **No upgrade or rollback target exists.** `make help` has no `smoke`, no `upgrade`, no
  `rollback`. Upgrade is entirely manual. E5/E6 build one from scratch.
- **`.DEFAULT_GOAL := notebooks-open`.** A bare `make` opens JupyterLab, not a help screen.
  Always name a target.

---

## E1. Fresh install on a clean checkout (macOS and Linux)

**Objective** — a brand-new clone with no `.env` reaches a running plant with a seeded week, on both macOS and Linux.

**Pre-conditions** — a truly clean checkout. Do this on a real machine or a fresh VM, not in
the working tree.

**Steps**
1. Verify prerequisites and record versions:
   ```bash
   git --version
   docker --version          # expect 24+ / 27.x
   docker compose version    # expect v2.x
   uv --version              # expect 0.5.11 or later
   cc --version              # MUST succeed — the Postgres image needs a compiler
   make --version            # expect GNU make 3.81+; 3.81 silently ignores
                             # .SHELLFLAGS -euo pipefail -a -c, so no errexit/nounset/pipefail
   ```
   **Record any failure here as a documentation finding** — these are the gates to a working install.
2. Clone and confirm the tree is clean before touching anything:
   ```bash
   git clone <repo-url> wwtp-iiot && cd wwtp-iiot
   git status --short          # expect: empty
   git rev-parse --short HEAD  # record this; most expected values are commit-pinned
   ```
3. **Confirm `.env` does not exist and is not tracked:**
   ```bash
   ls -la .env 2>&1            # expect: No such file
   git ls-files | grep -x '.env' # expect: no output
   head -9 .gitignore           # expect .env first, then .env.*, !.env.example
   ```
4. Confirm `.env.example` holds **no real credentials** — three tests enforce this, but read it:
   ```bash
   grep -nE '(PASSWORD|PASSWD|SECRET|TOKEN|_KEY|APIKEY|API_KEY)=' .env.example
   # expect only placeholder values (markers: replace-me, changeme, change-me, example, your-)
   uv run pytest tests/test_clean_checkout.py -q     # expect 5 tests
   ```
   Note the whitelist works **by variable name**, not by value shape.
5. `make up` — the documented one-liner (`docker compose build` → `up -d` → `seed` → `wait`):
   ```bash
   time make up 2>&1 | tee evidence/E1/make-up.log
   ```
   Expect **~2m40s** and ~4.3 M readings. `make up` pulls in `make setup` (creates `.env` from
   `.env.example`, **never overwriting**) and `make sync` (`uv sync --all-extras`, skipped when
   `.venv` already satisfies `--locked --dry-run`).
6. **Confirm `make setup` did not overwrite an existing `.env`.** Do this deliberately:
   ```bash
   echo "CUSTOM=marker" >> .env
   make setup
   grep -c CUSTOM .env        # expect: 1 — still there
   ```
   Then remove the line.
7. Verify the four required vars are set. With any missing, compose refuses to interpolate:
   ```bash
   grep -E '^(POSTGRES_PASSWORD|GATEWAY_DB_PASSWORD|WEB_DB_PASSWORD|GRAFANA_ADMIN_PASSWORD)=' .env
   # expect 4 lines
   ```
   Prove the refusal by temporarily blanking one:
   ```bash
   sed -i.bak 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=/' .env
   docker compose config -q; echo "exit=$?"   # expect non-zero + a message naming POSTGRES_PASSWORD
   mv .env.bak .env
   ```
   Restore immediately.
8. Verify the core services are up and healthy:
   ```bash
   docker compose ps
   # expect: softplc (healthy), db (healthy), gateway (healthy); init-db and seed Exited (0)
   ```
   ```bash
   docker compose config --services | sort
   # expect 8: db gateway grafana init-db scada seed softplc web
   ```
   Four are behind profiles and will not appear as running: `scada`, `web`, `grafana`, `seed`.
9. Confirm the port bindings are **loopback-only** (this is the DONE item in
   `docs/SECURITY.md`, with a test behind it):
   ```bash
   docker compose ps --format '{{.Name}}\t{{.Ports}}'
   # expect 127.0.0.1 (or $HOST_BIND) on 5432/5020/4840/3000/3001/18880
   ```
   ```bash
   lsof -nP -iTCP -sTCP:LISTEN | grep -E '5020|4840|5432|3000|3001|18880'
   # expect 127.0.0.1:*, never *:*
   ```
10. Confirm the data landed:
    ```bash
    make query SQL="SELECT count(*) FROM reading"
    # expect 4239284
    make query SQL="SELECT max(ts) - min(ts) FROM reading"
    # expect 7 days
    ```
11. Confirm the gateway is actually writing (not only seeded):
    ```bash
    docker compose logs gateway --since 2m | grep -E 'polls=' | tail -3
    # expect a line like: polls=N offered=N published=N (..% filtered) spool=N files db=wwtp modbus_err=0 opcua_err=0
    ```
    `db=` must read a real database name and both error counters must be 0.
    ```bash
    make query SQL="SELECT DISTINCT source FROM reading ORDER BY 1"
    # see the caveat in E4 step 3 — expect opcua, seed
    ```
12. **Optional first-run friction to watch for:** on macOS, an `Exec format error` means a
    wrong-architecture image; fix with `docker compose build --no-cache`. Jupyter resolving to
    `/opt/homebrew/anaconda3/bin/python` means the wrong interpreter was used. Docker Desktop's
    sparse `Docker.raw` does not shrink — reclaim via Resources → Reclaim disk space.
13. Repeat the whole sequence on **Linux**. Record any step that behaves differently.

**Expected result** — prerequisites verified; tree clean; `.env` absent then created; no real credentials in the template; `make up` green in ~2m40s; 4 required vars set; 8 services defined with 4 core healthy; all ports on 127.0.0.1; 4 239 284 rows; gateway writing with 0 errors.

**Pass/fail criteria**
- PASS: all of the above on **both** macOS and Linux, with no manual edits to tracked files
  and no `git status` dirt at the end.
- FAIL: any step requiring intervention not documented in `README.md` or
  `docs/GETTING-STARTED.md`. Record each as an install-friction finding with the exact error.
- FAIL: `make setup` overwrites `.env`; a port binds to `0.0.0.0`; compose starts with a
  required var missing.
- `tests/test_clean_checkout.py` (5 tests) is a hard FAIL.

**Evidence** — all six version commands, `git status`/`git rev-parse`, `.gitignore` head, the `.env.example` credential grep, the `make up` log with timing, the compose-refusal transcript, `docker compose ps`, `config --services`, the port bindings, the two count queries, the gateway log lines, per-OS notes.

**Risks / caveats**
- **Run E1 on a genuinely clean checkout.** The working tree here already has a `.env` with
  `POSTGRES_PORT=55433`, `GRAFANA_PORT=3002` and `SOFTPLC_SEED=${SOFTPLC_SEED:-20260926}`.
  On this host the DB is on **55433**, not 5432.
- GNU make 3.81 silently ignores `.SHELLFLAGS -euo pipefail -a -c`, so **every recipe runs
  without errexit**. A command failing mid-recipe will not abort the target. Read logs, do not
  trust a zero exit alone.
- `SOFTPLC_SEED` is read by the process, not compose — which is why `tools/env.sh` *sources*
  `.env` rather than `include`-ing it (shell expansion must apply).

---

## E2. Configuration contract

**Objective** — every configuration knob behaves as documented, and the required/optional split is real.

**Pre-conditions** — E1.

**Steps**
1. Enumerate every variable compose consumes, and classify it:
   ```bash
   grep -oE '\$\{[A-Z_]+(:[?-][^}]*)?\}' compose.yaml | sort -u | tee evidence/E2/compose-vars.txt
   ```
   **Required (`:?`, 4):** `POSTGRES_PASSWORD`, `GATEWAY_DB_PASSWORD`, `WEB_DB_PASSWORD`,
   `GRAFANA_ADMIN_PASSWORD`.
   **With defaults (`:-`):** `POSTGRES_USER=wwtp`, `POSTGRES_DB=wwtp`, `POSTGRES_PORT=5432`,
   `HOST_BIND=127.0.0.1`, `MODBUS_PORT=5020`, `OPCUA_PORT=4840`, `WEB_PORT=3001`,
   `SCADA_PORT=18880`, `GRAFANA_PORT=3000`, `GRAFANA_ADMIN_USER=admin`,
   `RETENTION_RAW_DAYS=7`, `RETENTION_MINUTE_DAYS=90`, `GATEWAY_LOG_LEVEL=INFO`,
   `GATEWAY_DEADBAND_DEFAULT=0.0`, `GATEWAY_SPOOL_MAX_MB=512`, `PLC_SCAN_MS=20`, `PLC_SPEED=1.0`,
   `SEED_DAYS=7`, `DEMO_STORM_AT_HOURS=2`, `GATEWAY_DB_ROLE=wwtp_gateway`, `WEB_DB_ROLE=wwtp_ui`,
   `ADMIN_ROOT=/scada`. There is **no `NODE_RED_EDITOR`** — it was listed here, and in
   `scada/README.md` and `ui/grafana/provisioning/dashboards/dashboards.yaml`, as if it
   were a knob. It is not in `compose.yaml` or `.env.example` and it is read by nothing:
   Node-RED has no setting that disables its editor. The control is the loopback bind on
   the published port.
2. **Verify `.env.example` covers every required var.** A test enforces this:
   ```bash
   uv run pytest tests/test_clean_checkout.py -q -k template
   ```
   And the template defines nothing *empty* — `${VAR:?}` is satisfied by a non-empty value, not
   by the name being present:
   ```bash
   grep -nE '^[A-Z_]+=\s*$' .env.example    # expect: no output
   ```
3. **Variables that exist only in `.env.example`, not in compose:** `SOFTPLC_SCAN_HZ=50`,
   `SOFTPLC_MODBUS_PORT=5020`, `SOFTPLC_OPCUA_PORT=4840`, `SOFTPLC_SEED`, and the commented
   `POSTGRES_TEST_PORT=55432` / `POSTGRES_TEST_PASSWORD`. Verify they still reach the process:
   ```bash
   grep -rn 'SOFTPLC_SCAN_HZ\|SOFTPLC_SEED' softplc/ | head
   ```
4. **Test `HOST_BIND` in both directions.** Loopback (default) then all interfaces:
   ```bash
   HOST_BIND=0.0.0.0 docker compose config | grep -A3 'published:'
   # expect 0.0.0.0:<port> — and record this as an EXPOSURE, not a pass
   HOST_BIND=127.0.0.1 docker compose config | grep -A3 'published:'
   ```
   Note `scada` binds **hard-coded** `127.0.0.1` and does *not* honour `HOST_BIND`. Verify:
   ```bash
   docker compose --profile scada config | grep -B2 -A2 1880
   ```
5. **Test port overrides end to end:**
   ```bash
   POSTGRES_PORT=5599 WEB_PORT=3101 GRAFANA_PORT=3100 SCADA_PORT=18899 docker compose up -d db init-db web
   make query SQL="SELECT 1"      # must reach 5599
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3101/api/health   # expect 200
   docker compose down
   ```
   Then restore. **Verify the port override survives `tools/env.sh`**, which sources `.env`
   *after* the inherited environment so make wins:
   ```bash
   POSTGRES_PORT=5599 make query SQL="SELECT 1"   # expect success against 5599, not 55433
   ```
   If this fails, `.env` is overriding the environment — a real finding.
6. **Test the retention knobs.** They are bound parameters, not hardcoded (A6.2), and compose
   passes them into `init-db`:
   ```bash
   RETENTION_RAW_DAYS=2 RETENTION_MINUTE_DAYS=30 docker compose up -d --force-recreate init-db
   make query SQL="SELECT view_name, schedule_interval FROM timescaledb_information.jobs WHERE proc_name LIKE '%retention%'"
   # expect intervals consistent with 2 days / 30 days
   docker compose up -d --force-recreate init-db   # restore defaults
   ```
7. **Test the gateway knobs:**
   ```bash
   GATEWAY_LOG_LEVEL=DEBUG GATEWAY_SPOOL_MAX_MB=16 GATEWAY_DEADBAND_DEFAULT=0.0 \
     docker compose up -d --force-recreate gateway
   docker compose logs gateway --since 1m | head -30
   ```
   Expect DEBUG-level lines and no crash from the 16 MB spool cap (it should drop the oldest
   complete files and say so, counting them in `dropped_overflow`).
8. **Test the soft PLC knobs** directly, outside compose:
   ```bash
   uv run python -m softplc.main --list-scenarios
   uv run python -m softplc.main --scan-ms=50 --modbus-port=5021 \
     --opcua-endpoint=opc.tcp://0.0.0.0:5022/wwtp/server/ --duration=10 --speed=1.0
   ```
   Flags: `--scan-ms 20`, `--modbus-host 0.0.0.0`, `--modbus-port 5020`,
   `--opcua-endpoint opc.tcp://0.0.0.0:4840/wwtp/server/`, `--scenario`,
   `--list-scenarios`, `--duration`, `--speed 1.0`, `--seed 0`, `--verbose`,
   `--report-every 30.0`, `--log-level INFO`.
   `--seed` must make two runs produce identical values:
   ```bash
   uv run python -m softplc.main --seed=42 --duration=5 --modbus-port=5023 \
     --opcua-endpoint=opc.tcp://0.0.0.0:5024/wwtp/server/ 2>&1 | grep -c 'scan'
   ```
   Repeat and diff. `SOFTPLC_SEED=20260926` is the default in `.env.example`.
9. **Gateway CLI flags** (no compose):
   ```bash
   uv run python -m gateway.main --help
   uv run python -m gateway.main --iterations 10 --no-postgres
   ```
   Expect `--iterations 10` to exit cleanly after 10 polls and log
   `no POSTGRES_DSN; spooling only`. Confirm the spool was written:
   ```bash
   ls -la /tmp/spool* 2>/dev/null; ls -la /srv/spool 2>/dev/null
   ```
   Pass `--spool-dir` if needed.
10. **Confirm the Makefile's env precedence** is deliberate and tested:
    ```bash
    uv run pytest tests/test_makefile_env.py -q    # expect 7 tests
    grep -n 'BASH_ENV\|export' Makefile | head
    # expect: export BASH_ENV := $(CURDIR)/tools/env.sh
    ```
    And `PY := $(CURDIR)/tools/py.sh` exists because a metacharacter-free recipe line bypasses
    `$(SHELL)` and never reads `BASH_ENV`:
    ```bash
    cat tools/py.sh
    uv run pytest tests/test_py_wrapper.py -q      # expect 3 tests
    ```

**Expected result** — 4 required vars, ~22 defaulted; template complete and non-empty; `HOST_BIND` honoured except for `scada`; port overrides work through both `docker compose` and `make`; retention/gateway/PLC knobs take effect; seeded PLC runs are reproducible; the Makefile env precedence test passes.

**Pass/fail criteria**
- PASS: as above. Specifically FAIL if a required var is missing from `.env.example`; if the
  template defines any variable empty; if a port override is silently ignored by `make`;
  if changing `RETENTION_*` does not change the policy interval; if two `--seed 42` runs differ;
  if `scada` honours `HOST_BIND` (that is a finding, so it must currently be hard-coded).
- `tests/test_clean_checkout.py` and `tests/test_makefile_env.py` are hard FAILs.

**Evidence** — the compose-vars enumeration, template greps, both `HOST_BIND` renderings, the port-override transcripts (both routes), the retention job query before/after, the gateway DEBUG log, `--list-scenarios` output, the two seeded runs diffed, `gateway.main --help`, the spool listing, `tools/py.sh` contents, pytest summaries.

**Risks / caveats**
- `.env` overrides your shell for `make`-driven targets. To genuinely override, edit `.env`
  or use `docker compose` directly.
- Changing retention requires an `init-db` recreate, not just a gateway restart.
- `GATEWAY_SPOOL_MAX_MB=16` will drop data. Do it on a scratch volume.

---

## E3. Health checks and observability of the stack

**Objective** — every health signal is real and correctly wired, and `make wait`/`make roles`/`make logs` do what they claim.

**Pre-conditions** — E1.

**Steps**
1. Enumerate the four compose healthchecks:
   ```bash
   docker inspect --format '{{.Name}} {{json .State.Health}}' \
     $(docker compose ps -q) 2>/dev/null | python3 -c "
   import json,sys
   for line in sys.stdin:
       name, rest = line.split(' ',1)
       h=json.loads(rest)
       if h: print(name, h['Status'], '|', h['Log'][-1]['ExitCode'] if h['Log'] else 'no log')"
   ```
   | service | check | timing |
   |---|---|---|
   | `softplc` | TCP connect to 5020 | 10 s / 3 s / 5, start 10 s |
   | `db` | `pg_isready -h 127.0.0.1 -U … -d …` | 5 s / 5 s / 20, start 10 s |
   | `gateway` | Python heredoc: `SELECT count(*) FROM signal` **and** attempt `TRUNCATE reading`, expecting refusal | 15 s / 5 s / 5, start 20 s |
   | `web` | `node -e "fetch('http://localhost:3000/api/health')"` | 15 s / 5 s / 5, start 20 s |
   `init-db` and `seed` are one-shot with no healthcheck. `grafana` uses the image's own
   `HEALTHCHECK`. `scada` has **none** — see step 6.
2. **Prove each healthcheck is load-bearing**, not decorative. Stop the dependency and confirm
   the dependent goes unhealthy:
   ```bash
   docker compose stop db; sleep 30
   docker compose ps        # expect gateway unhealthy / restarting, web unhealthy
   docker compose start db; sleep 40
   docker compose ps        # expect healthy again
   ```
3. **The gateway healthcheck is the most interesting one.** It proves a *negative* — that the
   role cannot erase history. Read it and run it standalone:
   ```bash
   docker compose exec gateway python -c "
   import os, psycopg
   with psycopg.connect(host=os.environ.get('POSTGRES_HOST','db'), dbname=os.environ.get('POSTGRES_DB','wwtp'),
                        user=os.environ.get('POSTGRES_USER'), password=os.environ['POSTGRES_PASSWORD']) as c:
       print('signals:', c.execute('SELECT count(*) FROM signal').fetchone()[0])
       try:
           c.execute('TRUNCATE reading'); print('LEAK')
       except psycopg.errors.InsufficientPrivilege:
           print('ok: scoped')"
   ```
   Expect `signals: 57` and `ok: scoped`. The full healthcheck also prints
   `scoped: reads the contract, cannot erase history`.
4. **`gateway.health()` is not exposed over HTTP.** Confirm there is no endpoint:
   ```bash
   docker compose exec gateway python -c "
   import socket
   for p in (8000,8080,9000,5000):
       s=socket.socket(); r=s.connect_ex(('127.0.0.1',p)); print(p, 'open' if r==0 else 'closed'); s.close()"
   # expect all closed
   ```
   To inspect the gateway's in-process health object, run it in the foreground:
   ```bash
   uv run python -m gateway.main --iterations 5 --report-every 1 --log-level DEBUG 2>&1 | head -40
   ```
   `health()` returns `running`, `modbus`, `opcua`, `stats`, `spool`, `postgres`,
   `deadband{offered,published}`. Stats keys: `polls`, `offered`, `published`,
   `spooled`, `dropped_overflow`, `modbus_failures`, `opcua_failures`, `uptime_s`.
5. `make wait`:
   ```bash
   make wait 2>&1 | tee evidence/E3/wait.log
   ```
   Blocks until the first readings land — 90 attempts × 2 s, then `try: docker compose logs gateway`.
   On a fresh `make up` it should return quickly. Test the failure path on an empty database:
   ```bash
   docker compose stop gateway
   # in another shell: make wait   → expect the timeout message within ~3 minutes
   docker compose start gateway
   ```
6. `scada` has **no healthcheck** — a genuine gap. Verify and report:
   ```bash
   docker compose --profile scada config | grep -A3 'scada:' | grep -c healthcheck   # expect 0
   ```
   Probe it manually instead:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:${SCADA_PORT:-18880}/scada   # expect 200
   ```
   **FINDING:** a service with a profile and no healthcheck cannot participate in
   `depends_on: service_healthy` ordering or in automated readiness.
7. `make roles` and `make logs`:
   ```bash
   make roles | tee evidence/E3/roles.txt
   make logs        # Ctrl-C after 10 s
   ```
   `make logs` runs `docker compose logs -f softplc gateway`. Expect the soft PLC's periodic
   report and the gateway's `polls=… published=…` line.
8. **Log rotation is configured** on the Python anchor:
   ```bash
   docker compose config | grep -A4 'logging:'
   # expect json-file, max-size 10m, max-file 3
   ```
   Note it is **not** applied to `db` or `grafana` (they use the `x-restart` anchor, not
   `x-python-service`). **FINDING:** an unbounded log driver on the two longest-running services.
9. `make db-live` and `tools.db_ready`:
   ```bash
   make db-live      # expect: yes
   docker compose stop db; make db-live   # expect: no
   docker compose start db
   ```
   `db-up` polls for 60 s and then runs `tools.db_ready` **unsilenced** on failure.
10. Verify the restarted stack comes back clean:
    ```bash
    docker compose restart gateway; sleep 30
    docker compose ps                      # expect healthy
    docker compose logs gateway --since 1m | grep -ci error   # expect 0
    make query SQL="SELECT count(*) FROM reading"             # expect growing
    ```

**Expected result** — 4 compose healthchecks + Grafana's image check; each fails when its dependency is stopped; the gateway check proves a negative; `make wait` returns fast with data and times out cleanly without; `scada` confirmed to have no healthcheck; log rotation on the Python anchor only; `make db-live` reflects reality.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if any healthcheck passes while its dependency is down —
   especially the gateway's, which had a documented bug where "a stderr redirect made a real
   connection failure look like a pass".
- **FINDING (expected):** `scada` has no healthcheck; `db` and `grafana` have no log rotation.
- FAIL: `make wait` hangs indefinitely; `make db-live` disagrees with `docker compose ps`.

**Evidence** — the health-status dump, the stop/start transcripts, the standalone gateway
scoping transcript, the closed-port scan, `make wait` both paths, the `scada` healthcheck grep
(0), `make roles`, `make logs` excerpt, the `logging:` config, `make db-live` both values, the
post-restart status.

**Risks / caveats**
- The known issue "the gateway never reconnects to Postgres" (found with `failures: 29702`
  while the container reported healthy) means **a healthy gateway can still be failing to
  write.** See E4 — do not rely on the healthcheck alone.
- `make wait`'s timeout is ~3 minutes; budget for it in the failure test.

---

## E4. Failure injection — the durability claims

**Objective** — the documented failure modes behave as stated, especially the spool and the gateway/Postgres disconnect.

**Pre-conditions** — E1, with the gateway running and data flowing. Keep `make up` state
restorable.

**Steps**
1. Establish the baseline:
   ```bash
   make query SQL="SELECT count(*) FROM reading" | tee evidence/E4/baseline.txt
   docker compose logs gateway --since 5m | grep 'polls=' | tail -3 | tee evidence/E4/baseline-gw.log
   ```
2. **DB down → spool drains, nothing lost.**
   ```bash
   docker compose stop db; sleep 30
   docker compose logs gateway --since 1m | grep -iE 'error|fail|retry' | tail -20 | tee evidence/E4/db-down.log
   docker compose start db; sleep 60
   make query SQL="SELECT count(*) FROM reading"
   ```
   Expect the gateway to log failures, keep spooling, and the count to resume. Check the
   writer's failure count afterwards:
   ```bash
   docker compose logs gateway --since 5m | grep -c 'batch'
   ```
3. **The known gateway-reconnect gap — verify it explicitly.** This is a documented open
   issue and the most valuable check in this case:
   ```bash
   grep -c reconnect storage/postgres/writer.py   # expect 0 — no reconnect logic
   ```
   ```bash
   docker compose stop db
   sleep 300     # let the failure counter climb
   docker compose start db; sleep 120
   docker compose logs gateway --since 10m | grep -iE 'failures|reconnect|recovered' | tail -20
   make query SQL="SELECT count(*) FROM reading"     # did it resume writing?
   ```
   **Expect: it may not recover without a gateway restart.** If so:
   ```bash
   docker compose restart gateway; sleep 30
   make query SQL="SELECT count(*) FROM reading"     # now it resumes
   ```
   Record precisely what happened and how long recovery took. **This is the highest-value
   finding available in E — a gateway that reports healthy while unable to write is a
   production-grade defect.**
4. **Verify the `source`-is-always-`opcua` bug.** `poll_once` merges both protocols into one
   dict, `SpoolRecord` has **no source field**, and `drain()` hardcodes `"opcua"`:
   ```sql
   SELECT DISTINCT source FROM reading ORDER BY 1;
   ```
   Expect **`opcua`** and **`seed`** only — never `modbus`. So the designed cross-protocol
   defence (the `source` column in the PK) is **absent, not weak**. Confirm the code:
   ```bash
   grep -n 'source' gateway/spool/store.py | head
   grep -n 'opcua' gateway/main.py | head
   ```
   **This is a confirmed defect, not a QA expectation.** Every other test in this plan that
   asserts on `source` assumes this. Record it.
5. **Spool behaviour — at-least-once and rotation.** Inspect the spool inside the container:
   ```bash
   docker compose exec gateway ls -la /srv/spool
   docker compose exec gateway sh -c "head -c 300 /srv/spool/spool-*.jsonl | head -3"
   ```
   Expect files named `spool-YYYYMMDD-HHMMSS.jsonl` (regex `^spool-(\d{8})-(\d{6})\.jsonl$`),
   named by the minute+second the file was **opened** — the design is 60 s rotation, not
   hourly, because `pending()` excludes the in-progress file. Record shape:
   ```json
   {"ts":<int seconds>,"s":"<signal_id>","v":<float>,"q":<int>}
   ```
   **Note the absence of `source`** — step 4 again.
6. **Verify spool overflow drops oldest, not newest, and says so:**
   ```bash
   GATEWAY_SPOOL_MAX_MB=1 docker compose up -d --force-recreate gateway
   sleep 300
   docker compose logs gateway --since 5m | grep -iE 'overflow|dropped|removed' | tail -10
   docker compose exec gateway ls /srv/spool | wc -l    # expect bounded
   docker compose up -d --force-recreate gateway          # restore
   ```
   Expect `dropped_overflow` to climb and `files_removed` to be reported; undeletable files
   recorded in `stats.corrupt`. The file **currently being appended to must never** be removed.
7. **Gateway killed mid-poll → nothing lost:**
   ```bash
   docker compose kill gateway; sleep 5; docker compose start gateway; sleep 30
   make query SQL="SELECT count(*) FROM reading"
   ```
   Expect a resume, with a possible duplicate re-delivery (at-least-once). Check for
   duplicates using the PK:
   ```sql
   SELECT count(*) FROM (SELECT ts, signal_id, source FROM reading
                         GROUP BY 1,2,3 HAVING count(*)>1) d;   -- expect 0
   ```
   The upsert `ON CONFLICT (ts, signal_id, source) DO UPDATE` means re-delivery is idempotent.
8. **Spool volume lost → the one unrecoverable case.** Requires `-v`:
   ```bash
   docker compose down -v
   docker compose up -d
   make query SQL="SELECT count(*) FROM reading"   # expect 0 until re-seeded
   make seed
   make query SQL="SELECT count(*) FROM reading"   # expect 4239284
   ```
9. **One protocol down → the other carries it:**
   ```bash
   docker compose stop softplc; sleep 60
   docker compose logs gateway --since 1m | grep -cE 'modbus_err|opcua_err'   # expect climbs
   docker compose start softplc; sleep 60
   docker compose logs gateway --since 1m | grep -cE 'modbus_err|opcua_err'
   ```
   Expect errors to climb then recover — or **not** recover (same reconnect risk as step 3).
   Record which.
10. **Read-only role attempt in the UI path.** `wwtp_ui` cannot write — already covered in
    A5.7/B7.9. Confirm the web container stays healthy despite the refusal:
    ```bash
    docker compose --profile ui ps web   # expect (healthy)
    ```
11. Restore everything and confirm a clean end state:
    ```bash
    make up && docker compose ps
    make query SQL="SELECT count(*) FROM reading"
    ```

**Expected result** — DB down spools and resumes; the reconnect gap reproduces; `source` is `opcua`+`seed` only; spool files are 60 s JSONL with no source field; overflow drops oldest and reports; kill/restart is idempotent; `-v` loses the backlog; protocol errors climb and may not recover.

**Pass/fail criteria**
- PASS for steps 1–2, 5–8, 11. **Record as findings** for steps 3, 4 and 9 — those are known
  defects; your job is to quantify and confirm them, not to declare them passing.
- **Hard FAIL** if data is lost where the design claims durability (spool DB-down, gateway kill).
- **Hard FAIL** if duplicates appear after restart (the upsert should prevent them).
- **FINDING (confirm and quantify):** no Postgres reconnect; `source` always `opcua`.

**Evidence** — baseline counts and gateway lines, the DB-down log, the reconnect grep (0) and the 5-minute failure sequence, the `source` query and code greps, the spool listing and record shape, the overflow log and file count, the kill/restart duplicate query, the `-v` sequence, the protocol-down error counts, the final `docker compose ps` and count.

**Risks / caveats**
- **Step 3 takes ~8 minutes** and step 6 ~5 minutes. Budget them.
- Steps 3, 4, 6 and 8 involve destructive or long-running operations. Take a dump first if the
  seeded week matters:
  ```bash
  docker compose exec db pg_dump -U wwtp -d wwtp > evidence/E4/wwtp.sql
  ```
  This is the same advice `tests/integration/conftest.py` prints. It also destroyed a seeded
  week once already.
- The gateway-reconnect gap may be fixed on your commit. If so, `grep -c reconnect` will be
  non-zero and step 3 should pass — record which you observed.

---

## E5. Upgrade path

**Objective** — establish what "upgrade" means here, since no target exists, and test the realistic scenarios.

**Pre-conditions** — E1. **Take a dump first** (E4 risk note).

**Steps**
1. **Establish that there is no upgrade mechanism:**
   ```bash
   make help | grep -iE 'upgrade|migrat|version'    # expect: no output
   grep -rn 'alembic\|schema_migrations\|version_id' --include='*.py' --include='*.toml' . \
     | grep -v '.venv' | head    # expect: no output
   ```
   `storage/postgres/schema.py:82-88` states the rationale: one schema file, `IF NOT EXISTS`
   throughout, so no migration framework.
2. **Test the actual upgrade mechanism that exists: re-running `init-db` against a populated
   database.** This is idempotent by design (A1.4):
   ```bash
   make query SQL="SELECT count(*) FROM reading"      # record N
   docker compose up -d --force-recreate init-db
   docker compose logs init-db | grep -icE 'error|already exists'   # expect 0
   make query SQL="SELECT count(*) FROM reading"      # expect N, unchanged
   ```
   **Data must survive an `init-db` re-run.** A count change here is a **P0 finding**.
3. **Test the genuine upgrade: a schema *change*.** This is where `IF NOT EXISTS` bites — a new
   column is **not** added to an existing table by an `IF NOT EXISTS` CREATE TABLE. Simulate it
   on a scratch database:
   ```bash
   docker compose exec -T db psql -U wwtp -d wwtp <<'SQL'
   -- snapshot current shape
   \d reading
   SQL
   ```
   Then add a column to `storage/postgres/schema.sql` on a branch, run `init-db`, and check
   `\d reading`:
   - a new table/relation → appears
   - a new **column** on an existing table → **does not appear** (needs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`)
   - a **changed CHECK constraint** → does not update
   - a **dropped column** → survives
   This is the concrete demonstration that **the project has no migration mechanism**, and it
   is the most important thing to report about upgrades. Revert the branch afterwards:
   ```bash
   git checkout storage/postgres/schema.sql
   ```
4. **Test the derived-artifact upgrade.** Generated files must be regenerated when the contract
   changes, and the drift gates catch it when they don't:
   ```bash
   git checkout -b contract-bump
   # edit contracts/tags.yaml: change one signal's unit or band
   make scada-check dashboards-check page-check tableplus-check   # expect: FAIL (drift)
   make scada-flows dashboards page tableplus                       # regenerate
   make scada-check dashboards-check page-check tableplus-check   # expect: PASS
   make up && make sql && make integration                          # expect: PASS
   git checkout contract-bump && git checkout -
   ```
   Every derived artefact must be regenerated together, or the gates fail. This is the real
   "upgrade" workflow for a contract change.
5. **Test a dependency upgrade** (`uv.lock` is committed at 259 KB):
   ```bash
   uv lock --dry-run                        # expect: no change
   uv lock --upgrade-package pymodbus       # expect: a diff (3.6.6 is pinned exactly)
   uv lock --check                          # restore; expect: lock matches pyproject
   ```
   `pymodbus==3.6.6` is pinned because 3.15 replaced `ModbusSlaveContext` with
   `ModbusDeviceContext`. **If you upgrade it, `tests/test_modbus.py` (63 tests) and the
   gateway's Modbus client will likely fail.** Report, do not attempt.
   ```bash
   uv run pytest tests/test_modbus.py tests/test_modbus_server.py tests/test_control.py -q
   ```
6. **Test a base-image upgrade.** `python:3.13.2-slim-bookworm` is pinned **by tag, not
   digest**, and TimescaleDB at `2.30.1-pg16`:
   ```bash
   grep -nE '^FROM' docker/Dockerfile.python ui/web/Dockerfile scada/nodered/Dockerfile
   docker compose build --no-cache
   docker compose up -d && make sql && make lessons     # expect PASS
   ```
   The tag-vs-digest argument is written out in `docs/SECURITY.md` and then **retracted** as
   "wrong in general" — InfluxDB published only SHAs on Quay; TimescaleDB publishes
   `2.30.1-pg16`. Record that the pinning policy is per-dependency and unwritten.
7. **CI runs a pinned `UV_VERSION: "0.5.11"` and `PYTHON: "3.13"`**, while
   `docker/Dockerfile.python` installs `uv==0.5.11` on `python:3.13.2-slim-bookworm`. Verify
   local parity:
   ```bash
   uv --version; python3 --version
   grep -nE 'UV_VERSION|PYTHON:' .github/workflows/gates.yml
   ```
   A local `uv` newer than 0.5.11 can produce a different `uv.lock` — a real upgrade hazard.
   ```bash
   git status --short uv.lock   # expect: empty after a `make sync`
   ```

**Expected result** — no upgrade target or migration tool; `init-db` re-run is idempotent and preserves data; a new column is **not** added to an existing table (demonstrating the gap); derived artefacts must be regenerated together; `uv lock` is clean; images build and all gates pass.

**Pass/fail criteria**
- PASS: as above, with steps 2, 4, 5, 6, 7 all green.
- **P0 FAIL** if an `init-db` re-run changes the row count or drops data.
- **FINDING (expected, important):** `IF NOT EXISTS` cannot add a column or update a
  constraint. There is no mechanism. Any schema change needs a hand-written `ALTER` migration
  — which nothing in the repo provides or checks for.
- FAIL if a `uv lock --dry-run` produces a diff (the committed lock is not reproducible), or if
  `git status` shows `uv.lock` modified after `make sync`.

**Evidence** — the no-upgrade greps, the before/after `init-db` row counts, the `ALTER TABLE`
demonstration with `\d reading` before and after, the contract-bump drift/regenerate/re-pass
sequence, the `uv lock` outputs, the `FROM` lines, the build + gate results, the CI env values,
`git status uv.lock`.

**Risks / caveats**
- Steps 3 and 4 modify tracked files on a branch. Revert carefully and verify
  `git status --short` is empty before continuing.
- `uv lock --upgrade-package` modifies `uv.lock`. Restore with `uv lock` or `git checkout uv.lock`.
- Re-running `init-db` also re-applies retention policies and re-seeds metadata. That is
  expected (idempotent) but verify it does not truncate.

---

## E6. Rollback

**Objective** — establish and test a rollback procedure, since none is provided.

**Pre-conditions** — E5 completed. **Dump the database first.**

**Steps**
1. **State plainly: there is no rollback target.** Confirm:
   ```bash
   make help | grep -i rollback   # expect: no output
   ```
2. **Take a pre-upgrade dump** — this is the only rollback mechanism that exists:
   ```bash
   mkdir -p evidence/E6
   docker compose exec -T db pg_dump -U wwtp -d wwtp -Fc -f /tmp/wwtp.dump
   docker compose cp db:/tmp/wwtp.dump evidence/E6/wwtp.dump
   ls -la evidence/E6/wwtp.dump
   ```
   Also dump **schema-only**, because roles are cluster state and are *not* restored by
   `pg_dump --schema-only`:
   ```bash
   docker compose exec -T db pg_dump -U wwtp -d wwtp --schema-only > evidence/E6/schema.sql
   grep -c 'CREATE ROLE' evidence/E6/schema.sql   # expect 0 — roles are excluded from schema.sql
   ```
3. **Record the roles and grants separately**, since they are cluster state:
   ```bash
   make roles > evidence/E6/roles-before.txt
   docker compose exec -T db psql -U wwtp -d wwtp -c '\du' > evidence/E6/roles-list.txt
   docker compose exec -T db psql -U wwtp -d wwtp -c '\dg' >> evidence/E6/roles-list.txt
   ```
4. **Test rollback of the database:**
   ```bash
   # break it
   make query SQL="DELETE FROM event WHERE kind='test'"
   docker compose exec -T db psql -U wwtp -d wwtp -c 'DROP TABLE IF EXISTS signal CASCADE'
   make query SQL="SELECT count(*) FROM signal"    # expect error or 0
   # roll back
   docker compose cp db:/tmp/wwtp.dump /tmp/restore.dump
   docker compose exec -T db pg_restore -U wwtp -d wwtp --clean --if-exists /tmp/restore.dump
   make query SQL="SELECT count(*) FROM signal"    # expect 57
   make query SQL="SELECT count(*) FROM reading"   # expect the pre-dump count
   ```
   Record the restore duration for 4.29 M rows.
5. **Test that `init-db` can rebuild an empty database from scratch** — the other half of
   rollback, and the more likely recovery path:
   ```bash
   docker compose exec -T db psql -U wwtp -d postgres -c 'DROP DATABASE wwtp'
   docker compose up -d db init-db
   docker compose logs init-db | grep -icE 'error'   # expect 0
   make roles | head                                # expect 3 group roles
   make query SQL="SELECT count(*) FROM signal"      # expect 57
   make query SQL="SELECT count(*) FROM reading"     # expect 0 — schema only, no data
   make seed
   make query SQL="SELECT count(*) FROM reading"     # expect 4239284
   ```
6. **Test image rollback.** Images are built locally, so rollback means rebuilding at the
   previous commit:
   ```bash
   git log --oneline -5
   git checkout <previous-sha>
   docker compose build --no-cache && docker compose up -d
   make sql && make lessons                        # expect PASS at the older commit
   git checkout -
   ```
   Record the rebuild time (the `images` CI job budgets 30 minutes).
7. **Test volume rollback for `grafana-data`.** Grafana state lives in a named volume, so
   rolling Grafana back means losing dashboards and users — though the dashboards are
   file-provisioned from the repo, so they come back:
   ```bash
   docker volume rm wwtp-iiot_grafana-data
   docker compose --profile observability up -d grafana
   docker compose exec grafana curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
     'http://localhost:3000/api/search?type=dash-db' | python3 -m json.tool   # expect 2 dashboards
   ```
   Expect the dashboards to reappear (file provisioning) but the admin user to be reset to the
   env-var password. Record that.
8. **`gateway-spool` rollback:** deleting the volume loses the backlog — the one
   unrecoverable case (E4.8). Document it.
9. **Validate the rolled-back stack end to end:**
   ```bash
   make check && make integration
   ```
10. **End state clean:**
    ```bash
    docker compose ps
    make query SQL="SELECT count(*) FROM reading"
    git status --short        # expect: empty
    ```

**Expected result** — no rollback target; a `pg_dump -Fc` dump is the mechanism; roles are dumped separately (they are cluster state); `pg_restore` recovers the data; `init-db` rebuilds an empty schema; older commits rebuild and pass; Grafana dashboards re-provision after volume loss; `gateway-spool` loss is unrecoverable.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if `pg_restore` cannot recover 57 signals and 4 239 284 rows;
  if `init-db` cannot rebuild from nothing; if a `pg_dump --schema-only` is mistaken for a
  full backup (**roles would be lost**).
- Record the restore duration — it is the number an operator needs during an incident.
- **FINDING (expected):** there is no documented rollback runbook. `docs/` has no incident
  procedure for this. See F4.

**Evidence** — the no-rollback grep, the dump sizes, the schema-only dump with
`CREATE ROLE` count 0, the role/grant dumps, the break-and-restore sequence with timings, the
`DROP DATABASE` + `init-db` rebuild sequence, the image rollback, the Grafana volume test, the
final `make check` output, `git status`.

**Risks / caveats**
- Steps 4 and 5 are **destructive**. Take the dump (step 2) and confirm it is non-trivial in
  size before proceeding.
- `pg_restore --clean --if-exists` needs the target to be reachable; use `docker compose cp`
  both directions.
- Volume names are prefixed `wwtp-iiot_`. Verify with `docker volume ls` rather than assuming.

---

## E7. Uninstall and cleanup

**Objective** — `make down` and `make clean` do what they claim, and disk is reclaimed.

**Pre-conditions** — E1.

**Steps**
1. **Non-destructive stop:**
   ```bash
   make down
   docker compose ps            # expect: no running services
   docker volume ls | grep wwtp # expect: db-data, gateway-spool, grafana-data still present
   ```
   `make down` = "stop, keeping the volumes".
2. **Destructive teardown:**
   ```bash
   make clean
   docker compose ps                            # expect: nothing
   docker volume ls | grep wwtp                 # expect: no output
   make query SQL="SELECT 1"                     # expect a connection error
   ```
3. **Full teardown including images:**
   ```bash
   docker compose down -v --rmi local
   docker image ls | grep wwtp                   # expect: no project images
   ```
   Note `docker compose build` tags images `wwtp-iiot-*`; `docker image prune` is gentler if
   you want to keep third-party base images.
4. **Verify disk reclamation — and the macOS caveat:**
   ```bash
   df -h . | tail -1
   docker system df
   docker builder prune -af
   ```
   On macOS the Postgres volume lives in a sparse `Docker.raw` that **does not shrink** even
   after the volume is deleted. Only Docker Desktop → Settings → Resources → *Reclaim disk
   space* reclaims it. Verify by comparing `df -h /` before and after.
5. **Clean rebuild to prove uninstall was complete:**
   ```bash
   make up
   make query SQL="SELECT count(*) FROM reading"   # expect 4239284
   git status --short                             # expect: empty
   ```
   This is step 25/26 of `docs/CLEAN-MACHINE-TEST-PLAN.md`, and the doc's own note applies: **"A
   run where every gate passes but step 26 is dirty has still found a bug."**
6. **Verify the working tree is untouched by a full cycle:**
   ```bash
   git status --short
   git diff --stat
   ```
   Expect both empty. `.env` is untracked-and-ignored, so it will not appear.
7. **Optional: verify the `.env` secrets are gone from any image layer:**
   ```bash
   docker compose --profile observability config | grep -i password | head
   # expect: ${VAR:?...} placeholders, NOT resolved values — compose config with
   # --resolve-image-digests does not substitute secrets into the printed output
   ```

**Expected result** — `make down` keeps volumes; `make clean` removes them; images removable; a clean `make up` reproduces 4 239 284 rows; `git status` empty throughout.

**Pass/fail criteria**
- PASS: as above. **FAIL** if `make clean` leaves volumes; if a rebuild does not reproduce the
  row count; if `git status` or `git diff --stat` is non-empty after a full cycle.
- Record the `df -h` before/after on macOS to confirm the sparse-volume caveat.

**Evidence** — `ps` and `volume ls` at each stage, the connection error, the image list,
`df -h` before/after, `docker system df`, the rebuild count, `git status`/`git diff --stat`.

**Risks / caveats**
- `make clean` destroys the seeded week. E4's dump advice applies.
- `make up` takes ~2m40s including the seed.

---

# F. Cross-Cutting Concerns

## F1. Security posture

**Objective** — verify the documented threat model matches reality, and that every claimed control actually holds.

**Pre-conditions** — E1, with all optional surfaces up (`scada`, `web`, `grafana`).

**Steps**
1. Read the threat model and its priority order. Out of scope: multi-tenancy, internet
   exposure, certificate lifecycle management, authenticated non-repudiation of operator
   actions. In scope, in order: (1) integrity of the **control path**, (2) integrity and
   availability of the **historian**, (3) confidentiality of metadata, (4) availability.
   ```bash
   grep -n '^#' docs/SECURITY.md | head -30
   ```
2. **Confirm there are three gaps, not two.** `docs/SECURITY.md` names three: OPC UA encryption,
   Modbus write protection, database credential scoping. The README's docs table says "the two
   known gaps" and `docs/VERIFYING.md` known-issue 9 is stale. Verify the SECURITY doc:
   ```bash
   grep -n 'three gaps\|two known gaps\|three known gaps' docs/SECURITY.md README.md docs/VERIFYING.md
   ```
   **FINDING:** inconsistent counts across docs. See F4/Q4.
3. **Gap 1 — OPC UA engineering ranges are advisory.** Ranges live in `EUInformation`,
   metadata for display; `asyncua` does not enforce. A remote client can write `DO = -40` and
   the historian records it with a valid tag name. Verify empirically:
   ```bash
   uv run python tools/opcua_browser.py browse | head -20
   uv run python tools/opcua_minimal_client.py
   ```
   Then write an out-of-range value and confirm it is accepted by the *wire* while the
   in-process writer would have refused it:
   ```bash
   uv run python - <<'PY'
   import asyncio
   from asyncua import Client
   async def main():
       async with Client(url="opc.tcp://127.0.0.1:4840/wwtp/server/") as c:
           node = c.get_node("ns=2;AERATION.AHU-1.DO")
           await node.write_value(-40.0)
           print("written:", await node.read_value())
   asyncio.run(main())
   PY
   ```
   Expect the write to **succeed**. Then check the historian:
   ```sql
   SELECT value, quality FROM reading WHERE signal_id='AERATION:AHU-1:DO'
   ORDER BY ts DESC LIMIT 3;
   ```
   A `-40` reaching `reading` is the gap, demonstrated. `docs/SECURITY.md` calls this "the
   single most important thing to take from this document."
4. **Gap 2 — Modbus is read-only by contract, not by the wire.** Function code 6 is answered
   for *any* address in range; no auth, no authz, no integrity. "The security model is 'the
   cable.'" Verify with `uv run pytest tests/test_modbus.py tests/test_control.py -q`
   (63 + 26 tests) and read what the control path does and does not check.
5. **Gap 3 — three services authenticate as the database owner.**
   ```bash
   for s in gateway web scada grafana seed init-db; do
     printf '%-9s ' "$s"
     docker compose --profile ui --profile scada --profile observability --profile demo \
       config | python3 -c "
   import sys,yaml,os
   cfg=yaml.safe_load(sys.stdin)
   env=cfg['services']['$s'].get('environment',{})
   print(env.get('POSTGRES_USER','<unset>'), '/', env.get('POSTGRES_HOST',''))"
   done
   # expect: gateway = wwtp_gateway (scoped)
   #         web     = wwtp_ui     (scoped)
   #         scada   = wwtp        (OWNER — gap)
   #         grafana = wwtp        (OWNER — gap)
   #         db / init-db / seed  = owner (legitimate or arguable)
   ```
   Confirm the two scoped ones really are scoped (A5.5, A5.7) and record the two that are not.
   The framing in `docs/SECURITY.md`: "it has scoped the hard cases and left the easy ones."
   The fix is one `storage.postgres.login_role` call each and is **not done**.
6. **Non-root containers.** Enforced but — per `docs/SECURITY.md` — **not fully tested**:
   ```bash
   for s in gateway softplc web scada; do
     printf '%-8s ' "$s"
     docker compose --profile ui --profile scada run --rm --no-deps --entrypoint sh "$s" -c 'id -u' 2>/dev/null
   done
   # expect: 10001 (gateway, softplc), 10002 (web), 1000 (node-red/scada)
   ```
   `tests/test_web_page.py` asserts the web image's `USER`; **nothing asserts the Python
   image's.** Verify the Python one by hand — that gap is the finding.
7. **Capability drops and no-new-privileges:**
   ```bash
   docker compose config | grep -E 'cap_drop|no-new-privileges|read_only|init:'
   # expect: cap_drop [ALL] on Python services; no-new-privileges on all; read_only on web
   docker compose ps --format '{{.Name}}' | while read c; do
     printf '%-26s ' "$c"
     docker inspect --format '{{.HostConfig.CapDrop}} np={{.HostConfig.SecurityOpt}} user={{.Config.User}}' "$c"
   done
   ```
   Note `db` and `grafana` get `no-new-privileges` but **no `cap_drop`**.
8. **Enforcement vs not-enforcement table.** Verify each row of `docs/SECURITY.md`'s table:
   | Control | Status | How to verify |
   |---|---|---|
   | non-root containers | enforced, **untested** | step 6 |
   | `no-new-privileges` | enforced | step 7 |
   | `cap_drop: ALL` | enforced (Python only) | step 7 |
   | loopback-only ports | **DONE**, test exists | E2.4 |
   | log rotation | enforced (Python only) | E3.8 |
   | OPC UA encryption | **NOT enforced** | step 3 |
   | OPC UA authentication | **NOT enforced** (anonymous, `SecurityPolicyNone`) | step 3 |
   | Modbus authentication | **NOT enforced** (impossible) | step 4 |
   | Modbus write protection | **NOT enforced** (contract only) | step 4 |
   | network segmentation | **NOT enforced** (one flat bridge) | step 9 |
   | TLS termination | **NOT enforced** | — |
9. **Network exposure.** One user-defined bridge `plant` (`name: wwtp-plant`, `driver: bridge`,
   `internal: false`) with every service on it. `internal: false` is required for the image
   builds and package installs; `compose.yaml` notes the soft PLC should not have egress in a
   real plant.
   ```bash
   docker network inspect wwtp-plant --format '{{json .Containers}}' | python3 -m json.tool | grep Name
   ```
   Expect all 8 services. **FINDING:** flat network, no segmentation, egress allowed.
10. **Secret handling:**
    ```bash
    head -9 .gitignore                       # expect .env first, then .env.*, !.env.example
    git ls-files | grep -xE '\.env|flows_cred\.json|\.credentialSecret|credentials\.json|jupyter-url'  # expect: no output
    git check-ignore -v .env scada/nodered/.credentialSecret notebooks/.jupyter-url workshops/ml/.jupyter-url
    ```
    Not done, and stated: **no Docker secrets or Vault** (env vars are visible in
    `docker inspect`), **no rotation story**, **no secret scanning in CI** ("deliberately not
    added here rather than added badly").
    ```bash
    docker inspect wwtp-iiot-gateway-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i password
    # expect: GATEWAY_DB_PASSWORD=<value> visible — the documented limitation
    ```
11. **Base image pinning:** by tag, not digest, with the tag-vs-digest argument written out and
    then **retracted** as "wrong in general" — InfluxDB published only SHAs on Quay;
    TimescaleDB publishes `2.30.1-pg16`. The conclusion recorded is that "a pinning policy
    should be written per dependency." **FINDING: no written policy.**
    ```bash
    grep -rnE '^FROM' docker/Dockerfile.python ui/web/Dockerfile scada/nodered/Dockerfile
    ```
12. **CI secrets:** none used. All four passwords are workflow-level literals:
    ```bash
    grep -n 'secrets\.' .github/workflows/gates.yml    # expect: no output
    grep -nE 'POSTGRES_PASSWORD|GATEWAY_DB_PASSWORD|WEB_DB_PASSWORD|GRAFANA_ADMIN_PASSWORD' .github/workflows/gates.yml
    # expect: itpass / itpass-gateway / itpass-web / itpass-grafana
    ```
    Correct for a throwaway database created and destroyed inside the runner.
13. **Control-path integrity** — the top-priority asset. Verify the operator control write is
    the only path and that it is audited:
    ```bash
    docker compose exec -T db psql -U wwtp -d wwtp -c \
      "SELECT ts, kind, severity, message, detail FROM event
       WHERE kind NOT LIKE 'alarm_%' ORDER BY ts DESC LIMIT 10;"
    ```
    After B6.8, expect `INSERT INTO event` rows for accepted setpoint writes ("Operator DO
    setpoint change"). **An unaudited control write is a finding** — verify the audit trail
    exists.

**Expected result** — three gaps confirmed and demonstrated; two services on the owner role; non-root verified including the untested Python image; loopback-only ports; no secrets tracked; CI uses no secrets.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if any tracked file contains a credential, if any port binds
  to `0.0.0.0`, or if a container runs as root.
- **FINDINGS to record** (all documented, all expected): three gaps vs "two"; ranges advisory;
  Modbus writable on the wire; `scada` + `grafana` on the owner role; Python non-root asserted
  by no test; flat network; env-var secrets visible in `docker inspect`; no secret scanning in
  CI; no rotation story; no digest pinning policy.
- **Step 3's `-40` write is expected to succeed.** If it is *refused*, the gap has been closed
  — record that as a positive finding and update Q-list accordingly.

**Evidence** — the SECURITY.md heading list, the gap-count grep, the `-40` write transcript plus
the resulting `reading` rows, the per-service `POSTGRES_USER` table, the `id -u` outputs per
image, the capability/privilege dump, the enforcement table with a column per row, the network
inspect, the `.gitignore`/`git ls-files`/`git check-ignore` outputs, the
`docker inspect | grep password` transcript, the `FROM` lines, the CI secret greps, the
control-path audit query.

**Risks / caveats**
- Step 3 **writes a physically impossible DO value into the historian.** Do it on a scratch
  database, or delete the rows afterwards:
  ```sql
  DELETE FROM reading WHERE value < 0;
  ```
  The writer role has no `DELETE`; use the owner.
- Step 4/10 are read-only verification of documented absences.
- This whole section verifies that the documented threat model is *accurate*. It does not
  attempt to close the gaps — closing them is an engineering task, not QA.

---

## F2. Observability — logs, metrics, traces

**Objective** — know exactly where each signal lives, and that it is emitted when something breaks.

**Pre-conditions** — E1.

**Steps**
1. **Logging.** Structured log lines with `key=value` summaries:
   ```bash
   docker compose logs gateway | grep 'polls=' | tail -5
   # polls=N offered=N published=N (P% filtered) spool=N files db=<name> modbus_err=N opcua_err=N
   docker compose logs softplc | tail -5
   docker compose logs --since 10m softplc gateway | grep -ciE 'traceback|exception'
   ```
   `--report-every 30.0` controls the gateway period; `GATEWAY_LOG_LEVEL` and `--log-level`
   control verbosity. `softplc` has `--verbose` to log **every** scan.
2. **Log rotation, and where it is missing:**
   ```bash
   docker compose config | grep -A4 'logging:'
   ```
   Expect `json-file`, `max-size: 10m`, `max-file: 3` — **on the `x-python-service` anchor
   only.** `db` and `grafana` use `x-restart`, which has no `logging:` block. **FINDING:**
   unbounded logs on the two longest-running services.
   ```bash
   docker inspect --format '{{.HostConfig.LogConfig}}' wwtp-iiot-db-1
   ```
3. **The gateway health object** is the closest thing to a metrics endpoint, and it is
   in-process only (E3.4):
   ```bash
   uv run python -m gateway.main --iterations 20 --report-every 5 --log-level INFO
   ```
   Stats: `polls`, `offered`, `published`, `spooled`, `dropped_overflow`, `modbus_failures`,
   `opcua_failures`, `uptime_s`. `health()` adds `spool`, `postgres`, `deadband{offered,published}`.
   Also `Deadband.accepted_since_start()`, `offered_since_start()`,
   `suppression_ratio(signal_id)`, `baseline(signal_id)`.
4. **Confirm there is no Prometheus / OpenTelemetry / metrics endpoint.** The Grafana
   datasource is PostgreSQL, not Prometheus — so Grafana panels are SQL, not PromQL:
   ```bash
   grep -rniE 'prometheus|opentelemetry|otel|meter|histogram|/metrics' \
     --include='*.py' --include='*.toml' . | grep -v '.venv\|node_modules' | head
   # expect: no output
   ```
   **This is why there are no Grafana alert rules (B5.10).**
5. **Traces:** none. There is no tracing instrumentation anywhere. **FINDING.**
6. **The `event` table is the durable observability record.** Its kinds are the audit trail:
   ```sql
   SELECT kind, count(*), min(ts), max(ts) FROM event GROUP BY 1 ORDER BY 2 DESC;
   -- expect only: alarm_raised, alarm_cleared, alarm_acknowledged,
   --               plus operator-control audit rows from Node-RED flow 3
   ```
   ```sql
   -- the "what has stopped reporting" view, as an observability query
   SELECT s.id, now() - max(r.ts) AS silence
   FROM signal s LEFT JOIN reading r ON r.signal_id = s.id
   GROUP BY s.id HAVING max(r.ts) IS NULL OR now() - max(r.ts) > interval '1 hour'
   ORDER BY silence DESC NULLS FIRST LIMIT 10;
   ```
7. **`tools/db_ready`** is the diagnostic used by `make db-up` and `make db-live`:
   ```bash
   uv run python -m tools.db_ready; echo "exit=$?"
   docker compose stop db; uv run python -m tools.db_ready; echo "exit=$?"   # expect non-zero
   docker compose start db
   uv run pytest tests/test_db_ready.py -q    # expect 5 tests
   ```
8. **Alarm observability:** `make coverage-json > coverage.json` is the machine-readable
   artefact, uploaded by the nightly CI job as the `alarm-coverage` artifact:
   ```bash
   make coverage-json && python3 -m json.tool coverage.json | head -40
   ```
   `ruff-findings` is uploaded by the `lint-debt` job. These are the only CI artifacts.
   ```bash
   grep -n 'upload-artifact' .github/workflows/gates.yml
   ```
9. **`docs/CI.md` is the gate index.** Cross-check every documented local command actually
   runs:
   ```bash
   for t in lint lint-debt types test integration sql lessons notebooks \
            workshop-notebooks scada-check dashboards-check tableplus-check page-check \
            web-build coverage; do
     grep -q "^$t:" Makefile && echo "ok: $t" || echo "MISSING: $t"
   done
   ```
10. **Confirm `make check` coverage honestly.** It runs
    `lint lint-debt types test sql lessons`. It does **not** cover `integration`, `notebooks`,
    `workshop-notebooks`, any drift gate, `coverage`/nightly, the image builds,
    `alarms.replay`, `test_scada_contract.py`, `test_grafana_dashboards.py`, or
    `test_alarm_replay.py -m integration`. **FINDING** if a doc calls `make check` "the full
    suite".

**Expected result** — `key=value` logs; log rotation on Python services only; no Prometheus/OTel/traces; the `event` table as the durable audit record; `tools.db_ready` exits non-zero when the DB is down; two CI artifacts; every documented target exists.

**Pass/fail criteria**
- PASS: as above. **FAIL** if `tools/db_ready` exits 0 with the DB stopped; if any documented
  target in `docs/CI.md` is missing from the Makefile; if a traceback appears in normal
  operation.
- **FINDINGS to record:** no metrics endpoint; no tracing; no log rotation on `db`/`grafana`;
  `make check` is not the full suite.

**Evidence** — log excerpts, the logging config, the `x-python-service` vs `x-restart`
comparison, `docker inspect` LogConfig, the Prom/OTel grep (empty), the `event` group-by, the
silence query, both `db_ready` exit codes, `coverage.json`, the `upload-artifact` grep, the
target-existence loop, the `make check` coverage list.

**Risks / caveats**
- Absence of tracing is a **design choice** consistent with "no message bus, no ORM, one
  Python image". Record it as a gap in the brief's observability expectations, not a defect.
- The `event` table is the only durable cross-component record. Since `wwtp_ui` has
  `SELECT` on it, the web `/alarms` page is the intended consumer.

---

## F3. Error handling and failure manifestation

**Objective** — every failure mode fails loudly, with a message that names the cause.

**Pre-conditions** — E1. Several steps are destructive; take an E4 dump first.

**Steps**
1. **Enumerate the documented failure table** and test each. From `docs/DATA-FLOW.md` plus the
   compose config:

   | Injected failure | Expected manifestation | Verify |
   |---|---|---|
   | DB down | nothing written; spool drains | E4.2 |
   | one batch fails | one WARNING; retried in arrival order | E4.2 log |
   | gateway killed | nothing; volume retains backlog | E4.7 |
   | spool volume lost | **backlog lost** — unrecoverable | E4.8 |
   | a signal goes quiet | an ambiguous gap (no data ≠ bad data) | A7.7, B3.8 |
   | a float decoded low-word-first | `~2.3e-41`; **nothing detects it** | A7.5 |
   | a batch fails 3× consecutively | `SystemExit`, not a hang | A4.4 |
   | `--reset` without `--database` | refused | A4.1 |
   | a required env var missing | compose refuses to interpolate | E1.7 |
   | a password wrong | `FATAL: password authentication failed for user "wwtp_gateway"` | below |

2. **Verify the bad-password message names the user.** The compose comment records this exact
   failure, caused by two variables having disagreed:
   ```bash
   sed -i.bak 's/^GATEWAY_DB_PASSWORD=.*/GATEWAY_DB_PASSWORD=wrong/' .env
   docker compose up -d --force-recreate gateway; sleep 20
   docker compose logs gateway --since 1m | grep -i 'password authentication'
   # expect: FATAL: password authentication failed for user "wwtp_gateway"
   mv .env.bak .env; docker compose up -d --force-recreate gateway
   ```
   **The message must name `wwtp_gateway`, not `wwtp`.** A generic message is a finding.
3. **Verify `GATEWAY_DB_PASSWORD` has no fallback.** `${GATEWAY_DB_PASSWORD:?...}` — compose
   explicitly rejects `${A:-${B}}` because "a silent fallback to the owner's password is a
   working configuration that defeats the point of having a scoped credential":
   ```bash
   grep -n 'GATEWAY_DB_PASSWORD\|WEB_DB_PASSWORD' compose.yaml
   # expect :? markers, no :-
   ```
   **This directly contradicts `docs/VERIFYING.md` known-issue 9**, which claims they "fall back
   to `POSTGRES_PASSWORD` if left empty". **FINDING — stale doc.** See F4/Q4.
4. **Verify the Node-RED required-password guard:**
   ```bash
   docker compose --profile scada run --rm -e POSTGRES_PASSWORD= scada 2>&1 | tail -3
   # expect: POSTGRES_PASSWORD is required to build the Node-RED database credential
   ```
5. **Verify `lib/db.ts` throws rather than defaulting to `localhost`** — a silent localhost
   default would connect to the wrong database (or the wrong one at all):
   ```bash
   docker compose --profile ui run --rm --no-deps -e POSTGRES_HOST= web \
     node -e "process.exit(0)" 2>&1 | tail -3
   # expect: an error naming POSTGRES_HOST, not a silent localhost connection
   ```
   Also check the source: `grep -n 'localhost' ui/web/lib/db.ts`.
6. **Verify the seeder's aborted-COPY path** (A4.4) and the hard-failure mode:
   ```bash
   uv run python -m storage.seed.main --days 1 --database no_such_db; echo "exit=$?"
   # expect: a clear "database does not exist" and exit non-zero, no partial write
   ```
7. **Verify the notebook port-vs-password diagnosis.** The classic misdiagnosis is `dsn()`
   falling back to 5432 and reporting a password error when the fault is the port:
   ```bash
   uv run python -c "
   import sys; sys.path.insert(0,'notebooks')
   import _data; d=_data.dsn()
   print('port', d.get('port'), 'dbname', d.get('dbname'))"
   ```
   Confirm the port matches `.env`. A mismatch is a finding.
   ```bash
   uv run pytest tests/test_db_ready.py tests/test_jupyter_url.py -q   # 5 + 12 tests
   ```
8. **Verify the alarm engine never raises a `critical` clear** (the latch contract, B5.5):
   ```sql
   SELECT count(*) FROM event e
   WHERE e.kind='alarm_cleared' AND e.severity='critical';
   -- expect 0
   ```
9. **Verify unknown nodes fail loudly, not silently.** Unknown deadband mode degrades to
   `ABSOLUTE` (the safe chatty one), but an **unknown detector name should `KeyError`**:
   ```bash
   uv run python -c "
   from alarms.detectors import detect
   try:
       detect('no_such_detector', window=None); print('FAIL: silently accepted')
   except Exception as e:
       print('ok:', type(e).__name__)"
   ```
   And that a suppressed reading never updates the baseline — a quiet failure mode:
   ```bash
   uv run pytest tests/test_deadband.py -q
   ```
   Verify specifically that NaN/inf always publishes (a NaN comparison is always False, so a
   NaN would otherwise suppress a reading **forever**):
   ```bash
   uv run python -c "
   from gateway.deadband import Deadband, BandRule
   d=Deadband(rules={'X': BandRule(1.0, 0.5, 'absolute', 100.0)})
   d.accept('X', float('nan'), 0); d.accept('X', 0.0, 0)
   print('published', d.published_since_start() if hasattr(d,'published_since_start') else d.accepted_since_start())"
   ```
10. **Verify one protocol failing does not kill the gateway** (the queue is the only shared
    state, and Modbus runs in a worker thread):
    ```bash
    docker compose stop softplc; sleep 90
    docker compose logs gateway --since 2m | grep -cE 'modbus_err|opcua_err'   # expect climbs
    docker compose ps gateway                                                  # expect healthy
    docker compose start softplc
    ```
    An unhealthy gateway when the PLC is down is arguably wrong — the gateway's job is to
    spool while it cannot reach the plant. Record which behaviour you observe.
11. **Verify the documented order-dependent flake.** `docs/CI.md` records
    `test_scanloop.py::test_pace_divides_by_speed_so_a_backfill_is_not_throttled` failing in a
    full run and passing alone — the only known order-dependent test:
    ```bash
    uv run pytest "tests/test_scanloop.py::test_pace_divides_by_speed_so_a_backfill_is_not_throttled" -q
    # expect: PASS alone
    uv run pytest tests/ -q -m "not slow and not integration" 2>&1 | tail -20
    # expect: the same test to FAIL here → confirms the documented flake
    ```
    **FINDING** if it is still order-dependent. This is a wall-clock test and should be fixed.

**Expected result** — every documented failure mode manifests loudly and names its cause; bad password names `wwtp_gateway`; no secret fallback; `db.ts` throws; seeder refuses; no `critical` clears; unknown detector raises; NaN always publishes; the gateway stays healthy when the PLC is down.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** on any *silent* failure: a missing env var defaulting, an
  unknown detector accepted, a bad password producing a generic message, or a NaN suppressing
  a reading.
- **FINDINGS to record:** `docs/VERIFYING.md` known-issue 9 is stale (no password fallback);
  the scanloop test is still order-dependent; the gateway's health when the PLC is down.

**Evidence** — the failure table with a verify result per row, the bad-password transcript,
the `GATEWAY_DB_PASSWORD` grep, the Node-RED guard, the `db.ts` run and source grep, the seeder
bad-target exit code, the port diagnosis, the critical-clear count, the unknown-detector
transcript, the deadband pytest summary, the NaN transcript, the PLC-down gateway status, the
scanloop alone-vs-full run.

**Risks / caveats**
- Steps 2, 6 and 10 involve restarting containers. Restore `.env` immediately after step 2.
- The NaN test in step 9 constructs a `BandRule` directly; check the constructor signature in
  `gateway/deadband.py` if the call signature differs.
- The `event` count in step 8 assumes B5 has run and produced events. Run B5 first, or expect 0
  trivially.

---

## F4. Documentation accuracy

**Objective** — every runbook, count and claim in `docs/` and `README.md` matches the code, or is logged as a finding.

**Pre-conditions** — E1.

**Steps**
1. **The repo already has an automated claim checker.** `tests/test_readme_claims.py` (83
   tests) asserts documented per-file test counts against `pytest --co`, the course count
   against `tools/check_sql.py`'s own output, and mypy's file count. Run it first — several
   findings below may already be caught by it:
   ```bash
   uv run pytest tests/test_readme_claims.py -q    # expect 83 tests
   ```
   ```bash
   uv run pytest tests/test_getting_started_agrees.py -q   # expect 13 — README and
                                                             # GETTING-STARTED must not diverge
   uv run pytest tests/test_readme_teaches_no_destructive_command.py -q   # expect 173
   uv run pytest tests/test_clean_machine_test_plan.py -q
   ```
2. **Establish the real numbers**, then compare to the docs:
   ```bash
   uv run pytest tests/ -q -m "not slow and not integration" --ignore=tests/integration \
     --co -q 2>/dev/null | tail -3
   uv run pytest tests/integration -q --co 2>/dev/null | tail -3
   uv run pytest tests/ -q -m "slow" --co 2>/dev/null | tail -3
   uv run mypy softplc gateway storage alarms scada tools ui workshops 2>&1 | tail -3
   uv run ruff check . --output-format concise 2>/dev/null | grep -cE ':[0-9]+:[0-9]+:'
   cat lint-debt-baseline.txt   # expect 156
   ```
   **Current values:** 1167 unit / 48 integration / 5 slow / 155 lint findings / 73 mypy files
   / 78 SQL queries in 26 files / 87 snippets in 18 lessons.
   ```bash
   grep -nE '1167|48|155|73|78|87|4303' docs/TESTING.md | head
   ```
3. **Find the stale block in `docs/VERIFYING.md`.** Its Part 10 expected-output block says
   **606 unit tests, 159 lint findings, 55 mypy files, 46 integration**. Current values are
   1167 / 155 / 73 / 48. **FINDING — stale.**
   ```bash
   grep -nE '606|159|55 mypy|46 ' docs/VERIFYING.md
   ```
4. **Find the stale dashboard titles.** `docs/VERIFYING.md:598` says to expect **"Plant
   overview"** and **"Discharge permit"**. Actual: **`WWTP — overview`** and
   **`WWTP — discharge permit`** (em dash, uid `wwtp-overview` / `wwtp-permit`).
   **FINDING — stale.** (B2.7.)
5. **Find the stale password-fallback claim.** `docs/VERIFYING.md` known-issue 9 says
   `GATEWAY_DB_PASSWORD` and `WEB_DB_PASSWORD` "fall back to `POSTGRES_PASSWORD` if left
   empty". They do not — compose uses `${VAR:?}` and explicitly rejects the fallback.
   **FINDING — stale.** (F3.3.)
6. **Find the stale gap count.** `README.md` says SECURITY has "the two known gaps";
   `docs/SECURITY.md` names **three**; `docs/LEARNING-LOG.md:1607` says "fifteen rules" while
   there are 16; `Makefile:908` says "twelve faults against sixteen rules" (correct);
   `alarms/rules.py:1` says "Fourteen rules" and `:24-27` says "three rules that are not
   thresholds" (there are four). **FINDING — several stale counts.**
   ```bash
   grep -n 'two known gaps' README.md
   grep -n 'three gaps' docs/SECURITY.md
   grep -n 'Fourteen rules\|fourteen rules\|fourteen rules over' alarms/rules.py
   grep -n 'Sixteen rules over eleven signals\|16 rules' docs/ALARMS.md
   ```
7. **Find the stale schema doc.** `sql/_shared/SCHEMA.md:42-43` describes a **`signal.holder`
   column** — "Those signals have `equipment_id = NULL` and a separate `signal.holder` naming
   the grouping." **No `holder` column exists.** The doc describes the pre-migration design.
   **FINDING — stale.** Confirm:
   ```bash
   grep -rn 'holder' storage/postgres/schema.sql sql/_shared/SCHEMA.md
   uv run pytest tests/test_postgres_schema.py -q   # pins the exact 16-column set
   ```
8. **Find the stale `compose.yaml` header.** Lines 12-17 still describe the stack as **two
   databases** ("InfluxDB 3 answers… Couchbase answers… running both in one process"), and line
   4 numbers them "the six core services" when 8 are defined. The InfluxDB/Couchbase stack was
   migrated to Postgres+Timescale. **FINDING — stale.**
   ```bash
   sed -n '1,20p' compose.yaml
   grep -cE 'influx|couchbase' compose.yaml
   docker compose config --services | wc -l   # expect 8
   ```
9. **Confirm the dead directories are dead.** `storage/influx`, `storage/couchbase` and
   `storage/rollup_worker` contain **only `__pycache__`** — no source. The rollup worker was
   deliberately replaced by the continuous aggregates; the `source` CHECK still admits
   `'rollup'` as a legacy value.
   ```bash
   ls -la storage/influx storage/couchbase storage/rollup_worker
   grep -rn 'storage.rollup_worker' --include='*.py' . | grep -v '.venv'   # expect: no output
   grep -n "'rollup'" storage/postgres/schema.sql                           # expect: present (legacy)
   ```
   **FINDING:** orphaned bytecode and a legacy enum value remain. Clean-up candidates, not bugs.
10. **Verify every service name a document tells the reader to type is real:**
    ```bash
    uv run pytest tests/test_readme_claims.py -q -k compose
    ```
    and by hand:
    ```bash
    docker compose --profile ui --profile scada --profile observability --profile demo \
      config --services | sort | tr '\n' ' '
    # expect: db gateway grafana init-db scada seed softplc web
    ```
    Cross-check against the docs:
    ```bash
    grep -rhoE 'docker compose (--profile [a-z]+ )?(up|run|exec|logs|ps|down)[a-z-]* [a-z-]+' docs/ README.md \
      | sort -u | head -30
    ```
11. **Verify the machine-specific numbers are labelled as such.** `docs/VERIFYING.md` step 4.5
    gives `reading_1m ≈ 198 000` and `reading_1h ≈ 6 400`; the notebook gate expects 185,455 and
    5,882. **FINDING — machine-specific figures presented as expectations**, which is exactly
    what `check_portable_numbers` forbids in notebook source.
    ```bash
    grep -nE '198 ?000|6 ?400' docs/VERIFYING.md
    ```
12. **Verify the READMEs' quick start is accurate and identical in both files:**
    ```bash
    grep -n -A6 'Quick start\|short version' README.md docs/GETTING-STARTED.md | head -30
    ```
    Both should be `make up`, `make query SQL="SELECT count(*) FROM reading"`, and the three
    `tools/opcua_browser.py` commands.
13. **Check the runbooks a QA engineer would actually follow.** For each, actually follow it:
    | Runbook | Follow it and verify |
    |---|---|
    | `README.md` "Quick start" | E1.5 |
    | `docs/GETTING-STARTED.md` | E1 steps |
    | `docs/TESTING.md` | every command in its inventory |
    | `docs/VERIFYING.md` (13 parts) | every pasteable command; note stale expectations (steps 3, 5, 10) |
    | `docs/CI.md` | every local-equivalent command |
    | `docs/CLEAN-MACHINE-TEST-PLAN.md` (27 steps, 5 phases) | as the master E2E script |
    | `docs/ALARMS.md` | B5 |
    | `docs/ALARM-TUNING.md` | `python -m alarms.tune` |
    | `docs/SECURITY.md` | F1 |
    | `docs/DATA-FLOW.md` | the failure table (F3.1) |
    | `docs/ARCHITECTURE.md` / `docs/DESIGN.md` | §1.1–1.6 of this plan |
    | `docs/DASHBOARD-STORIES.md` | B3/B4 |
    | `docs/GLOSSARY.md` / `docs/TERMINOLOGY.md` | spot-check 10 terms against `contracts/` |
    | `docs/LEARNING-LOG.md` (4303 lines) | **historical**; treat stale counts as expected |
    ```bash
    ls docs/adr/    # ADRs are the design record
    ```
14. **Record the known issues `docs/VERIFYING.md` itself lists (11 of them)** and confirm each
    is still open. The most important:
    - the **factor-of-7** alarm-harness disagreement (first open thread)
    - **5 unfixable false-positive** rules; **4 rules never fired**
    - **no TS test runner** (`ui/web` untested)
    - `dsn()` defaulting `POSTGRES_PASSWORD` to `"wwtp"`
    - **the gateway never reconnects to Postgres** (found with `failures: 29702` while
      healthy)
    ```bash
    grep -n 'Known issues' -A40 docs/VERIFYING.md | head -50
    ```
15. **Verify the clean-machine plan's own numbers are current.** It is the master script:
    ```bash
    grep -nE '1167|48|155|73|4\.2|28728|42284|28,728' docs/CLEAN-MACHINE-TEST-PLAN.md | head -20
    ```

**Expected result** — `test_readme_claims.py` (83) and `test_getting_started_agrees.py` (13) pass; current counts confirmed; the four stale blocks located; dead directories confirmed dead; every documented service name real.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if `test_readme_claims.py` or
  `test_getting_started_agrees.py` fails — those are the automated guards.
- **FINDINGS to log** (each with file:line): `docs/VERIFYING.md` Part 10 stale counts;
  `docs/VERIFYING.md:598` stale dashboard titles; `docs/VERIFYING.md` known-issue 9 stale
  password-fallback claim; `README.md` "two gaps" vs three; `alarms/rules.py` docstring
  counts; `sql/_shared/SCHEMA.md` non-existent `holder` column; `compose.yaml` header
  describing two databases and "six core services"; `docs/VERIFYING.md` machine-specific
  aggregate counts; orphaned `storage/{influx,couchbase,rollup_worker}` bytecode; the legacy
  `'rollup'` source value.
- A document being stale is a **finding, not a blocker.** Do not edit docs to match; that is
  the maintainer's call. Record and move on.

**Evidence** — the four pytest summaries, the actual-vs-documented count table, each stale
block quoted with file:line, the `holder` grep, the `compose.yaml` header, the dead-directory
listings, the services list, the runbook-follow log with a row per document, the VERIFYING.md
known-issues list with an open/closed column.

**Risks / caveats**
- **`docs/LEARNING-LOG.md` is 4,303 lines and is a historical record.** Stale counts in it are
  expected and are not findings. Note this distinction so the report is not padded.
- Stale docs do not block a release. Blockers are: data loss, credential leakage, silent
  failures, and a wrong pass/fail verdict in a gate.
- `docs/CLEAN-MACHINE-TEST-PLAN.md` needs ~60 GB free and 90 min–2 h for phases 1–3, plus
  ~19 GB and 46 min for the optional phase 4.

---

## F5. Drift gates and generated-artifact integrity

**Objective** — prove nothing generated was hand-edited, and that the contract is the single source of truth.

**Pre-conditions** — none for most gates; no database needed.

**Steps**
1. **Run the whole drift gate suite** (the CI `drift` job):
   ```bash
   uv run python -m scada.generate_tags --check;        echo "tags=$?"
   uv run python -m scada.build_flows --check;          echo "flows=$?"
   uv run python -m ui.grafana.generate_dashboards --check; echo "dashboards=$?"
   uv run python -m tools.extract_sql --check;           echo "sql=$?"
   uv run python -m tools.opcua_address_space --check;   echo "address-space=$?"
   uv run python -m ui.web.generate_page --check;        echo "page=$?"
   docker compose --profile ui --profile scada --profile observability config -q; echo "compose=$?"
   # expect: all 0
   ```
   Or via make: `make scada-check dashboards-check tableplus-check page-check`.
2. **Verify the drift gates are bidirectional** — a `--check` that passes on an *unmodified*
   tree but also passes on a *modified* tree is worthless:
   ```bash
   cp ui/grafana/dashboards/wwtp-overview.json /tmp/wwtp-overview.json.bak
   python3 -c "
   import json; f='ui/grafana/dashboards/wwtp-overview.json'
   d=json.load(open(f)); d['title']='TAMPERED'; json.dump(d,open(f,'w'),indent=2)"
   uv run python -m ui.grafana.generate_dashboards --check; echo "expect non-zero: $?"
   cp /tmp/wwtp-overview.json.bak ui/grafana/dashboards/wwtp-overview.json
   uv run python -m ui.grafana.generate_dashboards --check; echo "expect 0: $?"
   ```
   Repeat for at least one of each: `scada/flows/01-mimic.json` (flows),
   `ui/web/lib/contract.json` (page), and a `sql/TablePlus/*.sql` file.
3. **Verify the generated file inventory:**
   ```bash
   ls scada/flows/                       # 01-mimic 02-annunciator 03-control tags.json
   ls ui/grafana/dashboards/             # wwtp-overview.json wwtp-permit.json
   ls ui/web/lib/contract.json
   ls sql/TablePlus/ | wc -l             # expect 64
   ls notebooks/*.ipynb | wc -l          # expect 10 (see Q7)
   ls workshops/ml/*.ipynb | wc -l       # expect 6
   ```
4. **Verify stable node ids.** `nid(name)` is `"a" + sha1(name)[:12]` — deliberately **not**
   `hash()`, which is salted per process, so flow diffs would be noise:
   ```bash
   uv run python -m scada.build_flows
   git diff --stat scada/flows/          # expect: empty, twice in a row
   uv run python -m scada.build_flows
   git diff --stat scada/flows/          # expect: still empty
   ```
   If the second run produces a diff, node ids are not stable — **FAIL**.
5. **Verify cell ids are content-derived.** `cell_id(kind, text)` =
   `f"c-{kind}-{blake2b(text, digest_size=4).hexdigest()}"`, so Jupyter rewriting ids does not
   cause false drift:
   ```bash
   uv run python -m tools.build_notebooks
   git diff --stat notebooks/            # expect: empty, twice
   uv run python -m tools.build_notebooks
   git diff --stat notebooks/            # expect: empty
   ```
6. **Verify the tags flow file is excluded from assembly.** `tags.json` is a tag list, not a
   flow — excluding it by name:
   ```bash
   grep -n 'tags.json' scada/nodered/entrypoint.sh
   docker compose --profile scada up -d scada
   docker compose logs scada | grep assembled    # expect: 3 file(s), 41 nodes
   ```
7. **Verify the contract is genuinely the only source of meaning.** Confirm the derived counts
   all trace to it:
   ```bash
   make contract | wc -l                        # 57
   docker compose --profile scada config >/dev/null
   uv run python -m tools.opcua_address_space | head -20
   # expect 8 areas / 22 equipment / 57 signals / 138 leaves
   ```
   `docs/CLEAN-MACHINE-TEST-PLAN.md` phase 1 expects exactly **8 areas / 22 equipment /
   57 signals / 138 leaves** from the browse. Verify live:
   ```bash
   uv run python tools/opcua_browser.py browse | head -30
   uv run pytest tests/test_opcua_address_space.py -q    # expect 12 tests
   ```
8. **Verify the lint-debt ratchet behaves correctly** — it fails only on growth:
   ```bash
   cat lint-debt-baseline.txt                        # 156
   uv run ruff check . --output-format concise > /tmp/ruff.txt || true
   grep -cE ':[0-9]+:[0-9]+:' /tmp/ruff.txt
   make lint-debt; echo "exit=$?"                    # expect 0 at 156
   ```
   Then verify it would fail on growth by adding one finding in a scratch file:
   ```bash
   echo "import os" > /tmp/x.py && cp /tmp/x.py tools/_drift_probe.py
   make lint-debt; echo "expect non-zero: $?"
   rm tools/_drift_probe.py
   make lint-debt; echo "expect 0: $?"
   ```
   And that `make lint` (scoped) is clean while `make lint-all` shows the 156:
   ```bash
   make lint; echo "scoped=$?"        # expect 0
   make lint-all 2>&1 | tail -3        # 156 findings
   ```
9. **Verify `make check` runs the gates it claims, and note what it omits** (F2.10).

**Expected result** — all 7 drift gates exit 0; each gate *fails* on a tampered file and passes again after restore; generation is idempotent twice over; the tag file is excluded; the contract trace yields 8/22/57/138; the lint ratchet fails on growth and passes at baseline.

**Pass/fail criteria**
- PASS: as above. **Hard FAIL** if any drift gate passes on a tampered file (a gate that cannot
  fail is worse than no gate); if generation is non-idempotent; if `make lint-debt` fails at
  baseline or passes with an extra finding.
- **FAIL** `make lint` non-zero.

**Evidence** — the seven gate exit codes; the tamper/restore transcripts for at least three
generators; the file inventory listing; the double-generation `git diff --stat` results; the
`entrypoint.sh` tags.json grep; the `assembled` line; `make contract | wc -l`; the browse
output with 8/22/57/138; the lint-debt before/during/after transcripts; `make lint` and
`make lint-all` outputs.

**Risks / caveats**
- Steps 2, 4, 5 and 8 modify tracked files. Always back up and restore, and verify
  `git status --short` is empty before continuing.
- `make lint-debt` emits `::error::` on growth and `::notice::` on shrinkage. Only growth fails
  by design.
- The 156 findings are **measured, not fixed**. Do not "fix" them — the ratchet exists to make
  growth visible. A rising number is the finding.

---

## Appendix A — Open questions and assumptions

These are things I could not resolve from the code alone. Each needs a maintainer answer. I
have written the affected test cases to **assert current behaviour and log the discrepancy**,
rather than guess.

| # | Question | Why it matters | Current behaviour | Affected cases |
|---|---|---|---|---|
| **Q1** | Does `signal.holder` exist? | `sql/_shared/SCHEMA.md:42-43` says grouping signals have "a separate `signal.holder` naming the grouping". **No `holder` column exists.** `holder` is derived from the id. `ARCHITECTURE.md` says the same thing correctly. | No column. `tests/test_postgres_schema.py` pins `signal` to exactly 16 columns. | A2.3, F4.7 |
| **Q2** | Are the SQL course's stored procedures expected? | The brief asks for tests of "stored procedures". **The project has none** — no user-defined functions, procedures or views. Only two Timescale continuous aggregates plus built-ins. | None exist, deliberately. | A0, Q6 |
| **Q3** | Was 60 s spool rotation correct, and is the naming confusing? | Files are named `spool-YYYYMMDD-HHMMSS.jsonl` by the minute+second they were **opened**, not the hour covered. Hourly was tried and rejected because `pending()` excludes the in-progress file, so nothing reached the DB for up to an hour. 60 s is described as the project's **maximum exposure**. | 60 s rotation; name = open time. | B6, E4.5 |
| **Q4** | Which doc claims are authoritative? | At least five disagree: `README.md` "two gaps" vs `SECURITY.md`'s three; `VERIFYING.md` known-issue 9 claims a password fallback that does not exist; `VERIFYING.md:598` gives wrong dashboard titles; `VERIFYING.md` Part 10 has stale counts; `rules.py` says "Fourteen rules". | Code wins; docs are stale. | F3.3, F4.2–4.6 |
| **Q5** | Is an installable CLI expected? | The brief says "an installable component (service/CLI/package)". `pyproject.toml` has **no `[project.scripts]`** — no pip-installable entry point. Everything is `python -m <module>`. The wheel packages 5 top-level modules. | No CLI. | E0 |
| **Q6** | Should migrations exist? | No `alembic`, no `schema_migrations`, no version tracking. One idempotent `IF NOT EXISTS` schema file. **This means a new column is never added to an existing table and a changed CHECK is never updated** — `IF NOT EXISTS` guards object creation only. | Deliberate, per `schema.py:82-88`. | E5.3 |
| **Q7** | Is notebook 07 missing or renamed? | `notebooks/07-what-are-you-estimating.md` has a `.md` extension **in `notebooks/` root**, so the builder (which globs `src/*.md`) never builds it. There is no `07-what-are-you-estimating.ipynb`. `docs/TESTING.md` and the CI job claim 11 notebooks. | 10 built. | C0, C2.3 |
| **Q8** | Is a model registry in scope? | The brief asks for "model registration" and "real-time inference". **Neither exists** — no `.pkl`/`.joblib`, no registry, no serving layer, no `mlflow`/`bentoml`. Models are fitted and discarded in notebook cells. | Not present, deliberately. | D0, D4, D7 |
| **Q9** | Which TimeSeriesDB version does CI use? | `compose.yaml` pins `timescale/timescaledb:2.30.1-pg16`; the CI `integration`/`notebooks`/`workshop-notebooks` jobs use a **service container** at `2.19.3-pg16`. A ten-minor-version gap. | Both are current; local and CI are not identical. | A6, D1 |
| **Q10** | Is `EVENT.GATEWAY`/`SOFTPLC`/`ALARM` in scope? | Compose defines **8** services but `docs/ARCHITECTURE.md` calls them "The five containers" and `compose.yaml:4` says "the six core services". | 8 defined, 4 behind profiles. | E1.8, F4.8 |
| **Q11** | Should the factor-of-7 alarm-harness disagreement block a release? | `docs/VERIFYING.md` lists it as the **first open thread** and says it undermines existing work. My B5 expects it and does not fail on it. | Known, open. | B5 (risks) |
| **Q12** | Should CI run on macOS? | Every CI job is `ubuntu-24.04`; no matrix, no OS variation. One gate (`free_port()` in the lesson checker) had a bug that was **structurally unfireable on macOS** because macOS starts ephemeral ports at 49152 and Linux at 32768. | Ubuntu only. | E1.13, F4.9 |
| **Q13** | Are there resource limits anywhere? | **No `deploy.resources`, no CPU/memory caps, anywhere.** Only `shm_size: 256mb` and `ulimits nofile 65536` on `db`. The 18-week workshop build peaks at ~19 GB. | None. | D6, E1.3 |
| **Q14** | Which is the intended ingestion path? | `docs/DATA-FLOW.md` says the gateway reads Modbus first then OPC UA so OPC UA overwrites Modbus, and `source` records which landed. **It does not**: `poll_once` merges both into one dict, `SpoolRecord` has no source field, and `drain()` hardcodes `"opcua"`. The designed cross-protocol defence — the `source` column in the PK — is absent. | Confirmed defect. | A7.5, E4.4 |
| **Q15** | Is `make check` "the fast gates" or "everything"? | It needs a **seeded database** for its `sql` leg (`db-still db-has-data`), so it is not runnable on a bare machine — yet the docs describe it as the fast path. It also omits integration, notebooks, workshop-notebooks and all drift gates. | 6 gates, needs data, omits 8+ | F2.10, F5.9 |
| **Q16** | Does `make help` document `setup`? | `make setup` is the **only** thing that writes `.env` and is a prerequisite of most targets, yet it is undocumented in `make help`. Same for `sync`, `db-up`, `db-live`, `db-still`, `db-has-data`, `notebooks-has-data`, `notebooks-open`. | Undocumented but required. | E1.5 |

---

## Appendix B — Evidence index

| Area | Primary evidence artifacts |
|---|---|
| A. SQL | `init-db.log`; contract vs DB counts; the 3 constraint-rejection errors; PK column list; seed log with timing; the 4-value fingerprint; aggregate row counts; `role_table_grants` dump; the gateway `TRUNCATE` refusal; `roles --check`; continuous-aggregate definitions; 6 data-quality queries incl. the `abs(value) < 1e-30` word-order query; `sql-course.log`; 5 `EXPLAIN (ANALYZE, BUFFERS)` outputs |
| B. Grafana / Node-RED / Web | datasource YAML; `/api/datasources/…/health`; `/api/search?type=dash-db`; rendered compose config; drift log; dashboard identity dump; 9 panel screenshots; panel-4 `rawSql`; panel-6 SQL + count; permit geomean-vs-arithmetic; `site.permit` JSON; alarm rule table (16/11); detector keys; `event` queries; 3-way alarm-view comparison; coverage matrix + `coverage.json`; Grafana zero-alert proof; `assembled … 41 nodes`; 4 Node-RED screenshots; credential `ls -la`; web route status codes; `/api/health` both states; `pg_stat_activity` count; sparkline + staleness screenshots; **`grep -rl POSTGRES_PASSWORD /app/.next/static \| wc -l` = 0**; the `touch /app` refusal |
| C. Notebooks | `dsn()` dict with password redacted; `current_database()` + row count; gate log with 4 checks per notebook; `notebook_data status`; fingerprint got-vs-expected; the read-only transaction transcript; the `NOT_PORTABLE` grep; the per-notebook output census; 14 figures listed; `notebooks-read` log; per-notebook key-cell screenshots; `statsmodels`/`lineplot` greps; post-session `git status` (dirty) and post-reset (clean) |
| D. ML | `workshop.log` with timing; panel shape/size/positives; `summarise`; the density and duplicate-pair check; `row_written` × `value_is_null` crosstab; 45 pytest results; the label-leakage intersection; hyperparameter greps; the seeded-estimator check; per-notebook runtime/RSS table; `TRAINER.md` number greps; the independent confusion computation; the IsolationForest flag count; the PCA quantiles; the 3 build logs; the `measure_long_window` verdict; `minimum_interval_s()`; `dataset.csv` survival check |
| E. Install | all six version commands; `make up` with timing; the compose-refusal transcript; `config --services`; port bindings; `HOST_BIND` both ways; the port-override transcripts (compose **and** make); retention job before/after; `--list-scenarios`; two seeded PLC runs diffed; the pg_dump/pg_restore timings; `DROP DATABASE` + `init-db` rebuild; `df -h` before/after |
| F. Cross-cutting | the `-40` OPC UA write transcript + resulting rows; the per-service `POSTGRES_USER` table; `id -u` per image; the capability dump; the network inspect; `.gitignore`/`git check-ignore`; `docker inspect | grep password`; the log-excerpt set; the Prom/OTel grep (empty); the failure-table verification column; the bad-password transcript; the unknown-detector transcript; `test_readme_claims.py` + 3 other doc-test suites; the stale-block quotes with file:line; the tamper/restore transcripts for 3 generators; the double-generation diffs; the lint-debt growth transcript |

---

## Appendix C — Master smoke script (P0, ~2h15m)

For a release gate. Run top to bottom; stop at the first failure.

```bash
cd /Users/dev/Developer/wwtp-iiot
mkdir -p evidence/P0

# --- E: install ---
git status --short                        # expect empty
git rev-parse --short HEAD                # record
docker --version; docker compose version; uv --version; cc --version; make --version
uv run pytest tests/test_clean_checkout.py -q          # 5
make setup && make sync
time make up 2>&1 | tee evidence/P0/make-up.log       # ~2m40s

# --- E3: health ---
docker compose config --services | sort | tr '\n' ' '; echo     # expect 8
docker compose ps                            # 4 core healthy
docker compose ps --format '{{.Ports}}'      # expect 127.0.0.1 only

# --- A: SQL ---
make query SQL="SELECT count(*) FROM reading"     # 4239284
make query SQL="SELECT count(*), sum(round(value*1000)::bigint),
                       sum(quality::bigint), sum(epoch FROM ts)::bigint
                FROM reading"                     # the 4-value fingerprint
make query SQL="SELECT count(*) FROM reading_1h"  # ~5882, NOT 2
make query SQL="SELECT count(*) FROM reading WHERE value IS NULL AND quality=0"  # 0
make query SQL="SELECT count(*) FROM (SELECT ts,signal_id,source FROM reading
                GROUP BY 1,2,3 HAVING count(*)>1) d"                        # 0
make query SQL="SELECT signal_id,count(*) FROM reading WHERE value IS NOT NULL
                AND value<>0 AND abs(value)<1e-30 GROUP BY 1"               # 0 rows
make roles | tee evidence/P0/roles.txt
make db-still
time make sql 2>&1 | tee evidence/P0/sql.log     # 78 queries

# --- B: Grafana, Node-RED, web ---
make scada-flows && make scada-check
make page-check && make dashboards-check
make grafana && make web && make scada
docker compose exec grafana curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
  'http://localhost:3000/api/datasources/uid/wwtp-postgres/health' | tee evidence/P0/grafana-health.json
docker compose exec grafana curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
  'http://localhost:3000/api/search?type=dash-db' | python3 -m json.tool | tee evidence/P0/grafana-search.json
docker compose logs scada | grep assembled        # 3 file(s), 41 nodes
docker compose logs scada | grep -ci error         # 0
for p in / /permit /alarms /api/health; do
  printf '%s -> %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${WEB_PORT:-3001}$p)"
done                                                    # four 200s
curl -s http://127.0.0.1:${WEB_PORT:-3001}/api/health | tee evidence/P0/web-health.json
docker compose --profile ui exec web sh -c \
  "grep -rl 'POSTGRES_PASSWORD\|POSTGRES_HOST\|NEXT_PUBLIC' /app/.next/static | wc -l"   # 0
# screenshot: both Grafana dashboards, 3 web pages, Node-RED mimic + annunciator

# --- C/D: notebooks ---
time make notebooks 2>&1 | tee evidence/P0/notebooks.log            # ~8 min
time make workshop && make workshop-notebooks 2>&1 | tee evidence/P0/workshop.log   # ~6 min
git status --short notebooks/ ; echo "expect empty above"

# --- F: gates and claims ---
make check 2>&1 | tee evidence/P0/check.log                        # ends "all gates green"
make integration 2>&1 | tee evidence/P0/integration.log             # 48
uv run pytest tests/test_readme_claims.py tests/test_getting_started_agrees.py \
               tests/test_readme_teaches_no_destructive_command.py -q   # 83 + 13 + 173
uv run pytest tests/test_web_page.py tests/test_scada_contract.py \
               tests/test_grafana_dashboards.py -q                     # 22 + 37 + live DB
make integration

# --- E7: clean teardown and rebuild ---
make down; make clean; docker volume ls | grep wwtp    # expect nothing
time make up                                            # ~2m40s
make query SQL="SELECT count(*) FROM reading"            # 4239284
git status --short                                       # MUST be empty
```

**P0 pass definition:** every command above matches its expected output, and
`git status --short` is empty at the end. A run where every gate passes but the tree is dirty
has still found a bug.

**Known P0-level findings to record even on a green run** (they are confirmed defects, not
test failures): the gateway does not reconnect to Postgres (Q/E4.3); `source` is always
`opcua`, never `modbus` (Q14/E4.4); `scada` has no healthcheck (E3.6); `db` and `grafana` have
no log rotation (E3.8); the scanloop pace test is still order-dependent (F3.11).

---

## Appendix D — Total test count

| ID | Area | Test cases | Est. hours |
|---|---|---|---|
| A | SQL layer | 11 (A1–A9, incl. A4b) | 3.0 |
| B | Grafana, Node-RED, web | 7 (B1–B7) | 5.0 |
| C | Notebooks | 3 (C1–C3) | 2.0 |
| D | ML workbooks | 7 (D1–D7) | 6.0 |
| E | Installation & deployment | 7 (E1–E7) | 4.5 |
| F | Cross-cutting | 5 (F1–F5) | 3.0 |
| — | Appendix C smoke script | 1 | 2.25 |
| | **Total** | **40 test cases + 6 area-note sections (A0–F0)** | **~26 h** |

Plus **16 open questions** (Appendix A) and a 4-part evidence index (Appendix B).

Full pass ≈ 26 h across 5–6 sessions. P0 only (Appendix C) ≈ 2 h 15 m. P0+P1 ≈ 14 h.
