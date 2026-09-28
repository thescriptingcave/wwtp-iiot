# 06 — Why OPC UA is asyncio and Modbus is not

**Next:** [07 — Security, and the warning on every start](07-security.md) · [Back to the course](../README.md) · [Previous: 05](05-writing.md)

This is the first lesson where the code is mostly right, and the argument it
makes is the one that justifies the whole shape of `softplc/main.py`. The
`ARCHITECTURE.md` claim is:

> OPC UA is asyncio. pymodbus's TCP server is a blocking thread with no
> asynchronous form. Rather than fight it, the gateway runs the asyncio loop for
> OPC UA and hands the Modbus client to a worker thread, with a queue between
> them.

Three threads, two event loops, and a heading that says otherwise. Let me start
with the measurement that makes all of it necessary, because it is the most
useful thing in this course and it takes nine lines.

## The question

> What actually happens to an OPC UA client when a Modbus read runs on the same
> thread?

## A blocking call stops everything

An asyncio event loop runs one task at a time. A coroutine that yields — at an
`await` — hands control back so another task can run. A function that does *not*
yield holds the loop until it returns, and nothing else happens: no other client,
no other task, no timers.

Here is a heartbeat ticking every 10 ms, and three ways of spending 300 ms:

```python
import asyncio, time

async def heartbeat(stop, ticks):
    while not stop.is_set():
        ticks.append(time.perf_counter())
        await asyncio.sleep(0.01)

def blocking():
    time.sleep(0.3)

async def bare():
    blocking()                          # no await

async def yielding():
    await asyncio.sleep(0.3)

async def offloaded():
    await asyncio.to_thread(blocking)

async def trial(name, fn):
    stop, ticks = asyncio.Event(), []
    hb = asyncio.create_task(heartbeat(stop, ticks))
    await asyncio.sleep(0.05)           # let it settle
    start = time.perf_counter()
    await fn()
    elapsed = time.perf_counter() - start
    stop.set()
    await hb
    print(f"  {name:36} {elapsed*1000:6.1f} ms, ticked {len(ticks):3} times")

await trial("time.sleep(0.3), not awaited", bare)
await trial("await asyncio.sleep(0.3)", yielding)
await trial("await asyncio.to_thread(blocking)", offloaded)
```

```
  time.sleep(0.3), not awaited         305.1 ms, ticked   5 times
  await asyncio.sleep(0.3)             301.1 ms, ticked  32 times
  await asyncio.to_thread(blocking)    307.0 ms, ticked  33 times
```

**Same 300 ms. Five ticks, or thirty-two.** The first row is the whole argument
for the thread in `softplc/main.py`: a Modbus read that blocked the loop would
not slow the OPC UA server down, it would stop it dead, and every connected
client would see a timeout.

That is also why the timing column is useless on its own. All three took about
the same wall time. **The damage is not visible in how long the blocking call
took** — it is visible only in what else failed to happen meanwhile, which is
why this class of bug is so hard to find and so easy to misdiagnose as a network
problem.

## `asyncio.to_thread` exists, and this project still uses a raw thread

The third row is the modern answer: hand the blocking call to a thread, get the
result back with an `await`, and the loop never notices. Why not use it for
Modbus?

Because `StartTcpServer` does not return. It is not a call that blocks for a
while and then gives an answer — it opens a socket and serves until the process
dies. `to_thread` still occupies a real thread for the life of the process, and
the work cannot be cancelled once it has left the loop, so you get exactly what
you started with plus a layer of indirection. A daemon thread is the honest
mechanism, and `softplc/servers/modbus_server.py:180` says so:

> A daemon is the honest mechanism: the listener lives until the process ends.

Which has a consequence at shutdown, stated plainly rather than papered over:

<!-- softplc/servers/modbus_server.py:219-226 — a source excerpt, not a session -->
<!-- check: skip -->
```python
async def stop(self) -> None:
    """Mark the server down.

    The listening thread cannot actually be stopped — pymodbus offers no
    shutdown hook — so this only clears the running flag. The socket closes
    when the process exits. Stating that plainly is better than pretending a
    cancellation would work.
    """
    self._running = False
    self._context = None
```

`stop()` does not stop the server. It renames a variable. That is not a bug — it
is what the library allows — and the value of writing it down is that nobody
later adds a test asserting the port is closed.

## Loop affinity, which fails silently

The other half of the design is subtler and much more dangerous. `softplc/main.py`
gives the OPC UA server **its own event loop on a background thread**, and the
docstring gives the reason:

> That is not incidental: the OPC UA server holds state bound to the loop it was
> created on, so starting it with `asyncio.run` and then talking to it from a
> second loop produces a connection timeout that looks like a networking fault.

Read that again: **a connection timeout that looks like a networking fault.** No
exception. No traceback. Just a client that stops being answered, and an engineer
who goes looking at the firewall.

Here is what actually happens. The server is built and used correctly on its own
loop, then used wrongly from a second one:

```python
import asyncio, threading
from softplc.servers.opcua import OpcUaServer

# A dedicated background loop — call it B. The snippet itself is already
# running in a loop, which is loop A. Two loops, deliberately.
loop_b = asyncio.new_event_loop()
ready = threading.Event()

def serve():
    asyncio.set_event_loop(loop_b)
    ready.set()
    loop_b.run_forever()

threading.Thread(target=serve, daemon=True).start()
ready.wait()

box = {}
async def build():
    # `port` is a free port the runner supplies, because the runner's own server
    # holds 48400 and a second bind there fails with "address already in use" —
    # and a hardcoded port here collided with another lesson before.
    s = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/x/")
    await s.start()
    await s.wait_ready()
    box["s"] = s

asyncio.run_coroutine_threadsafe(build(), loop_b).result(10)
s = box["s"]

print("  this snippet's loop is NOT the loop the server was built on\n")
n = asyncio.run_coroutine_threadsafe(s.publish(), loop_b).result(10)
print(f"  publish() on the owning loop   -> {n}, no error")
s.set_value("AERATION:AHU-1:DO", 5.0, 0)
await s.publish()
print("  publish() on the wrong loop    -> no exception, no log, no bad return")
loop_b.call_soon_threadsafe(loop_b.stop)
```

