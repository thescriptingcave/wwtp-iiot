# The OPC UA course

Nine lessons on the protocol, run against a live OPC UA server.

**Start at [01 — How a client actually talks to an OPC UA server](01-talking-to-a-server.md).**
It assumes you know nothing about OPC UA, nothing about this repository, and
nothing about the plant it runs against. Everything in it works against any OPC UA
server.

There is a second set of lessons in [audit/](audit/README.md) about this
repository specifically. They are worth reading if you are reviewing this project
or changing it, and they are the wrong door if you are here to learn the
protocol. The distinction is deliberate and it took nine lessons written the wrong
way to see it — the story is at the bottom of this file.

## Why this course exists at all

OPC UA is the primary protocol in this project: 500 lines of server, a client, a
browser tool, 370 lines of tests. For a long time it had **no lessons at all**.
Every mention of it was an architecture line, a command to run, a
`45 nodes assembled` message, or a glossary entry.

The cause was the same one that shaped the dashboards, and it is worth stating
plainly because it is a mistake worth not repeating:

> **The two things most worth learning are the two things this project automated.**

The SQL course works because the SQL is hand-written and runs against a live
database — the artefact *is* the lesson. The OPC UA address space was generated,
so you could read the generator and learn the answer, and could not learn how to
*find* it.

The nine audit lessons are what came out of fixing that. They work, every snippet
runs, and they read as a post-mortem rather than a course — which is a failure no
green tick would have caught. **A thing that works perfectly can still be the
wrong thing**, and these nine are kept for the record rather than deleted.

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
output above it goes stale.

That limitation is why every expected output in both folders was written by
running the snippet and pasting what came back, never by predicting it. It earns
its keep more often than you would expect: writing lesson 02, a claimed
`DisplayName` of "Dissolved oxygen" turned out to be `do_mg_l`, a subscription
demo claimed zero notifications and produced sixty-five, and a browse loop that
looked correct walked the whole tree backwards because it ignored `IsForward`.
All three were caught by running, and all three would have been caught by nothing
else.

## The lessons

**Start at 01.** These nine teach the protocol. They work against any OPC UA
server, assume no knowledge of this repository, and each one is a concept taught
by doing it.

| # | Lesson | What it is about |
|---|---|---|
| 01 | [How a client actually talks to an OPC UA server](01-talking-to-a-server.md) | the whole surface, once: connect, browse, walk, read, subscribe, disconnect |
| 02 | [Nodes, classes and types](02-nodes-and-types.md) | what a thing *is*: `Object` vs `Variable`, type definitions, references, namespaces |
| 03 | [Reading data properly](03-reading-data.md) | attributes, timestamps, batch reads, and the read that raises |
| 04 | [Data types, units and ranges](04-units-and-ranges.md) | why a `Double` is not a measurement, and the range nobody enforces |
| 05 | [Subscriptions in depth](05-subscriptions-in-depth.md) | intervals vs filters, and what a deadband really discards |
| 06 | [Status codes and quality](06-status-and-quality.md) | `Uncertain` is not a softer `Bad`, and why you keep the value |
| 07 | [Writing values](07-writing.md) | planned |
| 08 | [Finding things at scale](08-discovery.md) | planned |
| 09 | [Building a client that survives](09-a-client-that-survives.md) | planned |

**"Planned" means it does not exist.** It is in the table so the shape of the
course is visible, not so it looks further along than it is. The same convention
`sql/README.md` uses for its `04-expert` stage.

## The audit — nine lessons about *this* repository

The first attempt at a course here, and it failed as one. Those nine lessons each
take a question about this server and answer it with measurements, which teaches a
reader who is auditing this codebase and leaves a reader who wants to learn OPC UA
with nothing. They are kept in [audit/](audit/README.md) because they are how the
findings below were produced, and a finding nobody can re-check is a rumour.

**Every snippet in both folders runs** against a live server, by
`tools/check_lessons.py`.

## What these lessons are honest about

The point of a course is to be right, including about the parts that are wrong.
Fourteen things are wrong or missing in this implementation, and each gets its own
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
12. **The security document ranks the wrong asset, and its top fix is already in
   the codebase.** The server configures *no* security — no
   `set_security_policy`, no `allow_anonymous`, no certificate — so a client
   negotiates `SecurityPolicyNone` and an anonymous walk reads the site name,
   the design flow, **all five permit limits**, 8 areas and all 57 signals with
   their units and bands. Meanwhile `SECURITY.md`'s number-one ranked asset is
   "integrity of the control path", which **nobody can attack** because lesson 05
   showed the path is inert; and its number-one remediation — "write handlers
   that reject out-of-range values" — is `OpcUaServer.write_value()`, which
   exists, is tested, and has no caller. Confidentiality of the compliance
   envelope appears nowhere in the ranked list. → **lesson 07**
13. **The largest generated artifact in the project is the one nobody can
   review.** The address space is 686 nodes — 12 per signal, 4 levels deep — built
   by 500 lines from `contracts/tags.yaml`, and it has **no file and no drift
   gate**, while the tag list, the flows, the dashboards and the extracted
   queries all have one. Generation also concentrates error: 57 signals share
   **1 property set and 1 DataType**, so one wrong decision corrupts the whole
   plant and looks identical to one right decision. Fourteen decisions are buried
   in that generator and three are unambiguously wrong. The fix is neither
   hand-writing nor abandoning generation — it is publishing the serialised space
   as a fifth drift-gated artifact, which would have caught all four defects as
   *values in a table* instead of lines in a loop. → **lesson 08**
14. ~~**The one field a client would use to date a reading is destroyed on every
   write.**~~ **FIXED.** `asyncua` stamps `SourceTimestamp` when a node is
   constructed; `publish()` wrote a `DataValue` carrying only a `Value` and a
   `StatusCode`, so the timestamp was *cleared* on every publish and **no client
   could date a reading from this server**. `publish()` and `_flush_states()` now
   stamp `SourceTimestamp`, via a `_utcnow()` helper that exists because
   `ua.DateTime.now()` is *not* usable here — it returns a naive local time and
   `asyncua` encodes naive datetimes against a UTC epoch, so the machine's offset
   becomes part of the value. The first version of the fix had exactly that bug
   and was off by seven hours; a test comparing a published timestamp against the
   wall clock is what caught it. → **lesson 09**, which ships
   [`tools/opcua_minimal_client.py`](../../tools/opcua_minimal_client.py) — a
   200-line reference client doing the other four things right, and showing why
   [`tools/opcua_browser.py`](../../tools/opcua_browser.py) crashes on the first
   degraded sensor.

## Related

- [`docs/SECURITY.md`](../../docs/SECURITY.md) — the threat model, and the three
  documented gaps, two of which are on this list
- [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) — where OPC UA sits, and
  why the gateway is asyncio
- [`docs/GLOSSARY.md`](../../docs/GLOSSARY.md) — the terms, briefly
- [`tools/opcua_browser.py`](../../tools/opcua_browser.py) — the same server,
  browsable by hand
- [The SQL course](../../sql/README.md) — 21 lessons, same conventions
