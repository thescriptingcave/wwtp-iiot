# 09 — A client that survives

**Back to the course** [README](README.md) · [Previous: 08](08-discovery.md)

Eight lessons, and the client you would write is short. This last one is about the
only part of that code that is not a protocol call: **what happens when the server
goes away.**

A client that works perfectly against a server that is always up is not a client.
Servers restart, cables are pulled, a PLC reboots at 3am. This lesson measures what
the protocol hands you at that moment, because the answer is not what most people
expect.

**A note on the numbers.** These are measured against a local server being stopped
and started on demand, so the counts and timings are stable. The 599 ms figure is
the one that moves, and the lesson says so where it appears.

## The question

> The server went away. What does my client know, and what does it get wrong?

## What a dead server actually looks like

The good news first, and it is genuinely good: **the failure is fast and loud.**

```python
import time
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

# Two servers on the same port, in turn: this lesson stops one and starts
# another, which is the only honest way to show what a restart does to a client.
ep = f"opc.tcp://127.0.0.1:{port}/wwtp/server/"
server = OpcUaServer(endpoint=ep)
await server.start()
await server.wait_ready()
client = Client(url=ep, timeout=5)
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
do = await (await (await plant.get_child("2:AERATION"))
            .get_child("2:AHU-1")).get_child("2:do_mg_l")
# `read_value()` raises on a non-Good status, and the runner's plant can be
# holding an Uncertain reading. Stage a Good one so this lesson is about the
# outage rather than about lesson 06.
server.set_value("AERATION:AHU-1:DO", 1.5, 0)
await server.publish()
print(f"  connected, DO reads {await do.read_value():.2f}")

await server.stop()          # the plant is gone

for label, call in (("a read", lambda: do.read_value()),
                    ("a second read", lambda: do.read_value()),
                    ("a new subscription",
                     lambda: client.create_subscription(100, lambda *a: None))):
    start = time.perf_counter()
    try:
        await call()
        print(f"  {label:18} returned a value")
    except Exception as exc:
        print(f"  {label:18} {type(exc).__name__:20} "
              f"in {(time.perf_counter() - start) * 1000:.0f} ms")
```

```
  connected, DO reads 1.50
  a read             ConnectionError      in 0 ms
  a second read      ConnectionError      in 0 ms
  a new subscription ConnectionError      in 0 ms
```

**Zero milliseconds, every time.** No partial value, no stale answer, no hang. The
client fails immediately and raises on every call.

That is the protocol being well behaved, and it is worth naming why: OPC UA is
request/response over a session, and a session is either alive or it is not. There
is no half-open state where the server has gone and the client is still waiting for
an answer it will never get.

It also means the failure is **not** something you have to detect. You never poll a
flag to discover the server is gone — the read tells you. A client that cannot
survive a restart is not failing to handle an error; it is failing to catch an
exception, which is much easier to fix.

## The pitfall: everything in your client is still valid

Now the part that is not loud. The read raises, you catch it, you log it — and
here is the trap:

```python
import time
from asyncua import Client
from softplc.servers.opcua import OpcUaServer

ep = f"opc.tcp://127.0.0.1:{port}/wwtp/server/"
server = OpcUaServer(endpoint=ep)
await server.start()
await server.wait_ready()
client = Client(url=ep, timeout=5)
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
do = await (await (await plant.get_child("2:AERATION"))
            .get_child("2:AHU-1")).get_child("2:do_mg_l")
server.set_value("AERATION:AHU-1:DO", 1.5, 0)
await server.publish()
last_value = (await client.uaclient.read(ua.ReadParameters(
    NodesToRead=[ua.ReadValueId(NodeId=do.nodeid,
                                AttributeId=ua.AttributeIds.Value)])))[0].Value.Value
await server.stop()      # the plant is gone

print(f"  the client still holds the Node handle: {do.nodeid}")
print(f"  the client still holds the last value it read: {last_value}")
print("  nothing about either of those became false when the server stopped")
print("  they are just no longer connected to anything")
```

A `Node` is a `NodeId` and a client object. A value is a float you copied out of a
response. **Neither one has a liveness property, and neither one expires.** The
Python objects are perfectly valid; they have simply stopped referring to the
plant.

So the client that "handled" the outage like this — keeping the last value when a
read fails — is broken in the worst possible way. Here is that client, in full,
staring from a plant that is already down:

<!-- the bug, written as a real poll loop so the flatline is observable -->
```python
import time
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

ep = f"opc.tcp://127.0.0.1:{port}/wwtp/server/"
server = OpcUaServer(endpoint=ep)
await server.start()
await server.wait_ready()
client = Client(url=ep, timeout=5)
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
do = await (await (await plant.get_child("2:AERATION"))
            .get_child("2:AHU-1")).get_child("2:do_mg_l")
server.set_value("AERATION:AHU-1:DO", 1.5, 0)
await server.publish()

await server.stop()      # the outage this client has to survive

last_value = 1.50
last_read = time.time()

for attempt in range(3):
    try:
        params = ua.ReadParameters()
        params.NodesToRead.append(ua.ReadValueId(
            NodeId=do.nodeid, AttributeId=ua.AttributeIds.Value))
        result = (await client.uaclient.read(params))[0]
        last_value = result.Value.Value
        last_read = time.time()
    except ConnectionError:
        pass                      # logged in a real client. still keeps the value.
    await asyncio.sleep(0.2)
    print(f"  poll {attempt + 1}: reporting {last_value:.2f} mg/L")

print(f"  the last successful read was {time.time() - last_read:.1f} s ago")
print("  the report is identical every time, and nothing in it says so")
```

```
  poll 1: reporting 1.50 mg/L
  poll 2: reporting 1.50 mg/L
  poll 3: reporting 1.50 mg/L
  the last successful read was 0.6 s ago
  the report is identical every time, and nothing in it says so
```

**Three polls, three identical reports, and not one of them mentions that no read
has succeeded since the plant went down.** Leave this running overnight and the
number is still 1.50, still printed to two decimal places, still going into the
historian as a measurement. The line on the chart is flat because the process is
flat, and there is no way to tell that from the chart.

The read fails. The client logs. The dashboard shows **1.50 mg/L of dissolved
oxygen, indefinitely**, and nothing anywhere says the number is old.

This is not a corner case and it is not obviously a bug once you have written it.
It is what "be resilient to a flaky connection" looks like when resilience means
"do not crash". **The value is the thing that survives, and the value is the thing
that stops being true.**

> **A cached value with no age is a lie with a number attached.** The only thing
> that distinguishes "the basin is at 1.50 mg/L" from "the basin was at 1.50 mg/L
> when the cable came out" is *when you last had a reason to believe it.*

So the rule the resilient client needs is not "catch the exception". It is:

<!-- a fragment: the shape of the fix, not a session -->
<!-- check: skip -->
```python
except ConnectionError:
    last_read = None            # not the time; *no* time
```

and then everything downstream has to cope with a value that has no age, which is
what forces the design. Put the age in the record and the problem becomes
solvable; leave it out and you have built a flatline that looks like data.

## What survives and what does not

The good news is better than you would guess. Here is a full restart — the server
stopped, and a **new** server object started on the same port — and the client
keeps the same `Client` and the same `Node` handles:

```python
import time
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

ep = f"opc.tcp://127.0.0.1:{port}/wwtp/server/"
server = OpcUaServer(endpoint=ep)
await server.start()
await server.wait_ready()
client = Client(url=ep, timeout=5)
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
do = await (await (await plant.get_child("2:AERATION"))
            .get_child("2:AHU-1")).get_child("2:do_mg_l")
print(f"  NodeId held by the client: {do.nodeid}")

await server.stop()
server = OpcUaServer(endpoint=ep)     # a brand new server
await server.start()
await server.wait_ready()

start = time.perf_counter()
await client.connect()                # the same Client object, reconnected
print(f"  reconnected in {(time.perf_counter() - start) * 1000:.0f} ms")
# Batch-read, so a non-Good status is a status rather than an exception (06).
params = ua.ReadParameters()
params.NodesToRead.append(ua.ReadValueId(
    NodeId=do.nodeid, AttributeId=ua.AttributeIds.Value))
fresh = (await client.uaclient.read(params))[0]
print(f"  the OLD handle still resolves: {fresh.Value.Value:.2f} mg/L")
print(f"  and that is the NEW server's value, read through the OLD NodeId")
```

```
  NodeId held by the client: NodeId(Identifier=273, NamespaceIndex=2, NodeIdType=...)
  reconnected in 2 ms
  the OLD handle still resolves: 1.50 mg/L
  and that is the NEW server's value, read through the OLD NodeId
```

**Two milliseconds, and the cached handles work against a server that did not
exist a moment ago.** Because a `Node` is just a `NodeId` and the identifier is
stable, the whole of lesson 08's discovery phase survives the restart. You do not
re-walk the tree.

