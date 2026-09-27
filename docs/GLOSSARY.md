# Glossary

Standards, protocols and specifications this project implements or depends on.
Each entry says **what it is**, **what this project does with it**, and **where to
read more** — because a standard you cannot go and read is a rumour.

Plant vocabulary is in [`TERMINOLOGY.md`](TERMINOLOGY.md) instead.

---

## Protocols

### Modbus TCP

**What it is.** A request/response protocol carrying registers — 16-bit values,
or pairs of them for 32-bit — between a master (a SCADA system) and a slave (a
device). Published as Modbus Application Protocol Specification 1.1b3; the TCP
transport is Modbus Messaging on TCP/IP Implementation Guide 1.1b.

**What this project does.** Implements a *server* on port 5020, serving 19
registers derived from the contract. 14 of them are linked to a signal; the other 5
are deliberately not measurements.

**The trap, and the reason it is here.** A 32-bit float occupies two 16-bit
registers, and **the protocol does not say which register holds the high word.**
Most devices are big-endian and some are little-endian, and the two frequently sit
side by side on the same device. Read a little-endian float as big-endian and you
get about `2.3e-41`: finite, inside every range, and wrong.

Nothing in Modbus will ever tell you this happened. That is the project's
signature bug class, and the only defence is to compare two independent
observations of the same physical quantity — which is why `source` is part of the
primary key of `reading`.

**Read:** `modbus.org` → Modbus Application Protocol. The specification is short
and unusually readable, and §6 of it is where the register model lives.

### OPC UA

**What it is.** A platform-standard protocol for industrial interoperability:
an address space, services, a type system, and a security model built in. It
replaces OPC DA and OPC AE, which were Windows-only and had no security model at
all. The specification is a set of parts, and Part 3 is the one you read for
services.

**What this project does.** Implements a *server* on port 4840 with an address
space generated from the contract: `AREA.HOLDER.FIELD`, with browse names taken
verbatim from the contract's field names. The gateway is a client.

**Read:** `reference.opcfoundation.org` → Part 3: Address Space Model, Part 4:
Services. The Address Space Model part is the one that changes how you think about
a data model.

**The gap:** security policy is `None` and clients are anonymous. See
[`SECURITY.md`](SECURITY.md).

**The other gap, and the more interesting one:** the engineering range travels in
`EUInformation`, which the specification defines as *metadata for display*. It is
not a constraint a server is required to enforce. A client that writes `DO = -40`
is not stopped, and the historian records it. **"The value is out of range" and
"the value is wrong" are different failures, and only one of them is loud.**

### Why no MQTT

Considered and excluded at the start. A broker would sit between the plant and the
historian and hide exactly the protocol behaviour this project exists to teach:
polling versus subscription, status codes, register word order, and what happens
when a client cannot keep up. OPC UA covers subscription natively, and Modbus is
polling by nature. Putting a broker in the middle would have made both lessons
harder to reach.

It would also have made the gateway stateful and therefore able to lose data —
which is the one thing this project's gateway is designed not to be.

---

## Storage

### PostgreSQL

**What it is.** An open-source relational database. SQL, transactions,
constraints, foreign keys, and `CHECK` constraints the database enforces rather
than trusting the writer.

**What this project does.** The only database. Five tables: `reading` as a
hypertable, plus `site`, `equipment`, `signal` and `event`.

**Read:** `postgresql.org/docs/current/` — the chapters on the manual are the best
SQL documentation written. Chapter 5 is the type system, chapter 7 is `SELECT`,
chapter 38 is the PL.

**What it bought, concretely:** `reading.signal_id REFERENCES signal(id)`. In the
previous storage engine a reading could name a signal that never existed and
nothing would object — there was no mechanism to express the constraint, because
there was no relation to constrain. It found a real modelling error on its first
run.

### TimescaleDB

**What it is.** A PostgreSQL extension adding hypertables, continuous aggregates,
retention policies, `time_bucket` and compression.

**What this project does.** `create_hypertable('reading', by_range('ts'))`, two
continuous aggregates both reading from `reading`, and retention policies on the
raw and 1-minute tiers.

**Read:** `docs.timescale.com` → "Hypercore" (formerly hypertables), and
"Continuous Aggregates". The rollup-weighting page is worth reading in full; it
describes the average-of-averages problem this project designed out rather than
solved.

