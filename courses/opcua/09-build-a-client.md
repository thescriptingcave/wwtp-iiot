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
      AERATION:AHU-1:DO                                             2.4  Good
      SITE:WEATHER:STORM                            0 [unmeasured]  Good
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

with **no `SourceTimestamp`**, so the field `asyncua` set at construction was
*cleared* on every publish. Measured:

```
  fresh server            value=1.5  SourceTimestamp=1790564165.416149
  after publish of 2.4    value=2.4  SourceTimestamp=None
  after publish of 9.9    value=9.9  SourceTimestamp=None
```

So the only field that dates a value was destroyed by the server's own write
path, and my "unmeasured" verdict reported a genuine 2.4 mg/L as never measured
— **forever**. A safeguard that cannot clear is a safeguard that gets switched
off, and this one fired on 100 % of real readings.

**That is now fixed**, and three verdicts are still what a client needs, because
the ambiguity does not go away when the timestamp starts working:

| verdict | condition | meaning |
|---|---|---|
| `measured` | a timestamp at or after we connected, **or** we have seen the value move | a real reading |
| `unmeasured` | a timestamp *before* we connected, never seen to move | the constructor's placeholder, or a reading from just before we arrived |
| `untimestamped` | no timestamp | a server that does not stamp its data; age unknown |

The middle row is the honest residual. A genuine measurement published a moment
*before* the client connected has a timestamp older than the connection, exactly
like a placeholder, and **one sample cannot tell them apart.** Only watching the
value move can — which is what `_moved` tracks, and what a subscription does
naturally. The client answers `unmeasured`, which is the right direction to be
wrong in: it tells an operator "I cannot vouch for this" rather than "this is
fine".

### The fix was wrong the first time too

The obvious implementation is `SourceTimestamp=ua.DateTime.now()`. **It does not
work**, and the reason is a real trap in the library:

<!-- asyncua's encoder, quoted — the branch that makes a naive datetime wrong -->
<!-- check: skip -->
```python
# asyncua/ua/ua_binary.py, datetime_to_win_epoch
if dt.tzinfo is None:
    ref = FILETIME_EPOCH_AS_DATETIME        # 1601-12-31, treated as UTC
else:
    ref = FILETIME_EPOCH_AS_UTC_DATETIME
```

A **naive** datetime is encoded as though it were already UTC, so a local time
has the machine's UTC offset baked into the value and the client reads it back
hours out. `ua.DateTime.now()` returns a naive local time. Measured on this
laptop, with the naive version in place:

```
  wall clock          1790566160.150795
  constructor stamp   2026-09-28 03:30:49.777213+00:00    delta  -0.1s
  published stamp     2026-09-27 20:30:49.814630+00:00    delta  -7.0h
```

**Seven hours, and completely invisible in the published value** — it looks like
an ordinary timestamp, and nothing on the server complains. The fix is
`datetime.now(timezone.utc)`, wrapped in a `_utcnow()` helper, and guarded twice:
once by a test that reads the source, and once by a test that compares a live
published timestamp against `time.time()`.

That is the second time in this course that the obvious implementation of a fix
produced a plausible wrong value. The first was a `Counter` that counted
distinct keys rather than entries. **Neither would have been found by reasoning
about the fix**, which is the whole argument for a test that measures the thing
rather than asserting what it ought to be.

**This was the finding worth keeping from the whole course.** Not "the server
publishes a placeholder" — lesson 03. Not "a filter is per-subscription" —
lesson 04. *The one field a client would use to know whether a value is real was
set once at construction and cleared on every write*, so no client could date a
reading from this server. Everything else in these nine lessons is a local
mishap. That one was structural: it was in the design, and no amount of care at
the client compensated.

### 4. Never collapse quality

<!-- a method extract from tools/opcua_minimal_client.py, not a session -->
<!-- check: skip -->
```python
plant.set_value("AERATION:AHU-1:DO", 2.4, 1)      # QUALITY_UNCERTAIN
await server.publish()
```

```
      AERATION:AHU-1:DO                                             2.4  Uncertain
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
so about every one.

Run it against `docker compose up -d plc` and the verdicts change to `measured`
as the plant publishes — which is the whole point of finding 14's fix, and the
reason this client can now be trusted on a running plant rather than only against
a bare server.

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
[the course README](README.md) lists all fourteen findings with the lesson each
one belongs to.

**The first of the three recommendations is now done.** `publish()` and
`_flush_states()` stamp `SourceTimestamp`, which is what finding 14 asked for
and what makes the `measured` verdict above possible at all. Two things about
that are worth keeping:

**The fix was wrong the first time, and the test that caught it is the only
reason it is right now.** The obvious implementation is
`SourceTimestamp=ua.DateTime.now()`. That returns a **naive local** datetime, and
`asyncua`'s `datetime_to_win_epoch` branches on `tzinfo`:

<!-- asyncua's encoder, quoted — the branch that makes a naive datetime wrong -->
<!-- check: skip -->
```python
if dt.tzinfo is None:
    ref = FILETIME_EPOCH_AS_DATETIME        # 1601-12-31, treated as UTC
else:
    ref = FILETIME_EPOCH_AS_UTC_DATETIME
```

A naive local time is therefore encoded as though it were already UTC, the
machine's offset becomes part of the value, and the client reads it back hours
out. Measured on this laptop: **seven hours**, and completely invisible in the
published value, which looks like an ordinary timestamp. The fix is
`datetime.now(timezone.utc)`, wrapped in a `_utcnow()` helper so the trap is
documented where the call is, and guarded twice — once by reading the source and
once by comparing a live published timestamp against `time.time()`.

That is the second time in this course that the obvious implementation of a fix
was wrong in a way that produced a plausible value. The first was a
`Counter` that counted distinct keys instead of entries. Neither would have been
found by reasoning about the fix.

**The remaining two recommendations are unchanged:**

1. ~~**Serialise the address space and gate it in CI.**~~ **DONE.** All 686
   addressable nodes are now serialised to `contracts/address-space.json` by
   `tools/opcua_address_space.py`, and a sixth step in the existing `drift` job
   fails when it moves — naming the line and quoting both values, so a reviewer
   is told which decision changed rather than handed a diff.
2. ~~**Bind loopback in `compose.yaml`.**~~ **DONE.** All five published ports
   bind `127.0.0.1` by default, with `HOST_BIND=0.0.0.0` as a documented opt-out.

And one thing the course found that is still not on the repository's own thread
list, because it is a finding about a tool rather than about the plant:
`tools/opcua_browser.py` calls `read_data_value()` with the default, so it will
crash on the first degraded sensor. It wants
`raise_on_bad_status=False`.
