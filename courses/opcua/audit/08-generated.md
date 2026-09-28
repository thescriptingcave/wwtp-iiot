# 08 — The address space as generated code, and what that cost

**Next:** [09 — Build a client](09-build-a-client.md) · [Back to the course](../README.md) · [Previous: 07](07-security.md)

This is the lesson where the course argues against a decision I made, and the
one where the argument is strongest. Lesson 02 ended with this:

> Lesson 08 comes back to the generated address space and asks whether generating
> it was the right call at all. This lesson is the strongest argument that it was
> not: the generator made a *wrong* address space reproducible, which is worse
> than a right one nobody trusted.

So here it is. The short answer is that generating it was the right call for
reasons that are still true, that it created a specific and measurable class of
defect, and that the fix is neither of the two obvious options.

## The question

> What did generating the address space buy, what did it cost, and which of those
> was bigger?

## What it bought

The generation is 500 lines in `softplc/servers/opcua.py`, and it produces this:

**686 addressable nodes, every one derived from `contracts/tags.yaml`.** Count
them:

```python
root = server.space.folder
per_depth = {}

async def rec(node, d=0):
    kids = await node.get_children()
    per_depth[d] = per_depth.get(d, 0) + len(kids)
    for k in kids:
        await rec(k, d + 1)

await rec(root)
total = sum(per_depth.values())
for d in sorted(per_depth):
    print(f"  depth {d}: {per_depth[d]:4}")
print(f"  total   : {total:4}  from 57 signals and 22 pieces of equipment")
print(f"  that is {total / 57:.1f} addressable nodes per signal")
```

```
  depth 0:   14
  depth 1:   27
  depth 2:  132
  depth 3:  513
  depth 4:    0
  total   :  686  from 57 signals and 22 pieces of equipment
  that is 12.0 addressable nodes per signal
```

Depth 3 is 513, which is 57 × 9 exactly: the nine properties every signal
carries, from lesson 01. Depth 4 is empty, so the whole plant is four levels deep
— area, equipment, signal, property — and a client that knows the ISA-95 shape
can find any tag in three hops without ever asking what anything is called.

The hand-written alternative is not a file you would write; it is 686 lines of
`add_variable` calls, 9 property blocks per signal, and a register of which
equipment exists — all of which would have to be kept in step with the same YAML
by hand, forever. Forgetting one deadband or mistyping one unit would produce a
server that disagrees with the contract, and nothing would notice, because the
contract and the server would be two documents.

The consistency argument is not decorative. The Modbus register map, the Node-RED
tag list, the Grafana panels and the SQL course all read from the same file.
Change `normal_low` on a signal and the address space, the register map, the
dashboard band and the course's expected output all move together. That is a
genuinely good property and it is the reason the generation exists.

**All of that is worth more than what follows.** I am not going to argue the
project should hand-write this.

## What it cost: 686 nodes and no artifact

Here is the cost, and it has one number in it. The `drift` CI job checks every
generated file in this project:

```
      - name: tag list
      - name: flows
      - name: dashboards
      - name: the course's extracted queries
      - name: the web page's read model
```

Five generated artifacts, each written to disk, each diffable, each with a
`--check` that fails when the output moves. And the address space — the largest
generated artifact in the repository, 686 nodes, twelve per signal — is **not
among them**, because it has no file. It is constructed at startup, served, and
discarded.

So the one generated thing big enough to be worth reviewing is the one thing
nobody can review. Its 24 tests in `tests/test_opcua.py` assert *shape* at
runtime — that a node exists, that a value round-trips — which is not the same as
a human having looked at the result and agreed with it. A pull request that
changed `_add_signal` would show a diff of Python plumbing, and every one of the
686 consequences would be invisible in the diff.

This is the concrete form of lesson 02's complaint. The unit information *is*
wrong — `EngineeringUnits` is a bare `Int32` where the spec wants an
`EUInformation` struct, and there is no `EURange` — and it survived because
nothing ever rendered the address space as something a person could read.

## What it cost: one decision instead of fifty-seven

The deeper cost is about error structure. Ask the built space what its 57 signals
look like:

