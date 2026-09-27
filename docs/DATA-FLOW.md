# Data flow

One scan, end to end. What each layer adds, and — more usefully — what each one
can lose.

```
process model → scan image → two protocol faces → deadband → spool → Postgres
     50 Hz        57 values      Modbus + OPC UA     filter    durable    typed
```

---

## 1. The process model: physics, 50 Hz

`softplc/process/plant.py` steps the whole plant forward by one scan period
(20 ms at the default 50 Hz) and returns a snapshot of 57 values plus a quality
code per signal.

This is the only component that knows what a wastewater treatment plant *is*.
Monod kinetics for the aeration basin, a mass balance for the clarifier, VFA and
alkalinity chemistry in the digester, breakpoint chlorination at the effluent,
a DO control loop and a chlorine dose controller.

**It has no idea a database exists.** That is not an accident of layering — it is
what makes 1 600 lines of process chemistry testable without a socket, and it is
why the storage migration did not touch this file.

The `bad_instrument` fault deliberately does *not* damage the process. A fouled
DO probe publishes `Uncertain` while the basin stays healthy, because a historian
that cannot tell a bad sensor from a bad process cannot be trusted to alarm on
anything.

## 2. The scan image: 57 values in a dict

`softplc/scanloop.py` holds the scan image — the register file a real PLC would
have. Every function block reads from it and writes to it, in a fixed order, on
one thread.

This is where the PLC idiom lives: the scan image is the *only* shared state, and
a block that reads a value another block wrote earlier in the same scan sees that
value, not the previous one. Getting that ordering wrong is the classic PLC bug
and it is asserted by tests.

## 3. Two protocol faces, deliberately different

Both serve the same scan image, and neither is a translation of the other. They
model two real industrial protocols, and the differences are the lesson.

### Modbus TCP, port 5020

19 registers. **14 of them are linked to a signal**; the other 5 are deliberately
not measurements — a heartbeat, a fault code, an equipment state bitfield, and the
two halves of a 32-bit runtime counter.

Each register declares its own word order, and **two of the nineteen disagree
with the other seventeen**:

```yaml
- address: 40100
  name: AERATION_DO
  signal: AERATION:AHU-1:DO
  word_order: big        # high word first
- address: 40102
  name: AERATION_SETPOINT_DO
  signal: AERATION:AHU-1:SETPOINT_DO
  word_order: big        # high word first
- address: 40108
  name: AERATION_BLOWER_VALVE
  signal: AERATION:AHU-1:BLOWER_VALVE
  word_order: little     # low word first — read this one wrong and see what happens
```

The `little` register is the **fifth** in a run of `big` ones, not the
neighbour of one, which is the harder case and the point: nothing about the
address or the ordering gives you a hint. The other is `40304`,
`AERATION_WASTE_RATE`.

> This example previously showed `40101`/`40103` with the second marked
> `little`. **Neither address exists** — the real ones are `40100` and `40102`,
> and both are `big` — and it claimed "the neighbours disagree" about a pair that
> did not. Three separate errors in four lines of illustrative YAML, in the one
> document that traces a scan end to end. The addresses are now taken from the
> contract rather than written by hand, which is what they should have been, and
> `tests/test_readme_claims.py` asserts the counts so the prose cannot drift from
> them again.

The wire offset took three shifts to get right, and all three are commented where
the arithmetic happens:

```
net PDU = contract address − 40000
        = (model index) − (one-slot block lead-in) − 1     [pymodbus's own offset]
```

Read a low-word-first float as high-word-first and you get about `2.3e-41`:
finite, in range, wrong. Ten tests exist solely to pin that offset, and they were
`xfail` for a while because the arithmetic was checked and the wire was not.

### OPC UA, port 4840

The address space is generated from the contract: `AREA.HOLDER.FIELD`, with
browse names verbatim from `sig.field` — which is why the client's lookup must
not lower-case it. A bug where it did made all 57 lookups fail silently, because
"no data" and "not found" look the same from the outside.

**Write permission is enforced on the wire. The range is not.** A client can write
a value outside the signal's engineering limits; the server accepts it and logs
it. That is one of the two open gaps in [`SECURITY.md`](SECURITY.md).

### Which one wins

The gateway reads Modbus first and OPC UA second, so OPC UA overwrites Modbus
per signal per poll. `source` records which one landed. The primary key is
`(ts, signal_id, source)`, so *both* can be stored — which is what makes the
cross-protocol comparison in
[`sql/00-02`](../sql/00-foundations/00-02_identity_and_values.md) a query rather
than a hunch.

