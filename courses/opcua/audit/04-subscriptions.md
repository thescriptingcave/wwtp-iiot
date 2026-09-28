# 04 — Subscriptions, and the feature this project does not use

**Next:** [05 — Writing](05-writing.md) · [Back to the course](../README.md) · [Previous: 03](03-reading-and-quality.md)

The module docstring lists what OPC UA buys over Modbus, and the second item is
the one people quote:

> **Subscriptions.** A client subscribes once and receives changes. Modbus makes
> the client poll, which means deadbanding, change detection and data volume all
> become the client's problem.

So: does this project use them?

```bash
git grep -c create_subscription -- '*.py'
```

```
tools/opcua_browser.py:1
```

**One occurrence, in the browser tool.** Not in the gateway, not in the server,
not in the soft PLC. The one client in this project that subscribes is the one
built to let a human look around.

**And the client this project actually wrote reads like this:**

```python
# gateway/clients/opcua_client.py:184,199
async def poll(self) -> OpcUaPollResult:
    ...
    response = await self._client.uaclient.read(params)
```

A batch read. Every cycle. `gateway/main.py:82` sets `poll_interval_s: float =
1.0`, so it reads all 57 nodes once a second, whether or not anything changed.

**The project chose OPC UA, built the address space, and then polls it exactly
as Modbus would require.** The strongest argument for the protocol is the one
argument the protocol is not being used for here.

## The question

> What does a subscription actually give you — and what does it quietly take away?

## What one looks like

A subscription is two objects: a `Subscription` (how often the server may send
you something) and one or more monitored items (which nodes you care about, and
what counts as a change). You do not get a callback per write; you get a
notification per *publishing interval*, carrying whatever changed inside it.

```python
from asyncua import ua

class Watcher:
    def __init__(self):
        self.seen = []
    def datachange_notification(self, node, value, data):
        self.seen.append(value)

node_id = space.variables["AERATION:AHU-1:DO"].node.nodeid
node = client.get_node(node_id)

watcher = Watcher()
sub = await client.create_subscription(500, watcher)   # 500 ms publishing
await sub.subscribe_data_change(node)                 # no filter

for i in range(20):
    server.set_value("AERATION:AHU-1:DO", 2.0 + i * 0.01, 0)
    await server.publish()
    await asyncio.sleep(0.05)
await asyncio.sleep(1.0)

print(f"20 publishes -> {len(watcher.seen)} notifications, "
      f"{len(set(watcher.seen))} distinct values")
await sub.delete()
```

```
20 publishes -> 21 notifications, 21 distinct values
```

One initial value, then one per change. That works, and it is exactly what the
docstring promises. It is also nearly pointless, and the next section is why.

## A subscription without a filter is barely better than polling

The redundancy is easiest to see with a value that oscillates. Twenty publishes,
two distinct values alternating:

```python
from asyncua import ua

class Watcher:
    def __init__(self):
        self.seen = []
    def datachange_notification(self, node, value, data):
        self.seen.append(value)

node = client.get_node(space.variables["AERATION:AHU-1:DO"].node.nodeid)
watcher = Watcher()
sub = await client.create_subscription(500, watcher)
await sub.subscribe_data_change(node)

for i in range(40):
    server.set_value("AERATION:AHU-1:DO", 2.0 + (i % 2) * 0.001, 0)
    await server.publish()
    await asyncio.sleep(0.05)
await asyncio.sleep(1.0)

print(f"40 publishes, 3 distinct values -> "
      f"{len(watcher.seen)} notifications carrying "
      f"{len(set(watcher.seen))} distinct values")
await sub.delete()
```

```
40 publishes, 3 distinct values -> 41 notifications carrying 3 distinct values
```

**Forty-one notifications, three facts.** Thirty-eight of them told the client
nothing it had not been told thirty milliseconds earlier.

