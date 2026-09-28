# 01 — The address space is a tree, and you can walk it

**Next:** [02 — engineering units, and the one lie in the type system](02-units-and-types.md) · [Back to the course](../README.md)

Modbus gives you a flat list of integers. Register 40104 holds a number, and
whether that number is m³/h or revolutions per minute is written down in a
spreadsheet somewhere that will drift out of date, and the wire has no opinion.

OPC UA gives you a tree of *nodes* that know what they are. This lesson walks
that tree by hand.

That is the whole point of the exercise, and it is worth being clear about why:
**the address space in this project is generated from `contracts/tags.yaml`.**
`softplc/servers/opcua.py` builds all 57 variables from the contract, so you
could read that file and learn what the tree looks like. You would learn the
*answer*. You would not learn how to find it, which is the transferable part, and
you would not notice anything wrong with it — and there is something wrong with
it, which [lesson 02](02-units-and-types.md) is about.

## The question

> What can a client discover about this plant without being told anything in
> advance?

## Before you start

```bash
docker compose up -d plc            # or just: python -m softplc.main
docker compose logs -f plc
```

Any python in this repo can also open a session for you:

```bash
uv run python tools/opcua_browser.py browse
```

Every snippet on this page is run by `tools/check_lessons.py` against a live
server, so if one of them stops working the gate fails rather than the lesson
quietly becoming fiction. The snippets are given `client`, `root` and `space`
already connected — see [the course README](../README.md) for the convention.

## The tree

`root` is the plant object. Everything hangs off it.

```python
areas = [(await c.read_browse_name()).Name
         for c in await root.get_children()]
print(areas)
```

```
['DesignFlow_m3h', 'Permit_eff_nh4_mg_l_30d_mean', 'Permit_eff_tss_mg_l',
 'Permit_eff_ph_min', 'Permit_eff_ph_max', 'Permit_dis_bacti_geomean',
 'INFLUENT', 'PRIMARY', 'SLUDGE', 'AERATION', 'SECONDARY', 'EFFLUENT',
 'UTILITY', 'SITE']
```

Read that twice, because the first thing in the list is not an area.

**Fourteen children, and six of them are properties, not places.** The permit
limits and the design flow are `HasProperty` references on the plant object —
metadata *about* the plant. The other eight are `HasComponent` references: the
ISA-95 areas.

This is the first thing that trips up a newcomer and it will trip you up:
`get_children()` does not distinguish a folder from a property, and neither
distinguishes them by name. A loop that assumes every child is a place to recurse
into will recurse into `Permit_eff_ph_max` and find nothing, and — depending on
how you wrote it — will either crash or quietly return an empty result.

The distinction is not in the name. It is in the *reference type* — and counting
them is more instructive than listing rows, because the count is where the
surprise is:

```python
from collections import Counter
from asyncua import ua

NAMES = {v: k for k, v in vars(ua.ObjectIds).items() if isinstance(v, int)}
counts = Counter(r.ReferenceTypeId.Identifier
                 for r in await root.get_references())
for i, n in sorted(counts.items()):
    print(f"{i:>4}  {NAMES.get(i, '?'):<18} x{n}")
```

```
   35  Organizes          x1
   40  HasTypeDefinition  x1
   46  HasProperty        x6
   47  HasComponent       x8
```

**Sixteen references, fourteen children.** `get_children()` returned 14 above and
this returns 16, and the two it hides are the interesting ones:

- `35 Organizes x1` — an *inverse* reference pointing back at `Objects`, the
  node this one hangs from. The plant object does not only know its children; it
  also knows its parent, and the parent knows about it. OPC UA graphs are
  bidirectional, which is why a client can walk *up* from a tag to find the plant
  it belongs to. A register map cannot do that.
- `40 HasTypeDefinition x1` — pointing at `BaseObjectType`. This is how the
  server says "this is an Object", and it is what a client reads to know whether
  a node is a folder or a measurement *before* it looks at the name.

So the three that matter are:

| id | reference | meaning here |
|---|---|---|
| 47 | `HasComponent` | a thing you can go into — the 8 areas |
| 46 | `HasProperty` | a fact about this thing — design flow, 5 permit limits |
| 40 | `HasTypeDefinition` | what kind of node this is |

Those three numbers are the difference between "a fact about this thing" and "a
thing you can go into", and they are why an OPC UA client can draw a picture of
a plant it has never seen.

## One level down

Areas contain equipment, and equipment contains variables. Navigation is by
browse path — a slash-separated list of names, each namespace-qualified:

```python
a = await root.get_child("2:AERATION")
names = [(await c.read_browse_name()).Name for c in await a.get_children()]
print(names)
```

```
['AHU-1', 'RAS-P-1', 'RAS-P-2', 'BLW-1', 'BLW-2', 'BLW-3', 'BLW-4']
```

The `2:` prefix is the **namespace index**. The server registers the contract's
namespace at index 2 on startup; index 0 is the OPC UA standard address space
and index 1 is the server's own. Omit the prefix and you are browsing the
standard definitions, which is a mistake that produces no error and no data.

## What a variable knows about itself

Here is the payoff. A dissolved-oxygen variable, with everything the server
publishes about it:

```python
a  = await root.get_child("2:AERATION")
ah = await a.get_child("2:AHU-1")
do = await ah.get_child("2:do_mg_l")
for c in await do.get_children():
    name = (await c.read_browse_name()).Name
    print(f"{name:24} = {await c.read_value()!r}")
```

```
EngineeringUnits         = 6152
UnitSymbol               = 'mg/L'
EngineeringRangeLow      = 0.0
EngineeringRangeHigh     = 20.0
NormalBandLow            = 1.5
NormalBandHigh           = 3.0
Deadband                 = 0.02
SamplingInterval_ms      = 1000
SignalId                 = 'AERATION:AHU-1:DO'
```

A client that connects knowing *nothing* about this plant now knows that
`do_mg_l` is dissolved oxygen in milligrams per litre, that its range is 0–20,
that the process normally sits between 1.5 and 3.0, and that it should be
sampled about once a second. It learned all of that by *asking the server*.

Compare with the Modbus version of the same plant, which is 4 314 holding
registers carrying integers and a `contracts/modbus.yaml` that exists solely to
tell a reader what register 40 104 meant. That document is a promise. This is a
fact the server cannot contradict.

`EngineeringUnits` is `6152` — an opaque **OPC UA unit id**, not a string. The
string is `UnitSymbol`, one line below it. The number is the part a historian
stores, because it is unambiguous; `mg/L` is the part a human reads. Which one
you use, and why a boolean and a pH and a flow rate all being `Double` is a
problem, is [lesson 02](02-units-and-types.md).

## Equipment, and a variable that is always zero

Equipment nodes carry their own metadata, and one variable per unit:

```python
bl = await (await root.get_child("2:AERATION")).get_child("2:BLW-1")
for c in await bl.get_children():
    print(f"{(await c.read_browse_name()).Name:16} = {await c.read_value()!r}")
```

```
EquipmentType     = 'blower'
RatedPower_kW     = 90.0
Duty              = 'lead'
RunState          = 0
```

Blower 1, 90 kW, the lead unit. And `RunState` is `0`, which is the initial
value it was constructed with at `softplc/servers/opcua.py:210`.

**If you are running the full plant, that is a bug you are looking at, not a
fact.** The scan loop in `softplc/main.py:242` stages the real state:

<!-- this is an excerpt of softplc/main.py, not a session — it is not runnable -->
<!-- check: skip -->
```python
for eq, state in states.items():
    self.opcua.set_equipment_state(eq, state)
```

…but nothing stages it until the process model is running. Open a bare
`OpcUaServer` — which is what a unit test does, and what the snippet gate in
`tools/check_lessons.py` does — and all 22 pieces of equipment sit at `RunState
= 0` forever, looking exactly like twenty-two stopped motors.

This is worth sitting with, because it is the exact failure mode this project
keeps running into: **a value that is always zero is indistinguishable from a
value that is genuinely zero.** The historian has the same problem and solves it
differently, with a row that records *when a signal last changed* — which is why
`ui/grafana` has a panel called "What has stopped reporting" next to every
trend.

## What you should take away

| Modbus | OPC UA |
|---|---|
| A flat array of integers | A tree of typed, named nodes |
| A register map in a document | The server *is* the document |
| The client polls | The client subscribes — [lesson 04](04-subscriptions.md) |
| Quality is your problem | Every value carries a StatusCode — [lesson 03](03-reading-and-quality.md) |
| Write permission is a convention | It is part of the model — [lesson 05](05-writing.md) |

None of this is free. The address space above took 500 lines of Python and a
generated namespace table to build, and a client that wants one value pays for
all the structure. [Lesson 08](08-generated.md) is about that trade honestly, and
about what this particular implementation gives up.

**Next:** [02 — engineering units, and the one lie in the type system](02-units-and-types.md)
