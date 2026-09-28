# 02 — Nodes, classes and types: what a thing *is*

**Next:** [03 — Reading data properly](03-reading-data.md) · [Back to the course](README.md) · [Previous: 01](01-talking-to-a-server.md)

[Lesson 01](01-talking-to-a-server.md) walked the address space by name. You
found the plant, walked into an area, and reached a value. That is enough to read
something and useless for understanding what you are looking at.

This lesson goes one level down. Three ideas, and they are the three you need
before anything else in OPC UA makes sense:

1. A node has a **class** — is this a thing, or a value?
2. A node has a **type** — what *kind* of thing is it?
3. Nodes are joined by **references** — and the reference, not the class, is what
   distinguishes a folder from the facts attached to it.

## The question

> If a client can browse a tree, how does it know which things are containers and
> which are data?

## A node has a class

Every node in an OPC UA server is one of a small number of classes, and the two
you will meet constantly are `Object` and `Variable`.

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()

plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
ahu = await (await plant.get_child("2:AERATION")).get_child("2:AHU-1")
do = await ahu.get_child("2:do_mg_l")

for node, label in ((client.nodes.objects, "the root"),
                    (plant, "the plant"),
                    (ahu, "a piece of equipment"),
                    (do, "a measurement")):
    print(f"  {label:22} {(await node.read_browse_name()).Name:24} "
          f"{(await node.read_node_class()).name}")
await client.disconnect()
```

```
  the root                Objects                             Object
  the plant               PLANT-A: Northgate Water...        Object
  a piece of equipment    AHU-1                               Object
  a measurement           do_mg_l                             Variable
```

**An `Object` is a container. A `Variable` holds a value.** Equipment is an
Object, the basin it sits in is an Object, and dissolved oxygen is a Variable.
That distinction is the whole reason browsing works: you recurse into Objects and
read Variables.

## Every node also has a type

The class says *container or value*. The **type definition** says what kind, and
it is a reference like any other:

```python
from asyncua import Client, ua

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")
do = await (await (await plant.get_child("2:AERATION"))
           .get_child("2:AHU-1")).get_child("2:do_mg_l")

for node, label in ((plant, "the plant"), (do, "a measurement")):
    for ref in await node.get_references():
        if ref.ReferenceTypeId.Identifier == ua.ObjectIds.HasTypeDefinition:
            kind = await client.get_node(ref.NodeId).read_browse_name()
            print(f"  {label:14} is a {kind.Name}")
await client.disconnect()
```

```
  the plant      is a BaseObjectType
  a measurement  is a BaseDataVariableType
```

These are standard OPC UA types, defined by the specification. A well-built
server uses richer ones — `AnalogItemType` for a measurement, `AnalogVariableType`
for a settable one — and the type is what tells a generic client how to *display*
and *validate* a value without knowing anything about your plant. That is the idea
behind self-description, and this server uses the plain base types, so a
discovery tool finds little to specialise on. You can still read everything; you
just have to read the properties yourself, which is lesson 04.

## The thing that trips everyone up

Here is a property — the unit symbol, a *fact about* the measurement:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()

async def at(*path):
    """Walk down from the plant to a node: at("AERATION", "AHU-1", "do_mg_l").

    The first hop is awaited and the rest are not optional — `get_child` is a
    coroutine, so forgetting one await gives you a coroutine object and a
    confusing error three lines later. Every step is awaited.
    """
    node = await client.nodes.objects.get_child(
        "2:PLANT-A: Northgate Water Reclamation Facility")
    for part in path:
        node = await node.get_child(f"{node.nodeid.NamespaceIndex}:{part}")
    return node

do = await at("AERATION", "AHU-1", "do_mg_l")

unit = await do.get_child("2:UnitSymbol")
print("UnitSymbol is a", (await unit.read_node_class()).name)
await client.disconnect()
```

```
UnitSymbol is a Variable
```

**It is a `Variable`, not a folder.** And so is `EngineeringRangeLow`, and
`Deadband`, and all nine facts attached to that measurement. They are all the
same class as the measurement itself.

If your browse loop is "recurse into every `Object` and read every `Variable`",
then it will read the measurement *and* read its unit symbol *and* read its deadband
— and treat "mg/L" and "0.02" as if they were process values. On a dashboard that
is a panel with a stray `mg/L` on it.

**This is the pitfall, and it is the first thing to cost anyone an afternoon.**
The class does not tell you. The *reference* does:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()

