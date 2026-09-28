# 09 — Build a client

**Next:** none — this is the last lesson · [Back to the course](README.md) · [Previous: 08](08-generated.md)

Eight lessons of things the server does wrong, and things the protocol does not
do for you. This one is the other half: a client, in the repository, that does
the right thing about all of it.

    python -m tools.opcua_minimal_client

It is [`tools/opcua_minimal_client.py`](../../tools/opcua_minimal_client.py) —
about 200 lines including the comments that explain *why* each safeguard is here,
and five of them are load-bearing.

## Why another client, when there is already a browser

`tools/opcua_browser.py` is 432 lines and does browse, read and subscribe. So
this is not a replacement, and it is not faster or better in general: it is a
*human* tool with a UI-shaped job.

It is also, on the evidence of this course, **less correct**:

| | browser | this client |
|---|---|---|
| discovers by browse name | yes | yes |
| reads preserving `StatusCode` | **no** — `read_data_value()`, which raises on `Uncertain` (lesson 03) | yes, one batch `Read` |
| distinguishes never-measured from measured | **no** | yes, three verdicts |
| keeps `Good` and `Uncertain` distinct | no — it prints a number | yes |
| filters a subscription knowingly | no | yes, and says why it doesn't |

So the browser **crashes on the first degraded sensor**, which is lesson 03's
finding, and it prints a plausible `1.5` for a plant that has never been
measured, which is the same lesson's other half. Both are defects in a tool whose
job is to be trusted by a person looking at a plant.

## The client

Three operations, and the shape of all of them follows from the lessons.

### 1. Discover, never hardcode a NodeId

<!-- a method extract from tools/opcua_minimal_client.py, not a session -->
<!-- check: skip -->
```python
async def _find_plant(self) -> Any:
    """Find the plant by browse name.

    Deliberately a search rather than a lookup: lesson 01's whole point is
    that a client should be able to find things it was never told about.
    """
    for child in await self._client.nodes.objects.get_children():
        if PLANT_MARKER in (await child.read_browse_name()).Name:
            return child
    return None
```

Connect, then walk down from whatever was found. And the namespace index is
read off the node it discovered rather than assumed:

<!-- a method extract from tools/opcua_minimal_client.py, not a session -->
<!-- check: skip -->
```python
node = self.plant
for part in path:
    node = await node.get_child(f"{node.nodeid.NamespaceIndex}:{part}")
```

Hardcoding `2:` works on this server and would break the first time the address
space were rebuilt — with no error, just a client that resolves nothing and
reports an empty plant. **My first version of this file did exactly that**, and
got it wrong by reaching for `protocol.ns_pool`, which does not exist.

### 2. Batch-read, so the StatusCode survives

<!-- a method extract from tools/opcua_minimal_client.py, not a session -->
<!-- check: skip -->
```python
params = ua.ReadParameters()
for _, path in wanted:
    node = await self.resolve(*path)
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value)
    )
response = await self._client.uaclient.read(params)
```

One request, every `DataValue` with its status intact. The obvious
alternative — `await node.read_data_value()` per tag — defaults to
`raise_on_bad_status=True`, so a degraded value arrives as an exception with the
number still in the response, and a caller who catches it loses the value *and*
the reason (lesson 03). The gateway already does it this way; this client copies
the call that works in production rather than inventing one.

### 3. Say whether a value is a reading

This is the safeguard the protocol cannot provide, and it took two attempts.

Every variable is constructed at its `normal_low`, so a fresh server reports
dissolved oxygen at 1.5 mg/L — the bottom of a healthy 1.5–3.0 band — with a
`Good` status (lesson 03). The client records when it connected and compares
`SourceTimestamp` against that:

<!-- a method extract from tools/opcua_minimal_client.py, not a session -->
<!-- check: skip -->
```python
plant.set_value("AERATION:AHU-1:DO", 2.4, 0)
await server.publish()
```

```
    AERATION:AHU-1:DO                             2.4 [untimestamped]  Good
    SITE:WEATHER:STORM                              0 [unmeasured]  Good
```

`SITE:WEATHER:STORM` was never published, so it is still the constructor's `0`,
and the client says **[unmeasured]**. That is the whole safeguard, and it
distinguishes two values that are numerically indistinguishable and would
otherwise both be read as "storm flag is off".

**And the second attempt is the interesting part.** The first version had two
verdicts and treated a missing `SourceTimestamp` as *unmeasured*. Which sounds
safe and is not: `publish()` writes

<!-- softplc/servers/opcua.py:407-412, the DataValue it writes -->
<!-- check: skip -->
```python
await entry.node.write_value(
    ua.DataValue(
        ua.Variant(entry.value, ua.VariantType.Double),
        StatusCode=status,
    )
)
```

with **no `SourceTimestamp`**, so the field `asyncua` set at construction is
*cleared* on every publish. Measured:

```
  fresh server            value=1.5  SourceTimestamp=1790564165.416149
  after publish of 2.4    value=2.4  SourceTimestamp=None
  after publish of 9.9    value=9.9  SourceTimestamp=None
```

So the only field that dates a value is destroyed by the server's own write
path, and my "unmeasured" verdict reported a genuine 2.4 mg/L as never measured —
**forever**. A safeguard that cannot clear is a safeguard that gets switched off,
and this one fired on 100 % of real readings.

Hence three verdicts, and a test that pins the defect:

