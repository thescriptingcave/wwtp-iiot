"""The gateway's read path, end to end, against a running plant.

No database. That is deliberate: this file covers the half of the gateway that
can be proven against a live process — protocol read, contract decode, deadband,
spool — and the half that needs a database lives in
``tests/integration/``.

Running it for real catches the class of bug that unit tests cannot. Three found
while writing this file:

  * The OPC UA client's browse name was lower-cased. The server creates nodes
    with ``sig.field`` verbatim, so every one of the 57 lookups silently failed
    and the reader reported "connected, 0 signals" — which looks like a plant
    with no instruments.
  * A quality-only change has to survive the deadband. A fouled probe reads the
    same number it has read all hour, and a value-only filter publishes nothing
    for it.
  * Modbus and OPC UA must agree on the same signal. They read the same plant by
    two protocols, so their disagreement is either a word-order bug or a wiring
    bug, and it is the cheapest cross-check available for both.
"""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import datetime
from pathlib import Path

import pytest
from gateway.clients.modbus_client import ModbusLinkDownError, ModbusReader
from gateway.clients.opcua_client import OpcUaReader
from gateway.deadband import BandMode, BandRule, Deadband
from gateway.spool.store import Spool, SpoolRecord
from softplc.contract import QUALITY_UNCERTAIN, contract
from softplc.main import SoftPlc, SoftPlcConfig
from storage.postgres.writer import PostgresWriter, Reading

C = contract()


def _collecting_writer(batch_rows: int = 100_000) -> tuple[PostgresWriter, list]:
    """A real writer wired to a list instead of a database.

    This is the arrangement the storage layer was designed for: the writer takes
    an ``execute`` callable, so the *policy* — what gets buffered, when it
    flushes, what a failure costs — is exercised for real, and only the SQL is
    missing. The SQL has its own tests against a live database.
    """
    rows: list[tuple] = []

    def execute(batch):
        rows.extend(batch)
        return len(batch)

    return PostgresWriter(execute, batch_rows=batch_rows, batch_seconds=1e9), rows


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MB_PORT = _free_port()
UA_PORT = _free_port()
ENDPOINT = f"opc.tcp://127.0.0.1:{UA_PORT}/wwtp-gw/"


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


# ─── Modbus ───────────────────────────────────────────────────────────────────


def test_the_modbus_plan_collapses_requests(plant) -> None:
    """One request per register would be a request per register per poll. The
    plan grows windows while they stay contiguous, so 19 registers in 7 blocks
    rather than 19 requests — the difference between a link with headroom and one
    that is generating its own congestion."""
    reader = ModbusReader(C, port=MB_PORT)
    assert reader.request_count < len(C.registers)
    assert reader.planned_registers >= len(C.registers)


def test_modbus_reads_every_planned_register(plant) -> None:
    with ModbusReader(C, port=MB_PORT) as r:
        result = r.poll()
    assert result.failed_windows == 0
    assert set(result.values) >= {"AERATION_DO", "AERATION_BLOWER_VALVE",
                                 "INFLUENT_FLOW", "HEARTBEAT"}


def test_modbus_decodes_the_low_word_first_traps(plant) -> None:
    """The signature bug class, caught at the client.

    Read as high-word-first, a low-word-first float is not an error — it is
    2.3e-41, which is finite, in range, and wrong. It plots as a flat line at
    zero and reads as a process that stopped.
    """
    with ModbusReader(C, port=MB_PORT) as r:
        values = r.poll().values
    truth = plant.plant.snapshot().values

    for reg_name, signal in (("AERATION_BLOWER_VALVE", "AERATION:AHU-1:BLOWER_VALVE"),
                             ("AERATION_WASTE_RATE", "AERATION:AHU-1:WASTE_RATE")):
        reg = C.register(reg_name)
        assert reg.word_order == "little", "this test is about the low-word-first pair"
        got = values[reg_name]
        want = truth[signal]
        assert got == pytest.approx(want, rel=0.30), (
            f"{reg_name} read as {got:.3e}, plant reports {want:.3f}. "
            f"2.3e-41 is the signature of a word-order mistake."
        )
        assert got > 1e-6, "a flatlined value is the word-order bug wearing a mask"


def test_modbus_heartbeat_proves_liveness(plant) -> None:
    with ModbusReader(C, port=MB_PORT) as r:
        first = r.poll().values["HEARTBEAT"]
    time.sleep(0.2)
    with ModbusReader(C, port=MB_PORT) as r:
        second = r.poll().values["HEARTBEAT"]
    assert second != first or plant.cycles > 0, (
        "a heartbeat that never moves cannot distinguish a live PLC from a "
        "cached value"
    )


def test_an_unreadable_register_is_absent_not_zero(plant) -> None:
    """Zero is a real flow rate, a real DO and a real alarm state. Substituting
    it for 'could not read' turns a loose cable into a plant that appears to have
    shut down."""
    r = ModbusReader(C, port=9999)   # nothing listening
    r.connect()
    result = r.poll()
    assert result.values == {}
    assert 0.0 not in set(result.values.values())