```python
from collections import Counter

# The gate already started a server and connected a client; this asks the
# *server's own* address space rather than starting a second one. Two lessons
# earlier this snippet had to start its own on 48401, because the runner holds
# 48400 and a second bind there fails.
root = server.space.folder

propsets, datatypes, n = Counter(), Counter(), 0
for area in await root.get_children():
    for holder in await area.get_children():
        for var in await holder.get_children():
            names = {(await p.read_browse_name()).Name
                     for p in await var.get_children()}
            if "SignalId" not in names:
                continue
            n += 1
            propsets[tuple(sorted(names))] += 1
            datatypes[(await var.read_data_type_as_variant_type()).name] += 1

print(f"  signals walked: {n}")
print(f"  distinct property sets : {len(propsets)} -> {list(propsets.values())}")
print(f"  distinct DataTypes     : {len(datatypes)} -> {dict(datatypes)}")
```

```
  signals walked: 57
  distinct property sets : 1 -> [57]
  distinct DataTypes     : 1 -> {'Double': 57}
```

**Fifty-seven signals. One property set. One DataType.**

A hand-written address space would have had 57 independent chances to be wrong,
and the law of large numbers would have given you a mix — some right, some wrong,
and a bug report naming the one that mattered. The generated space has
**exactly one chance**, and it is not a chance at all: it is a single decision
that cannot be half-made.

This cuts both ways, and the honest version says so. Generation is *better* when
the decision is right — the consistency argument above is real and it is why
everything downstream can trust `normal_low`. It is **worse** when the decision
is wrong, because the blast radius is the entire plant rather than one tag, and
because a systematic fault looks exactly like a systematic *success*: 57
identical, well-formed, wrong signals. Nothing about the output distinguishes
"correct by design" from "wrong by design". The two produce byte-identical
looking trees.

That is the real argument against generation, and it is not "hand-writing is
better". It is: **generation converts a reviewable error into an unreviewable
one, and the difference is invisible from the outside.**

## Fourteen decisions, and what they cost

`build_address_space` and `_add_signal` between them make about fourteen choices
that a reader would not call decisions, because they are buried in what looks
like plumbing:

| | decision | verdict |
|---|---|---|
| 1 | namespace at index 2, from the contract's URI | fine |
| 2 | the plant object's browse name is `f"{site.id}: {site.name}"` | fine |
| 3 | design flow and each permit key become properties on the plant object | fine, and useful |
| 4 | ISA-95 Area → Equipment → Variable, built in one pass | fine, and the comment explains why |
| 5 | equipment with no signals still gets a node | deliberate, and correct |
| 6 | `RunState` is `Int32`, initialised to `0` | **wrong whenever nothing drives the plant** — lesson 01 |
| 7 | `EquipmentType`, `RatedPower_kW`, `Duty` as properties | fine |
| 8 | signals become `add_variable` — a `BaseDataVariableType` | **the root of lesson 02's three symptoms** |
| 9 | a variable's initial value is `sig.normal_low` | **wrong** — lesson 03's worst finding |
| 10 | nine fixed-named properties on every signal | fine in intent, wrong in execution |
| 11 | `EngineeringUnits` is a bare `Int32` from a 43-entry table | **wrong** — lesson 02 |
| 12 | `_variant_type` returns `Double` whatever the unit | **wrong** — lesson 02 |
| 13 | `set_writable()` only where the contract says so — 2 of 57 | correct, and deliberate |
| 14 | `by_browse_path` is keyed `area.holder.field` | fine |

**Three of the fourteen are unambiguously wrong, and a fourth is wrong whenever
the server is started without the process model.** All four were found in this
course, by walking the running server — not by reading the generator, and not by
reading the contract.

That is the finding, and it is more specific than "generated code is risky". The
generator is not hard to read. It is *easy* to read, because it looks like
plumbing, and the four defects are all in lines that read like plumbing:

<!-- softplc/servers/opcua.py:233 and 130-132 — the two that matter most -->
<!-- check: skip -->
```python
parent.add_variable(ns, sig.field, ua.Variant(sig.normal_low, _variant_type(sig.eu)),
                    varianttype=_variant_type(sig.eu))
...
def _variant_type(eu: str) -> ua.VariantType:
    """Choose a variant type. Everything numeric is a Double in this plant."""
    return ua.VariantType.Double
```

