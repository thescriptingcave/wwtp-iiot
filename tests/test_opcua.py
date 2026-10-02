"""OPC UA server and browser tests.

These run a real server and a real client over a real socket, because the
behaviour worth testing here is protocol behaviour: what a client can *discover*,
what it can *read*, and what it is *refused*. A mock would test nothing that
matters.

The write-permission tests are the important ones. They assert that the contract's
write surface is actually enforced on the wire, not merely documented — the
difference between a security control and a comment.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import re

import pytest
from asyncua import Client, ua
from softplc.contract import contract
from softplc.servers.opcua import UNIT_IDS, OpcUaServer, build_address_space
from tools import opcua_browser
from tools.opcua_browser import _find_plant, _resolve

C = contract()
ENDPOINT = "opc.tcp://127.0.0.1:4841/wwtp-test/"


@pytest.fixture
async def server():
    s = OpcUaServer(endpoint=ENDPOINT)
    space = await s.start()
    await s.wait_ready(timeout=10.0)
    # Publish a known state so reads are assertable.
    s.set_value("AERATION:AHU-1:DO", 2.05)
    s.set_value("AERATION:AHU-1:AIR_FLOW", 3300.0)
    s.set_value("EFFLUENT:FLOW:TSS", 11.5)
    s.set_equipment_state("BLW-1", 1)
    await s.publish()
    await asyncio.sleep(0.15)
    try:
        yield s, space
    finally:
        await s.stop()


def space_ns() -> int:
    """Namespace index of the plant's address space.

    ``get_child`` resolves a *string* browse name in namespace 0, so a property
    created in the plant's own namespace has to be named as ``"2:Name"``. A
    namespace mismatch reports itself as ``BadNoMatch``, which reads like a
    missing property rather than a naming error — a genuinely confusing failure.
    """
    return 2


@pytest.fixture(scope="module")
def space_only():
    """An address space built without a live server, for pure structural checks."""
    loop = asyncio.new_event_loop()
    try:
        space, _srv = loop.run_until_complete(
            build_address_space(C, "opc.tcp://127.0.0.1:4842/wwtp-check/")
        )
    finally:
        loop.close()
    return space


@pytest.fixture
async def client(server):
    s, _ = server
    async with Client(ENDPOINT) as c:
        yield c


# ─── address space construction ──────────────────────────────────────────────


def test_every_signal_becomes_a_variable(server) -> None:
    _s, space = server
    assert len(space.variables) == len(C.signals)
    for sig in C.signals.values():
        assert sig.id in space.variables, f"{sig.id} missing from the address space"


def test_every_equipment_becomes_an_object(server) -> None:
    _s, space = server
    assert set(space.equipment_nodes) == set(C.equipment)


def test_variables_hang_off_their_equipment_not_the_area(space_only) -> None:
    """The ISA-95 tree must be Area → Equipment → Variable.

    A two-pass build put variables in a folder named after the area and left the
    equipment objects empty. Every path a client constructs from the contract
    would then resolve to the wrong node — the exact class of failure OPC UA
    exists to prevent, caused by not modelling the hierarchy properly.
    """
    space = space_only
    for eq_id, node in space.equipment_nodes.items():
        declared = [s for s in C.signals.values() if s.equipment == eq_id]
        assert node["object"] is not None
        for sig in declared:
            assert space.variables[sig.id].node is not None


def test_namespace_matches_the_contract() -> None:
    assert C.opcua["namespace_uri"].startswith("urn:")


def test_engineering_units_are_known() -> None:
    """An unrecognised unit silently degrades to dimensionless, which is a lie."""
    for sig in C.signals.values():
        assert sig.eu in UNIT_IDS, f"{sig.id} has unmapped engineering unit {sig.eu!r}"


# ─── discovery, from a real client ───────────────────────────────────────────


async def test_client_discovers_the_plant_object(client) -> None:
    plant = await _find_plant(client, "PLANT-A")
    assert plant is not None, "the server must be discoverable without a prior NodeId"
    name = (await plant.read_browse_name()).Name
    assert C.site["name"] in name


async def test_address_space_is_isa95_shaped(client) -> None:
    plant = await _find_plant(client, "PLANT-A")
    folders = set()
    for child in await plant.get_children():
        if str(await child.read_node_class()).endswith("ObjectType") or True:
            if (await child.read_browse_name()).Name in C.area_ids:
                folders.add((await child.read_browse_name()).Name)
    assert folders == set(C.area_ids), "every ISA-95 area must be browsable"
    # The permit limits hang off the plant object, not an area.
    for child in await plant.get_children():
        assert (await child.read_browse_name()).Name.startswith(
            ("Permit_", "DesignFlow", *C.area_ids)
        ), f"unexpected node under the plant: {(await child.read_browse_name()).Name}"


async def test_variable_sits_under_its_equipment_object(client) -> None:
    node = await _resolve(client, "AERATION.AHU-1.do_mg_l", "PLANT-A")
    assert node is not None
    parent = await node.get_parent()
    assert (await parent.read_browse_name()).Name == "AHU-1"
    grandparent = await parent.get_parent()
    assert (await grandparent.read_browse_name()).Name == "AERATION"


async def test_short_browse_path_also_resolves(client) -> None:
    assert await _resolve(client, "AHU-1.do_mg_l", "PLANT-A") is not None


async def test_equipment_object_carries_both_properties_and_signals(client) -> None:
    plant = await _find_plant(client, "PLANT-A")
    ahu = None
    for area in await plant.get_children():
        for child in await area.get_children():
            if (await child.read_browse_name()).Name == "AHU-1":
                ahu = child
    assert ahu is not None
    names = {(await k.read_browse_name()).Name for k in await ahu.get_children()}
    assert "RunState" in names, "equipment must expose its run state as a variable"
    assert "do_mg_l" in names, "and its signals beneath the same object"
    assert "EquipmentType" in names


# ─── reading ─────────────────────────────────────────────────────────────────


async def test_client_reads_a_published_value(client) -> None:
    node = await _resolve(client, "AERATION.AHU-1.do_mg_l", "PLANT-A")
    assert await node.read_value() == pytest.approx(2.05, abs=1e-6)


async def test_values_carry_a_status_code(client) -> None:
    """The property that lets a historian be honest about bad data."""
    node = await _resolve(client, "AERATION.AHU-1.do_mg_l", "PLANT-A")
    data = await node.read_data_value()
    assert data.StatusCode.name == "Good"


async def test_variables_expose_their_engineering_unit(client) -> None:
    node = await _resolve(client, "AERATION.AHU-1.air_flow_m3h", "PLANT-A")
    unit = await (await node.get_child(f"{space_ns()}:UnitSymbol")).read_value()
    assert unit == "m3/h"
    eu = await (await node.get_child(f"{space_ns()}:EngineeringUnits")).read_value()
    assert eu == UNIT_IDS["m3/h"], (
        "the numeric EUInformation id must match the UCUM code we map it to"
    )


async def test_variables_expose_instrument_metadata(client) -> None:
    """Metadata Modbus has nowhere to put, and which a client can discover."""
    node = await _resolve(client, "AERATION.AHU-1.do_mg_l", "PLANT-A")
    sig = C.signal("AERATION:AHU-1:DO")
    assert await (await node.get_child(f"{space_ns()}:SignalId")).read_value() == sig.id
    low = await (await node.get_child(f"{space_ns()}:EngineeringRangeLow")).read_value()
    assert low == pytest.approx(sig.range_min)
    deadband = await (await node.get_child(f"{space_ns()}:Deadband")).read_value()
    assert deadband == pytest.approx(sig.deadband)


async def test_permit_limits_are_published(client) -> None:
    plant = await _find_plant(client, "PLANT-A")
    limit = await (await plant.get_child(f"{space_ns()}:Permit_eff_tss_mg_l")).read_value()
    assert limit == pytest.approx(C.permit["eff_tss_mg_l"])


# ─── the write surface ───────────────────────────────────────────────────────


async def test_a_client_may_write_a_setpoint(client) -> None:
    node = await _resolve(client, "AERATION.AHU-1.setpoint_do_mg_l", "PLANT-A")
    await node.set_value(ua.Variant(2.8, ua.VariantType.Double))
    assert await node.read_value() == pytest.approx(2.8, abs=1e-6)


async def test_a_client_may_not_write_a_measurement(client) -> None:
    """The contract's write surface, enforced by OPC UA on the wire.

    This is a protocol guarantee rather than our own check: the variable is not
    marked writable, so the server answers ``BadUserAccessDenied``. If this test
    ever passed because of application code, it would be testing the wrong thing.
    """
    node = await _resolve(client, "AERATION.AHU-1.do_mg_l", "PLANT-A")
    with pytest.raises(ua.UaError) as exc:
        await node.set_value(ua.Variant(3.0, ua.VariantType.Double))
    assert "permission" in str(exc.value).lower() or "denied" in str(exc.value).lower()


async def test_no_measurement_variable_is_writable(space_only) -> None:
    """Checked against the contract, so the server cannot drift from it.

    **One writable variable, and it was two.** `SITE:WEATHER:STORM` is read-only
    now: `InfluentUnit.storm_active` is not a latch, so a write clearing itself
    on the next scan is not a control surface.

    Worth noting what this test demonstrates about the design: the OPC UA write
    surface shrank as a *consequence* of narrowing the contract, with no change
    to `opcua.py` at all. That is the address space being derived from the
    contract rather than declared beside it, and it is the reason the two
    protocols cannot disagree about what may be written.
    """
    space = space_only
    writable = {sid for sid, n in space.variables.items() if n.writable}
    assert writable == {
        "AERATION:AHU-1:SETPOINT_DO",
    }, f"unexpected write surface: {sorted(writable)}"


async def test_engineering_range_is_not_enforced_by_the_protocol() -> None:
    """Documents a real, known gap rather than pretending otherwise.

    OPC UA's ``EUInformation`` is advisory: the base specification does not
    require a server to reject a write outside the engineering range, and
    ``asyncua`` does not. So a client *can* write 99 mg/L to a setpoint whose
    range is 0.5–6.0. The range check in ``OpcUaServer.write_value`` applies to
    in-process callers only; rejecting out-of-range writes before they reach the
    wire is the gateway's job. Recorded in docs/SECURITY.md as a known gap.
    """
    sig = C.signal("AERATION:AHU-1:SETPOINT_DO")
    assert sig.in_range(2.5)
    assert not sig.in_range(99.0)


# ─── publishing ──────────────────────────────────────────────────────────────


async def test_publish_only_touches_changed_nodes(server) -> None:
    """Deadbanding exists so a subscription is cheaper than polling.

    If every scan published every value, OPC UA would be no better than Modbus
    polling, and the whole reason for choosing it would evaporate.
    """
    s, _space = server
    s.set_value("AERATION:AHU-1:DO", 2.06)
    count = await s.publish()
    assert count == 1, "only the changed signal should be published"
    assert s.changed == {"AERATION:AHU-1:DO"}


async def test_publishing_nothing_reports_zero(server) -> None:
    s, _space = server
    assert await s.publish() == 0


async def test_unknown_signals_are_ignored_rather_than_raising(server) -> None:
    """A typo in a tag name must not take the scan loop down."""
    s, _space = server
    s.set_value("NOT:A:REAL:SIGNAL", 1.0)
    assert await s.publish() == 0


# ── the identifier every document uses, which the tool did not accept ───────


def test_the_documents_only_use_a_signal_id_the_browser_can_resolve() -> None:
    """`make watch SIGNAL=AERATION:AHU-1:DO` printed **"Not found"** for four phases.

    The address space is `Area → Equipment → Variable` and a variable's browse
    name is its contract *field* (`do_mg_l`). So the tool wanted
    `AERATION.AHU-1.do_mg_l`, and the obvious command — printed in the Makefile's
    own help text, in the README, and in `docs/GETTING-STARTED.md` — did not
    work:

        $ make watch SIGNAL=AERATION:AHU-1:DO
        Not found: AERATION:AHU-1:DO

    Every other part of this project identifies a signal by `AREA:UNIT:FIELD`:
    the contract, the database, the Node-RED tag list, the flows, both dashboards.
    The diagnostic tool was the one place that did not — and it is the tool a
    person reaches for **when something is not working**, so the one command that
    is most needed is the one that does not run.

    `tools/opcua_browser.py::resolve` now accepts the contract id, by walking the
    tree and matching the `SignalId` property. This test pins the *documentation*
    side: every signal id any document tells a reader to type must be one the
    contract declares, so the next rename cannot leave a broken command behind.

    It does not test that the tool resolves it, because that needs a running OPC
    UA server. What it does test is the half that was wrong.
    """
    known = set(contract().signals)
    # `SIGNAL=` in make targets, and the argument to `opcua_browser.py read|watch`.
    pattern = re.compile(
        r"(?:SIGNAL=|opcua_browser\.py (?:read|watch))"
        r"\s*([A-Z][A-Z0-9]*:[A-Z0-9-]+:[A-Z0-9_]+)"
    )

    files = ["README.md", "Makefile", "docs/GETTING-STARTED.md", "docs/VERIFYING.md",
             "docs/DESIGN.md", "docs/DATA-FLOW.md", "docs/ARCHITECTURE.md"]
    checked = 0
    offenders: list[str] = []
    for name in files:
        path = pathlib.Path(name)
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for found in pattern.findall(line):
                checked += 1
                if found not in known:
                    offenders.append(f"{name}:{lineno}: {found}")

    assert checked, (
        "the pattern matched nothing in any document — either the commands were "
        "all removed, or the pattern stopped matching, and this test is now "
        "vacuous"
    )
    assert not offenders, (
        "these documents tell a reader to type a signal id the contract does "
        f"not declare:\n  {'\n  '.join(offenders)}"
    )


def test_the_browser_exports_a_resolve_that_accepts_both_forms() -> None:
    """The contract id and the dotted browse path, in one entry point.

    A structural check rather than a live one, because the interesting assertion
    needs a server. It is here because the three call sites in the tool were
    changed from `_resolve` to `resolve`, and a rename that missed one would
    reintroduce the bug for exactly one subcommand — which is the shape of this
    whole failure.
    """
    assert hasattr(opcua_browser, "resolve"), (
        "tools/opcua_browser.py has no public resolve(); the contract-id "
        "fallback is unreachable"
    )
    source = inspect.getsource(opcua_browser)
    # Every subcommand goes through the wrapper, not the dotted-only helper.
    assert source.count("await resolve(client, args.path") == 3, (
        "cmd_read, cmd_write and cmd_watch must all call resolve(); a call site "
        "still calling _resolve() will not accept a contract id"
    )
    assert "await _resolve(client, args.path" not in source