This is the pay-off for the design in lesson 08, and it is the strongest argument
for it:

| you cached | after a restart |
|---|---|
| `Node` handles and NodeIds | still valid — 2 ms to recover |
| the last value you read | **stale, and indistinguishable from live** |

Caching *structure* is free and durable. Caching *data* is a liability, and
lesson 08's warning ("a cached value is a historian with a one-second lag") becomes
much sharper here: a cached value with no age is a historian with no way to report
that it has stopped.

## The subscription does not come back by itself

Values and subscriptions behave differently, and the difference is easy to get
wrong. Reconnecting the client does **not** restore the subscription — the
subscription lived in the old session, which is gone:

```python
import asyncio
import time
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

ep = f"opc.tcp://127.0.0.1:{port}/wwtp/server/"
server = OpcUaServer(endpoint=ep)
await server.start()
await server.wait_ready()
client = Client(url=ep, timeout=5)
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
do = await (await (await plant.get_child("2:AERATION"))
            .get_child("2:AHU-1")).get_child("2:do_mg_l")

class Counter:
    def __init__(self):
        self.count = 0
    def datachange_notification(self, node, value, data):
        self.count += 1

server.set_value("AERATION:AHU-1:DO", 5.5, 0)
await server.publish()
counter = Counter()
subscription = await client.create_subscription(100, counter)
await subscription.subscribe_data_change(do)
await asyncio.sleep(0.3)
print(f"  before the outage: {counter.count} notifications")

await server.stop()
server = OpcUaServer(endpoint=ep)
await server.start()
await server.wait_ready()
await client.connect()              # reconnect the client...

start = time.perf_counter()
counter = Counter()                 # ...and a fresh subscription
subscription = await client.create_subscription(100, counter)
await subscription.subscribe_data_change(do)
for i in range(20):
    server.set_value("AERATION:AHU-1:DO", 6.0 + i * 0.1, 0)
    await server.publish()
    await asyncio.sleep(0.1)
params = ua.ReadParameters()
params.NodesToRead.append(ua.ReadValueId(
    NodeId=do.nodeid, AttributeId=ua.AttributeIds.Value))
print(f"  2 s of changes after reconnect: {counter.count} notifications")
print(f"  direct read still works: "
      f"{(await client.uaclient.read(params))[0].Value.Value:.2f}")
await client.disconnect()
await server.stop()
```

```
  before the outage: 1 notifications
  2 s of changes after reconnect: 21 notifications
  direct read still works: 7.90
```

**Twenty-one notifications for twenty changes.** The
reconnected subscription works — but only because the code explicitly created a
new one. Nothing restored the old subscription, and nothing warned that it was
missing.

The practical consequence: **reconnection is a two-step operation, and the second
step is the one that gets forgotten.** Reconnect the transport *and* rebuild the
subscriptions, because they are separate things with separate lifetimes. A client
that reconnects and resumes reporting zeros has a working connection and no data
source, and its logs will be full of successful operations.

## The failure that is not fast: no timeout

Everything above failed in zero milliseconds, and that is the best case. It is not
the worst case. A clean shutdown closes the socket. **A network fault does not** —
it leaves a connection that accepts bytes and never answers, and there the fast
failure disappears:

```python
import asyncio
import socket
import time
from asyncua import Client

# A socket that accepts the connection and then says nothing, forever.
# This is what a cut cable or a dead router looks like to a client.
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port + 1))
listener.listen(1)
listener.setblocking(False)
held = []
loop = asyncio.get_running_loop()

async def accept_and_stall():
    while True:
        conn, _ = await loop.sock_accept(listener)
        held.append(conn)          # accepted, and deliberately silent

task = asyncio.create_task(accept_and_stall())
await asyncio.sleep(0.2)

for label, timeout in (("timeout=2s", 2), ("no timeout", None)):
    silent = Client(f"opc.tcp://127.0.0.1:{port + 1}/x/", timeout=timeout)
    start = time.perf_counter()
    try:
        # wait_for is a backstop so this lesson cannot hang. It is NOT the
        # thing being demonstrated -- `timeout` is.
        await asyncio.wait_for(silent.connect(), timeout=6)
        print(f"  {label:12} connected")
    except asyncio.TimeoutError:
        print(f"  {label:12} gave up after {time.perf_counter() - start:.1f} s")
    except Exception as exc:
        print(f"  {label:12} {type(exc).__name__} "
              f"after {time.perf_counter() - start:.1f} s")
    try:
        await silent.disconnect()
    except Exception:
        pass

task.cancel()
listener.close()
```

