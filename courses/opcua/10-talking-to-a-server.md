# 10 — How a client actually talks to an OPC UA server

**Start here if you want to learn the protocol.** · [Back to the course](README.md)

This lesson is different from the other nine, and deliberately so.

Those lessons were about this repository: which decisions its server makes, and
what went wrong. This one is about **the protocol**. It works against a server, so
you can follow it without caring who wrote it or whether it is well designed. The
plant here is a convenient place to practise, not the subject.

By the end you will have written a client that connects, finds things by name,
reads values, and asks to be told when they change. Those four operations are
what an OPC UA client does, and they are enough to talk to any server on earth —
a simulator, a PLC, a building management system, a robot cell.

**If you read one section, read "Reading many values as one batch."** It is where
the difference between a client that works and a client that falls over lives.

## Before you start

The snippets here run against a server this repository starts for you, so there
is nothing to install and nothing to launch. If you would rather follow along
against a real one, any OPC UA server works; a browser that speaks the protocol
is enough to see the same tree this lesson walks.

Everything is `async`. OPC UA is a message-passing protocol, so a "call" is a
message that comes back later, and Python's `async`/`await` is how you write that
without threads. Every call that talks to the server is awaited; the few that
only read local state are not.

---

## 1. Connect

A connection does more than open a socket. It creates a **session**, and a
session has terms: the server will drop you if you go quiet for longer than the
timeout, and it expects a heartbeat in between.

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")

print("before connecting, we asked for a", client.session_timeout, "ms session")
await client.connect()
print("connected. do we have a session?", client.uaclient.has_session)
```

```
before connecting, we asked for a 3600000 ms session
connected. do we have a session? True
```

**The server is allowed to say no, and it did.** The line on stderr you did not
ask for is the server's answer:

```
Requested session timeout to 3600000ms, got 600000ms instead
```

It wanted one hour and offered ten minutes. The client accepts, because the
session timeout is the **server's** decision — it has to be, since the server is
the one that will drop you.

That number now matters to you, because it is how long the client may go without
saying anything before the session dies. Ten minutes of silence is fine for a
script that reads once and exits. It is *not* fine for a dashboard that only
prints on change, and that is the subject of section 4.

## 2. Find things by name

Here is the idea that separates OPC UA from a register map: **the server tells
you what it has.**

You did not receive a document saying "register 40104 is the influent flow". You
connect and ask. Every node has a name, and every node has children:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()

objects = client.nodes.objects
print("the root of every server's address space is", 
      (await objects.read_browse_name()).Name)
for child in await objects.get_children():
    print("  -", (await child.read_browse_name()).Name)
```

```
the root of every server's address space is Objects
  - Locations
  - Server
  - Aliases
  - PLANT-A: Northgate Water Reclamation Facility
```

Three of those are standard — every OPC UA server has `Objects`, `Server` and
`Aliases`, describing itself. The fourth is this plant, and you found it without
being told it existed.

That is the whole trick, and it is worth pausing on. With Modbus, the mapping from
register to meaning lives in a document you obtain separately, and the document
and the device can disagree. Here the *server* is the document. Anything that can
speak the protocol can enumerate the plant, including a program you have never
seen.

## 3. Walk down to what you want

Nodes have children, so you get to what you want by walking. Each step is a name,
and names are unique among siblings:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()

# find the plant, then walk: AERATION -> AHU-1 -> do_mg_l
node = client.nodes.objects
for name in ("PLANT-A: Northgate Water Reclamation Facility",
             "AERATION", "AHU-1", "do_mg_l"):
    for child in await node.get_children():
        if (await child.read_browse_name()).Name == name:
            node = child
            break
    else:
        raise LookupError(f"no child called {name!r}")

print("found:", (await node.read_browse_name()).Name)
print("value:", round(await node.read_value(), 2), "mg/L")
print("...and it is a live plant, so yours will differ.")
```

```
found: do_mg_l
value: 2.55 mg/L
...and it is a live plant, so yours will differ.
```

The number is dissolved oxygen in milligrams per litre, and it moves. That is
not a nuisance for this lesson, but it is worth noticing that you learned the unit
without being told it — see section 5.

The loop is longer than a one-liner because **namespace matters**. Node names
are unique among siblings, not in the whole tree — there could be an `AHU-1` under
two different areas. The `get_child("2:AHU-1")` shorthand handles this by
qualifying the name with its namespace index, and the `2:` in that call is a real
thing you must get right, not decoration. Get it wrong and you will browse a
different, standard part of the address space and find nothing, without an error
worth reading.

**A value on its own is a trap.** `read_value()` gives you a number and nothing
else — not when it was measured, not whether the instrument is working. Section 5
is about why that matters and what to do instead.

## 4. Ask to be told when things change

Polling works, and for a historian polling is often the right answer. But polling
means you re-ask for values that have not changed, and with fifty tags that is
fifty questions a second to be told nothing fifty times a second.

OPC UA's answer is a **subscription**: you say which nodes interest you, and the
server pushes changes to you.

Here is a demonstration worth running, because it makes the point better than any
prose. Watch one value that is **not changing**, and notice that the subscription
stays silent.

```python
import asyncio
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

