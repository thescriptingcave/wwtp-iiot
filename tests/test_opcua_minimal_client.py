"""The minimal client's safeguards, checked against a live server.

    python -m tools.opcua_minimal_client

Five safeguards, each of which exists because a lesson found the alternative
producing a plausible wrong answer. This file asserts they fire — a safeguard
that never triggers is indistinguishable from a safeguard that does not work,
and the failure mode is a client that looks careful.

Every test here starts a real server, because the whole point of the client is
what it does with values a real server hands it. None of them mocks `asyncua`.
"""

from __future__ import annotations

import pytest
import tools.opcua_minimal_client as mod
from asyncua import ua
from softplc.servers.opcua import OpcUaServer
from tools.opcua_minimal_client import MinimalClient

PORT = 48405
ENDPOINT = f"opc.tcp://127.0.0.1:{PORT}/wwtp/server/"

WANT = [
    ("AERATION:AHU-1:DO", ("AERATION", "AHU-1", "do_mg_l")),
    ("SITE:WEATHER:STORM", ("SITE", "WEATHER", "storm_flag")),
]


@pytest.fixture
async def plant():
    """A bare server with nothing driving it — the state lesson 03 is about."""
    s = OpcUaServer(endpoint=ENDPOINT)
    await s.start()
    await s.wait_ready()
    yield s
    await s.stop()


async def test_a_fresh_servers_values_are_reported_as_unmeasured(plant) -> None:
    """Safeguard 3, first half: the constructor placeholder is not a reading.

    `normal_low` for DO is 1.5, which is the bottom of a healthy 1.5-3.0 band,
    and the status is `Good`. A client printing `1.5` with no qualification is
    reporting a number that has never been measured.
    """
    c = MinimalClient(endpoint=ENDPOINT)
    assert await c.connect()
    try:
        readings = {r.signal: r for r in await c.read(WANT)}
    finally:
        await c.close()

    do = readings["AERATION:AHU-1:DO"]
    assert do.value == 1.5, "the placeholder changed; lesson 03 needs revisiting"
    assert do.status == "Good", (
        "the placeholder is no longer Good, which is an improvement — say so"
    )
    assert do.freshness == "unmeasured", (
        f"a value constructed before we connected must be 'unmeasured', got "
        f"{do.freshness!r}"
    )
    assert "unmeasured" in str(do), "the verdict has to be visible in the output"


async def test_a_published_value_is_not_reported_as_a_placeholder(plant) -> None:
    """Safeguard 3, second half: the verdict must actually be able to change.

    This is the test that caught the bug. The first version of the client had a
    two-state verdict and treated a missing `SourceTimestamp` as "unmeasured",
    which meant a genuine reading of 2.4 mg/L was reported as never-measured —
    forever. A safeguard that cannot clear is a safeguard that gets ignored.
    """
    plant.set_value("AERATION:AHU-1:DO", 2.4, 0)
    await plant.publish()

    c = MinimalClient(endpoint=ENDPOINT)
    assert await c.connect()
    try:
        readings = {r.signal: r for r in await c.read(WANT)}
    finally:
        await c.close()

    do = readings["AERATION:AHU-1:DO"]
    assert do.value == 2.4, "the published value did not arrive"
    assert do.freshness != "unmeasured", (
        "a value published after we connected is being called a placeholder"
    )
    # On the server as it stands this is "untimestamped" rather than "measured",
    # because `publish()` clears SourceTimestamp. See the test below.
    assert do.freshness == "untimestamped", (
        f"expected 'untimestamped' on the current server, got {do.freshness!r}. "
        f"If this is now 'measured', publish() sets SourceTimestamp and lesson 09 "
        f"and the client docstring need updating."
    )


async def test_publish_clears_the_source_timestamp(plant) -> None:
    """The finding, pinned: the one field that dates a value is destroyed.

    `asyncua` stamps `SourceTimestamp` when the node is constructed. `publish()`
    writes a `DataValue` carrying only a `Value` and a `StatusCode`, so the
    timestamp is *cleared* on every write. A client therefore cannot learn how
    old a value is, and the fix is one line in `publish()`:
    `DataValue(..., SourceTimestamp=ua.DateTime.now())`.
    """
    c = MinimalClient(endpoint=ENDPOINT)
    assert await c.connect()
    try:
        before = await c.timestamp_of("AERATION", "AHU-1", "do_mg_l")
        assert before is not None, (
            "a freshly constructed variable has no SourceTimestamp, so the "
            "unmeasured/untimestamped distinction in the client is unnecessary"
        )
        plant.set_value("AERATION:AHU-1:DO", 2.4, 0)
        await plant.publish()
        after = await c.timestamp_of("AERATION", "AHU-1", "do_mg_l")
    finally:
        await c.close()

    assert after is None, (
        f"publish() now preserves SourceTimestamp ({after}); the 'untimestamped' "
        f"verdict, the client docstring and lesson 09 are all stale"
    )


async def test_quality_is_never_collapsed(plant) -> None:
    """Safeguard 4: `Uncertain` is reported as `Uncertain`.

    Worth a test because the server cannot produce `Bad` at all — `publish()`
    maps every non-zero quality to `Uncertain` (lesson 03) — so a client that
    handled only two states would look correct here forever.
    """
    plant.set_value("AERATION:AHU-1:DO", 2.4, 1)      # QUALITY_UNCERTAIN
    await plant.publish()

    c = MinimalClient(endpoint=ENDPOINT)
    assert await c.connect()
    try:
        readings = {r.signal: r for r in await c.read(WANT)}
    finally:
        await c.close()

    assert readings["AERATION:AHU-1:DO"].status == "Uncertain", (
        "an Uncertain reading was reported as something else"
    )
    assert readings["AERATION:AHU-1:DO"].value == 2.4, (
        "an Uncertain reading should still carry its value — it is usable, and "
        "discarding it is what the gateway's docstring warns against"
    )


async def test_discovery_finds_the_plant_without_a_node_id(plant) -> None:
    """Safeguard 1: the client locates the plant by name, not by NodeId.

    The namespace index is read off the discovered node rather than assumed to
    be 2, which is what makes this survive a rebuilt address space.
    """
    c = MinimalClient(endpoint=ENDPOINT)
    assert await c.connect()
    try:
        name = await c.plant.read_browse_name()
        assert "PLANT-A" in name.Name
        node = await c.resolve("AERATION", "AHU-1", "do_mg_l")
        assert node is not None
        # Resolving a thing that does not exist must fail loudly, not return a
        # node that silently reads nothing. The exception is asyncua's
        # `BadNoMatch` — a specific StatusCode, not a bare Exception, so this
        # test fails if the *kind* of failure changes as well as if it stops.
        with pytest.raises(ua.UaStatusCodeError) as excinfo:
            await c.resolve("AERATION", "AHU-1", "no_such_signal")
        assert excinfo.value.code == ua.StatusCodes.BadNoMatch
    finally:
        await c.close()


async def test_a_server_without_the_plant_is_reported_not_assumed(plant) -> None:
    """Safeguard 1, negative case: no plant object means failure, not zero tags.

    The alternative is to carry on with `self.plant is None` and hand back a
    client that reports an empty tag list without ever saying why — which looks
    exactly like a plant with no instruments.
    """
    original = mod.PLANT_MARKER
    mod.PLANT_MARKER = "NO-SUCH-PLANT"
    try:
        c = MinimalClient(endpoint=ENDPOINT)
        assert await c.connect() is False, (
            "connect() claimed success against a server with no matching plant"
        )
    finally:
        mod.PLANT_MARKER = original
