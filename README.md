# wwtp-iiot

A simulated wastewater treatment plant that speaks real industrial protocols,
stores its own history in two databases chosen for two different jobs, and
carries enough instrumentation to tell you *why* it does what it does.

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
        └──────────────► gateway: deadband → spool ──► InfluxDB 3   (raw time series)
                                          │                └► rollup worker
                                          └──────────────► Couchbase      (metadata,
                                                                   events, N1QL)
```

## What makes it a teaching project rather than a demo

**The contract is the source of truth.** `contracts/tags.yaml` defines all 57
signals, 21 pieces of equipment, their engineering units, their permit limits and
their Modbus addresses. The PLC, the gateway, the alarm engine, the dashboards
and the tests all read that one file. Nothing is declared twice, so nothing can
drift. Three rules are enforced by the loader and covered by tests:

- a tag is identified by `AREA:EQUIPMENT:MEASUREMENT`, and the value lives in the
  field, never in the name;
- descriptive metadata goes to Couchbase, not into InfluxDB tags;
- equipment *state* is a separate measurement from equipment *readings*.

**The process maths is hand-written.** The only runtime dependency is PyYAML.
Monod kinetics, the clarifier mass balance, digester VFA/alkalinity, breakpoint
chlorination, the DO control loop and the chlorine dose controller are all in
`softplc/process/`, in plain arithmetic, with the constants and their
calibration history in comments. A library would have hidden the lesson.

**Modbus is modelled honestly, including its traps.** Nineteen of the registers
deliberately use low-word-first ordering while their neighbours use
high-word-first. Reading a low-word-first register as high-word-first yields
`2.3e-41` — a finite, in-range, wrong number. Nothing in Modbus will ever tell
you that happened. That is the bug class this project exists to teach.

**Quality is data, not a footnote.** A fouled DO probe publishes as
`Uncertain` with a StatusCode, and the *process* stays healthy. The `bad_instrument`
scenario separates the two, because a historian that cannot tell a bad sensor
from a bad process cannot be trusted to alarm on anything.

**Faults have expected signatures.** `contracts/fault-scenarios.yaml` gives each
of eleven faults the signature an operator should see, plus `detectable_by` and
`NOT_detectable_by` — because knowing what a fault will *not* tell you is how
you avoid a confident wrong diagnosis.

## Quick start

Requires Docker with Compose v2 and `uv`.

```bash
cp .env.example .env      # then set INFLUX_LICENSE_KEY and the passwords
docker compose up -d
docker compose logs -f softplc gateway
```

Then browse the plant, and poke it:

```bash
python tools/opcua_browser.py browse
python tools/opcua_browser.py watch AERATION:AHU-1:DO
```

Full walkthrough in [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md).

## Documentation

| Document | What it is for |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | Why the architecture is shaped this way, and what was rejected |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, boundaries, and how a value travels |
| [docs/DATA-FLOW.md](docs/DATA-FLOW.md) | One scan, end to end, and what each layer adds |
| [docs/TERMINOLOGY.md](docs/TERMINOLOGY.md) | Plant, control and IIoT vocabulary, defined once |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | Standards and specifications, each with a source to study |
| [docs/TESTING.md](docs/TESTING.md) | What is verified, how, and what is deliberately not |
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | From nothing to a running plant |
| [docs/SECURITY.md](docs/SECURITY.md) | The threat model, and the two known gaps, stated plainly |
| [docs/LEARNING-LOG.md](docs/LEARNING-LOG.md) | What surprised me, and why — the most useful file here |

## The SQL track

`sql/` is a five-stage course, beginner to expert, run against the data this
project generates. It is not an appendix; it is where you learn to ask the
questions the plant exists to answer.

| Stage | Directory | What you learn |
|---|---|---|
| 00 | `sql/00-foundations/` | Reading a schema and a time-series table honestly |
| 01 | `sql/01-beginner/` | `SELECT`, `WHERE`, aggregation, the shape of the data |
| 02 | `sql/02-intermediate/` | `JOIN`, CTEs, window functions, first time bucketing |
| 03 | `sql/03-advanced/` | Gap filling, continuous aggregates, retention, `INFORMATION_SCHEMA` |
| 04 | `sql/04-expert/` | Window frames, time-weighted averages, change detection, query planning |

Each lesson has a question, a query and an expected result in
`sql/_answers/`. Run them against the seeded database and check yourself.

## Phase status

- [x] **Phase 1** — contracts, process model, scan loop, control blocks, fault engine
- [x] **Phase 2** — Modbus TCP server, OPC UA server, browser tool, runnable soft PLC
- [x] **Phase 3** — gateway (deadband, spool, both protocol readers), InfluxDB
  line protocol and rollup worker, Couchbase metadata and seeder
- [ ] **Phase 4** — alarm engine with detection by what the fault does *not* show
- [ ] **Phase 5** — Grafana dashboards, the custom Next.js page, Node-RED SCADA flows

Phase 3 is complete in code and unit-tested; the parts that need a live
InfluxDB and Couchbase are wired in `compose.yaml` but not yet exercised by an
integration run. Two known gaps are named in `docs/LEARNING-LOG.md`: the contract
does not link a signal to the Modbus register that carries it, so the gateway
reads Modbus and publishes only OPC UA; and the seed run is not yet checked
against a real database.

## Licence

Proprietary. This is a portfolio project; no open-source licence is granted.
