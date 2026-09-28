# 07 — Writing values

**Next:** [08 — Finding things at scale](08-discovery.md) · [Back to the course](README.md) · [Previous: 06](06-status-and-quality.md)

Reading is the easy half. This lesson is about the other half: what happens when
you change something, and the four ways a write can go wrong.

The thing to hold onto for the whole lesson:

> **A write is a request to change a node. Nothing more. Whether that changes the
> process is a separate question, and the protocol does not answer it.**

**A note on the numbers.** This is a controlled server, so the values are stable.

## The question

> I wrote a setpoint. Did I change the plant?

## Who is allowed to write

Every variable has an access level, and it is a set of flags. Read it before you
try to write:

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
root = client.get_node(server.space.folder.nodeid)

for signal, label in (("AERATION:AHU-1:DO", "a measurement"),
                      ("AERATION:AHU-1:SETPOINT_DO", "the DO setpoint")):
    node = client.get_node(server.space.variables[signal].node.nodeid)
    level = await node.get_user_access_level()
    print(f"  {label:18} {sorted(flag.name for flag in level)}")
await client.disconnect()
await server.stop()
```

```
  a measurement      ['CurrentRead']
  the DO setpoint    ['CurrentRead', 'CurrentWrite']
```

A measurement is read-only. A setpoint is readable and writable. **This is a
protocol guarantee, not a convention** — try to write the measurement and the
server refuses at the protocol level, before your value is even looked at. That
is one of the strongest things in OPC UA, and lesson 06 showed the refusal is
specific: `BadUserAccessDenied`, not a generic failure.

## The four ways a write goes wrong

Now the interesting part. A write can be **refused**, and it can be **accepted**,
and those are not the same as **taking effect**. All four of these are real:

| | what happens | how you find out |
|---|---|---|
| 1 | **Refused** — the node is read-only | `BadUserAccessDenied` |
| 2 | **Accepted and kept** — you changed the node | read it back |
| 3 | **Accepted and overwritten** — the process overwrites you | you only notice later |
| 4 | **Accepted and ignored** — nothing reads that node | you never notice |

Number 4 is the dangerous one, and it is invisible by construction.

## 1 and 2: refused, and kept

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
root = client.get_node(server.space.folder.nodeid)
measurement = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)
setpoint = client.get_node(server.space.variables["AERATION:AHU-1:SETPOINT_DO"].node.nodeid)

try:
    await measurement.write_value(9.9)
    print("  a measurement accepted a write — that would be a protocol bug")
except Exception as exc:
    print(f"  writing a measurement: {type(exc).__name__}")

await setpoint.write_value(3.3)
print(f"  writing the setpoint: reads back {await setpoint.read_value()}")
await client.disconnect()
await server.stop()
```

```
  writing a measurement: UaStatusCodeError
  writing the setpoint: reads back 3.3
```

Both behave as you would hope. The measurement is refused with a specific code;
the setpoint is accepted and reads back. This is the part that is easy, and it is
where most people stop testing.

## 3: accepted, then silently overwritten

Here is the same successful write, and what the scan loop does to it:

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer
from softplc.process.plant import Plant
from softplc.contract import contract

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
setpoint = client.get_node(server.space.variables["AERATION:AHU-1:SETPOINT_DO"].node.nodeid)

# a plant running, publishing its setpoint every cycle
plant = Plant(contract())
for _ in range(30):
    plant.step(0.1)

await setpoint.write_value(3.3)
print(f"  client wrote 3.3, node holds {await setpoint.read_value()}")

# one more scan cycle: the model republishes its own value
plant.step(0.1)
model_value = plant.snapshot().values["AERATION:AHU-1:SETPOINT_DO"]
server.set_value("AERATION:AHU-1:SETPOINT_DO", model_value, 0)
await server.publish()
print(f"  after one scan cycle, node holds {await setpoint.read_value()}")
await client.disconnect()
await server.stop()
```

```
  client wrote 3.3, node holds 3.3
  after one scan cycle, node holds 2.0