Both are four lines. Both are wrong. Both read as obviously fine, and I wrote both
and then had to discover them by asking a live server what it had actually built.

## Why this is not an argument for hand-writing

The obvious response is to write the address space by hand, and it is worse on
every axis except one:

- you would have 686 lines to maintain and 57 chances to drift from the contract
- a hand-written tree can be *partly* wrong, which sounds safer and means a
  client's view of the plant depends on which tag it happens to ask for
- and, critically, a hand-written tree is still not *reviewed* — it is just
  reviewable in principle. Nothing forces anyone to look at 686 lines, and the
  project's own record is that a 500-line generator containing four defects went
  unexamined for the life of the project

The evidence for that last point is this course. Seven lessons in, the count of
real defects found by reading documentation is high and the count found by
*running* something is higher, and **every single one of them was found by
observing the running system rather than by reading the source.** Lesson 02's
reference-type numbers, lesson 03's fabricated `normal_low`, lesson 04's
notification counts, lesson 05's accepted 99.0, lesson 06's five ticks, lesson
07's anonymous walk. Not one was found by careful reading, and three of them were
in code I had written.

## The fix, which is neither option

The cost is not generation. It is that **the output of the generation is never
looked at.** And this project has already solved that exact problem four other
times, for the tag list, the flows, the dashboards and the extracted queries —
every one of them is written to a file, committed, and diffed in CI.

So the fix is a fifth one of those — **and it is now done**:

1. ~~**Serialise the built address space to a file** — node id, browse name,
   node class, data type, parent, and the property set — committed alongside the
   contract.~~ `contracts/address-space.json`, 686 nodes, written by
   `tools/opcua_address_space.py`.
2. ~~**Add it to the `drift` job** with a `--check` that fails when the serialised
   form moves.~~ Sixth step in the job that already had five, and it names the
   first differing line with both values.
3. **The serialised form is the review surface.** A pull request that changes
   `_add_signal` shows 57 changed rows saying
   `data_type: BaseDataVariableType → AnalogItemType`, which is a reviewable
   diff. Today it shows a Python function.

Verified rather than asserted: changing `_variant_type` to return `Int32` makes
`--check` fail with
`line 253: committed '"data_type": "Double"', built '"data_type": "Int32"'`, and
`tests/test_opcua_address_space.py` does exactly that as a test.

The file also records the two decisions that lesson 08 could only argue about —
`constructed_from: {field: normal_low, value: ...}` on all 57 signals, and
`run_state_constructed_as: 0` on all 22 pieces of equipment. Both were
invisible in a diff of the generator and both are now a row you can see.

## What is actually true

| claim | verdict |
|---|---|
| Generating the address space was the right call | **yes** — 686 nodes, one source, five downstream consumers |
| It produced reviewable output | **no** — no file, no drift gate, the largest artifact ungated |
| Hand-writing would be safer | **no** — 686 lines, 57 drift opportunities, still unreviewed |
| One bad decision can affect all 57 signals | **yes, and measured** — 1 property set, 1 DataType |
| The defects were findable by reading the generator | **apparently not** — all four found by running it |
| The project already has the fix four times over | **yes** — tag list, flows, dashboards, extracted queries |

The honest summary is that generation was correct and *incomplete*. It solved
consistency and did not solve reviewability, and it is entirely possible — as it
was here — to get the first right, the second wrong, and never notice for the
life of the project, because the thing that was wrong was invisible and the
thing that was right was checked by CI jobs — four of which gate a generated
artefact, and a fifth which gates the web page's read model.

That is also the answer to the question this course started with. The reason
nobody learned OPC UA from this project is not that the code is generated. It is
that **the generated output was never a thing a person looked at**, and neither
the code nor the contract was ever going to make it one. Building a lesson that
walks the running server was the workaround. Publishing the address space is the
fix, and it is a day of work that this course has already specified.

**Next:** [09 — Build a client](09-build-a-client.md)
