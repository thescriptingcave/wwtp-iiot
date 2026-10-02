"""A write that arrives is not a write that takes. These tests are about the gap.

Every test in `test_modbus_server.py` proves the server *queues* an accepted
write. Nothing there proves the plant *applies* it, because the queue and the
plant are on opposite sides of a seam that did not exist until this was built:
`ModbusTcpServer.take_writes()` → `SoftPlc._apply_pending_writes()`.

That seam is where the original fault lived, and it is invisible from either
side. The server was asked "does a write reach your queue?" and could answer
yes. The plant was asked "does a setpoint change when someone writes it?" and
was never asked at all. So the seam gets its own file and its own assertions,
and the assertions are about *the plant's state*, not the server's bookkeeping.

Two failure modes are pinned here, and they are opposites:

* **The write reverts.** `ModbusTcpServer._flush()` rewrites the whole holding
  block every scan. A value that reached the block but not the plant is gone on
  the next one — which is exactly what used to happen, so a written setpoint read
  back as its previous value while the client was told it succeeded.
* **The write lands but nothing responds.** `AerationControl` receives
  `setpoint_mg_l` by value at construction, so setting the plant's attribute
  alone leaves the PI loop integrating against the startup value forever. The
  register would read the operator's number and the dissolved oxygen would not
  move, which is the most expensive kind of bug to find: every indicator says it
  worked.
"""

from __future__ import annotations

import socket

import pytest
from pymodbus.client import ModbusTcpClient
from softplc.contract import contract
from softplc.main import SoftPlc, SoftPlcConfig
from softplc.servers.modbus import decode_float32, encode_float32
from softplc.servers.modbus_server import ModbusTcpServer

C = contract()
SETPOINT = "AERATION:AHU-1:SETPOINT_DO"
SETPOINT_REG = C.register("AERATION_SETPOINT_DO")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MB_PORT = _free_port()

#: Simulated seconds per real second. Fast enough that a few hundred scans is a
#: meaningful slice of plant behaviour, slow enough that the influent and
#: clarifier are not sprinting. The scan count, not wall-clock, is what the
#: assertions wait on.
SPEED = 40.0


@pytest.fixture(scope="module")
def plc():
    p = SoftPlc(SoftPlcConfig(
        scan_ms=20,
        modbus_host="127.0.0.1",
        modbus_port=MB_PORT,
        opcua_endpoint=f"opc.tcp://127.0.0.1:{_free_port()}/wwtp-writeback/",
        speed=SPEED,
    ))
    p.start()
    yield p
    p.stop()


@pytest.fixture
def client(plc):
    c = ModbusTcpClient("127.0.0.1", port=MB_PORT)
    c.connect()
    try:
        yield c
    finally:
        c.close()


def _scan(plc: SoftPlc, n: int) -> None:
    """Step the plant `n` scans and return.

    **Scans are driven explicitly rather than run in the background, and that is a
    deliberate choice.** `SoftPlc.run()` is blocking — it spins the loop until
    `duration_s` of *simulated* time has elapsed — so a background scan thread
    would have to be raced, torn down, and joined, and the assertions would
    become "wait long enough and hope". It also has a sharp edge: `stop()` stops
    the event loop out from under the running `_run`, so the future `run()` is
    blocked on never completes and the thread raises `TimeoutError` 30 seconds
    later, in whichever test happens to be running then.

    Stepping `_scan` directly makes every assertion here deterministic and
    removes the wall-clock dependency entirely: "survives 300 scans" means 300
    scans, not "survives at least 300 if the machine was not busy".

    What this does *not* cover is `_run`'s pacing loop, and it is not trying to —
    `test_end_to_end.py` exercises `run()` for real. The thing under test is the
    seam between a protocol write and the plant, and the seam is in `_step`.
    """
    dt = plc.config.scan_ms / 1000.0 * plc.config.speed
    for _ in range(n):
        plc._call(plc._step(dt))


def _setpoint_offset() -> int:
    return ModbusTcpServer.wire_offset(SETPOINT_REG.address)


def _write_setpoint(client: ModbusTcpClient, value: float):
    return client.write_registers(
        _setpoint_offset(),
        list(encode_float32(value, SETPOINT_REG.word_order)),
        device_id=1,
    )


def _read_setpoint(client: ModbusTcpClient) -> float:
    off = _setpoint_offset()
    rr = client.read_holding_registers(off, count=2, device_id=1)
    assert not rr.isError(), f"reading the setpoint failed: {rr}"
    return decode_float32(rr.registers[0], rr.registers[1], SETPOINT_REG.word_order)


# ─── the write takes ─────────────────────────────────────────────────────────


def test_a_written_setpoint_reaches_the_plant(plc, client) -> None:
    """The value the contract says is writable lands in the plant.

    Two assignments are asserted, because the bug is that one of them was
    missing. `plant.aeration.setpoint_do_mg_l` is what the snapshot publishes, so
    the register reads it back. `aeration_control.setpoint_mg_l` is what the PI
    loop integrates against, and it is a *separate object* that was handed a copy
    of the number at construction.
    """
    assert not _write_setpoint(client, 3.75).isError()
    _scan(plc, 5)

    assert plc.plant.aeration.setpoint_do_mg_l == pytest.approx(3.75, abs=1e-6), (
        "the write did not reach the plant's aeration state, so the signal the "
        "snapshot publishes still carries the old setpoint"
    )
    assert plc.aeration_control.setpoint_mg_l == pytest.approx(3.75, abs=1e-6), (
        "the write reached the plant but not the control block. AerationControl "
        "takes setpoint_mg_l by value at construction (main.py:104), so the PI "
        "loop is still integrating against the startup value while the register "
        "shows the operator's number — the plant appears to obey and does not."
    )


