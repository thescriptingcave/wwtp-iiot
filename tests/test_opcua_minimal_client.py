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

import time

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


async def test_a_value_published_after_connecting_is_measured(plant) -> None:
    """Safeguard 3, second half: the verdict must actually be able to change.

    This is the test that caught the bug. The first version of the client had a
    two-state verdict and treated a missing `SourceTimestamp` as "unmeasured",
    which meant a genuine reading of 2.4 mg/L was reported as never-measured —
    forever. A safeguard that cannot clear is a safeguard that gets ignored.

    The ordering matters and is the realistic one: connect, *then* publish, then
    read. A value published before we connected is genuinely ambiguous — see
    `test_a_value_published_before_connecting_is_reported_as_unmeasured`.
    """
    c = MinimalClient(endpoint=ENDPOINT)
    assert await c.connect()
    try:
        plant.set_value("AERATION:AHU-1:DO", 2.4, 0)
        await plant.publish()
        readings = {r.signal: r for r in await c.read(WANT)}
    finally:
        await c.close()

    do = readings["AERATION:AHU-1:DO"]
    assert do.value == 2.4, "the published value did not arrive"
    assert do.freshness == "measured", (
        f"a value published after we connected is {do.freshness!r}; it should be "
        f"'measured'. If this now fails, publish() has stopped setting "
        f"SourceTimestamp and lesson 09 needs rewording."
    )


async def test_a_value_published_before_connecting_is_ambiguous(plant) -> None:
    """The limitation, stated rather than papered over.

    A real measurement published a moment *before* we connected has a timestamp
    older than our connection, exactly like the constructor's placeholder. One
    sample cannot tell them apart — only observing the value *move* can, which is
    what `_moved` tracks and what a subscription would do naturally.

    So the client reports `unmeasured`, which is the conservative answer: it
    tells an operator "I cannot vouch for this" rather than "this is fine". That
    is the correct direction to be wrong in, and it is worth a test so the
    behaviour is deliberate.
    """
    plant.set_value("AERATION:AHU-1:DO", 2.4, 0)
    await plant.publish()

    c = MinimalClient(endpoint=ENDPOINT)
    assert await c.connect()
    try:
        readings = {r.signal: r for r in await c.read(WANT)}
        # Read again after a change: now the client has seen it move.
        plant.set_value("AERATION:AHU-1:DO", 2.6, 0)
        await plant.publish()
        after = {r.signal: r for r in await c.read(WANT)}
    finally:
        await c.close()

    assert readings["AERATION:AHU-1:DO"].freshness == "unmeasured", (
        "a pre-connection value that has not been seen to move is ambiguous, "
        "and 'unmeasured' is the conservative verdict"
    )
    assert after["AERATION:AHU-1:DO"].freshness == "measured", (
        "once the client has seen the value move, it is unambiguously measured"
    )


async def test_publish_sets_the_source_timestamp(plant) -> None:
    """Lesson 09's fourteenth finding, and the fix that closed it.

    `asyncua` stamps `SourceTimestamp` when a node is *constructed*.
    `publish()` used to write a `DataValue` carrying only a `Value` and a
    `StatusCode`, so the field was **cleared** on every write — and a client
    could not date a reading at all. The fix is `SourceTimestamp=_utcnow()` in
    `publish()`.

    This test asserts the fixed behaviour *and* the part that was wrong in the
    fix: the timestamp must agree with the wall clock. The first version used
    `ua.DateTime.now()`, which returns a **naive local** time, and `asyncua`
    encodes a naive datetime against a UTC epoch — so the value went out
    looking ordinary and came back seven hours out. Only a comparison against
    `time.time()` catches that.
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

    assert after is not None, (
        "publish() no longer sets SourceTimestamp. A client cannot date a "
        "reading, which is the fourteenth finding in courses/opcua/ and the "
        "reason the reference client needs three freshness verdicts."
    )
    assert after > before, (
        f"the timestamp did not move: {before} -> {after}. A value that is "
        f"re-published with a stale timestamp is worse than one with none."
    )
    # The part the naive-datetime bug slipped past.
    drift = abs(after.timestamp() - time.time())
    assert drift < 5, (
        f"the published SourceTimestamp is {drift:.0f}s from the wall clock. "
        f"`_utcnow()` must return a tz-aware datetime: asyncua encodes a naive "
        f"one against a UTC epoch, so a local time arrives as though it were "
        f"already UTC — wrong by exactly the machine's offset, and invisible in "
        f"the value itself."
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
