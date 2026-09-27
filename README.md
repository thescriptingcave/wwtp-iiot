# wwtp-iiot

A simulated wastewater treatment plant that speaks real industrial protocols,
stores its own history in one database, and carries enough instrumentation to tell
you *why* it does what it does.

The point is not the plant. The point is that every layer between a float in a
basin and a number on a dashboard is built here, in the open, with the reasoning
written down.

```
  process model ──► scan loop ──► function blocks ──► scan image
        │                                                   │
        │                                    ┌──────────────┴──────────────┐
        │                                    ▼                             ▼
        │                            Modbus TCP  :5020              OPC UA  :4840
        │                            (polling, word-order traps)     (status codes,
        │                                                              subscriptions)
        │                                    │                             │
        │                                    └──────────────┬──────────────┘
        │                                                   ▼
        └──────────────► gateway: deadband → spool ──► Postgres + Timescale
                                          │              reading (hypertable)
                                          │              signal · equipment · site
                                          └────────────► event
```

## What makes it a teaching project rather than a demo

**The contract is the source of truth.** [`contracts/tags.yaml`](contracts/tags.yaml)
defines all 57 signals, 22 pieces of equipment, their engineering units, their
permit limits, their Modbus addresses and word orders, 11 faults and 6 scenarios.
The PLC, the gateway, the database, the alarm engine, the dashboards and the tests
all read that one file. Nothing is declared twice, so nothing can drift.

**The database enforces what used to be a convention.** `reading.signal_id` is a
foreign key onto `signal`, so a reading cannot name a signal that does not exist.
There was no mechanism for that in the previous storage engine — a tag could say
`equipment=FLOW` and nothing could say there is no such asset. **That constraint
found a real modelling error on its first run**, after a year of querying had not.

**The process maths is hand-written.** The only hard runtime dependency is PyYAML.
Monod kinetics, the clarifier mass balance, digester VFA/alkalinity chemistry,
breakpoint chlorination, the DO control loop and the chlorine dose controller are
all in [`softplc/process/`](softplc/process), in plain arithmetic, with the
constants and their calibration history in comments. A library would have hidden
the lesson.

**Modbus is modelled honestly, including its traps.** Nineteen of the registers
deliberately use low-word-first ordering while their neighbours use
high-word-first. Reading a low-word-first register as high-word-first yields
`2.3e-41` — a finite, in-range, wrong number. Nothing in Modbus will ever tell you
that happened, and no range check anywhere in the system will catch it, because
there is nothing wrong with the number *as a number*. The only defence is to
compare two independent observations of the same physical quantity, which is why
`source` is part of the primary key of `reading`.

**Quality is data, not a footnote.** A fouled DO probe publishes as `Uncertain`
with a StatusCode, and the *process* stays healthy. The `bad_instrument` scenario
separates the two, because a historian that cannot tell a bad sensor from a bad
process cannot be trusted to alarm on anything. And the schema makes the
representation a rule rather than a habit:

```sql
CONSTRAINT reading_null_is_not_good CHECK (value IS NOT NULL OR quality <> 0)
```

**Faults have expected signatures.** [`contracts/fault-scenarios.yaml`](contracts/fault-scenarios.yaml)
gives each of eleven faults the signature an operator should see, plus
`detectable_by` and `NOT_detectable_by` — because knowing what a fault will *not*
tell you is how you avoid a confident wrong diagnosis.

**The design decisions record what was rejected, and how.** The project used to
run on InfluxDB 3 and Couchbase. The stated reason was that they are "two
genuinely different jobs", and that justification turned out to run backwards: the
split was *caused by* the time-series choice. Fifteen bugs, six missing SQL
features, and 80 documents in three key-sets with one shape each are all in
[`docs/DESIGN.md`](docs/DESIGN.md) with the measurements.

## Quick start

Requires Docker with Compose v2 and `uv`. **No licence key, no account, nothing
from outside this repository** — about five minutes.

```bash
cp .env.example .env          # set POSTGRES_PASSWORD
docker compose up -d
docker compose --profile demo run --rm seed   # 4.3 M readings, ~2 min
```

Then browse the plant and query it:

```bash
uv run python tools/opcua_browser.py browse
uv run python tools/opcua_browser.py watch AERATION:AHU-1:DO
uv run python tools/sqlrun.py "SELECT count(*) FROM reading"
```