```
  this snippet's loop is NOT the loop the server was built on

  publish() on the owning loop   -> 0, no error
  publish() on the wrong loop    -> no exception, no log, no bad return
Task was destroyed but it is pending!
task: <Task pending name='Task-4' coro=<InternalServer._set_current_time_loop() ...
Task was destroyed but it is pending!
task: <Task pending name='Task-6' coro=<BinaryServer._close_task_loop() ...
```

**No exception. No log line. No non-zero return.** `publish()` succeeds, returns a
count, and the server's internal tasks are quietly orphaned and then garbage
collected mid-flight. A second later the client times out and the fault is
attributed to the network.t. A second later the client times out and the fault is
attributed to the network.

That is the argument for `SoftPlc._call()`:

<!-- softplc/main.py:119-121 — the whole bridge, in three lines -->
<!-- check: skip -->
```python
def _call(self, coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
    """Run a coroutine on the PLC's loop from any thread."""
    return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)
```

Every cross-thread entry into the PLC's loop goes through it, which is the only
reason the design holds together. Note also `timeout=30.0`: a thread boundary
with no timeout is a hang with extra steps, and this is a plant simulator that
people leave running.

## The sync/async seam

One more boundary, and it is the one that produced a documented scar. The scan
loop is synchronous — it steps the model and returns a snapshot — while every
`asyncua` setter is a coroutine. So values are **staged** on the way in and
**flushed** on the way out:

```python
from softplc.servers.opcua import OpcUaServer

# set_value is a plain method; publish is a coroutine
node = space.variables["AERATION:AHU-1:DO"].node
print("before:", await client.get_node(node.nodeid).read_value())

# what happens if you forget to await an asyncua setter
coro = client.get_node(node.nodeid).write_value(
    ua.DataValue(ua.Variant(9.9, ua.VariantType.Double)))
print("forgot to await ->", type(coro).__name__, "and nothing was written")
del coro

print("after: ", await client.get_node(node.nodeid).read_value())
print("the warning asyncio printed is the only clue you get")
```

```
before: 1.5
forgot to await -> coroutine and nothing was written
after:  1.5
```

`RuntimeWarning: coroutine 'Node.write_value' was never awaited`, on stderr, from
a library's internals. And that is exactly why `set_equipment_state` documents
the seam rather than apologising for it:

> Staged rather than written, because the scan loop is synchronous and
> `asyncua`'s setters are coroutines. Calling one without awaiting produces a
> RuntimeWarning and silently does nothing — so staging is not a convenience
> here, it is the only correct way to cross that boundary.

The general lesson is the one every asyncio codebase learns the hard way:
**unawaited coroutines fail silently.** They do not raise, they do not log at
error level, and the value simply does not change — which in this project looks
exactly like the deadband problem from lesson 03 and the fabricated `normal_low`
from lesson 03's first half. A value that is stale and a value that was never
sent are the same observation.

## Three threads, two loops — and a heading that says otherwise

Here is the whole shape of the process:

| # | thread | runs | why |
|---|---|---|---|
| 1 | main | `asyncio.run(_run(args))` | the scan loop and the report |
| 2 | `softplc-loop` | `self._loop.run_forever()` | the OPC UA server's own loop |
| 3 | `modbus-tcp` | `StartTcpServer` | pymodbus, blocking, daemon |

`ARCHITECTURE.md:108` heads that section:

> ## One event loop, two protocols, one thread

It is two event loops and three threads, and the paragraph immediately below
describes the second loop and the second thread. So the heading is contradicted
by its own body — which is the same failure as lessons 02 to 05, and worth
naming as a pattern rather than as a slip: **in this area the prose describes an
intended design, and the code describes the built one, and they differ.** Three
of the four findings in this lesson are a comment that does not match the code
beneath it.

The body text, unlike the heading, is accurate and unusually good. "The scan
loop is single-threaded and the queue is the only shared state" is the actual
invariant, and it is the sentence that would let somebody reason about the
concurrency. A heading cannot be fixed by reading further down; that is what
makes it the wrong place for the one false word.

## What is actually true

| claim | verdict |
|---|---|
| A blocking call would stall the OPC UA server | **yes** — 5 ticks instead of 32, measured |
| Modbus therefore gets its own thread | **yes** — and a daemon, because it cannot be stopped |
| The OPC UA server needs its own loop | **yes** — and breaking that fails *silently* |
| The cross-thread bridge has a timeout | **yes** — 30 s, in `_call()` |
| Values are staged across the sync/async seam | **yes** — and unawaited coroutines fail silently |
| There is one event loop and one thread | **no** — there are two and three |

This is the one part of the protocol work where the design is defensible on its
own terms, and it is worth being clear that the lesson's findings are about
*documentation accuracy* rather than about behaviour. A reader who trusts the
heading will look for one loop and one thread and will not find them; a reader
who reads the body will find a better explanation of this codebase's concurrency
than most projects manage.

The one thing to fix is the heading, and the one thing to keep is the habit the
comments demonstrate — three separate places in this code say plainly what a
mechanism *cannot* do, which is the reason any of it is debuggable at all.

**Next:** [07 — Security, and the warning on every start](07-security.md)
