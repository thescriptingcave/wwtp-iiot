# 05 — Subscriptions in depth

**Next:** [06 — Status codes and quality](06-status-and-quality.md) · [Back to the course](README.md) · [Previous: 04](04-units-and-ranges.md)

[Lesson 01](01-talking-to-a-server.md) created a subscription and watched it stay
silent. That was the easy case. This lesson is about the four settings that
control what a subscription actually delivers, and it opens with a measurement
that contradicts most people's expectations.

**A note on the numbers.** These are counts from a controlled experiment — a
server nobody else is driving — so they are stable. Notification counts on a live
plant are not, and the last snippet says why.

## The question

> I subscribed to a signal. How many notifications do I get, and what decides it?

## Two intervals, and neither one is the answer

A subscription has two timers, and the distinction is the whole thing:

- **Publishing interval** — how often the server may *send* you a batch. You asked
  for 100 ms; the server may revise it.
- **Sampling interval** — how often the server *looks at* the value to see whether
  it changed. The filter is applied here.

Here is the experiment. Thirty changes to one signal over 600 ms, varying each
setting, counting what arrives:

```python
import asyncio
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

class Counter:
    def __init__(self):
        self.count = 0
    def datachange_notification(self, node, value, data):
        self.count += 1

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
node = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

async def trial(label, publishing=100, deadband=None, sampling=None):
    counter = Counter()
    subscription = await client.create_subscription(publishing, counter)
    if deadband is None:
        await subscription.subscribe_data_change(node, sampling_interval=sampling or 50)
    else:
        await subscription.deadband_monitor(node, deadband, ua.DeadbandType.Absolute)
    for i in range(30):
        server.set_value("AERATION:AHU-1:DO", 2.0 + i * 0.01, 0)
        await server.publish()
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.6)
    await subscription.delete()
    print(f"  {label:44} {counter.count:3} notifications")

await trial("no filter, publishing 50ms", publishing=50)
await trial("no filter, publishing 500ms", publishing=500)
await trial("no filter, sampling 10ms", sampling=10)
await trial("no filter, sampling 2000ms", sampling=2000)
await trial("deadband 0.05", deadband=0.05)
await trial("deadband 0.5", deadband=0.5)
await client.disconnect()
await server.stop()
```

```
  no filter, publishing 50ms                    31 notifications
  no filter, publishing 500ms                   31 notifications
  no filter, sampling 10ms                      31 notifications
  no filter, sampling 2000ms                    31 notifications
  deadband 0.05                                  2 notifications
  deadband 0.5                                   1 notifications
```

**Read that table before drawing any conclusion from it.**

The two intervals changed nothing: 31 notifications whether the server looked at
the value every 10 ms or every 2 seconds, and whether it was allowed to send every
50 ms or every 500 ms. **The deadband changed everything: 31 → 2.**

That is not a quirk of this server, and the reason is worth internalising:

> **A notification count is not a measure of how often the value changed. It is a
> measure of how often the server thought the change was worth reporting.**

The intervals control *delivery* — when the server is allowed to talk, and how
often it looks. Neither decides *whether* to tell you. Only the filter does.

So a client that "reduces load" by raising the publishing interval is tuning the
wrong thing, and one that raises the sampling interval to "get fresher data" is
doing the opposite of what they think.

## The server revises what you asked for

Ask for something aggressive and the server may answer with something slower. The
server's response is in the create result, and it is not optional to look at:

```python
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()

result = await client.uaclient.create_subscription(
    ua.CreateSubscriptionParameters(
        RequestedPublishingInterval=5,      # aggressive: 5 ms
        RequestedLifetimeCount=60,
        RequestedMaxKeepAliveCount=20),
    ua.CreateSubscriptionResponse())
print(f"  we asked for     5 ms")
print(f"  the server gave {result.RevisedPublishingInterval} ms")
await client.disconnect()
await server.stop()
```

```
  we asked for     5 ms
  the server gave 5.0 ms
```

**It granted it, and that is the finding.** This server will happily give you a
5 ms publishing interval, which is a denial-of-service request against a device
that may be running a 20 ms control loop. The specification expects servers to
*revise* such requests downward, and they do — a real PLC will typically floor
you at its own scan time and print a revision you have to read.

The point is not that this server is permissive. It is that **`RevisedPublishingInterval`
is a field you have to read**, and the same is true of every negotiated setting:
the lifetime, the keepalive count, the maximum notifications per publish. A client
that assumes its request was granted has a subscription it may not have, and finds
out when its data looks subtly wrong and nothing says why.

To see the revision yourself, ask for something the server cannot give — and
`asyncua` warns on stderr whenever the values differ, which is helpful right up
until it is scrolled off the top of a log.

## The pitfall: a deadband is not a filter on delivery, it is a filter on *information*

The deadband cut 31 notifications to 2, which looks like pure win. Here is what
the client actually ended up holding:

```python
import asyncio
from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

class Recorder:
    def __init__(self):
        self.values = []
    def datachange_notification(self, node, value, data):
        self.values.append(value)

server = OpcUaServer(endpoint=f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await server.start()
await server.wait_ready()
client = Client(f"opc.tcp://127.0.0.1:{port}/wwtp/server/")
await client.connect()
node = client.get_node(server.space.variables["AERATION:AHU-1:DO"].node.nodeid)

recorder = Recorder()
subscription = await client.create_subscription(100, recorder)
await subscription.deadband_monitor(node, 0.05, ua.DeadbandType.Absolute)

for i in range(30):
    server.set_value("AERATION:AHU-1:DO", 2.0 + i * 0.01, 0)
    await server.publish()
    await asyncio.sleep(0.02)
await asyncio.sleep(0.6)
await subscription.delete()

print(f"  the signal was driven through 30 values, 2.00 up to 2.29")
print(f"  the client was told: {recorder.values}")
print("  the client did not see the 2.0 -> 2.29 climb at all.")
await client.disconnect()
await server.stop()
```

```
  the signal was driven through 30 values, 2.00 up to 2.29
  the client was told: [1.5, 2.0]
  the client did not see the 2.0 -> 2.29 climb at all.
```

**Look at what the client holds: `1.5` and `2.0`.** The first is the value before
the test started. The second is the *start* of the climb. The climb itself — the
whole 2.00 to 2.29 — never arrived, because a 0.05 deadband measured from the
last reported value was not satisfied until the end.

So a client on this subscription has a value that is **0.29 mg/L out of date and
does not know it**, and there is nothing in the notification that says so. If you
are charting that, your chart has a flat line across a moment when the process was
moving the whole time. If you are alarming on it, a spike that crossed your
threshold and came back is **invisible**.

That is the honest shape of a deadband, and it is worse than "you missed some
intermediate values": with an absolute deadband you can end up holding a value
that is arbitrarily old, and nothing marks it. The deadband is measured from the
**last reported** value, not from the last sampled one, so the further the signal
drifts the more you accumulate unseen change.

That is the trade, and it is a real one:

| you want | use | you accept |
|---|---|---|
| never miss a change | no filter | every sample, including the boring ones |
| low bandwidth, low CPU | a deadband | the shape between notifications is lost |
| a bounded queue | set `queue_size` | **the oldest changes are dropped** |

The third row is the one people forget. A subscription has a finite queue, and
when your handler is slower than the changes arrive, the server drops the oldest
and tells you it did. That is the correct behaviour — the alternative is unbounded
memory on the device — but it means **a slow handler silently loses data**. If your
handler does anything slow, hand the value to a queue and return immediately.

## Choosing a deadband

A deadband is the difference between the last reported value and the current one
that the server ignores. Too small and you are back to every sample; too large
and you are blind to real movement.

The number comes from the process, not from the protocol. A dissolved oxygen probe
in a basin does not move meaningfully over 0.02 mg/L, so reporting every 0.02 is
waste. But the *right* answer is a question about your data, not a constant:

```python
from softplc.contract import contract
c = contract()
print(f"  {'signal':28} {'unit':10} deadband")
for signal_id in ("AERATION:AHU-1:DO", "AERATION:AHU-1:AIR_FLOW",
                  "INFLUENT:FLOW:FLOW", "EFFLUENT:FLOW:PH"):
    signal = c.signal(signal_id)
    low, high = signal.normal_low, signal.normal_high
    width = (high - low) / 1000.0
    print(f"  {signal.field:28} {signal.eu:10} {width:.4f}  "
          f"(0.1 % of its {low:g}-{high:g} range)")
```

```
  signal                      unit      deadband
  do_mg_l                     mg/L      0.0015  (0.1 % of its 1.5-3 range)
  air_flow_m3h                m3/h      2.0000  (0.1 % of its 2000-2000 range)
  flow_m3h                    m3/h      2.0000  (0.1 % of its 200-2200 range)
  ph                          {pH}      0.0070  (0.1 % of its 6-8.5 range)
```

A percentage of the range is a defensible starting point, and it is a *starting
point*: what you actually want is "the smallest change anyone would care about",
which is a question about your process and usually about your alarm thresholds.

Note that the deadband is **absolute by default** — a fixed number, not a
percentage. `DeadbandType.Percent` makes it relative to the range, which is
sometimes what you want and is a different unit, so read which one you are
setting.

## What to take away

- **Intervals control delivery; the filter controls information.** Raising the
  publishing interval to reduce load tunes the wrong thing, and the count will
  not change.
- **Read `RevisedPublishingInterval`.** The server may give you ten times
  slower than you asked for, successfully, and a client that ignores it has a
  subscription it does not have.
- **A deadband discards the shape of the signal.** Thirty changes became two
  notifications, and a threshold crossed in between is invisible. Use it when the
  bandwidth matters, not by default.
- **A subscription has a bounded queue and it drops the oldest.** A slow handler
  loses data silently.
- **A deadband is absolute unless you ask for percent.** Same word, different
  unit.

**If you skip this lesson**, lesson 06's advice about not discarding data will
sound theoretical, and you will have already built the mechanism that discards it.

---

**A note on this server.** It publishes every changed value with no deadband of
its own, so the count of 31 is "one per change, plus the initial value on
subscribe". A server that sampled on a schedule would deliver fewer and tell you
nothing was missed — which is the case where the client's job is hardest, and
which the `RevisedSamplingInterval` is how you detect.

**Next:** [06 — Status codes and quality](06-status-and-quality.md)