## 4. The deadband: the filter, and what it costs

`gateway/deadband.py`. A reading is published when:

* it is the **first** reading of that signal since the writer started, or
* the value has moved by more than the signal's deadband (absolute or relative,
  per the contract), or
* the signal's mode is `always`, or
* **its quality is not Good** — a fault is never filtered

That last rule is the important one and it is asserted by a test. A deadband
exists to stop a historian storing noise, and a naive implementation that filters
uniformly will happily suppress the reading that says the blower has stopped.

**What it costs, stated honestly:** a row exists when the value moved, so "no
readings" is ambiguous between *the plant was steady* and *the instrument is
gone*. The `sql/02-04` lesson found **thirteen signals that produced exactly one
reading in a week**, and nothing in the schema objects. The industry answer is
periodic key-value reporting — force a reading every N minutes regardless of
movement — and this project does not do it because it would roughly triple the
row count.

## 5. The spool: the durability guarantee

`gateway/spool/store.py`. JSON lines on a named volume, flushed per poll, trimmed
by size.

**This is the only component whose job is not to lose data**, and the rules follow
from that:

* the gateway starts and runs with **no database at all**. A gateway that refused
  to start without one would throw away the one guarantee it exists to provide.
  It logs `no POSTGRES_DSN; spooling only` and keeps reading the plant.
* a failed write **keeps its rows**, in arrival order. The spool is the durable
  copy; losing a batch in the writer as well would be the one place data vanishes.
* the writer's automatic flush **backs off for one batch interval after a
  failure**. Without that, a failed flush leaves the buffer over the row bound, so
  every subsequent `add` retries immediately: 57 doomed round trips per second,
  against a database that is already unwell.

## 6. Postgres: five tables

`reading` (hypertable) plus `site`, `equipment`, `signal`, `event`.

```
 ts          TIMESTAMPTZ      NOT NULL
 signal_id   TEXT             NOT NULL REFERENCES signal (id)
 value       DOUBLE PRECISION            -- NULL means no value
 quality     SMALLINT         NOT NULL DEFAULT 0   CHECK (quality IN (0,1,2))
 source      TEXT             NOT NULL    CHECK (source IN ('opcua','modbus','seed','rollup'))
 PRIMARY KEY (ts, signal_id, source)
 CHECK (value IS NOT NULL OR quality <> 0)
```

A reading is an observation of a signal, at a moment, by a source. The primary
key says so, which is why three readings of one signal at one instant from one
protocol is a **duplicate key error** rather than three rows.

Batched: 2 000 rows or 5 seconds, whichever comes first. Both bounds are needed —
a size bound caps the transaction, and a time bound means a plant that goes quiet
does not sit on its last readings in memory where a crash loses them.

## 7. Retention and the two tiers

| Tier | Resolution | Kept | Served by |
|---|---|---|---|
| `reading` | 1 s | 7 days | `time_bucket('1 minute', ts)` |
| `reading_1m` | 1 min | 90 days | `reading_1m` |
| `reading_1h` | 1 h | indefinitely | `reading_1h` |

**Both aggregates read from `reading`, not from each other.** That is the decision
the whole rollup design turns on, and
[`DESIGN.md`](DESIGN.md#both-continuous-aggregate-tiers-roll-up-from-reading-not-from-each-other)
has the reasoning: averaging the 1-minute averages into an hourly mean is wrong as
soon as two minutes hold different numbers of points, and the error is largest
exactly when the data is most interesting.

---

## Failure modes, and what each one costs

| Failure | What you see | What it costs |
|---|---|---|
| The database is down | `postgres.failures` climbing in `/health`; `spool` growing | **Nothing.** The spool absorbs it and drains on recovery. |
| One batch write fails | one `WARNING` per batch, not per row | Nothing. The batch is retried in order. |
| The gateway is killed | nothing | Nothing. The spool is on a volume; the next process resumes. |
| The spool volume is lost | the backlog | The backlog. The only unrecoverable case, and it needs `docker compose down -v`. |
| A signal goes quiet | a gap in `reading` | Ambiguity: steady, or dead. See `sql/02-04`. |
| A float is decoded low-word-first | `2.3e-41` | **Nothing detects it** except a cross-protocol comparison, which is why `source` is in the primary key. |

The last two rows are the honest answer to "what can this system not tell you".
