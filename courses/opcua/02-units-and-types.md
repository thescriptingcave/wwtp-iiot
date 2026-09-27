# 02 — Units, types, and the one lie in the type system

**Next:** [03 — Reading, and what a StatusCode is for](03-reading-and-quality.md) · [Back to the course](README.md) · [Previous: 01](01-the-address-space.md)

[Lesson 01](01-the-address-space.md) ended on a claim worth stress-testing. The
server publishes `EngineeringUnits = 6152` and `UnitSymbol = 'mg/L'` on every
variable, and the module docstring calls this *"the single thing that makes the
point of OPC UA concrete: the client is told what the number means."*

So: is it? A client that has never seen this plant, using nothing but the OPC UA
specification, can it work out what `do_mg_l` measures?

## The question

> Is the unit information a *conformant* OPC UA client can find, or only one that
> already knows this project's conventions?

## The information is there

Start with what works. Every measured signal carries a unit, and the value the
plant uses is unambiguous — `mg/L` is a string no client can misread:

```python
a  = await root.get_child("2:AERATION")
ah = await a.get_child("2:AHU-1")
for name in ("do_mg_l", "air_flow_m3h", "blower_rpm"):
    v = await ah.get_child(f"2:{name}")
    unit = await v.get_child("2:UnitSymbol")
    uid  = await v.get_child("2:EngineeringUnits")
    print(f"{name:16} {await uid.read_value():>6}  {await unit.read_value()!r}")
```

```
do_mg_l            6152  'mg/L'
air_flow_m3h       4981  'm3/h'
blower_rpm         4304  'rev/min'
```

`6152` is an OPC UA unit id from the standard catalogue. `4981` is m³/h. `4304`
is rev/min. A client can switch on those numbers and get the right answer. **On
this evidence the docstring is right.**

## And here is why it is not

Now ask the question the spec would. Not *"what does this server call its
unit?"* but *"what type is `EngineeringUnits`, and where does the specification
say it lives?"*

```python
a  = await root.get_child("2:AERATION")
ah = await a.get_child("2:AHU-1")
do = await ah.get_child("2:do_mg_l")
eu = await do.get_child("2:EngineeringUnits")
print("DataType of EngineeringUnits:",
      (await eu.read_data_type_as_variant_type()).name)

names = [(await c.read_browse_name()).Name for c in await do.get_children()]
print("has EURange, the standard property:", "EURange" in names)
print("has EngineeringRangeLow, ours:    ",
      "EngineeringRangeLow" in names)
```

```
DataType of EngineeringUnits: Int32
has EURange, the standard property: False
has EngineeringRangeLow, ours:     True
```

**The specification says `EngineeringUnits` is an `EUInformation`** — a
structure with a `NamespaceUri` and a `UnitId`, because a unit id alone is
meaningless without knowing *which catalogue* it came from. UCUM, UN/CEFACT 20,
the OPC UA annex: three catalogues, overlapping id spaces, and a bare `6152`
cannot say which. This server writes a bare `Int32`, which is a type mismatch a
conformant client will either reject or silently mis-decode.

And the property is on the wrong node entirely. The standard place for unit and
range is the **`AnalogItemType`**, which carries `EngineeringUnits` and
`EURange` as part of its type definition:

```python
a  = await root.get_child("2:AERATION")
do = await (await a.get_child("2:AHU-1")).get_child("2:do_mg_l")
tdef = None
for r in await do.get_references():
    if r.ReferenceTypeId.Identifier == ua.ObjectIds.HasTypeDefinition:
        tdef = (await client.get_node(r.NodeId).read_browse_name()).Name
print("do_mg_l's type definition:", tdef)
```

```
do_mg_l's type definition: BaseDataVariableType
```

`BaseDataVariableType` is the bare root of the variable hierarchy. The
conformant choice is `AnalogItemType`, and this server's variables are not
instances of it.

**One cause, three symptoms.** `add_variable()` at
`softplc/servers/opcua.py:232` creates a plain variable, and the unit and range
are then bolted on as *ad-hoc properties* with names the specification does not
use. Had the variable been created as an `AnalogItemType`, `EngineeringUnits`
and `EURange` would have come with the type, correctly typed, for free — and
there would be nothing to get wrong.

## What that costs, concretely

A client that discovers this server by walking the standard type hierarchy —
which is what discovery tools do, and what a generic historian integration does —
looks for `AnalogItemType` instances and their `EURange`. It finds:

```
AnalogItemType instances: 0
```

So it learns that this server has 57 untyped numbers, and it will say so in its
own UI. The information is present, readable, and *invisible to the thing the
protocol was designed for*. That is the difference between carrying a fact and
being conformant, and it is worth holding onto, because "we put the unit on the
node" feels identical to "we are self-describing" and is not.

The honest scorecard for this server:

| | |
|---|---|
| A client *we* wrote can read the unit | **yes** — `tools/opcua_browser.py` does |
| A client reading `UnitSymbol` can read it | **yes** — a plain string |
| A client following the spec finds it | **no** — wrong type, wrong node, wrong property names |
| A discovery tool shows it | **no** — no `AnalogItemType` to find |