# A server nobody is driving, so we control exactly what changes and when.
# Port 48402, because the lesson runner already holds 48400.
server = OpcUaServer(endpoint="opc.tcp://127.0.0.1:48402/wwtp/server/")
await server.start()
await server.wait_ready()

client = Client("opc.tcp://127.0.0.1:48402/wwtp/server/")
await client.connect()
node = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

class Watcher:
    def __init__(self):
        self.seen = []
    def datachange_notification(self, node, value, data):
        # Called by the client library when the server pushes a change.
        self.seen.append(value)

watcher = Watcher()
subscription = await client.create_subscription(500, watcher)   # 500 ms
await subscription.subscribe_data_change(node)

# Phase 1: leave it alone. Note what arrives anyway.
await asyncio.sleep(1.5)
print(f"after 1.5s of nothing changing: {len(watcher.seen)} notification(s)")
if watcher.seen:
    print(f"  which is: {watcher.seen} — the value on subscribe, not a change")

# Phase 2: now change it, twice.
server.set_value("AERATION:AHU-1:DO", 2.4, 0)
await server.publish()
await asyncio.sleep(0.6)
server.set_value("AERATION:AHU-1:DO", 3.1, 0)
await server.publish()
await asyncio.sleep(1.0)
print(f"after two changes:             {len(watcher.seen)} notifications")
print(f"values received: {watcher.seen}")

await subscription.delete()
await client.disconnect()
await server.stop()
```

```
after 1.5s of nothing changing: 1 notification(s)
  which is: [1.5] — the value on subscribe, not a change
after two changes:             3 notifications
values received: [1.5, 2.4, 3.1]
```

**One notification, and it is not a change.** When you subscribe, the server sends
the current value once so you start from a known state rather than from nothing.
That is helpful, and it is also the reason a naive count is misleading: "three
notifications" here means *one initial reading plus two actual changes*.

The part that matters is the silence that followed. For a second and a half
nothing happened at all, and that is the subscription earning its keep. Polling
would have asked three times in that window and reported "1.5" every time — three
identical numbers carrying no information, which is work the client did for
nothing.

So the rule is: **a notification is not a change, and a subscription is not a
polling loop with better manners.** If you find yourself treating every
notification as new information, you are reading the count and not the content.

The important thing in that snippet is `await asyncio.sleep(2)`. **A subscription
is not a blocking call.** You do not wait for a notification; you carry on with
your program and the library calls your handler when something arrives. If you
forget the sleep, the program finishes and the subscription is torn down before
the server has any chance to push.

The handler is called from the client's own background task, so there are two
rules worth learning now rather than debugging later:

- **Do not block in the handler.** Anything slow you do there delays the next
  notification, and a queue that cannot keep up drops the oldest changes
  (`overflow`). Hand the value to a queue and process it elsewhere.
- **The handler must not raise.** An exception in a callback is not going to be
  reported to whoever wrote the program that made the subscription.

## 5. Read many values as one batch

Read this section even if you skip the rest. It is the difference between a client
that works and one that falls over at 3am.

The obvious way to read fifty tags is fifty calls. The wrong way is fifty calls
that each carry their own error handling, because a degraded reading on tag 7
raises and the other forty-nine never arrive.

The fix is one request, one response, and no exceptions:

```python
from asyncua import Client, ua

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()

root = client.nodes.objects
node = root
for name in ("PLANT-A: Northgate Water Reclamation Facility",
             "AERATION", "AHU-1"):
    node = await node.get_child(f"2:{name}")
tags = {"DO": await node.get_child("2:do_mg_l"),
        "AIR_FLOW": await node.get_child("2:air_flow_m3h"),
        "BLOWER_RPM": await node.get_child("2:blower_rpm")}

# one request for all three
params = ua.ReadParameters()
for node_id in tags.values():
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node_id.nodeid, AttributeId=ua.AttributeIds.Value))
response = await client.uaclient.read(params)

for (name, _), result in zip(tags.items(), response):
    print(f"  {name:12} = {result.Value.Value:>8}  status {result.StatusCode.name}")
await client.disconnect()
```

```
  DO           = 2.551822635021689  status Good
  AIR_FLOW     = 4936.881503557899  status Good
  BLOWER_RPM   = 1045.6614734828472  status Good