```

**The write succeeded, read back correctly, and then vanished.** No error, no
status code, no event. The process is the source of truth and it republishes every
cycle, so anything you wrote to a node the process also drives is temporary by
construction.

The practical consequence: **a write to a value the process owns is not a
command, it is a suggestion that lasts one cycle.** If you want to change how a
plant behaves you have to change the thing the control logic actually reads — the
setpoint the loop uses, not the copy the loop publishes. This server does not
expose that as a writable node, which is honest: there is no OPC UA write here
that changes the process.

## The pitfall: a write that means nothing at all

The case above is at least visible, because something overwrites the value. Now
consider the write that is *not* overwritten and *also* does nothing — because
nothing reads the node.

This is the shape of a real industrial incident. A client writes a setpoint, gets
no error, the write sticks, and the process carries on doing exactly what it was
doing. From the client's side everything succeeded:

```python
from asyncua import Client
from softplc.servers.opcua import OpcUaServer
from softplc.process.plant import Plant
from softplc.contract import contract

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
setpoint = client.get_node(server.space.variables["AERATION:AHU-1:SETPOINT_DO"].node.nodeid)

await setpoint.write_value(3.3)
value = await setpoint.read_value()
print(f"  the client wrote 3.3 and read back {value}")
print("  every check a client can make has passed")

# the process, meanwhile
plant = Plant(contract())
for _ in range(50):
    plant.step(0.1)
print(f"  the control loop is using setpoint {plant.aeration.setpoint_do_mg_l}, not the node")
```

```
  the client wrote 3.3 and read back 3.3
  every check a client can make has passed
  the control loop is using setpoint 2.0, not the node
```

**Every check the client can make has passed, and the process is unchanged.** The
client wrote a node; the control loop reads a different field. The write was
accepted because nothing forbade it, and the gap between "I can write this" and
"this affects the process" is not something the protocol can close for you.

So how do you know? Not by writing and reading back — that always passes. You have
to check the *effect*:

- **subscribe to a signal that the change should move**, and watch it move
- **read the process's own setpoint**, if the server exposes it
- **know, before you write, what reads that node**

That last one is the real skill. "Can I write it?" is a question the server
answers. "Does anything act on it?" is a question only you can answer, and
getting it wrong is how a control room discovers that a setpoint change was
ignored at 3am.

## Writing safely

The three things to do before a write that matters, in order:

1. **Read the access level.** If `CurrentWrite` is absent, stop — the server will
   refuse anyway, and a specific error is better than a guess.
2. **Read the current value first.** You want to log what it was, so the change is
   reversible and the record is complete. This is one read and it is the step people
   skip.
3. **Check the effect, not the write.** Subscribe to something downstream before
   you write, so you can see whether it took.

And after the write, the part almost nobody does:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
setpoint = client.get_node(server.space.variables["AERATION:AHU-1:SETPOINT_DO"].node.nodeid)

previous = await setpoint.read_value()
print(f"  before: {previous}")
await setpoint.write_value(3.3)
after = await setpoint.read_value()
print(f"  after:  {after}")
print(f"  changed: {after != previous}   (logged, and reversible)")

# and the write is not silent about itself: the StatusCode says whether it stuck
params_read = ua.ReadParameters()
params_read.NodesToRead.append(
    ua.ReadValueId(NodeId=setpoint.nodeid, AttributeId=ua.AttributeIds.Value))
result = (await client.uaclient.read(params_read))[0]
print(f"  write status: {result.StatusCode.name}")
await client.disconnect()
await server.stop()
```

```
  before: 2.0
  after:  3.3
  changed: True   (logged, and reversible)
  write status: Good
```

`before`, `after`, and a status code. Three lines that turn an invisible action
into a recorded one.

## What to take away

- **Access level is a protocol guarantee.** Read-only is refused at the protocol
  level with a specific code. That part works.
- **A write to a value the process owns lasts one cycle.** The process republishes
  and your write is gone, silently. That is not a command, it is a suggestion.
- **A write can succeed, read back, and change nothing at all** — because nothing
  reads the node. No client-side check can detect this. Only watching the effect
  can.
- **Read before, read after, and record both.** It is one extra read and it is the
  difference between a reversible action and an untraceable one.

**If you skip this lesson**, you will write a setpoint, see it succeed, and have no
way to explain why the process did not change.

---

**A note on this server.** Its only writable nodes do not change the process —
there is no writable node wired to the control loop, and that is a deliberate
choice to have a small write surface rather than an oversight. That makes it a poor
place to *learn* writes and an excellent place to see why the distinction between
"accepted" and "took effect" matters. Measurements are in
[audit/05](../audit/05-writing.md).

**Next:** [08 — Finding things at scale](08-discovery.md)