async def at(*path):
    """Walk down from the plant to a node: at("AERATION", "AHU-1", "do_mg_l").

    The first hop is awaited and the rest are not optional — `get_child` is a
    coroutine, so forgetting one await gives you a coroutine object and a
    confusing error three lines later. Every step is awaited.
    """
    node = await client.nodes.objects.get_child(
        "2:PLANT-A: Northgate Water Reclamation Facility")
    for part in path:
        node = await node.get_child(f"{node.nodeid.NamespaceIndex}:{part}")
    return node

do = await at("AERATION", "AHU-1", "do_mg_l")

for ref in await do.get_references():
    name = (await client.get_node(ref.NodeId).read_browse_name()).Name
    kind = {46: "HasProperty", 47: "HasComponent", 40: "HasTypeDefinition"}
    print(f"  {name:24} via {kind.get(ref.ReferenceTypeId.Identifier, '?')}")
await client.disconnect()
```

```
  AHU-1                     via HasComponent
  BaseDataVariableType      via HasTypeDefinition
  EngineeringUnits          via HasProperty
  UnitSymbol                via HasProperty
  EngineeringRangeLow       via HasProperty
  ...
```

`HasComponent` means **"this is a thing you can go inside"**. `HasProperty` means
**"this is a fact about the thing you are already in"**. Same node class, and the
reference is the only thing that distinguishes them.

So the rule for a browse loop is:

| reference | meaning | what to do |
|---|---|---|
| `HasComponent` | a child thing | **recurse** |
| `HasProperty` | a fact about this thing | read as metadata, not as data |
| `HasTypeDefinition` | what this is | read once, ignore |
| `Organizes` | loose containment | ignore — it is also how a node points *back up* |

Here is that loop, written correctly:

```python
from asyncua import Client, ua

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()
plant = await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")

#: The reference types that mean "a child thing", and only forwards.
DOWN = (ua.ObjectIds.HasComponent, ua.ObjectIds.Organizes)

async def browse(node, depth=0, max_depth=1, seen=None):
    """Walk down into child things; read properties; never follow a reference back."""
    seen = set() if seen is None else seen
    if depth > max_depth or node.nodeid in seen:
        return
    seen.add(node.nodeid)
    for ref in await node.get_references():
        identifier = ref.ReferenceTypeId.Identifier
        child = client.get_node(ref.NodeId)
        name = (await child.read_browse_name()).Name
        if identifier == ua.ObjectIds.HasProperty:
            print(f"{'  ' * depth}{name} = {await child.read_value()}   (property)")
        elif ref.IsForward and identifier in DOWN:
            print(f"{'  ' * depth}{name}   (component)")
            await browse(child, depth + 1, max_depth, seen)

await browse(plant)
await client.disconnect()
```

```
DesignFlow_m3h = 1800.0   (property)
Permit_eff_nh4_mg_l_30d_mean = 10.0   (property)
Permit_eff_tss_mg_l = 30.0   (property)
INFLUENT   (component)
  SCREEN-1   (component)
  PIT-1   (component)
  FLOW   (component)
PRIMARY   (component)
  PRI-CL-1   (component)
SLUDGE   (component)
  THK-1   (component)
  DIG-1   (component)
AERATION   (component)
  AHU-1   (component)
  BLW-1   (component)
...
UTILITY   (component)
SITE   (component)
```

(elided — it is 8 areas and their equipment, and every line is one more thing
found by asking rather than by being told.)

Properties and components are now visibly different things, and a dashboard built
on this loop cannot accidentally chart a unit string.

**Two things in that loop are not optional, and the first version got both wrong.**

**References are bidirectional, and the protocol tells you which way.**

Every `Organizes` or `HasComponent` reference exists in the model twice: once as
"the plant contains INFLUENT", and once as "INFLUENT is inside the plant". Both
are the same reference type and the same pair of nodes. The only difference is
`IsForward`, and it is the single most useful flag in the whole browse API:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()
area = await (await client.nodes.objects.get_child(
    "2:PLANT-A: Northgate Water Reclamation Facility")).get_child("2:INFLUENT")

for ref in await area.get_references():
    name = (await client.get_node(ref.NodeId).read_browse_name()).Name
    print(f"  {'forward ' if ref.IsForward else 'INVERSE '} {name[:44]}")
await client.disconnect()
```

```
  forward  FolderType
  forward  SCREEN-1
  forward  SCREEN-2
  forward  PIT-1
  forward  PIT-2
  forward  PIT-3
  forward  GRIT-1
  forward  FLOW
  forward  LIFT
  INVERSE  PLANT-A: Northgate Water Reclamation Facilit
```