This is important, because it is the argument the docstring makes *against*
Modbus — *"data volume becomes the client's problem"* — and a filterless
subscription reproduces it exactly. The client asked to be told about changes and
was told about all of them, including the ones that were not changes. **A
subscription is not a cheaper poll. It is a poll with a different delivery
mechanism, and the filter is what makes it cheaper.**

## The filter, and the contract already has the number

`DataChangeFilter` is what turns that 41 into something useful. The contract
carries a deadband for all 57 signals — dissolved oxygen is 0.02 mg/L — and the
filter takes exactly that number:

```python
from asyncua import ua

class Watcher:
    def __init__(self):
        self.seen = []
    def datachange_notification(self, node, value, data):
        self.seen.append(value)

node = client.get_node(space.variables["AERATION:AHU-1:DO"].node.nodeid)
watcher = Watcher()
sub = await client.create_subscription(500, watcher)
await sub.deadband_monitor(node, 0.02, ua.DeadbandType.Absolute)

for i in range(40):
    server.set_value("AERATION:AHU-1:DO", 2.0 + i * 0.01, 0)
    await server.publish()
    await asyncio.sleep(0.05)
await asyncio.sleep(1.0)

print(f"40 publishes, ramping 0.01 each -> {len(watcher.seen)} notifications")
print(f"  distinct values {len(set(watcher.seen))}, "
      f"last {watcher.seen[-1] if watcher.seen else 'none'}")
print(f"  the value on the server right now: "
      f"{await node.read_value()}")
await sub.delete()
```

```
40 publishes, ramping 0.01 each -> 2 notifications
  distinct values 2, last 2.0
  the value on the server right now: 2.39
```

Two notifications instead of forty-one. That is the feature.

**And read the last three lines again.** The client's most recent value of
dissolved oxygen is **2.0 mg/L**. The server's is **2.39**. The client is
**16 % low** — 0.39 mg/L of dissolved oxygen it does not know it is missing —
and nothing in the notification says the value is stale. The address space holds
one value; the client holds another; there is no version, no sequence number, and
no "as of" that would let a historian notice the divergence.

## Two clients, one server, one instant

That divergence is not an edge case. It is what a per-subscription filter
*means*, and the cleanest way to see it is two clients at once:

```python
from asyncua import ua

class Watcher:
    def __init__(self, name):
        self.name = name
        self.seen = []
    def datachange_notification(self, node, value, data):
        self.seen.append(value)

node_id = space.variables["AERATION:AHU-1:DO"].node.nodeid
node = client.get_node(node_id)          # the client the gate gave you

a, b = Watcher("A"), Watcher("B")
sub_a = await client.create_subscription(500, a)
await sub_a.subscribe_data_change(node)                    # no filter
sub_b = await client.create_subscription(500, b)
await sub_b.deadband_monitor(node, 0.02, ua.DeadbandType.Absolute)

for i in range(40):
    server.set_value("AERATION:AHU-1:DO", 2.0 + i * 0.01, 0)
    await server.publish()
    await asyncio.sleep(0.05)
await asyncio.sleep(1.0)

for w in (a, b):
    print(f"  client {w.name}: {len(w.seen):3} notifications, "
          f"last value {w.seen[-1] if w.seen else '-'}")
print(f"  the server says: {await node.read_value()}")
await sub_a.delete(); await sub_b.delete()
```

```
  client A:  41 notifications, last value 2.39
  client B:   2 notifications, last value 2.0
  the server says: 2.39
```

Same server. Same node. Same instant. Client A is correct, client B is 16 % low,
and **both are receiving `Good` StatusCodes.** There is no field in either
notification that says "your picture of this value is behind".
This is the cost side of the feature, and it is the part the docstring's one
paragraph does not mention. Three consequences worth naming:

- **A filter is negotiated per subscription.** It is not a property of the node,
  so two clients can hold genuinely different beliefs about the same value at the
  same moment, with nothing to reconcile them.
