# 05 — Writing, and the difference between enforced and not

**Next:** [06 — Why OPC UA is asyncio and Modbus is not](06-async.md) · [Back to the course](../README.md) · [Previous: 04](04-subscriptions.md)

`docs/SECURITY.md` carries a gap with a number attached, and this lesson is where
that number gets measured. From `softplc/servers/opcua.py:452`:

> **Write permission** is enforced by OPC UA itself. A read-only variable answers
> `BadUserAccessDenied`, so a client cannot write a measurement. That is a
> protocol guarantee and it holds.
>
> **Engineering range** is *not* enforced by the base specification.
> `EUInformation` is advisory … A client can therefore write 99 mg/L to a DO
> setpoint whose range is 0.5–6.0, and the write will be accepted.

Both halves of that are checkable, and the first one is the most reassuring thing
in this project. The second is only the start.

## The question

> If an operator changes a setpoint from a client, what actually happens?

## How much of the plant can be written at all

```python
from softplc.contract import contract
c = contract()
writable = [s for s in c.signals.values() if s.writable]
print(f"{len(writable)} of {len(c.signals)} signals are writable")
for s in writable:
    print(f"  {s.id:30} {s.field:20} range [{s.range_min}, {s.range_max}] {s.eu}")
```

```
1 of 57 signals are writable
  AERATION:AHU-1:SETPOINT_DO     setpoint_do_mg_l     range [0.5, 6.0] mg/L
```

One. A dissolved-oxygen setpoint. (It was two, and the second was a weather
flag: `SITE:WEATHER:STORM` was `writable: true` while nothing applied a write to
it, and `InfluentUnit.storm_active` is not a latch — it clears itself once the
storm's duration elapses. A write that reports success and vanishes on the next
scan is worse than one that is refused, so it is read-only now.) The design is right and the
comment at `opcua.py:477` says why: *"the contract's write surface is
deliberately small."* In a real plant you would also want to be able to tell a
pump from a setpoint without reading the source, and `UserAccessLevel` in the
address space does that — which, unlike the units, is a real part of the model.

## The guarantee that holds

Write to a measurement, and OPC UA stops you. Not this project's code — the
protocol:

```python
a  = await root.get_child("2:AERATION")
ah = await a.get_child("2:AHU-1")
ro = await ah.get_child("2:do_mg_l")
try:
    await ro.write_value(ua.DataValue(ua.Variant(99.0, ua.VariantType.Double)))
    print("ACCEPTED — the guarantee has failed")
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")
```

```
BadUserAccessDenied: User does not have permission to perform the requested operation.(BadUserAccessDenied)
```

This is `set_writable()` at `opcua.py:272` doing the work, and it is a real
protocol guarantee rather than a convention. It is worth saying plainly, because
the rest of this lesson is a list of things that are *not* guaranteed, and it
would be easy to come away thinking OPC UA is unreliable. **Write permission is
enforced, by the protocol, and it works.**

## The guarantee that does not

Now the same write, to a variable that *is* writable, with a value that is
absurd:

```python
ah = await (await root.get_child("2:AERATION")).get_child("2:AHU-1")
sp = await ah.get_child("2:setpoint_do_mg_l")
print("before:", await sp.read_value())
await sp.write_value(ua.DataValue(ua.Variant(2.5, ua.VariantType.Double)))
print("wrote 2.5, in range  ->", await sp.read_value())
await sp.write_value(ua.DataValue(ua.Variant(99.0, ua.VariantType.Double)))
print("wrote 99.0, out of range ->", await sp.read_value())
print("the contract says this signal's range is [0.5, 6.0]")
```

```
before: 2.0
wrote 2.5, in range  -> 2.5
wrote 99.0, out of range -> 99.0
the contract says this signal's range is [0.5, 6.0]
```

**99.0 mg/L of dissolved oxygen setpoint, accepted, 16× the contract's maximum.**
The server publishes `EngineeringRangeLow` and `EngineeringRangeHigh` as
properties (lesson 01) and neither is checked, because `EUInformation` and
`EngineeringRange` are advisory in the specification and `asyncua` does not
enforce them.

The range *is* checked in one place — in this function:

<!-- softplc/servers/opcua.py:480-485 -->
<!-- check: skip -->
```python
sig = self.c.signal(signal_id)
if not sig.in_range(value):
    raise ua.UaError(
        f"{signal_id} = {value} is outside its engineering range "
        f"[{sig.range_min}, {sig.range_max}]"
    )
```

And here is the part I did not expect. **That function has no caller.**

```bash
git grep -n "write_value" -- '*.py' | grep -v '^tests/'
```

```
softplc/servers/opcua.py:407:            await entry.node.write_value(
softplc/servers/opcua.py:447:            await node["state"].write_value(
softplc/servers/opcua.py:452:    async def write_value(self, signal_id: str, value: float) -> None:
```

Two calls, and both are the server writing *out* to a node. Line 452 is the
definition. The contract-enforcing write path exists, is correctly written, is
covered by tests, and **is unreachable from any client** — because a wire write
is handled by `asyncua` setting the node's value directly, never routing through
`OpcUaServer.write_value()`.

So the range check is not "a courtesy for in-process callers" as the docstring
says. There are no in-process callers either. It is dead code that reads like a
security control, which is worse than not having it, because the next person to
read the file will believe writes are checked.

## And the writes are inert anyway

Suppose the range *were* enforced, and a client wrote a legal 3.0 mg/L setpoint.
Would the plant change? No — and this is the finding that made me rewrite the
lesson I had planned.

