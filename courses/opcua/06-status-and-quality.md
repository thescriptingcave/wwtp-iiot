# 06 — Status codes and quality

**Next:** [07 — Writing values](07-writing.md) · [Back to the course](README.md) · [Previous: 05](05-subscriptions-in-depth.md)

Every value in an OPC UA server carries a `StatusCode`, and this lesson is about
what to do with it. It is the most important lesson in the course and the shortest,
because the reasoning behind it is not something the protocol can give you.

The whole idea in one sentence:

> **A number without a status is a claim you cannot check. A number with a status
> is evidence.**

**A note on the numbers.** These come from a controlled server, so the counts and
codes are stable.

## The question

> The reading says `2.4`. How much do I trust it, and what do I do about it?

## Three codes, and the middle one is the interesting one

```python
from asyncua import ua
for name in ("Good", "Uncertain", "Bad"):
    code = getattr(ua.StatusCodes, name)
    print(f"  {name:10} = {int(code):>12}")
```

```
  Good       =            0
  Uncertain  =   1073741824
  Bad        =   2147483648
```

Three states, and the temptation is to treat it as a two-way split — good data
and bad data. It is not, and `Uncertain` is where the engineering is:

| status | means | what a client should do |
|---|---|---|
| `Good` | the value is trustworthy | use it |
| `Uncertain` | a real value, and something is wrong with how it was obtained | **use it, and record the status** |
| `Bad` | no usable value | do not use it; the gap is real |

`Uncertain` is not a softer `Bad`. It means *the number is probably right and the
provenance is not*. A fouled dissolved oxygen probe still tells you the basin is
not being aerated. A calibration interval that has expired still tells you the
reading is roughly right. **Discarding these values destroys real information**,
and that is the mistake this lesson exists to prevent.

## The pitfall: throwing away the value because of the status

Here is what an `Uncertain` reading looks like, and the natural thing to write
with it:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
node = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

# a fouled probe: the value is real, the provenance is not
server.set_value("AERATION:AHU-1:DO", 0.4, 1)      # quality 1 = Uncertain
await server.publish()

# The natural first thing to write.
try:
    value = await node.read_value()
    print(f"  read {value}")
except Exception as exc:
    print(f"  dropped it: {type(exc).__name__}")
await client.disconnect()
await server.stop()
```

```
  dropped it: UaStatusCodeError
```

**The number is gone.** `0.4 mg/L` of dissolved oxygen — a real reading, flagged
as untrustworthy — and the exception carries neither the value nor a way to get it.
Your tag now has a hole in it, and the hole is indistinguishable from a sensor
that failed to answer.

The value was right there. You threw it away because of a flag attached to it, and
you have converted "a reading you did not fully trust" into "no reading at all",
which is strictly worse information.

## Keeping both

One batch read, and the value and its status stored together:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
root = client.get_node(server.space.folder.nodeid)

tags = {name: client.get_node(server.space.variables[sid].node.nodeid)
        for name, sid in [("DO", "AERATION:AHU-1:DO"),
                          ("AIR_FLOW", "AERATION:AHU-1:AIR_FLOW"),
                          ("BLOWER_RPM", "AERATION:AHU-1:BLOWER_RPM")]}

# DO is fouled; the other two are fine
server.set_value("AERATION:AHU-1:DO", 0.4, 1)
await server.publish()

params = ua.ReadParameters()
for n in tags.values():
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=n.nodeid, AttributeId=ua.AttributeIds.Value))
response = await client.uaclient.read(params)

rows = []
for (name, _), result in zip(tags.items(), response):
    status = result.StatusCode.name
    # Bad means no usable value. Everything else is a reading plus a caveat.
    if status == "Bad":
        rows.append((name, None, status))
    else:
        rows.append((name, result.Value.Value, status))

for name, value, status in rows:
    print(f"  {name:11} {str(round(value, 2) if value is not None else '-'):>8}"
          f"  {status:10} stored")
await client.disconnect()
await server.stop()
```

```
  DO               0.4  Uncertain  stored
  AIR_FLOW      2000.0  Good       stored
  BLOWER_RPM     600.0  Good       stored
```