- **Suppressed data is gone.** A deadband does not queue, summarise or timestamp
  what it drops. A historian built on a deadbanded subscription has *no record*
  that DO went from 2.0 to 2.39 in nineteen steps — it has two rows, 2.0, and a
  gap. `sql/02-intermediate/02-04_gaps.md` is the long version of why that
  matters.
- **The right place for a deadband is the source, not the subscription.** This
  project's `gateway/deadband.py` is an *application-level* filter, applied by
  the client on the way in. That is a defensible design and it has one advantage
  over the OPC UA filter: there is exactly one of it, so every client gets the
  same data. The `Deadband` property in the address space (lesson 01) documents
  the number so a client *can* filter — and no client does, because the server
  publishes every changed value regardless.

## The three false claims this exposed

Writing the lesson meant reading the code paths, and three comments do not
describe them.

**The class docstring describes machinery that does not exist.**

> Rather than creating a subscription per value — which is what most examples
> do … the server keeps one internal subscription with a data-change filter and
> pushes updates through a single writer callback.

There is no internal subscription. `OpcUaServer` holds
`self._dirty: set[str]` and `publish()` walks it. No `DataChangeFilter`, no
`MonitoredItem`, nothing. The *design* is sound — batching server-side is
correct, and the comment's advice about per-value subscriptions is right — but
it is implemented as a set of dirty strings, not as the thing it names.

**`mark_dirty` misplaces the deadband.**

> Called from the scan loop, which knows what changed because the deadband said
> so. Pushing only real changes is what makes an OPC UA subscription cheaper
> than Modbus polling — and the reason the contract carries a deadband for every
> signal in the first place.

Three claims, and:

- `mark_dirty` is not called by the scan loop. `set_value()` calls it.
- The scan loop has no deadband. `softplc/main.py:236-238` filters on **exact
  equality** — `entry.value == values[signal_id] and entry.quality == quality`.
  Every distinct float is published, however far below the deadband it falls.
- The only deadband is `gateway/deadband.py`, on the **client** side, downstream
  of the wire. So the server publishes more than it needs to, the client filters,
  and the sentence's "cheaper than Modbus polling" describes a saving that has
  not been taken.

**The comparison is never made.** `docs/ARCHITECTURE.md` has a table for the
ports and a paragraph for the asyncio difference, and the subscriptions claim is
the one bullet in the docstring that the project has no experience of. It is
also, by a wide margin, the best argument for the protocol.

## What is actually true

| claim | verdict |
|---|---|
| A client can subscribe instead of polling | **yes** — 20 lines, demonstrated above |
| The project's gateway does | **no** — `poll()` at 1.0 s, batch read, no subscription |
| A subscription is cheaper than polling | **only with a filter** — 41 notifications vs 2 |
| The filter is a property of the data | **no** — it is negotiated per subscription |
| A filtered client's values are the server's values | **no** — and nothing marks the difference |
| The server filters before publishing | **no** — exact equality, not the contract's deadband |

The honest summary is uncomfortable and worth sitting with: **this project
picked OPC UA for a reason it then declined to use, and wrote the polling,
deadbanding and change-detection machinery that the reason was supposed to
eliminate.** `gateway/deadband.py` is 241 lines of careful, well-tested code
doing by hand — on the wrong side of the wire — exactly the job an OPC UA
`DataChangeFilter` does natively, and doing it in the one place where every
client benefits from a single consistent decision.

The change is not small, and it is not free: a subscription-based gateway has to
cope with reconnects, with notifications arriving in bursts, and with a filter
whose divergence from the server's values has to be recorded rather than ignored.
That is a piece of work, not a patch, and it belongs on the open-thread list with
the rest.

What is cheap and worth doing regardless: **fix the two comments.** Three
sentences describe machinery that is not there, and a docstring is the one piece
of documentation every future reader is guaranteed to believe.

**Next:** [05 — Writing](05-writing.md)