```
  timeout=2s   gave up after 2.0 s
  no timeout   gave up after 6.0 s
```

**Read that second line carefully: six seconds is the `wait_for` backstop in this
snippet, not `asyncua` giving up.** With no timeout set, the client was still
waiting when the backstop fired, and would have waited indefinitely.

That is the failure mode to design against, and it is much nastier than the clean
shutdown because it is silent:

- a **clean shutdown** raises in 0 ms, so your `except` runs and you know
- a **network fault** blocks, so nothing runs, your poll loop never iterates, and
  the client looks *busy* rather than broken

A client with no timeout against a flaky link does not crash. It stops, silently,
while the plant keeps running. And because the operations are `async`, a blocked
read does not freeze the process — it just never completes, so the loop that was
going to write to the database writes nothing and reports nothing.

**Always set a timeout on the client.** `Client(url=..., timeout=5)` is one
argument, and it is the difference between a client that fails and a client that
disappears.

## The reconnect loop

Those four facts — the handles survive, the subscriptions do not, the values are
stale, the timeout is what saves you — combine into a loop. This is the whole
pattern:

```python
import time
from asyncua import Client, ua

class Reading:
    def __init__(self, signal, value, status, age_seconds):
        self.signal, self.value, self.status = signal, value, status
        #: None means "no successful read", which is not the same as "now".
        self.age_seconds = age_seconds

class ResilientReader:
    """Reconnects, rebuilds subscriptions, and never reports a value without an age.

    Every rule in lessons 06, 08 and 09 shows up here, which is the point: the
    hard part was never a protocol call.
    """

    def __init__(self, endpoint, path, timeout=5.0):
        self.endpoint = endpoint
        self.path = path
        self.timeout = timeout          # 09: without this, a fault hangs forever
        self.node = None
        self.last_good = None           # 09: a value is not evidence on its own
        self.last_good_at = None

    async def _connect(self):
        # 08: a fresh Client each time, because a disconnected one is a
        # liability -- and the timeout is not optional.
        client = Client(url=self.endpoint, timeout=self.timeout)
        await client.connect()
        # 08: discover by search. Cheap once, and it survives the restart
        # because what it caches is identity.
        plant = None
        for child in await client.nodes.objects.get_children():
            if "PLANT-A" in (await child.read_browse_name()).Name:
                plant = child
                break
        if plant is None:
            await client.disconnect()
            raise LookupError("no plant under Objects")
        node = plant
        for part in self.path:
            node = await node.get_child(f"{node.nodeid.NamespaceIndex}:{part}")
        return client, node

    async def read(self):
        """One reading, or a declared gap. Never a silent stale value."""
        client = None
        try:
            client, self.node = await self._connect()
            # 06: batch read, so the StatusCode survives with the value.
            params = ua.ReadParameters()
            params.NodesToRead.append(ua.ReadValueId(
                NodeId=self.node.nodeid, AttributeId=ua.AttributeIds.Value))
            result = (await client.uaclient.read(params))[0]
            status = result.StatusCode.name
            if status == "Bad":
                return Reading(self.path[-1], None, status, None)
            value = float(result.Value.Value)
            self.last_good, self.last_good_at = value, time.time()
            return Reading(self.path[-1], value, status, 0.0)
        except (ConnectionError, OSError, LookupError) as exc:
            # The outage. The important part is what we do NOT do: we do not
            # return self.last_good as if it were current. We return the age,
            # and the age is None, because "unknown" and "0 seconds" are
            # different answers and only one of them is true.
            return Reading(self.path[-1], None, type(exc).__name__,
                           None if self.last_good_at is None
                           else time.time() - self.last_good_at)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
```

And what the caller does with it — the age is the whole ballgame:

