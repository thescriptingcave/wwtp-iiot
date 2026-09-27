# 03 — Reading, and what a StatusCode is for

**Next:** [04 — Subscriptions](04-subscriptions.md) · [Back to the course](README.md) · [Previous: 02](02-units-and-types.md)

The module docstring makes a claim that this project treats as load-bearing:

> **StatusCodes.** Every value carries its quality. A failing sensor reports
> `Bad` rather than a plausible number, which is the thing that makes a
> historian honest.

And the gateway's reader says the same thing harder:

> The temptation is to read the number and ignore the code… So this reader
> returns quality alongside every value, from the same place, and there is no
> code path that produces a value without one.

Both are about **a failing sensor**. This lesson asks what a client actually
receives from a failing sensor, and the answer involves a number that has never
been measured.

## The question

> If a sensor is failing right now, how would a client know?

## Reading a value is two different operations

The first thing to understand is that `read` has two shapes, and the obvious one
throws away the interesting half.

```python
a  = await root.get_child("2:AERATION")
do = await (await a.get_child("2:AHU-1")).get_child("2:do_mg_l")

plain = await do.read_value()
full  = await do.read_data_value()
print("read_value()       ->", plain)
print("read_data_value()  -> value", full.Value.Value,
      "status", full.StatusCode.name)
print("  SourceTimestamp  ->", full.SourceTimestamp)
```

```
read_value()       -> 1.5
read_data_value()  -> value 1.5 status Good
  SourceTimestamp  -> 2026-09-27 23:28:09.675892+00:00
```

The timestamp is the only line here that will differ when you run it, because it
is *now* — which is the point of the next section.

`read_value()` returns a number. `read_data_value()` returns a `DataValue`: the
number, a `StatusCode`, a source timestamp and a server timestamp. **The status
is not a property of the number — it is a separate field that `read_value()`
throws away**, and that is the whole subject of this lesson.

## What a fresh server reports for a plant that has never run

Look at that `1.5` again. It is not a measurement. It is the value the
constructor chose.

```python
from softplc.contract import contract
c = contract()
print(f"{'signal':22} {'reads':>8}  {'normal_low':>10}  {'normal_high':>11}")
for sid, holder, field in [
    ("AERATION:AHU-1:DO", "2:AHU-1", "do_mg_l"),
    ("AERATION:AHU-1:BLOWER_RPM", "2:AHU-1", "blower_rpm"),
    ("INFLUENT:FLOW:FLOW", "2:FLOW", "flow_m3h"),
]:
    a = await root.get_child("2:" + sid.split(":")[0])
    node = await (await a.get_child(holder)).get_child(f"2:{field}")
    sig = c.signal(sid)
    print(f"{field:22} {await node.read_value():>8}  {sig.normal_low:>10}"
          f"  {sig.normal_high:>11}")
```

```
do_mg_l                1.5       1.5         3.0
blower_rpm           600.0     600.0      1800.0
flow_m3h              200.0     200.0      2200.0
```

Every one of them is **`normal_low`**, with a `Good` status, from a server that
has not measured anything.

`softplc/servers/opcua.py:233` constructs each variable as:

<!-- an excerpt of softplc/servers/opcua.py:232, not a session -->
<!-- check: skip -->
```python
parent.add_variable(ns, sig.field, ua.Variant(sig.normal_low, ...))
```

`normal_low` was chosen because it is in range and it is a plausible starting
number. It is the bottom of the band the process is *expected* to sit in. So the
server's initial state is a plant that is **exactly on the edge of healthy,
everywhere, reporting `Good`** — which is a specific and dangerous shape of
wrong:

- a dissolved oxygen probe reads 1.5 mg/L, the bottom of its 1.5–3.0 band
- four blowers read 600 rpm, the bottom of their 600–1800 band, which to any
  threshold means **the blowers are running**
- influent flow reads 200 m³/h, the bottom of 200–2200

Nothing in the value says any of this is invented. The `SourceTimestamp` is
*now*, because `asyncua` stamps it at construction. This is the same defect class
as the pH read from the TSS signal in the permit dashboard — a plausible number
that is wrong — except that here it is not a bug in a query. **It is the
server's default state**, and a client cannot tell it from a working plant.

The fix is not a code change to the constructor; it is a `Bad` status until the
first real publish. Which brings us to the second problem.

## `Bad` cannot reach the wire

`publish()` decides the status, and it has one branch for two states:

<!-- the whole of the status decision, from softplc/servers/opcua.py:402 -->
<!-- check: skip -->
```python
status = (
    ua.StatusCode(ua.UInt32(ua.StatusCodes.Good))
    if entry.quality == 0
    else ua.StatusCode(ua.UInt32(ua.StatusCodes.Uncertain))
)
```

`quality == 0` is `Good`. **Everything else is `Uncertain`** — `QUALITY_UNCERTAIN`
and `QUALITY_BAD` take the same path, because there is no third branch. So drive
a value through the full round trip:

```python
from softplc.contract import QUALITY_GOOD, QUALITY_UNCERTAIN, QUALITY_BAD
from softplc.contract import QUALITY_NAMES
from gateway.clients.opcua_client import quality_from_status

node_id = space.variables["AERATION:AHU-1:DO"].node.nodeid
print(f"{'plant model':11} {'on the wire':11} {'gateway recovers':17} value")
for q in (QUALITY_GOOD, QUALITY_UNCERTAIN, QUALITY_BAD):
    server.set_value("AERATION:AHU-1:DO", 2.0, q)
    await server.publish()
    params = ua.ReadParameters()
    params.NodesToRead.append(
        ua.ReadValueId(NodeId=node_id, AttributeId=ua.AttributeIds.Value))
    rv = (await client.uaclient.read(params))[0]
    back = quality_from_status(rv.StatusCode)
    print(f"{QUALITY_NAMES[q]:11} {rv.StatusCode.name:11} "
          f"{QUALITY_NAMES[back]:17} {rv.Value.Value}")
```

