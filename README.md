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

**Two contract files, and nothing is declared twice.**
[`contracts/tags.yaml`](contracts/tags.yaml) describes the plant: 57 signals, 22
pieces of equipment, their engineering units and ranges, their permit limits,
their Modbus addresses and word orders.
[`contracts/fault-scenarios.yaml`](contracts/fault-scenarios.yaml) describes the
eleven faults the plant can be asked to suffer and the six scenarios that drive
them.

They are separate on purpose, and the reason is the interesting part: **a fault
is a test instrument, not a description of the plant.** Putting eleven
hypothetical failures in the same file as 57 real signals would make both harder
to read, and — worse — would make it easy to read a fault's expected signature as
if it were a fact about the equipment. A reader who sees `lift_pump_failure` next
to `PUMP-1` should be uncertain about which of them is real.

The PLC, the gateway, the database seeder, the alarm engine, the Node-RED flows,
the Grafana dashboards, the web page and the tests all derive from them, so a
signal renamed in one place is renamed everywhere. To see the current list:

```bash
# the plant contract
git grep -l 'tags\.yaml' -- '*.py' | grep -v '^tests/'
git grep -l 'fault-scenarios' -- '*.py' | grep -v '^tests/'
```

> This paragraph used to say `tags.yaml` held "11 faults and 6 scenarios" and
> that "all" of the above "read that one file". Both were wrong.
>
> The correction then replaced the vague claim with "nineteen source files read
> the first" — **a new number, in the same sentence, that was wrong the moment it
> was written.** A grep for the filename gives twelve; a grep for the filename
> *or* the loader gives twenty-five, and four of those are a comment, a JSON
> import and a page footer. There is no single integer that is both true and
> cheap, so there is no integer here: there is a command you can run.
>
> The original failure and this one are the same failure. **A count in prose is a
> measurement or it is nothing**, and five of the README's were stale — the
> `03-advanced/` stage was described as unwritten *after* it was written, and the
> course was described as "57 queries" when it had 64. None was caught by a test
> or by reading the code; they were caught by counting, once, by hand, on the way
> to a push. `tests/test_readme_claims.py` now asserts the numbers that *can* be
> asserted, and the point of that file is the sentence above it: it can prove the
> counts are right and it cannot prove the claims are true.

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

**Modbus is modelled honestly, including its traps.** Seventeen of the nineteen
registers are high-word-first, and **two are low-word-first** —
`AERATION_BLOWER_VALVE` and `AERATION_WASTE_RATE`. Reading one of those as
high-word-first yields `2.3e-41`: a finite, in-range, wrong number. Nothing in
Modbus will ever tell you that happened, and no range check anywhere in the
system will catch it, because there is nothing wrong with the number *as a
number*. The only defence is to compare two independent observations of the same
physical quantity, which is why `source` is part of the primary key of `reading`.

> This said "**Nineteen** of the registers deliberately use low-word-first
> ordering while their neighbours use high-word-first". It is two. And the error
> was in the more useful direction to be wrong in: it made the *common* case sound
> like the dangerous one, so a reader would have gone looking for the trap in the
> wrong sixteen registers.
>
> `docs/DATA-FLOW.md` made the same mistake with a YAML example at addresses
> `40101` and `40103` — **neither of which exists**; the real addresses are
> `40100` and `40102`, and both of those are `big`. The `little` register is at
> `40108`, the fifth in a run of `big` ones, so "the neighbours disagree" is not
> even locally true.
>
> Found by writing the verification steps in `docs/VERIFYING.md` and running
> step 3.2, which printed `0 low-word-first, 19 high-word-first` and was
> obviously wrong in a way that pointed straight at the claim. The count is now
> asserted by `tests/test_readme_claims.py`.

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
# set POSTGRES_PASSWORD
cp .env.example .env
docker compose up -d
# 4.3 M readings, ~2 min
docker compose --profile demo run --rm seed
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
| [`courses/opcua/`](courses/opcua/README.md) | The OPC UA course — **9 of 9 lessons** — and the **fourteen** things this implementation gets wrong |
| [`ui/web/README.md`](ui/web/README.md) | The custom dashboard, its four decisions, and what is *not* verified |
| [`docs/CI.md`](docs/CI.md) | The six CI jobs, and the three broken things writing the file found |
| [`docs/LEARNING-LOG.md`](docs/LEARNING-LOG.md) | Every wrong assumption — **the most useful file here** |
| [`docs/adr/`](docs/adr/) | Decision records |

