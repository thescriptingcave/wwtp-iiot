# The audit — nine lessons about this repository

**Not a course.** If you opened the OPC UA course to learn the protocol, you
want [the teaching lessons](../README.md), starting with
[01 — How a client actually talks to an OPC UA server](../01-talking-to-a-server.md).

These nine are a different thing. Each one takes a single question about **this
server** and answers it with measurements: what does the address space actually
contain, where is the deadband really applied, what can a client not tell apart.
They are how the fourteen findings in the [course README](../README.md) were
found, and they are kept because a finding nobody can re-check is a rumour.

They were the first attempt at a course here, and they failed as one. Nine times
in a row they asked "what did I get wrong" instead of "here is how this works",
which teaches a reader who is auditing this codebase and leaves a reader who
wants to learn OPC UA with nothing. Every snippet ran, every output was pasted
from a real run, and the gate was green throughout — and it still read as a
post-mortem. **A thing that works perfectly can still be the wrong thing**, and
that is worth recording rather than quietly replacing.

## What is in here

| # | Lesson | The question it answers |
|---|---|---|
| 01 | [The address space is a tree, and you can walk it](01-the-address-space.md) | What can a client discover without being told anything? |
| 02 | [Units, types, and the one lie in the type system](02-units-and-types.md) | Is the unit information a conformant client can find? |
| 03 | [Reading, and what a StatusCode is for](03-reading-and-quality.md) | If a sensor is failing, how would a client know? |
| 04 | [Subscriptions, and the feature this project does not use](04-subscriptions.md) | What does a subscription give you, and what does it take? |
| 05 | [Writing, and the difference between enforced and not](05-writing.md) | If an operator changes a setpoint, what happens? |
| 06 | [Why OPC UA is asyncio and Modbus is not](06-async.md) | What happens to a client when a Modbus read blocks? |
| 07 | [Security, and the warning on every start](07-security.md) | What does someone get by connecting today? |
| 08 | [The address space as generated code, and what that cost](08-generated.md) | Was generating it the right call? |
| 09 | [Build a client](09-build-a-client.md) | Can the safeguards actually be written? |

Each lesson is self-contained and each states plainly which of its claims are
now fixed and which are still open. Several findings have been closed since —
`SourceTimestamp` now gets published, the address space is serialised, the ports
bind loopback — and those lessons say so rather than being quietly deleted, because
"this was wrong, here is why, here is the fix" is the part worth reading.

## How to run them

Same as the teaching lessons, and the same gate:

```bash
uv run python tools/check_lessons.py --verbose
```

Every Python block here runs against a live server. The handful marked
`<!-- check: skip -->` are source excerpts or deliberately-broken examples, and
each carries a line saying which.

## What they are good for

If you are **reviewing this project** — as a reader deciding whether to trust it,
or as someone who has been asked to change it — these are the most useful files in
the repository. They are unusually specific, they name the line numbers, and they
tell you which of their own claims have since been fixed.

If you are **learning OPC UA**, they are the wrong door. Walk in through
[lesson 01](../01-talking-to-a-server.md) instead; it assumes nothing and works
against any server.