**The version matters.** `timescale/timescaledb:2.30.1-pg16` pins both the
extension and the PostgreSQL major, which is a pin that means something. The
`-oss` variants are Apache-2.0 only; the plain image is what continuous
aggregates need.

### Hypertable

A table partitioned by time into *chunks*, presented as one relation. A query
with a timestamp range does not examine rows outside it — visible in the plan as
`Chunks excluded during startup`, and the reason a `WHERE ts` query on four
million rows is fast.

### Continuous aggregate

An incrementally-maintained materialised rollup. `reading_1m` and `reading_1h`.

**Both aggregate from `reading`, not from each other.** Rolling 1 minute into
1 hour by averaging the 1-minute averages is wrong as soon as two minutes hold
different numbers of points, and they always do — the deadband filtered some of
them and the last window of a run is short. The error is largest exactly when the
data is most interesting, which is during an event. Aggregating both tiers from raw
costs a second pass and removes the problem entirely.

`materialized_only = true` means the aggregate does not answer for the last
incomplete window. A rollup that answers for a window that is still changing
teaches a reader to trust a number that is about to move.

---

## Standards and conventions

### IEC 61131-3

**What it is.** The international standard for programmable languages
(Structured Text, Function Block Diagram, ladder logic) and for the *organisation*
of control system software into program organisation units, function blocks,
functions, and global variable lists.

**What this project takes from it.** The function-block abstraction and the
scan-cycle model: typed inputs, explicit internal state, deterministic execution
order, one shared scan image. Not the languages — everything here is Python, and
the value of the standard is the architecture rather than the syntax.

**Read:** the standard is paywalled. The IEC 61131-3 website has the
organisation-of-software parts summarised, and CODESYS documentation describes the
model in practice without pretending to be the standard.

### OPC UA Information Model

**What it is.** Part 3 of the OPC UA specification: how types, nodes, references
and instances are arranged. The `AREA.HOLDER.FIELD` structure here is an
information model — a hierarchy of typed nodes with browse names — and reading
Part 3 is the difference between generating an address space and generating a
folder tree.

### IEEE 754

**What it is.** The standard for binary floating-point arithmetic. §7 is the one
that matters here: it specifies the result of an operation, including which
rounding mode, but it **does not require `x + y + z` to equal `x + (y + z)`**
because it cannot — 0.1 + 0.2 ≠ 0.3 is a consequence of the representation, not a
defect in it.

**What this project does with it.** Provides the reason a `DOUBLE PRECISION`
aggregate is not bit-reproducible. The same query over the same rows returns
`6391.155254170624` and then `6391.155254170615`, because the order rows are
summed in is not pinned. It also explains the signature bug's arithmetic: a
little-endian float misread as big-endian is a bit-pattern reinterpretation, not a
rounding error, and the result is a valid float that happens to be 2.3e-41.

**Read:** `754r.ieee.org`. Short standard, famously hard to read, and §6.2 on
exponentials is the part that will make the Modbus trap obvious.

### RFC 3339

**What it is.** Date and time on the internet: `2026-09-26T14:30:00Z`.

**What this project does with it.** PostgreSQL's `TIMESTAMPTZ` is an absolute
instant and is not a calendar time, which is the right choice for telemetry — a
reading is an instant, and a plant that observes daylight saving has a problem
this project does not have because it stores UTC.

### ISO 8601

The date-and-time standard, of which RFC 3339 is a profile. Named here only to
avoid the common confusion: they overlap but are not the same specification.

---

## Licences of the components

Recorded because a supply-chain list that is out of date is worse than none.

| Component | Licence | Note |
|---|---|---|
| PostgreSQL | PostgreSQL Licence | Open source, permissive |
| TimescaleDB | Apache 2.0 (Community) / Timescale Licence | The pinned image includes TSL features |
| Python | PSF | |
| PyYAML | MIT | The only hard runtime dependency |
| `asyncua` | LGPL 3.0 | OPC UA server and client |
| `pymodbus` | BSD 3-Clause | Modbus. **Pinned to 3.6.6** — 3.15 changed the datastore API |
| Grafana | AGPL 3.0 | Profile `observability` |
| Docker images | various, see the vendor | |

**This project itself grants no licence.** It is a portfolio project and is marked
proprietary. The dependencies above are listed so that the provenance is visible,
not to imply any grant.