| verdict | condition | meaning |
|---|---|---|
| `measured` | a timestamp at or after we connected | a real reading |
| `unmeasured` | a timestamp *before* we connected | the constructor's placeholder |
| `untimestamped` | no timestamp | the server is not timestamping; age unknown |

`untimestamped` is the honest answer on this server once anything has been
published. The fix is one line — `SourceTimestamp=ua.DateTime.now()` in
`publish()` — and
`tests/test_opcua_minimal_client.py::test_publish_clears_the_source_timestamp`
fails the moment it is applied, saying so.

**This is the finding worth keeping from the whole course.** Not "the server
publishes a placeholder" — lesson 03. Not "a filter is per-subscription" —
lesson 04. *The one field a client would use to know whether a value is real is
set once at construction and cleared on every write*, so no client can date a
reading from this server. Everything else in these nine lessons is a local
mishap. This one is structural: it is in the design, and no amount of care at
the client compensates.

### 4. Never collapse quality

<!-- a method extract from tools/opcua_minimal_client.py, not a session -->
<!-- check: skip -->
```python
plant.set_value("AERATION:AHU-1:DO", 2.4, 1)      # QUALITY_UNCERTAIN
await server.publish()
```

```
    AERATION:AHU-1:DO                             2.4 [untimestamped]  Uncertain
```

The value is still there and still usable, and the status says so separately.
`Uncertain` is *not* folded into `Good` — which is the specific thing
`gateway/clients/opcua_client.py` spends a paragraph arguing for, and the thing
`opcua_browser.py` loses by raising.

And it is worth saying that a two-state client would look **correct** on this
server forever, because `publish()` cannot emit `Bad` at all (lesson 03). A
safeguard that is never tested against the case it exists for is a safeguard
nobody knows works.

### 5. If you filter a subscription, say so

<!-- a method extract from tools/opcua_minimal_client.py, not a session -->
<!-- check: skip -->
```python
sub = await self._client.create_subscription(500, Handler())
await sub.subscribe_data_change(node)
```

Unfiltered, deliberately. A `DataChangeFilter` is negotiated *per subscription*,
so two clients on one node hold different values at the same instant — measured
at 2.39 and 2.0, both `Good`, with no staleness marker on either (lesson 04). An
unfiltered subscription over-reports (41 notifications carrying 3 distinct
values) but never disagrees with the server. **A filtered one is cheaper and
quietly stale**, and a reader copying this code should watch that trade being
made on purpose rather than by accident.

## Running it

```
$ python -m tools.opcua_minimal_client
read from opc.tcp://127.0.0.1:4840/wwtp/server/
    AERATION:AHU-1:DO                                1.5 [unmeasured]  Good
    AERATION:AHU-1:AIR_FLOW                         2000 [unmeasured]  Good
    INFLUENT:FLOW:FLOW                               200 [unmeasured]  Good
    SITE:WEATHER:STORM                                 0 [unmeasured]  Good

  4 of 4 values are not trustworthy readings:
    AERATION:AHU-1:DO is the server's constructor placeholder (1.5), not a measurement
    AERATION:AHU-1:AIR_FLOW is the server's constructor placeholder (2000), not a measurement
    INFLUENT:FLOW:FLOW is the server's constructor placeholder (200), not a measurement
    SITE:WEATHER:STORM is the server's constructor placeholder (0), not a measurement
```

That is the output of a tool with nothing driving it, and it is the most useful
output in the project: four numbers, all `Good`, all wrong, and the client says
so about every one. Run it against `docker compose up -d plc` and every verdict
becomes `untimestamped` — real values, unknown age, which is the honest state of
this server today.

## What the course added up to

Nine lessons, and the findings sort into three kinds:

**Documentation that overstated working code** — lessons 02, 04, 05, 06. The
address space is not conformant, three comments describe machinery that does not
exist, a range check has no caller, and a heading says "one thread" where there
are three. All of it fixable by editing prose, and all of it found by running
something.

**Design decisions that were never reviewed** — lessons 01, 03, 08. Every
variable constructed at `normal_low`, so a fresh server is healthy everywhere.
`SourceTimestamp` cleared on every write. 686 generated nodes with no artifact
and no drift gate, four of fourteen decisions wrong. None of these is a typo.

**A threat model that does not match the code** — lesson 07. The top-ranked
asset is inert, the top remediation already exists unwired, and the actually
exposed asset — the permit limits — is not in the list at all.

Only the third kind survives a careful read of the source. That is the lesson I
would keep from all nine, and it is not about OPC UA: **the reviewable surface
and the defective surface are different surfaces, and a project that reviews only
the reviewable one will find documentation bugs forever and design bugs never.**

## Where to go next

The course is finished, and the honest summary of what it changed is on one page:
[the course README](README.md) lists all thirteen findings with the lesson each
one belongs to. Three of them are worth doing this week, and none of them is
hard:

1. **Set `SourceTimestamp` in `publish()`.** One line, and every client in the
   world can date a reading. The test that pins the defect tells you when it is
   done.
2. **Serialise the address space and gate it in CI.** A fifth `--check` beside
   four that already exist (lesson 08).
3. **Bind loopback in `compose.yaml`.** Two tokens per port, and "anyone who can
   reach the port" becomes a bounded claim (lesson 07).

And the two things the course found that are not in this repository's own
thread list, because they are findings about documentation rather than about the
plant: the `SourceTimestamp` defect, and the browser tool's `read_data_value()`
call, which will crash on the first degraded sensor and should be
`raise_on_bad_status=False`.