## The alarm engine

```bash
# the fault × rule matrix, about eight minutes
make coverage
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

## Acknowledgement, and why it took a phase

`AlarmEngine.acknowledge()` existed for two phases, was tested, and was called by
nothing — and the annunciator flow I shipped carried a comment saying it closed
that gap. It could not acknowledge anything, because an acknowledgement is a
*write* to `event` and no flow performed one.

Fixing it meant rebuilding alarm state from the event log
([`alarms/replay.py`](alarms/replay.py)), which forces a question the state
machine had been dodging: **is an acknowledgement per rule or per occurrence?**
Per occurrence means an operator acknowledges a flapping alarm on every flap. Per
rule forever means an alarm that is genuinely new an hour later is silenced by an
acknowledgement for something that is not the same. So: **per rule, cleared when
the condition clears** — which is what an operator means by "I've seen this".

The restart path is still not right, and `scada/README.md` says so: a restarted
engine re-raises an already-acknowledged critical. The operator path works.

## The SQL track

[`sql/`](sql/README.md) is a course, beginner to expert, run against a week of
data this project generates. It is not an appendix; it is where you learn to ask
the questions the plant exists to answer.

| Stage | What you learn |
|---|---|
| [00](sql/00-foundations/) | What a hypertable is; identity versus value and why they are different *tables*; why `value` can be NULL |
| [01](sql/01-beginner/) | `SELECT`, `WHERE`, aggregation, `time_bucket`, `CASE`, `HAVING` |
| [02](sql/02-intermediate/) | CTEs, joins, window functions, and telling a quiet signal from a dead one |
| [03](sql/03-advanced/) | Continuous aggregates and why a policy cannot refresh a backfill; chunks and chunk exclusion; retention; `EXPLAIN` and reading a plan |
| `04-expert/` | Unwritten — time-weighted averages, change detection, query planning |

Every ````sql` block is executed against a live server:

```bash
# 80 queries across 26 files
uv run python tools/check_sql.py sql/
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
  `scada` profile. 37 tests guard the generator rather than the output — and four
  of them execute every flow's SQL against a live database.
  [`scada/README.md`](scada/README.md)
- [x] **Phase 5a** — Grafana: a provisioned datasource and **two dashboards
  generated from the contract**, including the only place a discharge-permit
  number is computed. 15 tests, two of which run every dashboard query against a
  live database. `python -m ui.grafana.generate_dashboards`
- [x] **Phase 5b** — the custom Next.js dashboard: overview, permit and alarm
  pages, server-rendered, **no credential in the browser**, generated read model,
  a read-only Postgres role, and a read-only container. 22 tests from Python; the
  rendering verified by building and running it. [`ui/web/README.md`](ui/web/README.md)
- [x] **Phase 6** — CI: six jobs, and writing the file found that **three of the
  four local gates had been failing, or not doing what their labels said, the
  whole time** — `make lint`, `make types` and `make test`. I had been reporting
  the subsets that pass. [`docs/CI.md`](docs/CI.md)

Every phase is exercised against a live database. The open threads are in
[`docs/LEARNING-LOG.md`](docs/LEARNING-LOG.md), triaged into **five things to do,
six properties of the design that will not change, and eleven finished** — and
the first of the five is the one that undermines work already done: the two alarm
harnesses disagree by a factor of seven and nobody has explained why.

## Licence

Proprietary. This is a portfolio project; no open-source licence is granted. The
licences of the dependencies are listed in
[`docs/GLOSSARY.md`](docs/GLOSSARY.md#licences-of-the-components).
