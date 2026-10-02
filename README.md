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
twelve faults the plant can be asked to suffer and the six scenarios that drive
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
gives each of twelve faults the signature an operator should see, plus
`detectable_by` and `NOT_detectable_by` — because knowing what a fault will *not*
tell you is how you avoid a confident wrong diagnosis.

**The design decisions record what was rejected, and how.** The project used to
run on InfluxDB 3 and Couchbase. The stated reason was that they are "two
genuinely different jobs", and that justification turned out to run backwards: the
split was *caused by* the time-series choice. Fifteen bugs, six missing SQL
features, and 80 documents in three key-sets with one shape each are all in
[`docs/DESIGN.md`](docs/DESIGN.md) with the measurements.

## Quick start

Needs **Docker with Compose v2** and [`uv`](https://docs.astral.sh/uv/). Nothing
else — no account, no licence key, nothing from outside this repository.

```bash
make up
```

One command. It writes a `.env` if you do not have one, starts the simulation,
gives it a week of history (about two and a half minutes, most of it the seed), and
blocks until the first readings actually land. You end up with **4.3 M readings**
in TimescaleDB.

```bash
make query SQL="SELECT count(*) FROM reading"

# one tag's value, status and metadata
uv run python tools/opcua_browser.py read AERATION:AHU-1:DO

# the address space as a tree -- structure, no values
uv run python tools/opcua_browser.py browse

# that tag, updating
uv run python tools/opcua_browser.py watch AERATION:AHU-1:DO
```

**The commands in this section are the same ones in
[`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md), and a test fails if they
diverge** — because two documents that each describe the setup differently is how
somebody ends up on a clean machine running a command that was never tested on one.

### Three courses, once the plant is up

Each is optional and none of them changes the plant. The workshop is the only one
that needs **no database** — it reads a CSV, so it runs on a laptop with nothing
else set up.

| | Start at | Run it with |
|---|---|---|
| SQL — 64 graded queries | [`sql/README.md`](sql/README.md) | `make sql` |
| Analyst notebooks — 11 | [`notebooks/README.md`](notebooks/README.md) | `make` |
| ML workshop — 6 notebooks | [`workshops/ml/TRAINER.md`](workshops/ml/TRAINER.md) | `make workshop` then `make workshop-notebooks` |

`make` on its own is the **notebooks**: it seeds `wwtp_notebooks` on the first run
(about two and a half minutes) and opens JupyterLab on
<http://127.0.0.1:8899>. `make notebooks` runs all eleven and fails if any number in
the prose no longer matches.

Full walkthrough, configuration reference and troubleshooting, in
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
| [`docs/CI.md`](docs/CI.md) | The eight CI jobs, and the three broken things writing the file found |
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
model through each of the twelve faults.

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
| [04](sql/04-expert/) | Time-weighted averages, change detection, reading a plan, and the query behind the shipped Grafana panel |

Every ````sql` block is executed against a live server:

```bash
# 78 queries across 26 files
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

## The analyst notebooks

[`notebooks/`](notebooks/README.md) is a third track, and it is the one for people
who already write pandas and SQL. It does not teach either. It is about **what
the plant is telling you and what it is not** — a question the two courses set up
but neither can answer, because both are about how to ask.

All eleven are written. They run against a **separately seeded database pinned to
a fixed week** — `2026-09-22 00:00` to `2026-09-28 23:59` UTC, 4,239,284 readings
— because their numbers are checked claims and a claim checked against data that
moves every time it is re-seeded is not a claim. An integer fingerprint of that
table is in the gate, and two independent seeds produce the identical one.

The findings are the point, and they are the ones an analyst would otherwise
argue about:

- **Nine of the 57 tags are one measurement under two names**, and after removing
  them **18 of 491 independent signal pairs still correlate above 0.99** — one
  daily cycle drives nearly the whole plant. (01)
- **`AVG(value)` and an hourly rollup do not disagree; they answer different
  questions**, and on the storm day they are **27 % apart** on the flow meter.
  `AVG` is the count-weighted mean, exactly, and a bucket with one reading gets
  the same vote as one with a thousand. (04)
- **A stationarity test is not a seasonality test.** The raw hourly plant power
  scores −4.96 on a Dickey-Fuller, past the 1 % line, with a lag-24
  autocorrelation of **0.998** — and **99.7 %** of its variance is the clock.
  (06)
- **A random split lies about time.** Rolling-origin validation moves the
  neighbour-in-time model from second to last and its error from **61.7** to
  **494.1** m³/h, while the *yesterday* baseline beats everything on all five
  test days. (08)
- **The contract's band is not a detector.** It missed all three injected
  instrument faults and raised **1,441** alarm-minutes on four signals when
  nothing was wrong; a per-signal baseline gets the same faults to **4**
  alarm-minutes. (09)
- **Mean concentration × volume is not a load**: **4,973** kg against a real
  **5,421**. And the averaging window can decide whether an event was a problem
  at all — **44.90** mg/L over the storm, **22.08** over its day, against a
  30 mg/L reference. (07)

They are authored as markdown and generated as `.ipynb`, and the generated files
are executed in CI. `make notebooks` rebuilds them, runs every one against the
pinned database, and fails if a number in the prose no longer matches:

```bash
make notebooks
```

That gate is four checks, not one: the `output` fences, every **bold** number in
the prose, every ` ```sql ` block executed for rows, and the seed fingerprint.
A number in the prose is a claim checked against what the notebook actually
printed — the discipline the SQL course already follows, and
`sql/00-foundations/00-03` is the reason: it drifted for months because its
expected output described a dataset that no longer existed.

## The machine-learning workshop

[`workshops/ml/`](workshops/ml/README.md) is a fourth track and it is **finished**.
The SQL course teaches tools, the notebooks teach what the plant is telling you,
and this one teaches **the moment a score stops being evidence** — using this
plant's data to do it.

```bash
# seeds three weeks at 1 s and writes workshops/ml/dataset.csv
make workshop
# JupyterLab on http://127.0.0.1:8898, rooted at workshops/ml/
make workshop-open
# the gate: 6 notebooks, 18 assertions, one CSV and no database
make workshop-notebooks
```

**No database and no Docker.** Participants read a CSV, which is the point — the
track has to run on the machine in front of them.

### The finding the dataset is built around

**The stored hourly table has no row at all inside the window of two of the three
instrument faults**, so a classifier fitted on a query of it has no positive examples
for two thirds of the classes. Drop the hours with nothing in them, exactly as a
query would, and **12 of 22 fault hours vanish**.

So `build_dataset.py` crosses every signal with every hour and left-joins, which
makes an absent hour a row with `n = 0` — 28,728 rows at a base rate of **0.077%**,
with every injected fault present.

The lesson the data hands over for free: **41% of ordinary hours are in exactly the
state a broken instrument is in**, so absence alone finds nothing, and the feature
that carries it is a per-signal comparison against its own recent history — a fault
hour's signal logged **596** rows in the previous day against a quiet hour's 20.

### What the six notebooks claim

| | Notebook | The number |
|---|---|---|
| 1 | `01-the-metric-turn` | accuracy **0.9993** for a model that learned nothing; the real forest gets 0.9995 |
| 2 | `02-the-dense-panel` | a query keeps **10 of 22** fault hours — two of three classes gone |
| 3 | `03-the-baseline-and-the-noise-floor` | the split-to-split spread is **42x** the difference between two feature sets |
| 4 | `04-the-forward-window` | F1 **0.444 → 0.833**, and the 0.833 needs tomorrow |
| 5 | `05-unsupervised` | **0 of 22** fault hours; 285 of 288 flags are one tag |
| 6 | `06-predictive` | a random split makes the model **5.3x** better, and it is worth nothing |

Accuracy moves from 0.9993 to 0.833 across all six and is never once the point.

Three of these were **not** what the plan predicted, and the corrections are the
useful part: a seasonal-naive baseline does *not* beat every model (a tree on
hour-of-day wins, 0.330 against 0.437), unsupervised detection finds neither the
faults nor the storm but one oddly-busy tag, and the per-signal baseline cannot be
shown to help at all — see below.

### The result worth knowing before you teach it

Notebook 03 concludes that the right feature cannot be shown to work on this panel.
Built at 8 weeks, that is measured rather than asserted:

| | 3 weeks | 8 weeks |
|---|---|---|
| positives | 22 | 62 |
| in a 20% test fold | 4 | 12 |
| split noise / effect | **42x** | **13x** |

**Still not enough.** Noise divided by effect falls as roughly 1/positives, so
reaching a ratio of 2 needs ~416 positives — about 54 weeks at the default
recurrence, or ~18 GB at a 12 h one. The honest line for a room is that *more data
moves the needle and does not move the conclusion*.

### Disk, and getting it back

`make workshop-long` builds a longer window for that experiment:

```bash
make workshop-long WORKSHOP_LONG_WEEKS=18 WORKSHOP_HOURS=12
```

Two dials, both inherited by the target: `WORKSHOP_LONG_WEEKS` (default 8) and
`WORKSHOP_HOURS` (default 36, floor 2). `WORKSHOP_SAMPLE_INTERVAL` (default 1) is
there too, but **60 s is not a free saving** — it preserves the fault count and
destroys the per-signal baseline the whole track is built on, because a quiet signal
writes zero rows in 24 h.

To reclaim the space:

```bash
docker compose exec -T db psql -U wwtp -d postgres -c "DROP DATABASE IF EXISTS wwtp_ml25"
rm workshops/ml/dataset-*.csv
docker builder prune -af
```

**The prune matters as much as the drop.** Postgres lives in the Docker volume
`wwtp-iiot_db-data`, and on macOS that sits inside a sparse `Docker.raw` image that
does not shrink when files are deleted. If the host disk does not come back,
Docker Desktop → Resources → **Reclaim disk space**.

And **keep the CSV, not the database**: `reading` carries `drop_after: '7 days'`
against `now()`, so the raw table trims itself within a day of the build, while
`reading_1h` — which the builder reads, and which has no retention policy — keeps
the whole window. The panel is rebuildable from that; the database is scaffolding.

## Phase status

- [x] **Phase 1** — contracts, process model, scan loop, control blocks, fault engine
- [x] **Phase 2** — Modbus TCP server, OPC UA server, browser tool, runnable soft PLC
- [x] **Phase 3** — gateway (deadband, spool, both protocol readers), Postgres +
  TimescaleDB storage, metadata seeder, SQL course through `02-intermediate`
- [x] **Phase 4** — alarm engine: eleven detectors, sixteen rules, and a coverage
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
- [x] **Phase 6** — CI: eight jobs, and writing the file found that **three of the
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