```

The figures are unrounded because a plant is noisy and rounding here would be
decoration. What does not vary between runs: three tags, one request, and a
status on every single value.

Two things to notice, and the second is the important one.

**One round trip.** Three tags, one message. This is why subscriptions are not
merely an optimisation — the request cost is per message, not per tag, so batching
is what makes a wide read cheap.

**Every value arrives with a status, and you can see it.** `status Good` is not
decoration. OPC UA has a status code on every value, and the important ones are:

| status | meaning | what to do |
|---|---|---|
| `Good` | the value is trustworthy | use it |
| `Uncertain` | the value is a real reading but something is wrong with it — a fouled probe, a degraded calculation | use it, mark it |
| `Bad` | there is no usable value here | **do not use it** |

This is the single most valuable thing the protocol gives you over a register
map. A register holds a number and no idea whether the instrument behind it is
working. A bad reading and a good reading are the same 16 bits, and a historian
that cannot tell them apart stores confident nonsense for a month.

Here is what that looks like in practice, on this plant, where a fault drives the
quality:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

# a server we can push a degraded value into
server = OpcUaServer(endpoint="opc.tcp://127.0.0.1:48401/wwtp/server/")
await server.start()
await server.wait_ready()

client = Client("opc.tcp://127.0.0.1:48401/wwtp/server/")
await client.connect()
node = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

# publish 2.4 with quality 1 (Uncertain)
server.set_value("AERATION:AHU-1:DO", 2.4, 1)
await server.publish()

params = ua.ReadParameters()
params.NodesToRead.append(
    ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value))
result = (await client.uaclient.read(params))[0]
print(f"  DO = {result.Value.Value}  status {result.StatusCode.name}")
print("  the value is still there, and the status tells you to be careful with it")
await client.disconnect()
await server.stop()
```

```
  DO = 2.4  status Uncertain
  the value is still there, and the status tells you to be careful with it
```

**Do not throw the value away.** An `Uncertain` reading is often exactly what you
want — a fouled DO probe still tells you the basin is not being aerated, and a
historian that discards the number loses a week of evidence. Discarding it is the
mistake. Recording it *without recording that it was uncertain* is the worse
mistake, and it is the one that is easy to make by accident, because the number
looks fine.

**Now the trap in the obvious way of doing all this.** `read_value()` and
`read_data_value()` are the methods you will reach for first, and they throw away
the status. Worse, they raise:

```python
# what NOT to do — this is the natural first thing to write
try:
    value = await node.read_data_value()
except Exception:
    pass
```

That `except` is the bug. An `Uncertain` value is not an exception, it is a
value with a warning attached — and catching it and moving on throws away both
the number *and* the reason. Your historian now has a gap where there was a
reading, and the gap is not distinguishable from an outage. The batch read above
has no such branch, which is the entire reason to prefer it.

## 6. Disconnect

Sessions are not free and do not last forever. A well-behaved client closes what
it opened:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()
print("has a session:", client.uaclient.has_session)

await client.disconnect()
print("after disconnect, has a session:", client.uaclient.has_session)
```

```
has a session: True
after disconnect, has a session: False
```

Note the asymmetry in what you did: you created a **subscription** in section 4
and called `subscription.delete()`; the session goes away with `disconnect()`.
The reason to care is that on a long-running client, a reconnect without cleaning
up first is how you end up with a server holding subscriptions for a client that
no longer exists — each one keeping a session alive and consuming memory on the
other side.

The `has_session` flag is also the cheap way to tell whether your client is
actually talking to anything. Checking it in a health endpoint catches the case
where the socket is still open but the server has forgotten you, which looks
exactly like working from the outside.

---

## What to take away

Four operations, and they compose into anything:

1. **Connect** — creates a session with a timeout the server sets. Go quiet for
   longer than that and you are dropped; the client sends heartbeats in between.
2. **Browse** — ask the server what it has. Nothing is configured on your side,
   which is the whole point and also the whole risk.
3. **Read** — preferably in one batch, and always looking at the status. The status
   is the difference between a historian and a machine that stores confident
   nonsense.
4. **Subscribe** — be told about changes. It costs one handler that must not
   block or raise, and it is how a client stops asking questions it already knows
   the answer to.

Two habits are worth forming now, because both are invisible when they are wrong:

- **Never discard a value because its status was not `Good`.** Record the value
  *and* the status. Discarding is a decision; make it deliberately.
- **Never let a status code become an exception you catch and forget.** One batch
  read, no per-value error handling, no gaps that look like outages.

## Where to go next

`tools/opcua_minimal_client.py` in this repository is about two hundred lines and
does all six of these, with the decisions above written down next to the code. It
is worth reading as a worked example once you have followed this lesson, and it
is a reasonable starting point for a client of your own.

To learn more about the protocol itself, the specification is public and long, but
you do not need all of it to be useful. Two things to read when you need them:

- **Nodes and references.** A node is a thing; a reference is how it connects to
  another thing. "HasComponent" means you can go into it, "HasProperty" means it
  is a fact about something else. This is how a client draws a picture of a plant
  it has never seen.
- **Security.** This server accepts anonymous connections and sends everything in
  the clear, which is convenient and wrong for anything real. On a real
  deployment you want certificates and encryption, and that changes the connection
  code above — so it is worth understanding before you build on it, not after.
  `docs/SECURITY.md` in this repository is a worked example of writing that down
  honestly.