Full walkthrough, including troubleshooting, in
[`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md).

## Documentation

| Document | What it is for |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Why the architecture is shaped this way, what was rejected, and the honest weaknesses |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, boundaries, and the four things deliberately not here |
| [`docs/DATA-FLOW.md`](docs/DATA-FLOW.md) | One scan end to end, and what each layer can lose |
| [`docs/TERMINOLOGY.md`](docs/TERMINOLOGY.md) | Plant, control and IIoT vocabulary, defined once |
| [`docs/GLOSSARY.md`](docs/GLOSSARY.md) | Standards and specifications, each with a source to study |
| [`docs/TESTING.md`](docs/TESTING.md) | What is verified, how, and what is deliberately not |
| [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) | From nothing to a running plant |
| [`docs/SECURITY.md`](docs/SECURITY.md) | The threat model, and the gaps stated plainly |
| [`docs/ALARMS.md`](docs/ALARMS.md) | The alarm engine, and the fault × rule coverage matrix |
| [`docs/ALARM-TUNING.md`](docs/ALARM-TUNING.md) | Every threshold, the measurement it came from, and the four that still do not work |
| [`scada/README.md`](scada/README.md) | The Node-RED operator flows, and how they are kept in step with the contract |
| [`docs/LEARNING-LOG.md`](docs/LEARNING-LOG.md) | Every wrong assumption — **the most useful file here** |
| [`docs/adr/`](docs/adr/) | Decision records |

## The alarm engine

```bash
make coverage     # the fault × rule matrix, about eight minutes
```

The fault library was built to feed an alarm engine, and for three phases it fed
nothing. It now does, and — more usefully — **it audits itself**: every rule names
the faults it claims to catch, every fault names how it is and is not detectable,
and the seeder can answer "did this rule fire?" because it replays the plant
model through each of the eleven faults.

The tool's first full run found that `aeration_do_sagging` fired on **ten of
eleven** faults, and then that **six rules fire on a healthy plant**. Tuning every
threshold against a measured healthy distribution took that to five, and
[`docs/ALARM-TUNING.md`](docs/ALARM-TUNING.md) records the measurements, the two
harness bugs that made every threshold wrong before the tuning, and the four
rules that still do not work — including one fault with no signature anywhere in
the signals the contract collects.

A rule set with no audit is a set of thresholds somebody liked the look of.

## The SQL track

[`sql/`](sql/README.md) is a course, beginner to expert, run against a week of
data this project generates. It is not an appendix; it is where you learn to ask
the questions the plant exists to answer.

| Stage | What you learn |
|---|---|
| [00](sql/00-foundations/) | What a hypertable is; identity versus value and why they are different *tables*; why `value` can be NULL |
| [01](sql/01-beginner/) | `SELECT`, `WHERE`, aggregation, `time_bucket`, `CASE`, `HAVING` |
| [02](sql/02-intermediate/) | CTEs, joins, window functions, and telling a quiet signal from a dead one |
| `03-advanced/` | Unwritten — continuous aggregates, retention, `EXPLAIN`, chunk behaviour |
| `04-expert/` | Unwritten — time-weighted averages, change detection, query planning |

Every ````sql` block is executed against a live server:

```bash
uv run python tools/check_sql.py sql/    # 57 queries across 16 files
```

The checker runs each query three times inside a transaction it rolls back, fails
on any error, on any answer that changes between runs, and on any query that
returns zero rows. It also distinguishes three kinds of instability, which turned
out to matter — see [`sql/01-03`](sql/01-beginner/01-03_aggregating.md) for why a
float aggregate can change the *order* of a result and not just its value.

[`sql/_shared/INFLUXQL-NOTES.md`](sql/_shared/INFLUXQL-NOTES.md) is a historical
note rather than a constraint: it records the six things the previous dialect could
not do, and the design rule that was reverse-engineered from a write rejection and
then written down as though it were a principle.

## Phase status

- [x] **Phase 1** — contracts, process model, scan loop, control blocks, fault engine
- [x] **Phase 2** — Modbus TCP server, OPC UA server, browser tool, runnable soft PLC
- [x] **Phase 3** — gateway (deadband, spool, both protocol readers), Postgres +
  TimescaleDB storage, metadata seeder, SQL course through `02-intermediate`
- [x] **Phase 4** — alarm engine: ten detectors, fifteen rules, and a coverage
  audit against the fault library. Every threshold derived from a measurement of
  a settled healthy plant, which took the false-positive count from six rules to
  five — and the remaining five cannot be tuned away.
  [`docs/ALARMS.md`](docs/ALARMS.md) · [`docs/ALARM-TUNING.md`](docs/ALARM-TUNING.md)
- [x] **Phase 4b** — Node-RED operator flows: a mimic, an alarm annunciator, and
  a range-checked setpoint, all **generated from the contract** and all behind a
  `scada` profile. 31 tests guard the generator rather than the output.
  [`scada/README.md`](scada/README.md)
- [x] **Phase 5a** — Grafana: a provisioned datasource and **two dashboards
  generated from the contract**, including the only place a discharge-permit
  number is computed. 15 tests, two of which run every dashboard query against a
  live database. `python -m ui.grafana.generate_dashboards`
- [ ] **Phase 5b** — the custom Next.js page. `ui/web/` has a correct Dockerfile
  and no source.

Phase 3 is complete and exercised against a live database: 378 unit tests, 17
integration tests, 57 course queries, all passing. The open threads are in
[`docs/LEARNING-LOG.md`](docs/LEARNING-LOG.md) and the most important is that the
fault library currently feeds nothing — there is no alarm engine yet.

## Licence

Proprietary. This is a portfolio project; no open-source licence is granted. The
licences of the dependencies are listed in
[`docs/GLOSSARY.md`](docs/GLOSSARY.md#licences-of-the-components).
