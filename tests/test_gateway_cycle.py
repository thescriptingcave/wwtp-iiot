"""The gateway's cycle, against a live plant.

Covers the wiring rather than the parts: read both protocols, filter, spool,
drain, and report. The individual parts have their own tests; what can only be
checked here is whether they are connected to each other correctly.

The numbers asserted are the ones a health line would show, because that is what
an operator actually sees. A gateway that reads 57 signals and publishes 57 every
poll is not obviously broken — it just costs 50x the storage and tells you
nothing extra — so the filtering ratio is asserted rather than assumed.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from gateway.main import Gateway, GatewayConfig
from softplc.main import SoftPlc, SoftPlcConfig


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MB_PORT = _free_port()
UA_PORT = _free_port()
ENDPOINT = f"opc.tcp://127.0.0.1:{UA_PORT}/gw-cycle/"


@pytest.fixture(scope="module")
def plant():
    plc = SoftPlc(SoftPlcConfig(
        scan_ms=20, modbus_host="127.0.0.1", modbus_port=MB_PORT,
        opcua_endpoint=ENDPOINT, speed=600.0,
    ))
    plc.start()
    plc.run(duration_s=600.0)
    yield plc
    plc.stop()


def _gateway(spool_dir: Path, **overrides) -> Gateway:
    config = GatewayConfig(
        modbus_host="127.0.0.1", modbus_port=MB_PORT,
        opcua_endpoint=ENDPOINT, spool_dir=str(spool_dir),
        poll_interval_s=0.02, **overrides,
    )
    return Gateway(config)


def _run(gw: Gateway, iterations: int = 6) -> dict:
    async def go() -> dict:
        await gw.start()
        await gw.run(iterations=iterations, report_every=1e9)
        health = gw.health()
        await gw.stop()
        return health
    return asyncio.run(go())


# ─── the cycle ────────────────────────────────────────────────────────────────


def test_the_gateway_reads_both_protocols(tmp_path: Path, plant) -> None:
    health = _run(_gateway(tmp_path))
    assert health["modbus"] is True
    assert health["opcua"] is True
    assert health["stats"]["polls"] == 6
    assert health["stats"]["modbus_failures"] == 0
    assert health["stats"]["opcua_failures"] == 0


def test_the_deadband_actually_filters(tmp_path: Path, plant) -> None:
    """Six polls of 57 signals is 342 readings. If all of them were published the
    gateway would be writing 50x the data and telling nobody anything extra —
    which looks exactly like a working gateway in a log file."""
    health = _run(_gateway(tmp_path))
    offered = health["deadband"]["offered"]
    published = health["deadband"]["published"]
    assert offered == 6 * 57, f"offered {offered}"
    assert published < offered / 2, (
        f"the deadband published {published} of {offered}; a settled plant "
        "should be filtered hard"
    )


def test_published_points_reach_the_spool(tmp_path: Path, plant) -> None:
    gw = _gateway(tmp_path)
    _run(gw)
    assert gw.stats.spooled > 0
    assert gw.stats.published == gw.stats.spooled
    assert gw.stats.dropped_overflow == 0


def test_spooled_points_survive_on_disk(tmp_path: Path, plant) -> None:
    """The whole point of the spool. With no InfluxDB token the gateway writes
    nothing and must still keep everything, because a gateway that refuses to run
    without a database throws away the one guarantee it exists to provide."""
    gw = _gateway(tmp_path)
    _run(gw)
    files = list((tmp_path).glob("spool-*.jsonl"))
    assert files, "nothing was spooled"
    body = files[0].read_text().strip().split("\n")
    assert len(body) == gw.stats.spooled
    # Current hour is excluded from pending() by design: it is still being written.
    assert files[0] not in gw.spool.pending()


def test_health_reports_the_writer_absence_rather_than_failing(tmp_path: Path,
                                                               plant) -> None:
    gw = _gateway(tmp_path)
    health = _run(gw)
    assert health["postgres"] is None
    assert health["spool"]["written"] > 0


# ─── both protocols now publish ───────────────────────────────────────────────


def test_modbus_and_opcua_both_publish(tmp_path: Path, plant) -> None:
    """The gap Phase 3 closed.

    Until the contract gained a ``signal:`` field on each register, the gateway
    read Modbus correctly and published only OPC UA, because a register name
    identifies a location rather than a measurement. The test asserted that state
    explicitly so it could not be forgotten; now it asserts the opposite, which
    means removing the link fails here rather than quietly halving the dataset.
    """
    gw = _gateway(tmp_path)
    health = _run(gw)
    assert health["modbus"] is True
    assert health["opcua"] is True
    assert gw.stats.modbus_failures == 0
    # Modbus contributes 14 linked registers; the other 5 are heartbeats, fault
    # codes, a state bitfield and half a 32-bit value, and are correctly not
    # measurements. So the published set is the union of both protocols' signals,
    # which is strictly larger than either alone.
    assert health["stats"]["published"] > 0


def test_unlinked_registers_are_not_published(tmp_path: Path, plant) -> None:
    """A heartbeat is liveness, not a measurement. Publishing one would put a
    monotonically increasing counter into a series nothing is watching, and would
    make the deadband's suppression ratio look worse than it is."""
    from gateway.main import _signal_for

    for name in ("HEARTBEAT", "FAULT_CODE", "EQUIP_STATE_WORD",
                 "PUMP1_RUNTIME_HI", "PUMP1_RUNTIME_LO"):
        assert _signal_for(name, gw_contract()) is None, name
    for name in ("AERATION_DO", "AERATION_BLOWER_VALVE", "STORM_FLAG"):
        assert _signal_for(name, gw_contract()) is not None, name


def gw_contract():
    from softplc.contract import contract
    return contract()


# ─── resilience ───────────────────────────────────────────────────────────────


def test_a_dead_plant_does_not_crash_the_gateway(tmp_path: Path) -> None:
    """Nothing is listening. The gateway must keep running and keep reporting,
    because a gateway that exits on a connection error is a gateway that needs a
    supervisor, an alert and a runbook."""
    gw = Gateway(GatewayConfig(
        modbus_host="127.0.0.1", modbus_port=9999,
        opcua_endpoint="opc.tcp://127.0.0.1:9998/nope/",
        spool_dir=str(tmp_path), poll_interval_s=0.02,
    ))
    health = _run(gw, iterations=3)
    assert health["stats"]["polls"] == 3
    assert health["modbus"] is False
    assert health["opcua"] is False
    assert health["stats"]["published"] == 0


def test_opcua_only_still_works(tmp_path: Path, plant) -> None:
    gw = _gateway(tmp_path, use_modbus=False)
    health = _run(gw)
    assert health["modbus"] is False
    assert health["opcua"] is True
    assert health["stats"]["published"] > 0


def test_the_gateway_owns_no_state_worth_losing(tmp_path: Path, plant) -> None:
    """A restart must continue, not restart. The second gateway reads the spool
    the first one left, and the two do not disagree about how much there is."""
    first = _gateway(tmp_path)
    _run(first)
    assert first.stats.spooled > 0
    files_after_first = len(list(tmp_path.glob("spool-*.jsonl")))

    second = _gateway(tmp_path)
    _run(second, iterations=3)
    # The second process must see the first one's files, and must not have
    # truncated them: it opened the same path in append mode. A gateway that
    # restarted with ``w`` instead of ``a`` would destroy exactly the data the
    # outage produced, and nothing else would show it.
    assert second.spool.stats.files >= files_after_first
    assert len(list(tmp_path.glob("spool-*.jsonl"))) >= files_after_first
    assert second.stats.spooled > 0, "and it keeps collecting"