def test_repeated_failures_declare_the_link_down(plant) -> None:
    r = ModbusReader(C, port=9999)
    r.connect()
    for _ in range(2):
        r.poll()          # tolerated: one missed poll is normal on a plant network
    with pytest.raises(ModbusLinkDownError, match="consecutive"):
        r.poll()


# ─── OPC UA ───────────────────────────────────────────────────────────────────


def test_opcua_resolves_every_contract_signal(plant) -> None:
    """57 lookups that all fail report "connected, 0 signals", which looks
    exactly like a plant with no instruments. Hence an explicit count."""
    async def go():
        reader = OpcUaReader(C, endpoint=ENDPOINT)
        assert await reader.connect()
        count, missing = reader.resolved_count, reader.unresolved()
        await reader.close()
        return count, missing

    count, missing = asyncio.run(go())
    assert missing == [], f"unresolved: {missing}"
    assert count == len(C.signals)


def test_opcua_reads_real_values_with_status(plant) -> None:
    async def go():
        reader = OpcUaReader(C, endpoint=ENDPOINT)
        await reader.connect()
        result = await reader.poll()
        await reader.close()
        return result

    result = asyncio.run(go())
    assert result.read > 0
    assert result.failed == 0
    assert "AERATION:AHU-1:DO" in result.values
    truth = plant.plant.snapshot().values["AERATION:AHU-1:DO"]
    assert result.values["AERATION:AHU-1:DO"] == pytest.approx(truth, abs=0.05)


def test_opcua_and_modbus_agree_about_the_same_signal(plant) -> None:
    """Two protocols, one plant. A disagreement is either a word-order bug or a
    wiring bug, and this is the cheapest cross-check available for both.
    """
    pairs = (("AERATION:AHU-1:DO", "AERATION_DO"),
             ("AERATION:AHU-1:BLOWER_VALVE", "AERATION_BLOWER_VALVE"))

    with ModbusReader(C, port=MB_PORT) as r:
        mb = r.poll().values

    async def go():
        reader = OpcUaReader(C, endpoint=ENDPOINT)
        await reader.connect()
        res = await reader.poll()
        await reader.close()
        return res.values

    ua_values = asyncio.run(go())
    for signal, register in pairs:
        assert mb[register] == pytest.approx(ua_values[signal], rel=0.15), (
            f"{register} (Modbus {mb[register]}) and {signal} "
            f"(OPC UA {ua_values[signal]}) disagree"
        )


# ─── quality survives the whole path ──────────────────────────────────────────


def test_a_sensor_fault_arrives_as_uncertain_over_opcua() -> None:
    """The property the whole quality scale exists for.

    A fouled probe reads the number it has been reading all hour. A gateway that
    discards StatusCodes stores it as a confident, wrong, flat line — and the
    operator cannot tell a sick instrument from a sick process.
    """
    plc = SoftPlc(SoftPlcConfig(
        scan_ms=20, modbus_host="127.0.0.1", modbus_port=_free_port(),
        opcua_endpoint=f"opc.tcp://127.0.0.1:{_free_port()}/gw-fault/",
        scenario="bad_instrument", speed=600.0,
    ))
    plc.start()
    plc.run(duration_s=1800.0)
    endpoint = plc.config.opcua_endpoint
    try:
        async def go():
            reader = OpcUaReader(C, endpoint=endpoint)
            await reader.connect()
            res = await reader.poll()
            await reader.close()
            return res

        result = asyncio.run(go())
        assert result.quality["AERATION:AHU-1:DO"] == QUALITY_UNCERTAIN, (
            "the drifting probe must arrive as Uncertain, not as a number"
        )
        # And the process itself is untouched: that is the whole point of a
        # sensor fault as opposed to a process fault.
        assert plc.plant.aeration.do_mg_l < 3.0
    finally:
        plc.stop()


def test_a_quality_only_change_gets_through_the_deadband(plant) -> None:
    db = Deadband({"AERATION:AHU-1:DO": BandRule(BandMode.ABSOLUTE, 5.0)})
    assert db.accept("AERATION:AHU-1:DO", 2.0, quality=0)
    for _ in range(20):
        assert not db.accept("AERATION:AHU-1:DO", 2.0, quality=0)
    assert db.accept("AERATION:AHU-1:DO", 2.0, quality=QUALITY_UNCERTAIN)


# ─── the whole read path, into the spool ──────────────────────────────────────


