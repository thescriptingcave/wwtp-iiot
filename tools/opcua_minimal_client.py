"""The smallest OPC UA client that is actually correct for this plant.

    python -m tools.opcua_minimal_client --endpoint opc.tcp://127.0.0.1:4840/wwtp/server/

Fourteen lessons' worth of findings live in here, and every one of them is a
safeguard rather than a feature. This is the client `tools/opcua_browser.py`
should have been and is not, because the browser is a *human* tool and this is a
*reference* one: 90 lines, no configuration, and five specific things done right.

## The five things

**1. Discover, never hardcode a NodeId.** Browse to the plant object by name
(lesson 01). A NodeId is an implementation detail of one server build; a browse
name is part of the model. `opcua_browser.py` documents this and then hardcodes
`2:AERATION` in places, which works right up until the address space is rebuilt.

**2. Batch-read, so the StatusCode survives.** `Node.read_data_value()` defaults
to `raise_on_bad_status=True`, so a degraded value arrives as an exception with
the number still in the response, and a caller who catches it loses the value
*and* the reason (lesson 03). A raw `uaclient.read()` returns every
`DataValue` with its status intact. The gateway already does this; the browser
does not, and therefore crashes on the first `Uncertain` signal.

**3. Distinguish measured from never-measured.** This is the one the protocol
cannot help with, and it took two states to write and needs three.

Every variable in this server is constructed at its `normal_low`, so a fresh
server reports dissolved oxygen at 1.5 mg/L — the bottom of a healthy band —
with a `Good` status and a `SourceTimestamp` of *now*, because `asyncua` stamps
it at construction (lesson 03). A client that prints 1.5 is lying by omission,
because nothing in the value says it was never measured.

So this client compares `SourceTimestamp` against the moment it connected, and
that handles the fresh-server case. Then a second discovery during development
of this file: `publish()` writes

    ua.DataValue(ua.Variant(entry.value, ua.VariantType.Double), StatusCode=status)

with **no `SourceTimestamp`**, so the field `asyncua` set at construction is
*cleared* on every publish. Verified against a live server:

```
  fresh server            value=1.5  SourceTimestamp=1790564165.416149
  after publish of 2.4    value=2.4  SourceTimestamp=None
  after publish of 9.9    value=9.9  SourceTimestamp=None
```

So the only field that could distinguish a placeholder from a measurement is
destroyed by the server's own write path, and a client that treats `None` as
"unmeasured" is wrong in the other direction — it would report a perfectly good
2.4 mg/L as never measured, forever, and eventually get ignored.

Hence **three** freshness verdicts, not two:

| verdict | condition | what it means |
|---|---|---|
| `measured` | a timestamp at or after we connected | a real reading |
| `unmeasured` | a timestamp *before* we connected | the constructor's placeholder |
| `untimestamped` | no timestamp | the server is not timestamping; age unknown |

`untimestamped` is the honest answer on this server once anything has been
published, and the fix is one line in `publish()` — set
`SourceTimestamp=ua.DateTime.now()`. Until then a client cannot know how old a
value is, and this one says so rather than guessing.

**4. Never silently downgrade quality.** `Good` and `Uncertain` are reported
separately and never collapsed. Note that on this server `Bad` cannot occur at
all (lesson 03: `publish()` maps every non-zero quality to `Uncertain`), so a
client that only handled two states is not being cautious, it is describing this
server rather than OPC UA.

**5. If a subscription is filtered, say so.** A `DataChangeFilter` is negotiated
*per subscription*, so two clients on one node hold different values at the same
instant — measured at 2.39 and 2.0, both `Good`, with no staleness marker on
either (lesson 04). A client that filters without reporting the deadband is
quietly holding a stale picture. This one subscribes unfiltered and says why.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from asyncua import Client, ua

log = logging.getLogger("opcua.minimal")

#: The marker used to find the plant object. A browse name, not a NodeId, so the
#: client survives the server being rebuilt.
PLANT_MARKER = "PLANT-A"


@dataclass
class Reading:
    """One value, with everything needed to decide whether to believe it."""

    signal: str
    value: float
    status: str
    freshness: str          # "measured" | "unmeasured" | "untimestamped"

    def __str__(self) -> str:
        # The freshness verdict changes what the reader is allowed to conclude,
        # so it has to be visible in the output. A number printed alone is the
        # defect this client exists to avoid.
        shown = f"{self.value:g}" if self.freshness == "measured" \
            else f"{self.value:g} [{self.freshness}]"
        return f"  {self.signal:34} {shown:>30}  {self.status}"


@dataclass
class MinimalClient:
    """Discover, read, and refuse to guess. See the module docstring."""

    endpoint: str
    plant: Any = None
    connected_at: float = 0.0
    nodes: dict[str, Any] = field(default_factory=dict)

    async def connect(self) -> bool:
        """Connect and locate the plant by browsing for it."""
        self._client = Client(url=self.endpoint)
        await self._client.connect()
        self.connected_at = time.time()
        self.plant = await self._find_plant()
        if self.plant is None:
            log.error("no plant object under Objects at %s", self.endpoint)
            await self.close()
            return False
        name = await self.plant.read_browse_name()
        log.info("connected to %s", name)
        return True

    async def _find_plant(self) -> Any:
        """Find the plant by browse name.

        Deliberately a search rather than a lookup: lesson 01's whole point is
        that a client should be able to find things it was never told about.
        """
        for child in await self._client.nodes.objects.get_children():
            if PLANT_MARKER in (await child.read_browse_name()).Name:
                return child
        return None

    async def resolve(self, *path: str) -> Any:
        """Walk a browse path from the plant, e.g. `("AERATION", "AHU-1", "DO")`.

        The namespace index is read off the node we discovered rather than
        assumed. It happens to be 2 on this server, but hardcoding it is how a
        client ends up browsing the standard address space and finding
        `DesignFlow_m3h` missing with no error to explain why.
        """
        node = self.plant
        for part in path:
            node = await node.get_child(f"{node.nodeid.NamespaceIndex}:{part}")
        return node

    async def read(self, wanted: list[tuple[str, tuple[str, ...]]]) -> list[Reading]:
        """Batch-read, so every StatusCode survives (safeguard 2).

        One `Read` request for the whole batch rather than a call per node: it is
        one round trip, and it is the same call the gateway makes, so the code a
        reader copies here is the code that already works in production.
        """
        params = ua.ReadParameters()
        for _, path in wanted:
            node = await self.resolve(*path)
            params.NodesToRead.append(
                ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value)
            )
        response = await self._client.uaclient.read(params)

        out: list[Reading] = []
        for (signal, _), rv in zip(wanted, response, strict=True):
            status = rv.StatusCode.name if rv.StatusCode is not None else "?"
            value = float(rv.Value.Value) if rv.Value is not None else float("nan")
            out.append(Reading(signal, value, status,
                               self._freshness(rv.SourceTimestamp)))
        return out

    def _freshness(self, stamp: Any) -> str:
        """Three verdicts, because the server only offers two signals (safeguard 3).

        `None` is not the same as "old". `publish()` clears `SourceTimestamp` on
        every write, so a perfectly good reading arrives untimestamped, and
        folding that into "unmeasured" would make this client report real data as
        fake forever — which is the failure mode of a safeguard nobody trusts.
        """
        if stamp is None:
            return "untimestamped"
        return "measured" if stamp.timestamp() >= self.connected_at else "unmeasured"

    async def watch(self, path: tuple[str, ...], seconds: float) -> list[float]:
        """Subscribe unfiltered, and say why (safeguard 5).

        An unfiltered subscription over-reports — lesson 04 measured 41
        notifications carrying 3 distinct values — but it never holds a value the
        server disagrees with. A filtered one is cheaper and quietly stale, and a
        reader copying this code should see that trade being made deliberately.
        """
        node = await self.resolve(*path)
        seen: list[float] = []

        class Handler:
            """Signature imposed by asyncua; only `value` is interesting here."""

            def datachange_notification(
                self, node: Any, value: Any, data: Any
            ) -> None:
                del node, data
                seen.append(value)

        sub = await self._client.create_subscription(500, Handler())
        await sub.subscribe_data_change(node)
        await asyncio.sleep(seconds)
        await sub.delete()
        return seen

    async def timestamp_of(self, *path: str) -> Any:
        """The raw `SourceTimestamp` for a node, or `None`.

        Exposed because the three-state freshness verdict above is a workaround
        for a server defect, and a reader who wants to check whether the defect
        is still present needs to see the field itself. When `publish()` starts
        setting `SourceTimestamp`, this returns a real time for a published
        value and `tests/test_opcua_minimal_client.py` fails, saying so.
        """
        node = await self.resolve(*path)
        params = ua.ReadParameters()
        params.NodesToRead.append(
            ua.ReadValueId(NodeId=node.nodeid, AttributeId=ua.AttributeIds.Value))
        rv = (await self._client.uaclient.read(params))[0]
        return rv.SourceTimestamp

    async def close(self) -> None:
        await self._client.disconnect()


async def _main(endpoint: str, seconds: float) -> int:
    c = MinimalClient(endpoint=endpoint)
    if not await c.connect():
        return 1
    try:
        readings = await c.read([
            ("AERATION:AHU-1:DO", ("AERATION", "AHU-1", "do_mg_l")),
            ("AERATION:AHU-1:AIR_FLOW", ("AERATION", "AHU-1", "air_flow_m3h")),
            ("INFLUENT:FLOW:FLOW", ("INFLUENT", "FLOW", "flow_m3h")),
            ("SITE:WEATHER:STORM", ("SITE", "WEATHER", "storm_flag")),
        ])
        print(f"read from {endpoint}")
        for r in readings:
            print(f"  {r}")
        suspect = [r for r in readings if r.freshness != "measured"]
        if suspect:
            print(f"\n  {len(suspect)} of {len(readings)} values are not "
                  f"trustworthy readings:")
            for r in suspect:
                if r.freshness == "unmeasured":
                    print(f"    {r.signal} is the server's constructor placeholder "
                          f"({r.value:g}), not a measurement")
                else:
                    print(f"    {r.signal} arrived with no SourceTimestamp, so its "
                          f"age is unknown (this server clears it on publish)")
        print(f"\nsubscribed for {seconds:g}s ...")
        await c.watch(("AERATION", "AHU-1", "do_mg_l"), seconds)
    finally:
        await c.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tools.opcua_minimal_client")
    p.add_argument("--endpoint", default="opc.tcp://127.0.0.1:4840/wwtp/server/")
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    return asyncio.run(_main(args.endpoint, args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