```
plant model on the wire gateway recovers value
Good        Good        Good              2.0
Uncertain   Uncertain   Uncertain         2.0
Bad         Uncertain   Uncertain         2.0
```

**`Bad` arrives as `Uncertain`.** Not because the gateway loses it — the
gateway is the one part of this that does it right, using a raw batch `read` that
preserves every `StatusCode`. It is lost on the way *out*, in a two-branch
conditional.

And the consequences run downhill:

- The gateway's `if quality == QUALITY_BAD: continue` branch, which exists to
  drop unusable readings, is **unreachable** against this server.
- The `Uncertain → QUALITY_UNCERTAIN` and `Bad → QUALITY_BAD` arms of
  `quality_from_status` can never both fire, so the mapping is untestable in
  production and is only covered by unit tests that construct the status by hand.
- `grep -c 'StatusCodes.Bad' softplc/servers/opcua.py` is **0**.

## And the plant never produces a `Bad` either

The status is not the only thing that is missing. Trace it to the source and the
whole quality mechanism turns out to be half-wired.

The contract defines three states:

```python
from softplc.contract import QUALITY_NAMES
print("the contract defines:", QUALITY_NAMES)
```

```
the contract defines: {0: 'Good', 1: 'Uncertain', 2: 'Bad'}
```

`QUALITY_BAD` is imported in four modules and used in two of them — the scan
loop's `mark_quality(key, code)` will hold any code, and the gateway's reader
will act on it. But the process model constructs its snapshot like this:

<!-- softplc/process/plant.py:469 -->
<!-- check: skip -->
```python
return PlantSnapshot(values=v, states=states, quality={})
```

**An empty dict, unconditionally, in the only place a `PlantSnapshot` is built.**
The single writer of a non-zero quality in the whole simulation is the fault
engine, and it writes one value:

<!-- softplc/faults/engine.py:458 -->
<!-- check: skip -->
```python
snap.quality[target] = QUALITY_UNCERTAIN
```

Every sensor fault in the library — `do_sensor_drift`, `sensor_flatline`,
`sensor_stuck_high`, `effluent_tss_stuck` — degrades to `Uncertain`. **No fault
in this project produces a `Bad` quality.** Which is a defensible design on its
own: a simulator that has no way to fail completely may not need to say so. But
it means the docstring's central sentence describes a capability the code does
not have, and `QUALITY_BAD` is a constant with no producer.

## The other half of the problem: reading raises

Now suppose a sensor *is* degraded and the status is honestly `Uncertain`. What
does the obvious client do?

```python
from softplc.contract import QUALITY_UNCERTAIN
node_id = space.variables["AERATION:AHU-1:DO"].node.nodeid
server.set_value("AERATION:AHU-1:DO", 2.0, QUALITY_UNCERTAIN)
await server.publish()
node = client.get_node(node_id)
try:
    await node.read_data_value()
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")
```

```
UaStatusCodeError: The operation was uncertain.(Uncertain)
```

**The default `raise_on_bad_status=True` turns a degraded-but-readable value
into an exception.** The value was right there the entire time:

```python
from softplc.contract import QUALITY_UNCERTAIN
node_id = space.variables["AERATION:AHU-1:DO"].node.nodeid
server.set_value("AERATION:AHU-1:DO", 2.0, QUALITY_UNCERTAIN)
await server.publish()
node = client.get_node(node_id)
dv = await node.read_data_value(raise_on_bad_status=False)
print("value", dv.Value.Value, "status", dv.StatusCode.name)
```

```
value 2.0 status Uncertain
```

This is a genuinely sharp edge, and it is the *opposite* of what the gateway's
docstring demands. A client that reads the obvious way and catches the exception
**discards the value and the reason together** — which is exactly the
"a week of confident, wrong, flat-lined data" outcome the docstring is warning
about, arrived at by obeying the API.

`tools/opcua_browser.py` calls `read_data_value()` with the default at lines 88
and 134. **This project's own browser crashes on the first degraded signal** —
and given that a fresh server is `Uncertain`-free but a faulted one is not, that
is a tool you cannot trust to show a fault.

## What is actually true

| claim | verdict |
|---|---|
| Every value carries a quality | **yes** — `DataValue.StatusCode` is always present |
| A failing sensor reports `Bad` | **no** — `Bad` has no producer and no path to the wire |
| A bad value is distinguishable from a good one | **partly** — `Uncertain` is distinguishable; `Bad` is not |
| A never-measured value is distinguishable | **no** — a fresh server reports `normal_low` as `Good` |
| A degraded value is safe to read | **no** — the default call raises |

Two of the five are the "plausible wrong number" this project exists to hunt, and
the first of them is the server's *starting state*: a plant sitting exactly on
the bottom of every healthy band, reporting `Good`, with a current timestamp.

That last one is the more instructive defect, because it is not a protocol
mistake. The constructor had to pick a value, and it picked a *plausible* one.
The lesson is the same as the permit dashboard's pH-from-TSS, one level earlier
in the stack: **an invented number that sits inside the expected range is harder
to catch than an invented number that does not**, because every downstream check
is calibrated to the expected range.

The fix for both is small — `Bad` until the first publish, and a third branch in
`publish()` — and both belong to the open thread list rather than to this branch.
Lesson 04 is where the quality question gets interesting, because a subscription
does not send a status for values that *did not change*, and this project's
deadband means most values do not change.

**Next:** [04 — Subscriptions](04-subscriptions.md)
