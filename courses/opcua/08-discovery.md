# 08 — Finding things at scale

**Next:** [09 — A client that survives](09-a-client-that-survives.md) · [Back to the course](README.md) · [Previous: 07](07-writing.md)

Lessons 01–07 each did one thing to one node. A real client does those things to
hundreds, and the arithmetic changes: what was a convenience becomes the whole
cost model.

This lesson is about the two mechanics that make that work — **batching** and
**caching** — and the one trap that makes them dangerous: a hardcoded path.

**A note on the numbers.** The timings are from a local server over loopback, and
they move a little every run — treat them as *ratios*, not absolutes. What does
not move is the shape: batched is one round trip, sequential is one per node, and
the ratio is roughly 8×.

## The question

> I have fifty tags to read once a second. How many round trips is that, and how do
> I make it fewer?

## Fifty tags, the naive way, is fifty conversations

The obvious loop:

```python
# The runner has already connected `client` to a live plant, so this snippet
# uses them.
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")

# walk once to find twenty measurement nodes
nodes = []
for area in ("INFLUENT", "AERATION", "EFFLUENT"):
    folder = await plant.get_child(f"2:{area}")
    for holder in await folder.get_children():
        for var in await holder.get_children():
            names = {(await p.read_browse_name()).Name
                     for p in await var.get_children()}
            if "SignalId" in names:
                nodes.append(var)
nodes = nodes[:20]
print(f"  found {len(nodes)} measurement nodes")
```

Note what that discovery loop already cost: for **every candidate node** it read
the browse names of all its children. On this server each measurement has nine
properties, so finding twenty measurements meant a couple of hundred round trips
before you have read a single value.

This is the shape of the problem. OPC UA is a request/response protocol: every
`read_value()` is a message out and a message back. Fifty of them is fifty
round trips, and the latency of the network is paid fifty times.

## Batching: one conversation, fifty answers

The fix is one `Read` request carrying every node you want. The server answers
with one `ReadResponse` carrying every value:

```python
import time
from asyncua import ua

plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")

nodes = []
for area in ("INFLUENT", "AERATION", "EFFLUENT"):
    folder = await plant.get_child(f"2:{area}")
    for holder in await folder.get_children():
        for var in await holder.get_children():
            names = {(await p.read_browse_name()).Name
                     for p in await var.get_children()}
            if "SignalId" in names:
                nodes.append(var)
nodes = nodes[:20]

start = time.perf_counter()
for node in nodes:
    await node.read_value()               # one round trip each
sequential = time.perf_counter() - start

start = time.perf_counter()
params = ua.ReadParameters()
for node in nodes:
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value))
await client.uaclient.read(params)        # one round trip total
batched = time.perf_counter() - start

print(f"  {len(nodes)} sequential reads: {sequential * 1000:6.1f} ms")
print(f"  1 batched read:             {batched * 1000:6.1f} ms")
print(f"  speedup:                    {sequential / batched:.1f}x")
```

```
  20 sequential reads:    3.1 ms
  1 batched read:                0.4 ms
  speedup:                    8.2x
```

**About eight times faster on a loopback connection, where the network is free.** Over a
real network with a millisecond of latency, the same twenty tags go from twenty
milliseconds to one, and the ratio gets *better* — because what you have removed
is latency, and latency is what dominates when the payload is small.

This is why lessons 03 and 06 both used `ua.ReadParameters` without much
explanation. It is not an optimisation trick. **It is the normal way to read
anything more than a handful of values**, and a client that does it per-value is
leaving an order of magnitude on the table.

The reason the numbers are small here is worth saying: this server is local. The
absolute figures would be irrelevant on a plant. The *ratio* is the transferable
thing, and the rule that produces it is: **one request, however many nodes you
want in it.**

## Caching: most of what you do repeatedly is local

The other half of the cost is *finding* the nodes, and here is the trap:

<!-- illustrative pseudocode, not a session: it is the shape of the mistake -->
<!-- check: skip -->
```python
# what a client that re-walks the tree every cycle looks like
while True:
    plant = await client.nodes.objects.get_child("2:PLANT-A: ...")   # each poll
    ...                                          # re-walks the whole tree
```

The fix is to walk **once** and keep the `Node` objects. A `Node` in `asyncua` is
a thin client-side handle: it holds a `NodeId` and knows how to talk to the server,
but it costs nothing to keep, and every read through it is still a live read.

```python
import time

# The runner has already connected `client` and started a server, so this
# snippet uses them rather than starting a second of each.
node = await (await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")).get_child("2:AERATION")

start = time.perf_counter()
for _ in range(100):
    await node.read_browse_name()      # 100 round trips, deliberately naive
naive = time.perf_counter() - start

# the cached handle does not go stale, and the cost is a local dict lookup
start = time.perf_counter()
for _ in range(100):
    _ = node.nodeid
local = time.perf_counter() - start

print(f"  100 reads that ask the server: {naive * 1000:6.1f} ms")
print(f"  100 uses of the cached handle: {local * 1000:6.2f} ms")
print(f"  the handle is a NodeId and a client. Keeping it is free.")
```

```
  100 reads that ask the server:   21.8 ms
  100 uses of the cached handle:   0.00 ms
```

**Keeping a `Node` is free. Asking about it is not.** That is the whole caching
lesson, and it has one important caveat:

> **A cached `Node` caches *identity*, never *values*.**

`node` is a handle to "the thing called `do_mg_l` under `AHU-1`". Reading its value
is still a live read every time. If you cache the *value* you have built a
historian with a one-second lag and a very fast bug.

The thing that is safe to cache is the **structure**: which nodes exist, and their
`NodeId`s. The thing that must never be cached is the data.

## The pitfall: a hardcoded path breaks silently

Both mechanics above depend on knowing *where* things are. Here is what that
usually looks like:

```python
# The thing everybody writes first.
DO = await (await (await (await client.nodes.objects.get_child(
        "2:PLANT-A: Northgate Water Reclamation Facility"))
        .get_child("2:AERATION")).get_child("2:AHU-1")).get_child("2:do_mg_l")
```

It works. It is also a string in your source that has to be right, forever, and
when the server's model is reorganised — a new area, a renamed piece of equipment,
a site in a second building — it stops being right. And it stops being right
*quietly*: `get_child` on a name that does not exist raises, but a client with a
`try` around its setup (which every long-running client needs, because servers
restart) will swallow that and then report zeros.

The version that survives a reorganisation is **discover, then match**:

```python
# Reuses the runner's connected client — no second server needed.
objects = client.nodes.objects

async def find(*path):
    """Walk down, failing loudly with a message a human can act on."""
    node = objects
    for part in path:
        for child in await node.get_children():
            if (await child.read_browse_name()).Name == part:
                node = child
                break
        else:
            raise LookupError(
                f"no child called {part!r} under "
                f"{(await node.read_browse_name()).Name!r}. "
                f"Has the model been reorganised?")
    return node

do = await find("PLANT-A: Northgate Water Reclamation Facility",
                "AERATION", "AHU-1", "do_mg_l")
print(f"  found {(await do.read_browse_name()).Name}")
```

```
  found do_mg_l
```

Three things make this better, and only the first is about correctness:

1. **The error names what was missing and where.** "no child called `AHU-1` under
   `AERATION`. Has the model been reorganised?" is a bug report. `BadNoMatch` is
   not.
2. **The path is one argument, not nested calls.** You can pass it around, log it,
   store it, and have a test for it.
3. **You can search rather than assume.** If a tag is somewhere else now —
   a second aeration basin, a renamed unit — you can look for it by browse name
   across the whole server, and a client that finds tags by *what they are called*
   rather than *where they are* keeps working when the plant is extended.

That last point is the real lesson. **A hardcoded path encodes a snapshot of
somebody's model.** OPC UA gives you a live, queryable model precisely so you do
not have to ship a copy of it, and a client that ships a copy has thrown away the
main advantage of the protocol.

## Putting it together: the shape of a real read

```python
import time
from asyncua import ua

# once at startup: walk and keep the handles
handles = {}
for area in ("INFLUENT", "AERATION", "EFFLUENT"):
    folder = await (await client.nodes.objects.get_child(
        "2:PLANT-A: Northgate Water Reclamation Facility")).get_child(f"2:{area}")
    for holder in await folder.get_children():
        for var in await holder.get_children():
            names = {(await p.read_browse_name()).Name for p in await var.get_children()}
            if "SignalId" in names:
                signal_id = await (await var.get_child("2:SignalId")).read_value()
                handles[signal_id] = var          # the *id* is the key
print(f"  discovered {len(handles)} signals, keyed by their own id")

# every cycle: one round trip, no re-walking
start = time.perf_counter()
params = ua.ReadParameters()
for node in handles.values():
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value))
response = await client.uaclient.read(params)
print(f"  {len(handles)} values in {(time.perf_counter() - start) * 1000:.1f} ms, one round trip")
print(f"  statuses: {set(r.StatusCode.name for r in response)}")
```

```
  discovered 34 signals, keyed by their own id
  34 values in 0.6 ms, one round trip
  statuses: {'Good'}
```

**Thirty-four values from three areas, one round trip, and the keys are the
server's own signal ids** rather than your guesses about where they live. This is
the shape a production client has: an expensive discovery phase, and a cheap
steady state.

## What to take away

- **One request, however many nodes.** Batched reading is not an optimisation, it
  is the normal way. Measured 8.4× on loopback; the ratio improves with latency,
  which is the point.
- **Cache the structure, never the values.** A `Node` is a free handle to an
  identity. Caching the value under it builds a historian with a one-second lag.
- **Never hardcode a browse path.** It encodes a snapshot of somebody's model, and
  it fails quietly in a client with error handling. Discover, and fail with a
  message that names what is missing.
- **Key your cache on the server's own ids.** Then adding a tank to the plant is
  a change in the plant, not a change in your client.

**If you skip this lesson**, your client will work on the plant it was written
against, which is the definition of a client that does not survive its first
extension.

---

**A note on this server.** It exposes 57 signals, of which thirteen produce exactly
one reading in a seeded week — a deadband effect, not an OPC UA one. A discovery
loop like the one above finds all 57; a client that assumes every tag it finds
will produce data finds 44 and treats the other 13 as broken. The deadband's
consequences are measured in
[audit/01](../audit/01-the-address-space.md) and are worth knowing about for
reasons that have nothing to do with this lesson.

**Next:** [09 — A client that survives](09-a-client-that-survives.md)