def test_a_written_setpoint_reads_back_over_the_wire(plc, client) -> None:
    """It survives many scans. This is the assertion the original bug failed.

    `_flush()` republishes the entire holding block from the runtime model every
    20 ms, so a value that reached the block without reaching the plant was gone
    within one scan. Before the write-back path, every setpoint written through
    this client read back as its previous value indefinitely, while the write
    itself returned no error.

    Three hundred scans is six seconds of simulated-at-40x plant time — far more
    than the two or three that would expose a reversion, so this cannot pass by
    racing the flush.
    """
    assert not _write_setpoint(client, 4.5).isError()
    _scan(plc, 300)

    assert _read_setpoint(client) == pytest.approx(4.5, abs=1e-4), (
        "the setpoint reverted: it reached the Modbus block but not the plant, so "
        "the next publish restored the previous value"
    )


def test_a_written_setpoint_changes_the_control_output(plc, client) -> None:
    """The plant *responds*. This is the assertion that would have caught it.

    The two tests above prove the number moved. This proves the loop noticed.
    Lowering the setpoint below the current dissolved oxygen gives the controller
    a negative error, so it must command less air — and the blower's duty falls.

    Comparing duty before and after rather than asserting an absolute value is
    what makes this robust: the plant is a simulation whose absolute numbers
    depend on when the test runs, but a controller that ignores its setpoint
    produces the *same* duty no matter what is written to it.
    """
    _scan(plc, 20)
    do_before = plc.plant.aeration.do_mg_l
    duty_before = plc.aeration_control.duty_pct

    # Well under the current DO, inside the contract's [0.5, 6.0] range.
    assert do_before > 1.0, (
        f"DO is {do_before:.2f} mg/L; this test needs the reading above 1.0 so a "
        "setpoint below it produces a negative error"
    )
    assert not _write_setpoint(client, 0.75).isError()
    _scan(plc, 400)

    assert plc.aeration_control.duty_pct < duty_before, (
        f"duty went from {duty_before:.2f}% to {plc.aeration_control.duty_pct:.2f}% "
        "after the setpoint was lowered from "
        f"{plc.aeration_control.setpoint_mg_l:.2f} toward 0.75 with DO at "
        f"{do_before:.2f}. A PI controller ignoring its setpoint produces the "
        "same duty regardless of what is written to it — which is what this "
        "assertion is there to detect."
    )


def test_a_setpoint_is_applied_exactly_once(plc, client) -> None:
    """A queued write is drained, not re-driven every scan.

    `take_writes()` returns and clears in one step. If it only returned, the
    setpoint would be re-applied on every scan — and while re-applying the same
    number looks harmless, it means the write path is a constant re-drive rather
    than an edge, and any future setter with side effects (arming a storm,
    incrementing a counter) would run thousands of times a minute.
    """
    assert not _write_setpoint(client, 2.6).isError()
    _scan(plc, 5)

    assert plc.modbus.write_stats()["pending"] == 0, (
        "writes are accumulating rather than being drained; the queue would grow "
        "without bound"
    )


# ─── the write is refused ────────────────────────────────────────────────────


def test_a_refused_write_does_not_reach_the_plant(plc, client) -> None:
    """A refusal must leave the setpoint alone, and say so.

    Both halves matter. The client must learn it failed — otherwise it retries
    forever against a refusal it cannot see — and the plant must be unchanged,
    or the client believes it commanded something it did not.
    """
    _scan(plc, 5)
    before = plc.plant.aeration.setpoint_do_mg_l

    assert _write_setpoint(client, 99.0).isError(), (
        "99 mg/L is outside the contract's [0.5, 6.0] range and must be refused. "
        "Node-RED's permit check is the client's courtesy; a laptop with a Modbus "
        "tool does not have it."
    )
    _scan(plc, 5)

    assert plc.plant.aeration.setpoint_do_mg_l == pytest.approx(before, abs=1e-6), (
        "a refused write still changed the plant"
    )
    assert _read_setpoint(client) == pytest.approx(before, abs=1e-4), (
        "a refused write reached the register block anyway — the operator sees an "
        "error and reads back a value the plant never accepted"
    )


def test_writing_a_read_only_register_is_refused(plc, client) -> None:
    """`FAULT_CODE` and `STORM_FLAG` used to be `writable: true`.

    Both accepted a write and discarded it on the next scan. They are read-only
    now, and the refusal is a Modbus exception rather than silence.
    """
    for name in ("FAULT_CODE", "STORM_FLAG", "AERATION_DO"):
        reg = C.register(name)
        off = ModbusTcpServer.wire_offset(reg.address)
        assert not reg.writable, (
            f"{name} was expected to be read-only; if it is writable again it "
            "needs a setter in SoftPlc._apply_pending_writes"
        )
        err = client.write_register(off, 1, device_id=1)
        assert err.isError(), f"writing read-only {name} was accepted"


def test_the_plant_still_cycles_after_a_refused_write(plc) -> None:
    """A refusal must not take the scan loop with it.

    The Modbus listener is a separate thread from the scan loop, so a refusal
    raised on one should not affect the other. This is the test for the failure
    mode where refusing a write crashed the server — a plausible outcome, since
    the refusal happens on pymodbus's thread and the queue is consumed on the
    scan loop's.
    """
    before = plc.cycles
    _scan(plc, 50)
    assert plc.cycles > before