The process model is the only source of truth, and it republishes every cycle:

<!-- softplc/main.py:236-240 -->
<!-- check: skip -->
```python
entry = self._space.variables.get(signal_id) if self._space else None
if entry is not None and entry.value == values[signal_id] \
        and entry.quality == quality:
    continue
self.opcua.set_value(signal_id, values[signal_id], quality)
```

`self._space` **is** the OPC UA address space (assigned at `main.py:131`), so
this compares the node against the model and overwrites on any difference. What
does the model produce?

```python
from softplc.process.plant import Plant
from softplc.contract import contract

plant = Plant(contract())
for _ in range(80):
    plant.step(0.1)
snap = plant.snapshot()
print("the model publishes SETPOINT_DO =", snap.values["AERATION:AHU-1:SETPOINT_DO"])
print("the control loop's own field     =", plant.aeration.setpoint_do_mg_l)
```

```
the model publishes SETPOINT_DO = 2.0
the control loop's own field     = 2.0
```

Both 2.0, and neither is reachable from a write. The dissolved-oxygen controller
computes `driving_force = c_star - self.setpoint_do_mg_l` at
`softplc/process/units.py:918`, reading **its own dataclass field**. Nothing reads
the OPC UA node back into the model — there is no code path from a client write to
a control decision.

So every client write is:

1. accepted, if the variable is writable
2. unchecked against the range, if it is out of range
3. **silently overwritten within one scan cycle**
4. and **never seen by any control loop**

A client can write 99 mg/L to the dissolved-oxygen setpoint, read it back, see
`Good`, and be entirely mistaken. The write is decorative. That is a stronger
statement than "`SECURITY.md` documents a gap", and it is the one that should be
in the security document.

The one thing that makes this defensible rather than merely broken is that it
fails **closed in the safe direction**: an out-of-range setpoint is ignored, not
applied. A control system that accepted 99 mg/L would be a different and much
worse bug. But it is accidental, not designed, and nothing states it.

## Where writes actually happen

One last piece, and it is the reason the write surface stayed this small. The
Node-RED control flow builds a **Modbus** write, not an OPC UA one:

```
$ git grep -c "modbus-write" -- 'scada/flows/03-control.json'
1
```

The project's only control path goes out over the protocol whose write semantics
are a register and a scale factor. OPC UA is read-only in practice, for the whole
plant, by every component.

## What is actually true

| claim | verdict |
|---|---|
| A client cannot write a measurement | **yes** — `BadUserAccessDenied`, enforced by the protocol |
| A client can write a setpoint | **yes** — 1 of 57 signals |
| The engineering range is enforced on write | **no** — 99.0 accepted against a 0.5–6.0 range |
| The range is checked somewhere in the server | **in a function with no caller** |
| A write changes the plant | **no over OPC UA** — overwritten next scan, never read by a control loop |
| **The same write changes the plant over Modbus** | **yes** — `ModbusTcpServer` has a write-back path and range-checks on the wire |
| Writes are audited | **over OPC UA, no** — no event, no `event` row, no log line |

Every verdict above is about OPC UA, and every one of them is still true. The
last-but-one row is the new information, and it is the reason this lesson is
worth re-reading: when the audit ran, Modbus writes were inert *for exactly the
same reason*. `pending_writes` existed in `modbus_server.py` with a docstring
describing a write path that was never implemented, and `_flush()` overwrote
the whole holding block every 20 ms, so an operator's setpoint arrived over the
wire, was acknowledged, and reverted. Both protocols were decorative, which made
"OPC UA's write model is correct and unused" a criticism of OPC UA's *position*
when it was really a criticism of the project's write paths in general.

Modbus has since been given a real write-back path (`_accept_write` →
`take_writes` → `SoftPlc._apply_pending_writes`). The contrast the audit asserted
but could not demonstrate is now demonstrable: **one contract, one writable
signal, reachable over Modbus and unreachable over OPC UA.** A client gets a
Modbus exception for a read-only register and an out-of-range setpoint, and
`BadUserAccessDenied` for the same measurement over OPC UA. The protocol that
is supposed to be safer about permissions is the one that cannot be used to
change the plant at all.

That inversion is worth sitting with. The lesson's original advice stands —
narrow write surfaces are good practice — but the reason OPC UA earns no credit
here is not that it is a worse protocol. It is that its write model was never
connected to anything, while the cruder protocol was connected to a loop.

The honest shape of the remaining gap is that OPC UA's write model is **correct
and unused**, described in a docstring that claims a range check which no code
path reaches.

Two of the three fixes are small and belong on the thread list:

- **Wire `OpcUaServer.write_value()` into a `DataChangeFilter`-free write handler**
  so the range is actually checked, and a range violation becomes a
  `BadOutOfRange` StatusCode rather than a dead branch. The specification has a
  status code for exactly this; the project simply never uses it.
- **Reconcile the two comments.** The docstring says the range check is "a
  courtesy for in-process callers" when there are none, and `SECURITY.md`
  describes a gap that is narrower than the truth — the real gap is not that the
  range is unchecked but that **no OPC UA write reaches anything at all.**

That second one matters most. A security document that understates a gap is
worse than one that has no gap, because it is the document somebody trusts when
deciding what to fix first. It also understates a gap that used to be twice as
wide: it described only the OPC UA path, and not the Modbus one.

**Next:** [06 — Why OPC UA is asyncio and Modbus is not](06-async.md)
