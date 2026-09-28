# The OPC UA course

Nine lessons, run against a real OPC UA server — this plant.

It exists because of a gap this project had and did not notice for a long time.
OPC UA was the primary protocol, 500 lines of server, a client, a browser tool
and 370 lines of tests, and **not one lesson**. Every mention of it in the
repository was one of four things: an architecture line, a command to run, a
`45 nodes assembled` verification message, or a glossary entry. The SQL course
had 21 lessons. This had none, and the reason is uncomfortable:

> **The two things most worth learning are the two things this project automated.**

The SQL course works because the SQL is hand-written and runs against a live
database — the artefact *is* the lesson. The OPC UA address space did not work
that way. It is generated from `contracts/tags.yaml` by
`softplc/servers/opcua.py`, so you can read that file and learn the answer. What
you cannot do is learn how to *find* it, which is the transferable part, and
without that you cannot notice when it is wrong.

So these lessons do the thing the generated code made impossible: they make you
walk the tree, and they tell you what is wrong with it when you get there.

## Before you start

The snippets run against a server the runner starts for you, so you do not need
docker up. You need the repo's virtualenv and the `asyncua` package, both of
which the repo already needs:

```bash
uv sync
```

If you would rather drive it by hand against a live plant:

```bash
docker compose up -d plc
uv run python tools/opcua_browser.py browse
```

## Running the snippets

Every ```python block in these lessons is executed by a gate, exactly the way
every ```sql block in the SQL course is executed by `tools/check_sql.py`:

```bash
uv run python tools/check_lessons.py          # run them all
uv run python tools/check_lessons.py --list   # what would run, what is skipped
uv run python tools/check_lessons.py --verbose
```

A SQL gate can only prove a query *parses* — a query returning no rows looks
exactly like a correct one. This gate can be stricter, because an OPC UA snippet
either connects to a server or it does not. Each snippet gets a **freshly started
server and a connected client**, so a lesson that writes a value cannot leave it
written for the lesson after it.

### The convention

The runner injects four names so that a snippet reads like a session rather than
a setup script:

| name | what it is |
|---|---|
| `client` | an `asyncua.Client`, already connected |
| `root`   | the plant's root object node — start browsing here |
| `space`  | the server-side `AddressSpace`, for the things a lesson quotes |
| `ua`     | `asyncua.ua`, for NodeIds, StatusCodes and ObjectIds |
| `asyncio`| so a snippet can sleep and let a subscription deliver |

A snippet is wrapped in an async function, so `await` works at its top level. The
cost is that a snippet cannot use a module-level `import *` or a triple-quoted
string spanning its own indentation. Nothing here does.

### When a block must not run

Some blocks are illustrations of code that lives somewhere else, or deliberately
provoke an error. Mark the block with a comment on the line above:

    <!-- check: skip -->

If a block is silently not being picked up, `--list` will say so, and the reason
is usually that the marker is more than one line above the fence.

### What the gate does not check

It proves the snippet **runs**. It does not compare the output printed in the
lesson against what the snippet printed — so a snippet can keep running while the
output above it goes stale. That is a real limitation, shared with
`tools/check_sql.py`, and it is why the first version of these lessons was
written by running every snippet and pasting what came back rather than by
writing plausible output. Two errors in lesson 01 were caught exactly that way,
including a reference id that had been guessed and was wrong.

## The lessons

| # | Lesson | Status |
|---|---|---|
| 01 | [The address space is a tree, and you can walk it](01-the-address-space.md) | **written** |
| 02 | [Units, types, and the one lie in the type system](02-units-and-types.md) | **written** |
| 03 | [Reading, and what a StatusCode is for](03-reading-and-quality.md) | **written** |
| 04 | [Subscriptions, and the feature this project does not use](04-subscriptions.md) | **written** |
| 05 | [Writing, and the difference between enforced and not](05-writing.md) | **written** |
| 06 | [Why OPC UA is asyncio and Modbus is not](06-async.md) | **written** |
| 07 | Security: certificates, endpoints, and why nobody exposes 4840 | planned |
| 08 | The address space as generated code, and what that costs | planned |
| 09 | Build a client: discover, read, subscribe | planned |

**"Planned" means it does not exist.** It is in the table so the shape of the
course is visible, not so it looks further along than it is. The same convention
`sql/README.md` uses for its `04-expert` stage.

## What these lessons are honest about

The point of a course is to be right, including about the parts that are wrong.
Eleven things are wrong or missing in this implementation, and each gets its own
lesson rather than a footnote:

1. **The address space is not conformant.** `add_variable()` creates a
   `BaseDataVariableType`, not an `AnalogItemType`, and the unit and range are
   bolted on as ad-hoc properties with names the specification does not use:
   `EngineeringUnits` is a bare `Int32` where the spec requires an
   `EUInformation` struct, and there is no `EURange` at all. The information is
   present and readable — by *this project's* client — and invisible to any
   client that walks the standard type hierarchy, which is what a discovery tool
   or a generic historian integration does. So the docstring's "the client is
   told what the number means" is true of our client and false of the protocol.
   One cause, three symptoms, and the fix is a few lines of `asyncua`.
   → **lesson 02**
2. **Every numeric value is a `Double`.** `_variant_type()` at
   `softplc/servers/opcua.py:130` takes an engineering unit and ignores it,
   returning `Double` for everything. So the storm flag — `{Boolean}` in the
   contract — is published as a floating-point number, and a pH and a cubic
   metre per hour are indistinguishable by type. → **lesson 02**
