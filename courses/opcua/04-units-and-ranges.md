# 04 — Data types, units and ranges

**Next:** [05 — Subscriptions in depth](05-subscriptions-in-depth.md) · [Back to the course](README.md) · [Previous: 03](03-reading-data.md)

You have a number. This lesson is about the three questions that number cannot
answer for you, and the fact that OPC UA has a mechanism for all three that most
clients never use.

- **What type is it?** (`2.4` — a pH, a mg/L, a metre, a boolean?)
- **What unit is it in?**
- **Is it in range, and who checks?**

**A note on the numbers.** The plant is live, so exact floats change per run and
snippets round. The types, units, ids and the accepted out-of-range write do not
change, and those are the parts worth learning.

## The question

> I have `2.4` from a client. What can I safely do with it?

## The honest answer is "almost nothing"

Here is a value, and the type the server publishes for it:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
root = client.get_node(server.space.folder.nodeid)
do = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)
storm = client.get_node(server.space.variables["SITE:WEATHER:STORM"].node.nodeid)

params = ua.ReadParameters()
for node in (do, storm):
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.DataType))
datatypes = [r.Value.Value for r in await client.uaclient.read(params)]

for node, label, dtype in zip((do, storm), ("dissolved oxygen", "storm flag"), datatypes):
    name = (await client.get_node(dtype).read_browse_name()).Name
    print(f"  {label:18} DataType -> NodeId {dtype.Identifier} = {name}")
await client.disconnect()
await server.stop()
```

```
  dissolved oxygen    DataType -> NodeId    11 = Double
  storm flag          DataType -> NodeId    11 = Double
```

**Both are `Double`.** One is a concentration in milligrams per litre; the other
is a boolean — a storm is happening or it is not. The server's type system says
they are the same thing, and it is right in the narrow sense that both arrive as
IEEE-754 doubles on the wire.

This is the single most important thing to internalise about OPC UA types: **the
type tells you the *representation*, not the *meaning*.** A `Double` is a
64-bit float. It is not a temperature, it is not a flag, and a client that treats
it as one of those is guessing.

## So where *is* the meaning?

It is in **properties** attached to the node, and OPC UA has a standard place to
look. On a well-built server you find them on an `AnalogItemType`, but they are
the same properties wherever they live:

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
root = client.get_node(server.space.folder.nodeid)
do = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

for name in ("UnitSymbol", "EngineeringUnits",
             "EngineeringRangeLow", "EngineeringRangeHigh"):
    prop = await do.get_child(f"2:{name}")
    print(f"  {name:22} = {await prop.read_value()!r}")
await client.disconnect()
await server.stop()
```

```
  UnitSymbol            = 'mg/L'
  EngineeringUnits      = 6152
  EngineeringRangeLow   = 0.0
  EngineeringRangeHigh  = 20.0
```

Four numbers, and together they are the difference between a number and a
measurement:

| property | what it is | why you need it |
|---|---|---|
| `UnitSymbol` | `'mg/L'` — a human-readable string | for display, and it is what a user sees |
| `EngineeringUnits` | `6152` — a numeric id from a standard catalogue | for *arithmetic*: converting to another unit, or rejecting an implausible one |
| `EngineeringRangeLow` | `0.0` | the smallest value the instrument can produce |
| `EngineeringRangeHigh` | `20.0` | the largest |

**You need both unit properties, and they do different jobs.** `UnitSymbol` is
for people and is ambiguous across locales and across standards — `mbar` and `psi`
are both "pressure" in English. `EngineeringUnits` is a number from a published
catalogue, so code can switch on it, convert it, and refuse it. If your client
reads only the string, it cannot do anything automated with units, and a
configuration change from `m³/h` to `L/s` is an undetectable unit change that
scales your chart by a thousand.

The range is what lets a client say *this reading cannot be right*. A dissolved
oxygen probe that reports 500 mg/L is a broken probe, and the range is how you
know without knowing anything about dissolved oxygen.

## The pitfall: the range is advisory, and the type is worse

Here is the check every client should make, written out:

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
root = client.get_node(server.space.folder.nodeid)
sp = client.get_node(server.space.variables["AERATION:AHU-1:SETPOINT_DO"].node.nodeid)

# the setpoint's declared range
lo = await (await sp.get_child("2:EngineeringRangeLow")).read_value()
hi = await (await sp.get_child("2:EngineeringRangeHigh")).read_value()
print(f"  the contract says this setpoint lives in [{lo}, {hi}] mg/L")

