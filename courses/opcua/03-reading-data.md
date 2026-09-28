# 03 — Reading data properly

**Next:** [04 — Data types, units and ranges](04-units-and-ranges.md) · [Back to the course](README.md) · [Previous: 02](02-nodes-and-types.md)

[Lesson 02](02-nodes-and-types.md) said a `Variable` holds a value. This lesson is
about the three things that surround that value, and all three are things you
need in order not to be quietly wrong:

1. **A node has several attributes, and only one of them is the value.**
2. **A value arrives with a timestamp, a status, and a quality — or it arrives
   incomplete.**
3. **The obvious read throws away the parts you needed.**

## The question

> I called `read_value()` and got a number. What did I not get?

**A note on the numbers below.** The plant this runs against is live, so the exact
float in every `Value` is different each time you run it. The snippets round for
that reason, and where a figure is elided or timestamped it says so. What does
*not* change between runs: the status, the presence or absence of a timestamp, the
data type, the access level, and everything in the prose. Those are the parts
worth learning.

## A variable is more than its value

A `Variable` node has about two dozen attributes. Most are set by the server and
never change. A few matter to a client:

```python
from asyncua import Client, ua

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
do = await (await (await plant.get_child("2:AERATION"))
           .get_child("2:AHU-1")).get_child("2:do_mg_l")

for attribute in (ua.AttributeIds.Value,
                  ua.AttributeIds.DataType,
                  ua.AttributeIds.AccessLevel,
                  ua.AttributeIds.MinimumSamplingInterval,
                  ua.AttributeIds.Historizing,
                  ua.AttributeIds.BrowseName):
    value = await do.read_attribute(attribute)
    print(f"  {attribute.name:26} = {str(value.Value)[:46]}")
await client.disconnect()
```

```
  Value                      = DataValue(Value=Variant(Value=2.4762...
  DataType                   = DataValue(Value=Variant(Value=NodeId(Identifier=11
  AccessLevel                = DataValue(Value=Variant(Value=1, VariantType=<Vari
  MinimumSamplingInterval    = DataValue(Value=Variant(Value=0.0, VariantType=<Va
  Historizing                = DataValue(Value=Variant(Value=False, VariantType=<
  BrowseName                 = DataValue(Value=Variant(Value=QualifiedName(Namesp
```

(elided at 46 characters by the print, which is why some lines end mid-token)

Four of these are worth reading twice:

- **`Value`** — the number. That is all `read_value()` gives you.
- **`DataType`** — a `NodeId` pointing at a type definition. This is the
  *server's* opinion about what the number is. Lesson 04 is about this.
- **`AccessLevel`** — a bit field. `1` is `CurrentRead`, and if bit 2
  (`CurrentWrite`) is set, this variable is writable. Lesson 07 is about this.
- **`MinimumSamplingInterval`** — the fastest the server will sample this. `0.0`
  means "no limit, sample as fast as you ask", which for a historian is a
  problem rather than a feature.

## The pitfall, and it is a quiet one

Every attribute read above goes through `read_attribute(id)`. It is very easy to
reach for the wrong function, and the wrong function does not fail.

```python
from asyncua import Client, ua

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()
do = await (await (await (await client.nodes.objects.get_child(
        "2:PLANT-A: Northgate Water Reclamation Facility"))
        .get_child("2:AERATION")).get_child("2:AHU-1")).get_child("2:do_mg_l")

# WRONG. The first positional argument of read_data_value is
# raise_on_bad_status, NOT an attribute index.
for attribute in (ua.AttributeIds.Value, ua.AttributeIds.DataType,
                  ua.AttributeIds.AccessLevel):
    data = await do.read_data_value(attribute)
    # rounded, because this is a live plant and the exact float changes every run
    print(f"  asked for {attribute.name:14} got {round(data.Value.Value, 4)}")
await client.disconnect()
```

```
  asked for Value          got 2.4762
  asked for DataType       got 2.4762
  asked for AccessLevel    got 2.4762
```

**It returns the value every time.** You asked for a type and got a float; you
asked whether the node is writable and got the number that happens to be there
now. No exception, no warning, and a `True` where you wanted a bit field.

Nothing catches this, because `raise_on_bad_status=True` is a perfectly valid
argument and the call succeeds. It is the same shape as the pH-read-from-the-TSS
signal in this project's dashboard history: a number that is not the number you
asked for, produced by code that is perfectly correct. **When a library call takes
a flag and a value, check which is which in the signature before you rely on it.**

## What else comes with a value

`read_value()` gives you the number. The thing you usually want is the record
around it:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()
do = await (await (await (await client.nodes.objects.get_child(
        "2:PLANT-A: Northgate Water Reclamation Facility"))
        .get_child("2:AERATION")).get_child("2:AHU-1")).get_child("2:do_mg_l")

data = await do.read_data_value()
print(f"  value       {round(data.Value.Value, 4)}")
print(f"  status      {data.StatusCode.name}")
print(f"  source time  {data.SourceTimestamp}")
print(f"  server time  {data.ServerTimestamp}")
await client.disconnect()
```

```
  value       2.4757
  status      Good
  source time  None
  server time  2026-09-28 06:28:38.351648+00:00
