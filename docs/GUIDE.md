# How this system works

This is the tour. It follows **one number** — an operator changing the aeration
dissolved-oxygen setpoint to 3.4 mg/L — from the keystroke that enters it to the
historian row it becomes, and at every step it says what can go wrong and what
the failure would look like from outside.

**Why one value.** Because everything in this repository is joined by code at
some seam, and seams are where it breaks. Every significant bug so far has lived
at a join and nowhere else. Following a single number visits all of them in the
order they occur, so you learn the system as a sequence of events rather than as
a folder tree.

If you want a *command* rather than a mechanism, use
[`TASKS.md`](TASKS.md). If you want to know what is verified and what is not,
[`TESTING.md`](TESTING.md). This document is about the third question: why it is
built this way, and what it will do to you.

---

## Contents

- [The number we are following](#the-number-we-are-following)
- [1. Someone types 3.4](#1-someone-types-34)
- [2. One number becomes two registers](#2-one-number-becomes-two-registers)
- [3. Into the PLC](#3-into-the-plc)
- [4. The scan picks it up](#4-the-scan-picks-it-up)
- [5. The block republishes itself](#5-the-block-republishes-itself)
- [6. To the historian](#6-to-the-historian)
- [7. To the screens, and to the audit trail](#7-to-the-screens-and-to-the-audit-trail)
- [The same setpoint over OPC UA does nothing](#the-same-setpoint-over-opc-ua-does-nothing)
- [What generalises](#what-generalises)
- [Where to go next](#where-to-go-next)

---

## The number we are following

`AERATION:AHU-1:SETPOINT_DO` is the **only writable signal in the entire
system**. One value, out of fifty-seven.

That is worth sitting with for a moment, because it tells you the shape of the
whole design. The plant publishes 57 measurements and accepts exactly one command,
and that one command is the target dissolved-oxygen concentration for the
aeration basin. Everything else is observation. This is a plant you watch, with
one thing you may adjust.

| | |
|---|---|
| Signal | `AERATION:AHU-1:SETPOINT_DO` |
| Permit range | **0.5 – 6.0 mg/L** (the contract's `writable` entry) |
| Engineering range | 0.5 – 6.0 mg/L (`range` in the signal) |
| Register | **40102**, `float32`, word order **big**, unit 1 |
| On the wire | PDU **102**, two registers |
| Sampled into the historian every | 5000 ms |
| Read by | `AerationControl.setpoint_mg_l` — and only that |

Every hop below is real code you can go read. Where a hop has a specific
failure, it is named.

---

## 1. Someone types 3.4

The operator's setpoint enters at `scada/flows/03-control.json`, an inject node
named `enter a setpoint`, and immediately hits a function node called **"check
against the permit range"**.

This is the first seam, and it is the one most people assume is the enforcement.
It is not. It is **the client's own courtesy check**, implemented in
JavaScript inside Node-RED, and it runs before anything is sent.

Its logic is *refuse, do not clamp*:

> A clamped setpoint is a setpoint the operator did not ask for, and an operator
> who types 20 and gets 6 will not try again; an operator who types 20 and is
> told "20 is outside 0.5 to 6" will.

**What can go wrong.** If the operator's browser, an API, or a second tool talks
to the PLC without going through Node-RED, this check does not run. Nothing
stops 9.0 being written to the PLC directly — *except that the PLC now checks it
too*, which it did not used to. See hop 3.

The permit range lives in exactly one place. `scada/flows/tags.json` carries
`write_range` per tag, generated from the contract, and the flow reads it from
there rather than hardcoding `0.5` and `6`. A flow that hardcoded a range would
be a second source of truth for what an operator may do to the plant, and it
would be the one not reviewed with the contract.

---

## 2. One number becomes two registers

Now the number has to become **two 16-bit Modbus registers**, because a float32
is four bytes and a Modbus register is two. A function node called "build the
Modbus write" does it with `Buffer`:

```javascript
const raw = Buffer.alloc(4);
raw.writeFloatBE(value, 0);
const words = [raw.readUInt16BE(0), raw.readUInt16BE(2)];
msg.payload = words;
```

**What can go wrong — and this is where the project's only control path spent
its entire life broken.**

A float32 has two halves, and **which half goes first is a convention.** Both
conventions are common in industry. Get it wrong and *nothing errors*: you write
a plausible float32 and the plant reads a different plausible float32.

The contract carries the answer per register, and the generator reads it from
there so the trap cannot be typed by hand. Most registers are `big` (ABCD). Two
are `little` (CDAB), and those two are marked as traps:

```
40108  AERATION_BLOWER_VALVE
40304  AERATION_WASTE_RATE
```

**The second thing that can go wrong here is worse.** The write node has a field
called `dataType`, and it looks like it means "a holding register". It does not.
It selects the **Modbus function code**, and nothing else:

| `dataType` | Function code | What it does |
|---|---|---|
| `Coil` | 5 | write single coil |
| `HoldingRegister` | **6** | **write single register** |
| `MCoils` | 15 | write multiple coils |
| `MHoldingRegisters` | **16** | **write multiple registers** |

`HoldingRegister` is function code 6, and **FC6 writes exactly one register and
ignores `quantity` entirely.** A node configured `dataType: "HoldingRegister",
quantity: 2` sends the first word of the float32 and **silently discards the
second.**

The flow was configured that way from the start. So the operator's setpoint had
never once been transmitted correctly: half a number went to the PLC, the low
half was whatever was there before, and every layer reported success.

The value is genuinely treacherous. The contract calls the thing a *holding
register*, so `dataType: "HoldingRegister"` reads as correct and
`MHoldingRegisters` reads like a typo. The one that means "multiple" is the one
that looks wrong.

There is a third, quieter trap in this hop that you will meet again: the address.
The contract says `40102`; the wire wants **102**. Three quantities compose —
the model indexes from 40000, the model is written with a one-slot lead-in, and
pymodbus resolves `PDU = block index - 1` — and they cancel to a subtraction.
`ModbusTcpServer.wire_offset()` is the authority, and the flow generator calls
it so a change to the block layout cannot leave the flows writing to the wrong
offset.

The failure mode is nasty. Send `40102` and you get
`ILLEGAL_DATA_ADDRESS` — *"register not supported by device"* — which names the
register as unsupported rather than as 40 000 too high.

---

## 3. Into the PLC

`ModbusTcpServer` receives the words. This is the hop where the project had to
build something that did not exist, and it is worth understanding why.

**pymodbus 3.6 has no write callback.** No hook, no event, no listener for
"a client wrote something". So there was nowhere to be told a write had arrived.

Every register write in the library, however, funnels through exactly one
method on the slave context:

```
register_write_message.py:71    FC6   write single register
register_write_message.py:216   FC16  write multiple registers
bit_write_message.py:93,229     FC5/FC15  coils
context.py:102                  where all of them land
```

So `ModbusTcpServer` **overrides `ModbusSlaveContext.setValues`** and sees every
write whatever function code it arrived under. Two things about that override
are worth knowing, because both fail silently:

- The parameter is named **`fc_as_hex`** and typed `str`. pymodbus passes an
  **`int`** — 6 or 16. An earlier version compared it to `"h"` and matched
  nothing at all, so the write path never ran. And every *"this must be
  refused"* test still passed, because a write that goes nowhere looks exactly
  like a refused one.
- An override runs **before** the parent's body, so `address` is the raw wire
  PDU. The parent's `address += 1` has not happened yet. "Correcting" for it
  moves writes onto the *previous* register — so a write to the setpoint at PDU
  102 gets refused as `AERATION_DO is read-only`, which is a truthful answer to
  the wrong question.

Then `_accept_write()` applies the policy, and this is where the contract is
actually enforced rather than merely documented:

| Check | Failure looks like |
|---|---|
| is it a register at all? | `ILLEGAL_DATA_ADDRESS` (2) |
| is it writable per the contract? | `ILLEGAL_DATA_VALUE` (3), naming the register |
| is it the right width? | `is 2 register(s) of float32; got 1` |
| is it in the engineering range? | `= 99 is outside the engineering range [0.5, 6]` |

The width check is what eventually **caught hop 2's bug**. The PLC started
refusing half-width writes with a message naming the width it wanted, and that
turned a silent corruption into a legible refusal.

**What can go wrong — and this is the deeper one.** Modbus has **no concept of
a read-only register.** Any conforming server answers FC6 for any address in
range. So this enforcement is *software in the same process that would be
compromised by the attacker*. It is genuinely better than documenting a
permission and not checking it — a laptop with a Modbus tool gets an exception
rather than a silent overwrite — and it is not a boundary.

A refusal is **returned as an `ExceptionResponse`, not raised.** Every pymodbus
handler does `result = context.setValues(...); if isinstance(result,
ExceptionResponse): return result`. Raising reaches the listener's generic
handler and comes back as `SlaveFailure` (4) regardless of the real cause.

An accepted write goes into `pending_writes`, a dict keyed by signal id.

---

## 4. The scan picks it up

Everything so far happened on the Modbus listener's thread. The plant runs on
another one. `pending_writes` is the join between them.

Every 20 ms, `SoftPlc._step` begins by draining it:

```python
self._apply_pending_writes()          # 1. the operator's writes
snapshot = self.faults.step(dt)       # 2. physics
... set_state / set_output ...
self.loop.scan_once()                 # 3. the control blocks
await self._publish(snapshot)         # 4. out to the world
```

**The order is the design.** Writes are first because the physics *reads* them.
Applying them after `faults.step` would take an extra scan to reach the
controller; applying them after `publish` would be undone by hop 5 in the same
scan.

`take_writes()` returns **and clears** in one step. A write must be applied
once. If it only returned, the setpoint would be re-applied every scan forever —
harmless-looking for a number, but it means the write path is a constant
re-drive rather than an edge.

### The copy trap — the most expensive thing in this document

The write must be applied **twice**:

```python
self.plant.aeration.setpoint_do_mg_l = value      # what the snapshot publishes
self.aeration_control.setpoint_mg_l = value       # what the PI loop uses
```

The first is what makes the register read back: `plant.snapshot()` publishes
`ae.setpoint_do_mg_l` as this signal (`softplc/process/plant.py:422`).

**The second is the one nobody remembers.** `AerationControl` was constructed
once, at startup, and took the setpoint *by value*:

```python
self.aeration_control = AerationControl(
    setpoint_mg_l=self.plant.aeration.setpoint_do_mg_l    # main.py:110
)
```

So the block holds its own `setpoint_mg_l`, and `AerationControl.update()`
integrates against `self.setpoint_mg_l` — *its own field*. Two objects, two
values, from one moment onwards.

**What the failure looks like: nothing.** Assign only the first, and:

- the register reads 3.4 ✓
- the historian records 3.4 ✓
- the mimic displays 3.4 ✓
- the audit trail says *accepted* ✓
- the plant looks healthy ✓
- **the dissolved oxygen does not move** — the loop is still integrating against
  the 2.0 it was handed at startup.

Every indicator says it worked. This is the most expensive failure mode in the
repository and the only defence is knowing it exists.

**And no test that reads the register can catch it**, because the register
publishes the *plant's* field, which is the one you did set. The only evidence is
behaviour — **air flow must rise when the setpoint rises, and fall when it
falls.** That is why `tests/test_modbus_writeback.py` asserts on `duty_pct`
rather than on the value you can read back.

---

## 5. The block republishes itself

The control blocks ran; the physics advanced. Now `_publish` pushes the model
into the Modbus datastore:

```python
def _flush(self) -> None:
    self._holding.setValues(self.BLOCK_LEAD_IN, self.model.holding)
```

One call, the whole block, every scan. **No per-register diff, no dirty
tracking.** Every published value is overwritten every 20 ms, including the
one the operator just wrote.

**This is why hop 4 had to exist.** The model is the only source of truth, so a
value written into the block is erased by the next publish unless the plant
itself absorbed it first. There is no way to make a write stick without a path
from the write back into the model — and that path is `_apply_pending_writes`.

It is also why **the same design means the OPC UA write path is inert**, which
is the next section.

---

## 6. To the historian

The PLC does not talk to the database. A separate service, the **gateway**,
pulls from it and writes to TimescaleDB:

```
softplc (Modbus/OPC UA)  →  gateway  →  deadband  →  spool  →  Postgres
```

**The gateway polls.** Once a second, `poll_interval_s = 1.0`, in batches. It
does not subscribe — `git grep create_subscription` finds one hit in the whole
repository, in a browser tool.

Two consequences worth internalising before you go looking for a bug:

- **The database is about a second behind.** A value you just injected may not
  be queryable yet. That is not a bug.
- **The deadband is on the wrong side of the wire.** Each signal carries a
  `deadband` — minimum meaningful change, suppressing duplicate storage — and it
  is applied by the *gateway*, on the way **into** the database. The PLC does
  not apply it. So a frozen sensor reading sails through the deadband
  perfectly well.

That last point is why `sensor_flatline` is the most deceptive instrument fault
in the library: a transmitter holding its last value produces readings that are
in range, change slowly enough to look alive, and satisfy every deadband rule.
`Deadband.accept` handles `None`, quality changes and recoveries carefully — and
a *frozen but plausible* value is none of those things.

---

## 7. To the screens, and to the audit trail

Three surfaces read the same historian, and one table records that it happened.

The **mimic** (`scada/flows/01-mimic.json`) is the operator's process diagram.
It polls and renders; it does not read the PLC's address space.

The **audit trail** is the row in `event`:

```sql
INSERT INTO event (ts, kind, severity, message, signal_id, detail)
VALUES (now(), 'setpoint_written', 'info', $1, $2, ...)
```

and the message it writes is:

```
setpoint AERATION:AHU-1:SETPOINT_DO set to 3.4 mg/L (range 0.5-6 mg/L)
```

**What can go wrong — and this one is worth its own paragraph.** The audit node
sits *after* the write node on the flow, so it runs only if the write succeeded.
That is correct, and it is a recent correction: before the PLC started refusing
out-of-range writes, the flow was "accepting" writes the plant discarded, and
`event` recorded setpoints that had never been applied.

An audit trail that records control actions which did not happen is worse than
no audit trail, because it is trusted.

Note also that `record what was written` reads `msg.req` — the request stashed by
the earlier function node — and binds the operator's number as a SQL
*parameter*. The alternative, splicing it into the statement, is a SQL injection
one mistyped number away.

---

## The same setpoint over OPC UA does nothing

Everything above describes the Modbus path. The identical setpoint, written over
OPC UA to the identical contract, from the same server process, **goes nowhere.**

The reason is hop 5. The OPC UA address space is rebuilt from the snapshot every
scan, so a write is overwritten within 20 ms. And no control loop reads the
address space — `AerationControl` reads the plant's dataclass field, not the
address space. So the write is not merely overwritten; it was never read by
anything in the first place.

`OpcUaServer.write_value()` exists, checks the engineering range, and **has no
caller.** A wire write is handled by `asyncua` setting the node directly.

| | Modbus | OPC UA |
|---|---|---|
| Write accepted? | yes | yes, silently |
| Reaches the plant? | **yes** | **no** |
| Range enforced on write? | yes, exception 3 | no — `EUInformation` is advisory |
| Enforced by | this server, in software | nothing |

The protocol with the better permission model is the one you cannot use to change
the plant. That is not a paradox, it is just what happens when one write path is
built and the other is not.

---

## What generalises

Three failures cost this repository real time, and they are the same mistake
wearing different clothes:

1. **A test that asserted the wrong thing was correct.** The Node-RED test read
   `dataType == "HoldingRegister"` and its own failure message explained, in
   detail, why that was right.
2. **A docstring that described the intent instead of the behaviour.** The
   generator's comment explained function code 16 writes; the call site passed
   the function-code-6 value.
3. **A gate running over the wrong subset**, reporting success because it never
   looked where the bugs were.

In each case the code was internally consistent and externally wrong. Nothing
errored. Every layer agreed with every other layer — and the thing that was wrong
was the thing **all of them agreed on**. That is why no amount of reading the
code or the prose would have found it: the reader inherits the shared assumption
and has nothing to check it against.

The defence is unglamorous, and it is most of what this repository does:

- **Execute the claim.** `tests/test_task_index.py` runs the commands in
  `docs/TASKS.md`. `test_scada_contract.py` runs the flows' SQL. The PLC refuses
  short writes so the half-float bug could not stay silent.
- **Make the failure name the cause.** Not `Illegal data value` but
  `AERATION_SETPOINT_DO is 2 register(s) of float32; got 1`. The first you
  cannot debug; the second you can.
- **Reintroduce the fault and watch the test fail.** Every behavioural claim in
  this document was checked that way. A test never seen failing is a guess with
  a green tick.
- **Know the difference between a value and a signal.** A value that *reads back*
  proves the wire works. A value that *changes the plant* proves the seam does.

That last one is the whole of hop 4, and it is the thing to carry away.

---

## Where to go next

**Operate it** — the operator's day, seven use cases, and what a real plant does differently: [`docs/USE-CASES.md`](USE-CASES.md)

**By task** — every task to the command that does it: [`docs/TASKS.md`](TASKS.md)

**Why the architecture is shaped this way, and what was rejected**: [`DESIGN.md`](DESIGN.md)

**One scan end to end, and what each layer can lose**: [`DATA-FLOW.md`](DATA-FLOW.md)

**Components, boundaries, the thread model**: [`ARCHITECTURE.md`](ARCHITECTURE.md)

**What is verified, and what is deliberately not**: [`TESTING.md`](TESTING.md)

**Every wrong assumption, with the reasoning intact**: [`LEARNING-LOG.md`](LEARNING-LOG.md)