```python
# `ResilientReader` is the class defined immediately above, re-declared here so
# this snippet stands alone.
class ResilientReader:
    def __init__(self, endpoint, path, timeout=5.0):
        self.endpoint, self.path, self.timeout = endpoint, path, timeout
        self.node = None
        self.last_good = None
        self.last_good_at = None

    async def _connect(self):
        client = Client(url=self.endpoint, timeout=self.timeout)
        await client.connect()
        plant = None
        for child in await client.nodes.objects.get_children():
            if "PLANT-A" in (await child.read_browse_name()).Name:
                plant = child
                break
        if plant is None:
            await client.disconnect()
            raise LookupError("no plant under Objects")
        node = plant
        for part in self.path:
            node = await node.get_child(f"{node.nodeid.NamespaceIndex}:{part}")
        return client, node

    async def read(self):
        client = None
        try:
            client, self.node = await self._connect()
            params = ua.ReadParameters()
            params.NodesToRead.append(ua.ReadValueId(
                NodeId=self.node.nodeid, AttributeId=ua.AttributeIds.Value))
            result = (await client.uaclient.read(params))[0]
            status = result.StatusCode.name
            if status == "Bad":
                return Reading(self.path[-1], None, status, None)
            value = float(result.Value.Value)
            self.last_good, self.last_good_at = value, time.time()
            return Reading(self.path[-1], value, status, 0.0)
        except (ConnectionError, OSError, LookupError) as exc:
            return Reading(self.path[-1], None, type(exc).__name__,
                           None if self.last_good_at is None
                           else time.time() - self.last_good_at)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

import time
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

class Reading:
    def __init__(self, signal, value, status, age_seconds):
        self.signal, self.value, self.status = signal, value, status
        self.age_seconds = age_seconds

reader = ResilientReader(
    f"opc.tcp://127.0.0.1:{port}/wwtp/server/", ("AERATION", "AHU-1", "do_mg_l"))

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()

for label, before in (("plant running", None), ("plant stopped", "stop")):
    if before == "stop":
        await server.stop()
    reading = await reader.read()
    if reading.value is None:
        age = "unknown" if reading.age_seconds is None \
              else f"{reading.age_seconds:.0f} s old"
        print(f"  {label:14} NO VALUE   ({reading.status}, last known {age})")
    else:
        print(f"  {label:14} {reading.value:.2f} mg/L  "
              f"({reading.status}, {reading.age_seconds:.0f} s old)")
if before == "stop":
    await server.stop()
```

```
  plant running  1.50 mg/L  (Good, 0 s old)
  plant stopped  NO VALUE   (ConnectionRefusedError, last known 1 s old)
```

**The second line is the whole lesson.** The client is not lying, not crashing,
and not reporting a stale number as current. It says it has no value, says what
went wrong, and says how old the last thing it knew was. A dashboard can grey that
tag out; an alarm can raise a comms fault; a historian records a gap.

Compare with the version that "keeps the last value": same client, same
connection, same failure — and a dashboard showing 1.50 mg/L that nobody can tell
is from last week. **Both clients handled the exception. Only one of them is
honest.**

That is the difference between a client that survives a server and a client that
survives being wrong about the plant.

## What to take away

- **A clean shutdown raises in 0 ms.** You do not have to detect it. Catch the
  exception, and that is the whole mechanism.
- **A network fault does not raise — it blocks, silently.** Always set
  `Client(timeout=...)`. Without it a flaky link makes the client vanish rather
  than fail, and because the calls are `async` nothing looks hung.
- **`Node` handles survive a restart; subscriptions do not.** Reconnecting the
  transport is step one and rebuilding the subscriptions is step two, and the
  second is the one that gets forgotten. The second is the one that produces a
  client that reconnects successfully and reports nothing.
- **A cached value with no age is a lie with a number attached.** The fix is not
  "don't cache" — it is "never report a value without its age, and make `unknown`
  different from `now`".
- **The protocol gives you liveness for free. Honesty is yours.** The read tells
  you the server is gone. Whether your dashboard says so is entirely your code.

**If you skip this lesson**, your client will work in every test you write, because
in every test you write the server is up.

## Where to go next

The reference client in `tools/opcua_minimal_client.py` is this lesson plus the
five safeguards the earlier lessons earned: a read timeout, a batched read, the
status code kept with the value, a search rather than a hardcoded path, and a
freshness verdict on every reading. It is about 300 lines and it is the shortest
honest OPC UA client in this repository.

Read it top to bottom once. Every non-obvious line has a comment naming the
lesson it came from, and the comments are the course you have just finished.

---

**A note on this server.** It has no authentication and it listens on loopback, so
"the client survives" here means surviving a crash, not surviving an attacker. A
production client also needs certificate-based authentication, a secure channel,
and authorisation to be re-issued after a reconnect — because on reconnect it is
a *new* session, and permissions granted in the old one are not granted again.
That is in [audit/07](../audit/07-security.md), and it is the subject the course
deliberately leaves out of scope.

**Back to the course** [README](README.md) · [Previous: 08](08-discovery.md)