```

**Look at `source time: None`.** That is not a bug in your code and not a
narrow escape — it is a value nobody has stamped. The plant is running and
publishing, so how is there no source timestamp?

Because the distinction is real: this server writes a value into the address space
without telling the client *when the process measured it*. The server knows
(microseconds ago, it just wrote it), and `ServerTimestamp` records that. The
`SourceTimestamp` is the field reserved for the *device's* own clock, and there
are no devices here — it is a simulation, so there is nothing to carry one.

**So the rule has to be conditional, and the condition is this:** if the plant is
real, `SourceTimestamp` is when the sensor measured and `ServerTimestamp` is when
the gateway got round to it, and the gap between them is your polling latency. If
the plant is a simulator, or the sensor has no clock, `SourceTimestamp` may be
absent entirely, and `ServerTimestamp` is the only time you have.

Never write `record["ts"] = data.SourceTimestamp` without deciding what happens
when it is `None`. Half the interesting cases in industrial data involve a source
that cannot tell you anything.

- `SourceTimestamp` is when the *device or process* says the value was measured.
- `ServerTimestamp` is when the *server* put it in the address space.

When a Modbus gateway polls a sensor, those are far apart: the source timestamp
is 500 ms old and the server timestamp is now, because that is when the gateway
got round to it. If you are storing history and you record `ServerTimestamp`
everywhere, you have recorded *when you looked* rather than *when it happened*,
and a historian full of those cannot be corrected for a clock problem
afterwards. **Record the source timestamp**, or record both and be honest that you
have two.

`StatusCode` is lesson 06. The short version: it is never absent, and `Good` is
not the only value.

## Reading one at a time, and why you should not

Reading fifty tags with fifty calls is the obvious thing and it is the wrong one.
Not because it is slow — because of what happens when one of them is degraded.

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

# a server we can push a degraded value into
server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
do = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

server.set_value("AERATION:AHU-1:DO", 2.4, 1)      # quality 1 = Uncertain
await server.publish()

# The natural first thing to write.
try:
    print("  read_value gave:", await do.read_value())
except Exception as exc:
    print(f"  raised {type(exc).__name__}: {exc}")
await client.disconnect()
await server.stop()
```

```
  raised UaStatusCodeError: The operation was uncertain.(Uncertain)
```

**A degraded value raised an exception and the number is gone.** The value was
right there. The exception carried no number and no way to recover it, so the
only two outcomes are "the tag is missing from this cycle" or "you wrote a
`try`/`except` and now you have a gap you cannot explain".

## One request, all of them, no exceptions

The fix is one read with every node in it, and no per-value error handling at
all:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()

tags = {name: client.get_node(server.space.variables[sid].node.nodeid)
        for name, sid in [("DO", "AERATION:AHU-1:DO"),
                          ("AIR_FLOW", "AERATION:AHU-1:AIR_FLOW"),
                          ("BLOWER_RPM", "AERATION:AHU-1:BLOWER_RPM")]}
server.set_value("AERATION:AHU-1:DO", 2.4, 1)      # DO is Uncertain
await server.publish()

params = ua.ReadParameters()
for node in tags.values():
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value))
response = await client.uaclient.read(params)

for (name, _), result in zip(tags.items(), response):
    flag = "" if result.StatusCode.name == "Good" else "   <-- look at this"
    print(f"  {name:11} = {result.Value.Value:>10}  {result.StatusCode.name}{flag}")
await client.disconnect()
await server.stop()
```

```
  DO          =       2.4  Uncertain   <-- look at this
  AIR_FLOW    = 2000.0  Good
  BLOWER_RPM  =  600.0  Good
```

Three tags, one round trip, no exceptions, and the degraded one is *visibly*
degraded with its value still attached. That is the shape you want: nothing
raised, nothing lost, and the problem is a column in your data rather than an
exception in your logs.

The same method reads any attribute, not just `Value` — put a different
`AttributeId` in the `ReadValueId` and you get timestamps, access levels, or the
data type for every node in one request.

## What to take away

- **A variable has attributes, and `read_value()` returns exactly one of them.**
  `read_attribute(id)` is the function that takes an attribute.
- **`read_data_value()`'s first argument is a flag, not an index.** Passing an
  attribute id there silently gives you the value again. Check the signature.
- **Record the source timestamp, not the server timestamp.** They answer
  different questions and only one of them is about the process.
- **Read in batches.** Not for speed — for failure. A batch read cannot lose a
  value to an exception, and a degraded value arrives as data rather than as a
  crash.

**If you skip this lesson**, lesson 06 will seem paranoid and lesson 05's
subscriptions will seem to lose values for no reason.

---

**A note on this server.** `MinimumSamplingInterval` is `0.0` on every node, which
tells a client there is no limit on how fast to sample — a server that will answer
as fast as you ask. It is convenient and it is also how a well-behaved client ends
up asking a hundred times a second for a value that changes once a second. The
`SamplingInterval_ms` property on each node has the real number. Neither is a
protocol requirement; both are worth knowing exist.

**Next:** [04 — Data types, units and ranges](04-units-and-ranges.md)