Three rows, nothing raised, and the fouled probe is **stored with its value and
its status**. Downstream you can then decide, with the whole history in front of
you:

- an operator dashboard greys out or marks anything `Uncertain`
- an alarm treats `Uncertain` as "look at this" rather than "ignore it"
- an analytic that needs clean data can filter on status *after the fact*, from
  data you kept

None of that is possible if you threw the value away at the first exception.

## The rule, and it is two lines

<!-- a fragment, not a session — the shape of the whole lesson in two lines -->
<!-- check: skip -->
```python
if status == "Bad":
    record_gap()          # no usable value; the gap is real
else:
    record(value, status) # a reading, and how much to trust it
```

`Bad` is the only status that means "do not use this". Everything else is data
with a caveat attached, and the caveat is more useful than the value if you throw
the value away.

## What a status code is *not*

Two things worth knowing before you rely on them.

**A status code is not a fault code.** `Good` does not mean the process is healthy
— it means *this reading* is sound. A plant can be discharging ammonia at eleven
times its permit with every value reporting `Good`, because every sensor is
working perfectly and the process is wrong. Status codes are about the
*measurement*, and alarming on process condition is a separate layer that reads
the measurements. Conflating them is how a database full of `Good` ends up full
of compliance failures.

**A status code is not a range check.** Lesson 04 wrote 99 mg/L to a setpoint
whose range was 0.5–6.0 and the server accepted it — with what status? A server
may well return `Good` for a value it knows is out of range, because the range is
advisory and the value is not corrupt. If you want ranges enforced, you check
them, as that lesson showed.

## One thing to know about this server

Ask it for a `Bad` value and you will not get one. Publishing maps every
non-zero quality to `Uncertain`:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
node = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

for quality in (0, 1, 2):
    server.set_value("AERATION:AHU-1:DO", 2.4, quality)
    await server.publish()
    params = ua.ReadParameters()
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value))
    got = (await client.uaclient.read(params))[0]
    print(f"  the plant said quality {quality} -> on the wire: {got.StatusCode.name}")
await client.disconnect()
await server.stop()
```

```
  the plant said quality 0 -> on the wire: Good
  the plant said quality 1 -> on the wire: Uncertain
  the plant said quality 2 -> on the wire: Uncertain
```

**Quality 2 became `Uncertain`.** A client written to handle only `Good` and
`Bad` would pass its tests against this server forever, because `Bad` never
arrives. That is a genuine hazard of testing against a friendly server, and it is
worth knowing when you later meet one that does produce `Bad`.

You can see `Bad` on this server in a different way — by writing to something you
may not write:

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
measurement = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)
try:
    await measurement.write_value(9.9)
except Exception as exc:
    print(f"  writing a measurement: {type(exc).__name__}: {exc}")
await client.disconnect()
await server.stop()
```

```
  writing a measurement: UaStatusCodeError: User does not have permission to
  perform the requested operation.(BadUserAccessDenied)
```

A specific status code, naming exactly what went wrong. That is the shape you
want from an error: a machine-readable cause, not a boolean. Lesson 07 is about
writes; this is what a refusal looks like.

## What to take away

- **`Uncertain` is not a softer `Bad`.** It is a real reading with a provenance
  problem, and it is usually the most useful thing on the screen.
- **Never discard a value because of its status.** `Bad` is the only status that
  means "no value"; everything else is a reading plus a caveat.
- **Read the status in a batch read, never through a per-value call.** That is
  what stops the status becoming an exception.
- **A status code is not a fault code and not a range check.** `Good` means the
  reading is sound, not that the process is fine.
- **Test against a server that produces all three states.** One that never sends
  `Bad` will let a two-state client pass forever.

**If you skip this lesson**, the natural next step is `try: value = read()` with
an `except: pass`, and you will spend a month wondering why your historian has gaps
exactly when the plant is misbehaving.

---

**A note on this server, which is not the lesson.** It cannot produce `Bad`, as
the table above shows. Its measurements also arrive carrying the constructor's
initial value until something publishes, so a client that trusts a value without
checking the age is reading a number nobody measured. Both are properties of this
server, not of the protocol, and both are measured in
[audit/03](../audit/03-reading-and-quality.md).

**Next:** [07 — Writing values](07-writing.md)