for attempt in (2.4, 99.0):
    await sp.write_value(attempt)
    got = await sp.read_value()
    inside = lo <= got <= hi
    print(f"  wrote {attempt:5}  read back {got:5}  "
          f"{'inside the range' if inside else 'OUTSIDE THE RANGE'}")
await client.disconnect()
await server.stop()
```

```
  the contract says this setpoint lives in [0.5, 6.0] mg/L
  wrote   2.4  read back   2.4  inside the range
  wrote  99.0  read back  99.0  OUTSIDE THE RANGE
```

**The server accepted 99.0 mg/L of dissolved oxygen setpoint** — sixteen times
the maximum it just told you about — and reported no error. This is not a bug in
the server and it is not a bug in the protocol.

It is the specification. OPC UA says the engineering range is **advisory**: it is
information for clients and for display, and a server is *permitted* to accept a
value outside it. The reasoning is sound and worth understanding — a range is a
statement about the instrument, and the server cannot always distinguish "the
instrument is misbehaving" from "somebody is commissioning it and the value is
supposed to be odd" — but the consequence is firm:

> **If you do not check the range, no client or server anywhere in the chain will
> check it for you.**

That is the whole lesson. The server told you the range, in a standard place, and
then ignored it. The check is four lines long and it is your job.

## Checking the range properly

Here is the version worth shipping. It is defensive in a specific, useful way:
it does not assume the properties exist.

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
root = client.get_node(server.space.folder.nodeid)

async def plausible(node, value):
    """Is this value inside the range the node publishes, if it publishes one?"""
    try:
        low = await (await node.get_child("2:EngineeringRangeLow")).read_value()
        high = await (await node.get_child("2:EngineeringRangeHigh")).read_value()
    except Exception:
        return True, "no range published — cannot check"
    if low <= value <= high:
        return True, f"inside [{low}, {high}]"
    return False, f"outside [{low}, {high}]"

for signal, name in (("AERATION:AHU-1:DO", "dissolved oxygen"),
                     ("SITE:WEATHER:STORM", "storm flag")):
    node = client.get_node(server.space.variables[signal].node.nodeid)
    ok, why = await plausible(node, await node.read_value())
    print(f"  {name:18} {ok!s:5} {why}")
await client.disconnect()
await server.stop()
```

```
  dissolved oxygen    True   inside [0.0, 20.0]
  storm flag          True   inside [0.0, 1.0]
```

Note the `except` branch. A node might publish no range at all — a lot of real
servers do not — and **a missing range is not a failed check, it is an absent
check.** Return `True` and carry on, or return `False` and refuse to store data
from a server that cannot tell you anything. Both are defensible; silently
crashing is not, and neither is treating "no range" as "range is 0 to infinity"
without saying so.

## What a *good* server does, and why this one does not

OPC UA's answer to "the type tells you the representation, not the meaning" is a
richer set of standard types. An `AnalogItemType` variable carries `EngineeringUnits`
as a properly-typed `EUInformation` structure and a standard `EURange` property, and
a client that knows the specification can then interpret any such node without
reading a single property by name.

This server uses `BaseDataVariableType` — the bare minimum — and publishes the
unit information as properties with names the specification does not use. So:

- **You can still read everything.** Every value, unit and range is there, and the
  snippets above read all of it.
- **A generic client cannot interpret it.** A discovery tool looks for
  `AnalogItemType` instances and their `EURange`, finds zero, and concludes this
  server has 57 untyped numbers.

The measurements are in [audit/02](../audit/02-units-and-types.md), and the
practical lesson for *you* is: when you point a generic tool at a server and it
shows you nothing useful, check whether the server is publishing types the
specification defines before assuming the tool is broken.

## What to take away

- **A `Double` is 64 bits of float.** It is not a unit, not a flag, and not a
  measurement. The type is representation, not meaning.
- **Read `UnitSymbol` for people and `EngineeringUnits` for code.** One is a
  string you cannot automate against; the other is a catalogue id you can switch
  on and convert.
- **The range is the only thing that tells you a reading is impossible**, and the
  server will not enforce it. Nothing in the chain will. Check it yourself.
- **A missing range is an absent check, not a passing one.** Decide what your
  client does about it deliberately.

**If you skip this lesson**, lesson 07 will look like the server is being
careless when it accepts a bad write, and it will be — but that is the
specification, and this lesson is why.

---

**A note on this server, which is not the lesson.** It publishes unit information
in non-standard property names, and the one unit thing it does get right — the
numeric id — is correct for `mg/L`. The measurements of what it does and does not
comply with are in [audit/02](../audit/02-units-and-types.md), and the teaching
above is complete without them.

**Next:** [05 — Subscriptions in depth](05-subscriptions-in-depth.md)