3. **A failing sensor publishes `Uncertain`, not `Bad`** — and cannot do
   otherwise. `publish()` at `softplc/servers/opcua.py:402` is
   `Good if quality == 0 else Uncertain`: one branch for two states, so a
   `Bad` is downgraded on the way out and the gateway's
   `if quality == QUALITY_BAD: continue` branch is unreachable.
   `grep -c 'StatusCodes.Bad' softplc/servers/opcua.py` returns **0**, while the
   docstring says a failing sensor reports `Bad`. → **lesson 03**
4. **Nothing produces a `Bad` in the first place.** `softplc/process/plant.py:469`
   is the only construction of a `PlantSnapshot` and it passes `quality={}`
   unconditionally; the sole writer of a non-zero quality in the whole
   simulation is `softplc/faults/engine.py:458`, and it writes
   `QUALITY_UNCERTAIN`. So `QUALITY_BAD` is a constant with no producer, and no
   fault in the library — drift, flatline, stuck-high, TSS — degrades to `Bad`.
   → **lesson 03**
5. **A never-measured signal is indistinguishable from a healthy one.** Every
   variable is constructed at `sig.normal_low` with a `Good` status, so a fresh
   server reports DO at 1.5 (the bottom of 1.5–3.0), blowers at 600 rpm (the
   bottom of 600–1800, which to a threshold means *running*) and influent at
   200 m³/h — all `Good`, all with a current `SourceTimestamp`. This is the
   project's own "plausible wrong number" class, except that it is the server's
   *default state* rather than a bug in a query, and an invented number inside
   the expected range is harder to catch than one outside it. → **lesson 03**
6. **The obvious read raises on a degraded value.**
   `read_data_value()` defaults to `raise_on_bad_status=True`, so an `Uncertain`
   value arrives as a `UaStatusCodeError` with the number still sitting in the
   response. A client that catches and discards throws away the value *and* the
   reason — the exact outcome `gateway/clients/opcua_client.py` warns against.
   `tools/opcua_browser.py` calls it with the default at two sites, so the tool
   you would reach for to see a fault cannot show one. → **lesson 03**
7. **The engineering range is advisory, and the check for it is dead code.** A
   client can write 99.0 mg/L to a DO setpoint whose contract range is
   0.5–6.0 — 16× the maximum — and the write is accepted, because
   `EUInformation` is advisory and `asyncua` does not enforce it. The one place
   that *does* check, `OpcUaServer.write_value()`, **has no caller**: a wire
   write is handled by `asyncua` setting the node directly, so the range check
   never runs. The docstring calls it "a courtesy for in-process callers" when
   there are none. → **lesson 05**
8. **Every client write is inert anyway.** The process model is the only source
   of truth and republishes every cycle, and `softplc/main.py:131` aliases the
   OPC UA address space as `self._space`, so any write is silently overwritten
   within one scan. The DO controller reads its own dataclass field
   (`units.py:918`), so no write ever reached a control loop in the first place.
   The write surface is 2 of 57 signals, is decorative, and the project's only
   real write path is the Node-RED flow's **Modbus** write. It fails in the safe
   direction — an absurd setpoint is ignored rather than applied — but
   accidentally, and nothing says so. → **lesson 05**
9. **`RunState` is zero until the process model drives it.** A bare
   `OpcUaServer` — a unit test, or the snippet gate — publishes 22 pieces of
   equipment all reading `0`, which is indistinguishable from 22 stopped motors.
   → **lesson 01**, where it is the closing example
10. **Nothing in the product subscribes, and three comments say otherwise.** The
   docstring sells subscriptions as the reason to prefer OPC UA over Modbus;
   `git grep -c create_subscription` finds exactly one hit in the whole
   repository, in `tools/opcua_browser.py`. The gateway polls a batch read once a
   second (`gateway/main.py:82`). The class docstring describes "one internal
   subscription with a data-change filter" — the implementation is
   `self._dirty: set[str]`. And `mark_dirty` says the scan loop filters on the
   deadband, where it filters on exact equality; the only deadband is
   `gateway/deadband.py`, on the wrong side of the wire. → **lesson 04**, which
   also shows the cost side: a filter is negotiated *per subscription*, so two
   clients on one node hold different values — measured at 2.39 and 2.0 at the
   same instant, both `Good`, with no staleness marker on either.
11. **The concurrency documentation contradicts itself, and the failure mode is
   silence.** `ARCHITECTURE.md:108` heads a section *"One event loop, two
   protocols, one thread"*; the process has two loops and three threads
   (`softplc-loop`, `modbus-tcp`, and the main thread running `asyncio.run`).
   The body text is accurate and good — the heading is the one false word, and a
   heading cannot be fixed by reading further down. More importantly, breaking
   loop affinity **raises nothing**: `publish()` from the wrong loop returns
   cleanly and orphans the server's tasks (*"Task was destroyed but it is
   pending!"*), which is a client timeout that reads as a network fault.
   → **lesson 06**, which also measures why the thread exists at all — an
   unawaited blocking call gives **5 ticks where an awaited one gives 32**.

## Related

- [`docs/SECURITY.md`](../../docs/SECURITY.md) — the threat model, and the three
  documented gaps, two of which are on this list
- [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) — where OPC UA sits, and
  why the gateway is asyncio
- [`docs/GLOSSARY.md`](../../docs/GLOSSARY.md) — the terms, briefly
- [`tools/opcua_browser.py`](../../tools/opcua_browser.py) — the same server,
  browsable by hand
- [The SQL course](../../sql/README.md) — 21 lessons, same conventions