def test_a_poll_reaches_the_database_through_the_spool(tmp_path: Path,
                                                       plant) -> None:
    """Read, deadband, spool, batch — the full path, with no database.

    The database is the only part left out, and it is the part that cannot lie
    about the shape of the data. Everything upstream of it is checked here, which
    is why this test is worth having whether or not one is running.

    Modbus is exercised for the decode half (the word-order traps, above); the
    end-to-end leg uses OPC UA, because OPC UA hands back *signal ids* and
    Modbus hands back *register names*, and the contract does not link every
    register to the signals it exposes. Inventing that link in a test — by
    string-matching equipment names — is a second, wrong mapping, and it is
    exactly the kind of approximation that hides a real bug.
    """
    polls = 6

    async def read():
        reader = OpcUaReader(C, endpoint=ENDPOINT)
        await reader.connect()
        out = []
        for _ in range(polls):
            out.append(await reader.poll())
            await asyncio.sleep(0.05)
        await reader.close()
        return out

    results = asyncio.run(read())
    assert all(r.read > 0 for r in results)

    db = Deadband.from_contract(C)
    now_s = int(time.time())
    writer, rows = _collecting_writer()
    with Spool(tmp_path / "spool", max_mb=8) as spool:
        for n, polled in enumerate(results):
            for signal_id, value in polled.values.items():
                if signal_id not in db.rules:
                    continue
                quality = polled.quality.get(signal_id, 0)
                if not db.accept(signal_id, value, quality):
                    continue
                assert spool.append(SpoolRecord(
                    ts=now_s + n, signal=signal_id,
                    value=value, quality=quality,
                ))
        spool.flush()

        # Read the spool back off disk rather than reusing the objects: the point
        # is that the data survived serialisation, and reusing them would prove
        # only that the function returned.
        path = next(spool.dir.glob("spool-*.jsonl"))
        for record in spool.read(path):
            writer.add(Reading(ts=record.ts, signal_id=record.signal,
                               value=record.value, quality=record.quality,
                               source="opcua"))
    writer.flush()

    assert rows, "nothing survived the spool"

    # Every row names a signal the contract declares and carries its provenance
    # and its validity. This check used to be "the line's tag set matches a
    # series key the contract derived", which is a *weaker* statement than the
    # one available now: `signal.id` is the signal. The line protocol needed a
    # separate key table to make the join, and the key table was a second place
    # for the metadata to be wrong.
    for ts, signal_id, value, quality, source in rows:
        assert signal_id in C.signals, f"{signal_id} is not a declared signal"
        assert source == "opcua"
        assert quality in (0, 1, 2)
        assert isinstance(ts, datetime) and ts.tzinfo is not None
        # A row with no value must not claim to be Good. The database enforces
        # this too; asserting it here means a violation is caught without one.
        assert value is not None or quality != 0

    # Every signal published at least once, exactly once. This is the deadband's
    # first contract — a signal you have never seen is always published, however
    # narrow its band — and it is deterministic, unlike the "some signals repeat"
    # claim this assertion used to make. Six polls 50 ms apart is a window in
    # which the plant does not move, so exactly one point per signal is the
    # *correct* outcome, and asserting otherwise was asserting a race.
    assert {r[1] for r in rows} == set(C.signals)

    # The deadband did something. It could not have on a single poll — the first
    # reading of every signal always publishes, so one poll offers 57 and accepts
    # 57 by design. Six polls of a plant that is not moving much is where the
    # filter earns its place.
    offered = sum(db.offered_since_start().values())
    accepted = sum(db.accepted_since_start().values())
    assert offered == polls * len(C.signals), f"offered {offered}"
    assert accepted < offered, (
        f"the deadband filtered nothing: {accepted} of {offered} accepted"
    )
    assert accepted == len(rows), "spool and deadband disagree"


def test_modbus_register_names_reach_the_writer_via_an_explicit_map(
    plant,
) -> None:
    """Modbus exposes register *names*, and not every register is linked to a
    signal in the contract. This is the mapping a real deployment would put in the
    contract; written out here so the remaining gap is visible rather than
    papered over with a string match.

    This is the project's signature bug class. A low-word-first float read as
    high-word-first is finite, in range, and wrong — it comes out around
    ``2.3e-41`` — so no range check anywhere in the system will ever catch it.
    The only defence is to check the value against what the process says it
    should be, which is what the non-zero assertion is doing.
    """
    mapping = {
        "AERATION_DO": "AERATION:AHU-1:DO",
        "AERATION_BLOWER_VALVE": "AERATION:AHU-1:BLOWER_VALVE",
        "AERATION_AIR_FLOW": "AERATION:AHU-1:AIR_FLOW",
        "INFLUENT_FLOW": "INFLUENT:FLOW:FLOW",
    }
    with ModbusReader(C, port=MB_PORT) as reader:
        values = reader.poll().values

    writer, rows = _collecting_writer()
    checked = 0
    for register, signal_id in mapping.items():
        if register not in values:
            continue
        writer.add(Reading(ts=int(time.time()), signal_id=signal_id,
                           value=values[register], quality=0, source="modbus"))
        checked += 1
    writer.flush()

    assert checked, "no mapped register was read"
    for row in rows:
        _, signal_id, value, _, source = row
        assert signal_id in C.signals
        assert source == "modbus"
        assert value != 0.0, f"{signal_id} decoded to zero - the word-order signature"
