# Architecture

Components, boundaries, and how a value travels from a float in a basin to a
number on a dashboard. For *why* the shape is this shape, read
[`DESIGN.md`](DESIGN.md). For one scan in detail, read
[`DATA-FLOW.md`](DATA-FLOW.md).

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

## The five containers

| Service | Image | What it is | Restart policy |
|---|---|---|---|
| `db` | `timescale/timescaledb:2.30.1-pg16` | PostgreSQL 16 + TimescaleDB | `unless-stopped` |
| `softplc` | project image | the simulated plant, serving both protocols | `unless-stopped` |
| `init-db` | project image | one-shot: apply schema, load the contract | `no` |
| `gateway` | project image | read both protocols, deadband, spool, write | `unless-stopped` |
| `web` | `ui/web` | the Next.js dashboard — **no source yet**, profile `ui` | `unless-stopped` |
| `grafana` | `grafana/grafana:11.5.1` | profile `observability` | `unless-stopped` |
| `seed` | project image | profile `demo`: a week of history | `no` |

One Python image, parameterised by command. One database. See
[`adr/0001`](adr/0001-single-postgres-database.md).

## The four boundaries

### 1. `contracts/tags.yaml` is the only place meaning is declared

57 signals, 22 assets, their units, engineering ranges, normal bands, deadbands,
sample rates, permit limits, Modbus addresses and word orders, 11 faults and 6
scenarios. The PLC, the gateway, the database seeder, the tests and the course
all read that one file.

Nothing is declared twice, so nothing can drift. Three rules are enforced by the
loader and covered by tests:

* a signal is identified by `AREA:HOLDER:MEASUREMENT`, and the value lives in a
  column, never in the name;
* descriptive metadata belongs to `equipment`, not to a measurement;
* equipment *state* is a measurement, not a column that changes every scan.

**A foreign key found a fourth rule this file had been violating.** 24 signals
name a grouping — `FLOW`, `LIFT`, `SITE`, `WEATHER` — rather than an asset, so
`signal.equipment_id` is nullable and `holder` is derived from the id. See
[`DESIGN.md`](DESIGN.md).

### 2. `softplc/` owns the physics and is entirely database-agnostic

`softplc/process/units.py` is 1 600 lines of hand-written Monod kinetics, mass
balances, digester VFA/alkalinity chemistry, breakpoint chlorination, a DO control
loop and a chlorine dose controller. The only runtime dependency in the whole
project is PyYAML.

**A library would have hidden the lesson.** Every constant carries its
calibration and its provenance in a comment, including the ones that were wrong
first.

### 3. `gateway/` is deliberately the thinnest component

It holds no state worth losing. The plant holds the physics, the spool holds the
data, the contract holds the meaning. Kill it and the next one starts and carries
on from the spool.

That is the whole design goal, and it is why the interesting logic lives in
modules testable without a socket: the deadband, the spool, and the writer's
buffering policy (which takes an `execute` callable, so its policy is tested with
a list and no database).

### 4. `storage/` is the only thing that knows what a database is

```
storage/postgres/schema.py     DDL + contract → tables.  The only module that
                               knows the schema.
storage/postgres/writer.py     Buffering policy. Takes an execute callable, so
                               the policy is testable without Postgres.
storage/seed/main.py           Replays the plant model through real fault
                               scenarios at ~29 000 readings/second.
```

## One event loop, two protocols, one thread

OPC UA is asyncio. pymodbus's TCP server is a blocking thread with no
asynchronous form. Rather than fight it, the gateway runs the asyncio loop for
OPC UA and hands the Modbus client to a worker thread, with a queue between them.
The scan loop is single-threaded and the queue is the only shared state.

The soft PLC has the same shape for the same reason, plus one more: it runs its
own loop on a background thread so the OPC UA server can bind to it.

## Ports

| Port | What | Host-exposed |
|---|---|---|
| 5432 | PostgreSQL | yes |
| 5020 | Modbus TCP (server) | yes |
| 4840 | OPC UA (server) | yes |
| 3000 | Grafana | profile only |
| 3001 | Next.js dashboard | yes |

## What is deliberately not here

* **No MQTT.** It was excluded at the start, on the grounds that a broker would
  hide the protocol behaviour the project exists to teach. OPC UA is the primary
  protocol and Modbus TCP the secondary one, and both are implemented directly.
* **No authentication in the demo stack.** The threat model is written down in
  [`SECURITY.md`](SECURITY.md) along with the two gaps and what closing them
  would take. A plant that pretends to be secure teaches the wrong lesson.
* **No ORM.** Every query in this project is a query somebody chose to write,
  and the SQL course reads the same tables the code writes.
* **No message bus between the gateway and the database.** A spool file on a
  volume is the durability mechanism, and it is the one component whose job is
  not to lose data.