## The lie in the type system

Units were the easy half. Types are where this server stops being honest, and
the mechanism is four lines long:

```python
from softplc.servers.opcua import _variant_type
for eu in ("mg/L", "{pH}", "{Boolean}", "1"):
    print(f"{eu:12} -> {_variant_type(eu).name}")
```

```
mg/L          -> Double
{pH}          -> Double
{Boolean}     -> Double
1             -> Double
```

`_variant_type()` accepts an engineering unit and **ignores it**, returning
`Double` unconditionally. The contract says the storm flag is a `{Boolean}` and
pH is `{pH}`, and both arrive over the wire as IEEE-754 doubles.

This is not a rounding concern. It removes the one distinction a type system
exists to make:

- a client cannot tell a **flag** from a **reading** without hardcoding the tag
- a client cannot tell **pH 7** from **7 mg/L** by type
- `0.0` is ambiguous between *false*, *off* and *no reading yet* — and this
  project's historian already has a version of that problem, which is why there
  is a "What has stopped reporting" panel next to every trend

The docstring says the address space is *"a browsable, **typed** address
space"*. It is browsable. It is typed in exactly one place: `RunState`, which
is correctly an `Int32`.

## Five different meanings, one id

Unit ids are supposed to disambiguate. Watch what happens when you ask which ids
are in use:

```python
from collections import Counter
from softplc.contract import contract
from softplc.servers.opcua import _unit_id

by = Counter(_unit_id(s.eu) for s in contract().signals.values())
print("unit ids in use:", len(by), "for",
      len({s.eu for s in contract().signals.values()}), "different units")
print("the collisions:")
for eu in ("1", "{1}", "{Boolean}", "{MPN}/100mL", "mg/(L.h)"):
    print(f"  {eu:14} -> {_unit_id(eu)}")
```

```
unit ids in use: 18 for 22 different units
the collisions:
  1               -> 12755
  {1}             -> 12755
  {Boolean}       -> 12755
  {MPN}/100mL     -> 12755
  mg/(L.h)        -> 12755
```

**Twenty-two units, eighteen ids.** Five of them collapse onto `12755`,
*"dimensionless"*: a plain ratio, a braced ratio, a boolean, a coliform count in
MPN/100 mL, and a mass flux in mg/(L·h).

Three of those five are dimensionless *in the physics*, so collapsing them is
arguably correct — `{MPN}/100mL` genuinely is dimensionless. But `{Boolean}` is
not a quantity at all, and it is sharing an id with three that are. A client
that switches on unit id to decide how to *format* a value will render a
coliform count and a storm flag identically, and there is no way for it to know
it should not.

## The trap that is not a bug today

`_unit_id()` ends with a default:

```python
def _unit_id(eu: str) -> int:
    return UNIT_IDS.get(eu, 12755)  # 12755 = dimensionless
```

An unknown unit becomes *"dimensionless"*. Not an error — a plausible value that
sorts, formats, and displays. So a typo in `contracts/tags.yaml` does not fail
the build, does not fail a test, and does not appear in a log. It produces a
variable that claims to have no unit, forever, and the only symptom is a client
that renders `1840.5` where it should render `1840.5 m³/h`.

```python
from softplc.servers.opcua import _unit_id
print("a unit that does not exist:", _unit_id("furlongs/fortnight"))
```

```
a unit that does not exist: 12755
```

**This is not a live bug.** All 22 units the contract actually uses are present
in the table, and a test asserts it. It is a trap with the safety on.

The same is true one level up, and it is a real error in the table rather than a
default: `mbar` and `psi` are both mapped to `425`.

```python
from softplc.servers.opcua import UNIT_IDS
print("mbar ->", UNIT_IDS["mbar"], " psi ->", UNIT_IDS["psi"])
```

```
mbar -> 425  psi -> 425
```

`425` is millibar, and a pound per square inch is not a millibar — it is about
69 of them. No signal in this plant uses `psi`, so nothing is wrong today. But
the table is a lookup someone will extend, and it currently contains a row that
is simply false. A test that asserts every id is unique would have caught it; a
test that asserts every id is *correct* would need the catalogue.

## Where this leaves the claim

The docstring's sentence — *"the client is told what the number means"* — is
true of this project's own client and false of the protocol's. Both halves are
worth keeping:

- **What is genuinely good:** the unit is in the address space at all, per
  signal, alongside its range, its normal band, its deadband and its sampling
  interval. A historian can be built from this server without a second document.
  That is the real difference from 4 314 integers and a spreadsheet.
- **What is genuinely missing:** conformance. `AnalogItemType`, `EURange`, and an
  `EUInformation` struct are each a few lines of `asyncua`, and until they exist
  the self-description is a private convention rather than a protocol feature.
- **What is a bug, not a limitation:** every value being a `Double`.

Lesson 08 comes back to the generated address space and asks whether generating
it was the right call at all. This lesson is the strongest argument that it was
not: the generator made a *wrong* address space reproducible, which is worse
than a right one nobody trusted.

**Next:** [03 — Reading, and what a StatusCode is for](03-reading-and-quality.md)