Note where the inverse sits: **last**, and looking unremarkable. A browse loop
that treats the first nine as children and recurses on all of them reaches the
plant on the tenth and walks the whole tree again. Nothing marks it as the
interesting one.

An area node points *back up* at the plant, and a browse loop that ignores
`IsForward` walks straight back the way it came. My first version of this loop
did exactly that and printed the plant's name once per area. The fix is not
"skip the plant" — it is `ref.IsForward`, which is correct for every model,
including ones with a shape you have not seen.

**Which is also why `DOWN` is a whitelist.** A list of exclusions has to be
extended for every new reference type a server might use, and a model that
references something unusual gets followed somewhere you did not intend. A
whitelist is safe by default and you decide deliberately to go further.

**A `seen` set**, because a real asset model has diamonds in it — a shared
library referenced from two places — and a walk without one will revisit a node
for every path that reaches it. Recursion over a graph is not recursion over a
tree, and the address space is a graph.

Both bugs are silent. Neither raises; they print more than you asked for.

## NodeIds: the thing you should not hardcode

Every node has a stable identity as well as a name. Here they are side by side:

```python
from asyncua import Client

client = Client("opc.tcp://127.0.0.1:4840/wwtp/server/")
await client.connect()

async def at(*path):
    """Walk down from the plant to a node: at("AERATION", "AHU-1", "do_mg_l").

    The first hop is awaited and the rest are not optional — `get_child` is a
    coroutine, so forgetting one await gives you a coroutine object and a
    confusing error three lines later. Every step is awaited.
    """
    node = await client.nodes.objects.get_child(
        "2:PLANT-A: Northgate Water Reclamation Facility")
    for part in path:
        node = await node.get_child(f"{node.nodeid.NamespaceIndex}:{part}")
    return node

do = await at("AERATION", "AHU-1", "do_mg_l")

print("name   :", (await do.read_browse_name()).Name)
print("nodeid :", do.nodeid)
print("display:", await do.read_display_name())
await client.disconnect()
```

```
name   : do_mg_l
nodeid : NodeId(Identifier=273, NamespaceIndex=2, NodeIdType=FourByte)
display: LocalizedText(Locale=None, Text='do_mg_l')
```

Three ways to name one node, and note that two of them are the same string here:

- **`NodeId`** — an identifier the server allocated. Unique, and the thing to
  *store*. It is an implementation detail: it changes if the model is rebuilt, and
  it means nothing to a human.
- **BrowseName** — unique among siblings, meaningful, and what you browse by.
- **DisplayName** — what a UI shows. Optional, not unique, and **often identical
  to the BrowseName.** It is not a better name, it is a cosmetic override, and a
  server that does not set one falls back to the BrowseName — which is what
  happened above. `do_mg_l` is an unfortunate example of that: the field is called
  "dissolved oxygen" in every human document about this plant, and the address
  space says `do_mg_l`.

That last point is the honest shape of the thing. **Browse names are chosen for
machines to be unique, not for humans to be pretty**, and a model that names
things `do_mg_l` is entirely normal. If you want "Dissolved oxygen" on a screen,
that is your client's job, and it belongs in a lookup table you control.

A browse path like `2:AERATION/2:AHU-1/2:do_mg_l` is built from BrowseNames and
namespaces, and it is the thing you should use. Lesson 08 is about why storing
one is a mistake.

## What to take away

- **`Object` is a container, `Variable` is a value.** That is what makes browsing
  possible.
- **Type definitions are richer than classes** and are what make a server
  self-describing. This server uses the plain base types.
- **A property is a `Variable` too.** The class will not tell you a fact from a
  thing — the *reference* will. `HasComponent` recurse, `HasProperty` read as
  metadata.
- **Three names per node.** `NodeId` to store, BrowseName to browse by,
  DisplayName to show.

**If you skip this lesson**, lesson 03 will still work and lesson 05 will be
mysterious — a subscription is created on a *node*, and this is what a node is.

---

**A note on this server, which is not part of the lesson.** It publishes unit
symbols, ranges and deadbands as plain properties on plain variables rather than
using `AnalogItemType`. Everything above still works: you can read all of it. What
you lose is that a generic client cannot *interpret* it without being told — which
is a property of the server, not of the protocol, and the audit lessons in
[audit/02](../audit/02-units-and-types.md) have the measurements.

**Next:** [03 — Reading data properly](03-reading-data.md)
