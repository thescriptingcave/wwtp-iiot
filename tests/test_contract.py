"""Contract tests — the highest-value tests in the project.

These validate ``contracts/tags.yaml`` as a *contract*: that it is internally
consistent, and that every consumer derived from it agrees. This is the drift
protection described in docs/TESTING.md — a tag renamed in one place and
forgotten in another is the classic industrial integration bug, and it produces
plausible-looking wrong data rather than an error.
"""

from __future__ import annotations

import pytest
from softplc.contract import (
    CONTRACT_PATH,
    QUALITY_BAD,
    QUALITY_GOOD,
    QUALITY_NAMES,
    QUALITY_UNCERTAIN,
    STATE_FAULT,
    STATE_NAMES,
    STATE_RUNNING,
    STATE_STANDBY,
    STATE_STOPPED,
    UDT_FLOAT32,
    Contract,
    ContractError,
    contract,
    load_contract,
)


@pytest.fixture(scope="module")
def c() -> Contract:
    return contract()


# ─── the contract itself is valid ─────────────────────────────────────────────


def test_contract_loads(c: Contract) -> None:
    assert c.version >= 1
    assert c.site["id"]
    assert c.measurements
    assert c.signals


def test_all_eight_areas_declared(c: Contract) -> None:
    assert set(c.area_ids) == {
        "INFLUENT",
        "PRIMARY",
        "AERATION",
        "SECONDARY",
        "EFFLUENT",
        "SLUDGE",
        "UTILITY",
        "SITE",
    }


def test_every_signal_belongs_to_a_declared_measurement(c: Contract) -> None:
    for sig in c.signals.values():
        assert sig.measurement in c.measurements
        assert sig in c.measurements[sig.measurement].signals


def test_equipment_is_an_asset_or_none_and_holder_is_always_present(
    c: Contract,
) -> None:
    """The two fields this test used to conflate, now kept apart.

    It previously asserted that a signal's middle id component was "declared
    equipment *or* one of the pseudo-units FLOW/LIFT/SITE/WEATHER", which is a
    list of exceptions hard-coded in a test to paper over an overloaded field.

    A foreign key caught it: fifteen signals named a holder that is not in the
    equipment table, so `equipment` was simultaneously "an asset id" and "the
    middle of the signal id". In the previous storage engine nothing could have
    complained, because there was nothing to complain with.

    So: `equipment` is an asset id or None, and `holder` is always present.
    """
    grouping = set()
    for sig in c.signals.values():
        assert sig.holder, f"{sig.id}: holder must always be set"
        if sig.equipment is None:
            grouping.add(sig.holder)
        else:
            assert sig.equipment in c.equipment, (
                f"{sig.id}: equipment {sig.equipment!r} is not declared equipment"
            )

    # The grouping nodes are named, not accidental, and the set is small enough
    # to state. A new one appearing is a design decision, not a typo.
    assert grouping == {"FLOW", "LIFT", "SITE", "WEATHER"}, grouping
    assert len(grouping) < len(c.equipment), "most signals are on real assets"


def test_a_grouping_signal_keeps_its_name_for_the_address_space(
    c: Contract,
) -> None:
    """`equipment` is None for a grouping signal, but the OPC UA folder is still
    called FLOW — a client browsing the address space must not find
    INFLUENT/INFLUENT instead of INFLUENT/FLOW.
    """
    flow = c.signals["INFLUENT:FLOW:FLOW"]
    assert flow.equipment is None
    assert flow.holder == "FLOW"
    assert flow.area == "INFLUENT"


# ─── design rule 1: tag by identity, field by value ───────────────────────────


def test_no_numeric_field_names(c: Contract) -> None:
    """Guards the cardinality trap: a value must never become a tag."""
    for m in c.measurements.values():
        for sig in m.signals:
            has_alpha = any(c.isalpha() for c in sig.field)
            assert has_alpha, f"{sig.id}: field {sig.field!r} looks like a value"


def test_identity_columns_are_identity_only(c: Contract) -> None:
    """Rule 1: identity on ``signal``, value on ``reading``.

    This used to assert that a hand-maintained tuple of InfluxDB tag *names*
    contained nothing value-bearing. The list was a string constant, so the test
    could only ever confirm that a constant matched itself — it had no way to
    reach the thing it was protecting, which was the write path.

    The columns are the schema's now, and the check that still has teeth is
    against the contract: a signal *field* is a column name on `signal`, so a
    field with no letters in it is almost certainly a value that was pasted
    where a name belongs.
    """
    identity = set(c.identity_columns())
    assert identity == {
        "area", "measurement", "field", "unit", "equipment_id",
    }, identity

    for m in c.measurements.values():
        for sig in m.signals:
            assert any(ch.isalpha() for ch in sig.field), (
                f"{sig.id}: field {sig.field!r} has no letters — a column name, "
                "not a value"
            )
            assert ":" not in sig.field and " " not in sig.field, (
                f"{sig.id}: field {sig.field!r} is not a usable column name"
            )


def test_signal_ids_are_unique(c: Contract) -> None:
    assert len(c.signals) == len(set(c.signals))


def test_measurement_field_pairs_are_unique(c: Contract) -> None:
    """A duplicate (measurement, field) is a duplicate column in one table.

    The wording is all that changed; the property did not. It used to be phrased
    as a silent overwrite, which was true and alarming — in the previous engine
    two signals with the same field name in the same measurement wrote to the
    same series and the second simply won. Here the primary key is
    ``(ts, signal_id, source)`` and the field name is not in it, so the collision
    is still possible to *create* and still has to be caught here. A constraint
    cannot express it: it is a statement about the contract, not about any row.
    """
    seen: set[tuple[str, str]] = set()
    for m in c.measurements.values():
        for sig in m.signals:
            key = (m.name, sig.field)
            assert key not in seen, (
                f"duplicate {key}: {sig.id} and an earlier signal share a field "
                "name within one measurement"
            )
            seen.add(key)


# ─── design rule 2: a measurement is a value, never a fact about the plant ────


def test_signals_do_not_embed_metadata_in_their_field_names(c: Contract) -> None:
    """No measurement field may be a serial number, calibration date, or location.

    Those are properties of an *asset*, and assets have a table. A signal is a
    value with a unit and a range, and a signal called ``calibration_due`` is a
    value that happens to have a range, which is a category error: it will be
    deadbanded, charted, and alarmed on exactly like dissolved oxygen.

    This rule needed a second database to express before, which was itself the
    argument against having two. The test survived the collapse; the rationale
    in its name did not need to change.
    """
    forbidden = ("serial", "calibrat", "location", "lat", "lon", "vendor", "model")
    for m in c.measurements.values():
        for sig in m.signals:
            for bad in forbidden:
                assert bad not in sig.field.lower(), (
                    f"{sig.id}: field {sig.field!r} describes the asset, not a "
                    "measurement — it belongs on `equipment`, not `signal`"
                )


# ─── ranges, deadbands, sampling ──────────────────────────────────────────────


def test_ranges_and_bands_are_ordered(c: Contract) -> None:
    for sig in c.signals.values():
        assert sig.range_min < sig.range_max
        assert sig.normal_low <= sig.normal_high


def test_normal_band_sits_inside_range(c: Contract) -> None:
    """A normal band outside the engineering range is always wrong."""
    for sig in c.signals.values():
        assert sig.range_min <= sig.normal_low, f"{sig.id}: normal_low below range_min"
        assert sig.normal_high <= sig.range_max, f"{sig.id}: normal_high above range_max"


def test_deadband_is_usable(c: Contract) -> None:
    """Deadband must be positive and smaller than the range.

    A deadband of zero means every sample is stored (5,184,000/day for 1 Hz
    across 60 tags); a deadband at or above the range span means the signal can
    never be stored at all.
    """
    for sig in c.signals.values():
        assert sig.deadband > 0, f"{sig.id}: deadband must be > 0"
        assert sig.deadband < sig.range_span, f"{sig.id}: deadband >= range span"


def test_sample_intervals_are_sane(c: Contract) -> None:
    for sig in c.signals.values():
        assert 100 <= sig.sample_ms <= 3_600_000, f"{sig.id}: sample_ms out of range"


def test_in_range_rejects_nan_and_inf(c: Contract) -> None:
    sig = c.signal("AERATION:AHU-1:DO")
    assert sig.in_range(2.0)
    assert not sig.in_range(float("nan"))
    assert not sig.in_range(float("inf"))
    assert not sig.in_range(-999.0)
    assert not sig.in_range(999.0)


# ─── design rule 3: equipment state is a separate measurement ────────────────


def test_state_equipment_all_exist(c: Contract) -> None:
    for eq in c.state_equipment:
        assert eq in c.equipment, f"state_equipment references unknown {eq!r}"


def test_state_equipment_covers_all_rotating_plant(c: Contract) -> None:
    """Pumps, blowers and drives must all report state, or the UI has holes."""
    must_report = {"PIT-1", "PIT-2", "PIT-3", "BLW-1", "BLW-2", "BLW-3", "BLW-4"}
    assert must_report <= set(c.state_equipment)


def test_no_state_field_on_a_process_measurement(c: Contract) -> None:
    """The whole point of rule 3: state is not a field on a process signal."""
    for m in c.measurements.values():
        for sig in m.signals:
            assert sig.field not in {"state", "run_state", "status"}, (
                f"{sig.id}: equipment state must be its own measurement, not a "
                "field on {m.name!r}"
            )


# ─── Modbus ──────────────────────────────────────────────────────────────────


def test_modbus_registers_do_not_overlap(c: Contract) -> None:
    occupied: dict[int, str] = {}
    for reg in c.registers:
        for addr in range(reg.address, reg.end_address + 1):
            assert addr not in occupied, (
                f"address {addr} claimed by {occupied.get(addr)} and {reg.name}"
            )
            occupied[addr] = reg.name


def test_float32_registers_state_word_order(c: Contract) -> None:
    """Omitting word_order means the gateway guesses — and guesses wrong."""
    for reg in c.registers:
        if reg.type == UDT_FLOAT32:
            assert reg.word_order in ("big", "little"), f"{reg.name}: no word order"


def test_contract_deliberately_contains_word_order_traps(c: Contract) -> None:
    """The mixed byte order is intentional. Remove this test only if the
    teaching moment in Phase 2 has been replaced with something equivalent."""
    traps = [r for r in c.registers if r.type == UDT_FLOAT32 and r.word_order == "little"]
    assert traps, "expected at least one low-word-first float to exercise the decode bug"


def test_float32_registers_span_two_registers(c: Contract) -> None:
    for reg in c.registers:
        if reg.type == UDT_FLOAT32:
            assert reg.width == 2
            assert reg.end_address == reg.address + 1


def test_registers_are_above_the_40000_convention(c: Contract) -> None:
    """Modbus 4xxxx = holding registers. Anything in 3xxxx is input-only."""
    for reg in c.registers:
        assert 40000 <= reg.address <= 49999, f"{reg.name}: {reg.address}"


def test_heartbeat_register_exists(c: Contract) -> None:
    """Liveness proof, and how a stuck gateway is detected."""
    hb = c.register("HEARTBEAT")
    assert hb.type == "int16"
    assert not hb.writable


# ─── writable surface ────────────────────────────────────────────────────────


def test_writable_surface_is_explicit_and_small(c: Contract) -> None:
    """A tight write surface is a security control, not an oversight."""
    assert len(c.writable) <= 5, "write surface has grown — review it"


def test_writable_signal_ids_and_register_names(c: Contract) -> None:
    for wid, spec in c.writable.items():
        assert "reason" in spec, f"{wid}: every writable value needs a stated reason"
        if wid in c.signals:
            assert c.signals[wid].writable
        else:
            # Otherwise it must name a Modbus register.
            assert c.register(wid), f"{wid}: unknown signal and unknown register"


def test_writable_specs_have_ranges(c: Contract) -> None:
    for wid, spec in c.writable.items():
        if "range" in spec:
            lo, hi = spec["range"]
            assert lo < hi, f"{wid}: range must be ascending"


# ─── permit and design flow ──────────────────────────────────────────────────


def test_permit_limits_are_defined(c: Contract) -> None:
    p = c.permit
    for key in ("eff_nh4_mg_l_30d_mean", "eff_tss_mg_l", "eff_ph_min", "eff_ph_max"):
        assert key in p, f"permit is missing {key}"


def test_effluent_nh4_range_covers_the_permit_limit(c: Contract) -> None:
    """The instrument must be able to observe a breach it must report."""
    permit = c.permit["eff_nh4_mg_l_30d_mean"]
    sig = c.signal("EFFLUENT:FLOW:NH4")
    assert sig.range_max > permit, (
        "instrument range must extend above the permit limit, or breaches are "
        "clipped and invisible"
    )


def test_effluent_tss_range_covers_permit(c: Contract) -> None:
    permit = c.permit["eff_tss_mg_l"]
    sig = c.signal("EFFLUENT:FLOW:TSS")
    assert sig.range_max > permit


def test_do_setpoint_sits_inside_do_normal_band(c: Contract) -> None:
    do = c.signal("AERATION:AHU-1:DO")
    sp = c.signal("AERATION:AHU-1:SETPOINT_DO")
    assert do.normal_low <= sp.normal_low, "setpoint below normal DO band"
    assert sp.normal_high <= do.normal_high, "setpoint above normal DO band"


# ─── quality and state vocabularies ──────────────────────────────────────────


def test_quality_vocabulary_is_complete() -> None:
    assert set(QUALITY_NAMES) == {QUALITY_GOOD, QUALITY_UNCERTAIN, QUALITY_BAD}


def test_state_vocabulary_is_complete() -> None:
    assert set(STATE_NAMES) == {
        STATE_STOPPED,
        STATE_RUNNING,
        STATE_FAULT,
        STATE_STANDBY,
    }


# ─── the loader must reject bad contracts ────────────────────────────────────


def _minimal_contract(tmp_path, **overrides):
    """A tiny but valid contract, for exercising the validator's rejections."""
    base = {
        "version": 1,
        "site": {"id": "S", "name": "Test"},
        "areas": [{"id": "A", "name": "Area"}],
        "equipment": [{"id": "E", "area": "A", "name": "Equip", "type": "pump"}],
        "measurements": [
            {
                "measurement": "m1",
                "unit": "A",
                "signals": [
                    {
                        "id": "A:E:P",
                        "field": "pressure_bar",
                        "eu": "bar",
                        "range": [0, 10],
                        "normal": [1, 5],
                        "deadband": 0.1,
                        "sample_ms": 1000,
                    }
                ],
            }
        ],
        "state_equipment": ["E"],
        "modbus": {
            "endpoint": "tcp://0.0.0.0:5020",
            "unit_id": 1,
            "registers": [
                {
                    "address": 40000,
                    "name": "R",
                    "type": "float32",
                    "word_order": "big",
                    "unit_id": 1,
                    # Every register needs a signal, or the whole contract is
                    # rejected — so a minimal contract links the one it has.
                    "signal": "A:E:P",
                }
            ],
        },
    }
    base.update(overrides)
    p = tmp_path / "tags.yaml"
    import yaml

    p.write_text(yaml.safe_dump(base))
    return p


def test_minimal_contract_is_accepted(tmp_path) -> None:
    c = load_contract(_minimal_contract(tmp_path))
    assert len(c.signals) == 1


def test_rejects_unknown_area_in_signal_id(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["measurements"][0]["signals"][0]["id"] = "NOPE:E:P"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="not declared in `areas`"):
        load_contract(p)


def test_rejects_deadband_at_or_above_range(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["measurements"][0]["signals"][0]["deadband"] = 10.0
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="deadband"):
        load_contract(p)


def test_rejects_overlapping_modbus_registers(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["modbus"]["registers"].append(
        {"address": 40001, "name": "R2", "type": "int16", "unit_id": 1}
    )
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="claimed by both"):
        load_contract(p)


def test_rejects_float32_without_word_order(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    del d["modbus"]["registers"][0]["word_order"]
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="word_order"):
        load_contract(p)


def test_rejects_malformed_signal_id(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["measurements"][0]["signals"][0]["id"] = "A:E"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="AREA:UNIT:EQUIPMENT"):
        load_contract(p)


def test_rejects_state_equipment_that_does_not_exist(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["state_equipment"] = ["NOPE"]
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="unknown equipment"):
        load_contract(p)


def test_rejects_duplicate_field_in_same_measurement(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["measurements"][0]["signals"].append(dict(d["measurements"][0]["signals"][0]))
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="duplicate signal id"):
        load_contract(p)


def test_missing_contract_file_raises_clearly(tmp_path) -> None:
    with pytest.raises(ContractError, match="contract not found"):
        load_contract(tmp_path / "nope.yaml")


def test_real_contract_path_is_correct() -> None:
    """Guards the parents[] path arithmetic."""
    assert CONTRACT_PATH.exists(), f"{CONTRACT_PATH} does not exist"
    assert CONTRACT_PATH.name == "tags.yaml"
    assert CONTRACT_PATH.parent.name == "contracts"


def test_rejects_a_modbus_block_without_unit_id(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    del d["modbus"]["unit_id"]
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="unit_id"):
        load_contract(p)


def test_rejects_a_modbus_block_without_endpoint(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    del d["modbus"]["endpoint"]
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="endpoint"):
        load_contract(p)


def test_contract_carries_the_modbus_block(c: Contract) -> None:
    """Consumers read the protocol config off the contract, not from a second
    source — so a renamed key fails at import rather than silently defaulting."""
    assert c.modbus["unit_id"] == 1
    assert c.modbus["endpoint"].startswith("tcp://")


def test_every_signal_declares_a_valid_deadband_mode(c: Contract) -> None:
    """The mode is validated at load, not in the one component that reads it.

    A typo like ``relitive`` would otherwise become a runtime surprise in the
    gateway — the component furthest from the file that contains the mistake.
    """
    for sig in c.signals.values():
        assert sig.deadband_mode in ("absolute", "relative", "always"), (
            f"{sig.id}: deadband_mode {sig.deadband_mode!r} is not valid"
        )
        # Relative mode is meaningless without a span, and a span of zero would
        # make it silently fall back to absolute.
        if sig.deadband_mode == "relative":
            assert sig.range_span > 0, f"{sig.id}: relative mode needs a range"


def test_rejects_a_misspelled_deadband_mode(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    first = d["measurements"][0]["signals"][0]
    first["deadband_mode"] = "relitive"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="deadband_mode"):
        load_contract(p)


def test_rejects_always_mode_with_a_nonzero_deadband(tmp_path) -> None:
    """``always`` ignores the threshold, so a non-zero one means the author
    expected filtering that will never happen. Better to say so at load."""
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    first = d["measurements"][0]["signals"][0]
    first["deadband_mode"] = "always"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="contradictory"):
        load_contract(p)


# ─── the register → signal link ───────────────────────────────────────────────
#
# Added in Phase 3 to close the gap that stopped the gateway publishing Modbus.
# A register name identifies a *location*; a signal is a *measurement*. The link
# between them is not derivable, so the contract states it.


def test_every_measurement_register_links_to_a_real_signal(c: Contract) -> None:
    linked = {r.name: r.signal for r in c.registers if r.signal}
    assert len(linked) >= 10, "the gateway needs something to publish"
    for name, signal_id in linked.items():
        assert signal_id in c.signals, f"{name} -> {signal_id!r} is not a signal"


def test_the_link_covers_the_word_order_traps(c: Contract) -> None:
    """The two low-word-first registers are the ones a client is most likely to
    get wrong, so they are exactly the ones that must be reachable *and* named.
    A trap you cannot address is a trap you cannot demonstrate."""
    traps = {r.name for r in c.registers if r.word_order == "little"}
    assert traps == {"AERATION_BLOWER_VALVE", "AERATION_WASTE_RATE"}
    for name in traps:
        assert c.register(name).signal is not None, name


def test_unlinked_registers_are_the_non_measurements(c: Contract) -> None:
    """A heartbeat, a fault code, a state bitfield and half a 32-bit value are
    not measurements. Saying so is the point — an implied link that does not
    exist is worse than a declared absence."""
    unlinked = {r.name for r in c.registers if not r.signal}
    assert unlinked == {
        "HEARTBEAT", "FAULT_CODE", "EQUIP_STATE_WORD",
        "PUMP1_RUNTIME_HI", "PUMP1_RUNTIME_LO",
    }


def test_no_signal_is_claimed_by_two_registers(c: Contract) -> None:
    """Otherwise the second is a shadow that is never read, because the first
    wins — and nothing says which."""
    claimed = [r.signal for r in c.registers if r.signal]
    assert len(claimed) == len(set(claimed))


def test_rejects_a_register_naming_a_signal_that_does_not_exist(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["modbus"]["registers"][0]["signal"] = "A:E:NOPE"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="not in the contract"):
        load_contract(p)


def test_rejects_two_registers_claiming_one_signal(tmp_path) -> None:
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["modbus"]["registers"].append({
        "address": 40010, "name": "R2", "type": "float32", "word_order": "big",
        "unit_id": 1, "signal": "A:E:P",
    })
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="claimed by both"):
        load_contract(p)


def test_rejects_a_contract_where_no_register_has_a_signal(tmp_path) -> None:
    """The whole point of the field. A contract without it is a contract the
    gateway cannot publish from, and that should be a load-time error rather than
    a gateway that runs and quietly stores nothing."""
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    del d["modbus"]["registers"][0]["signal"]
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="declares a 'signal:'"):
        load_contract(p)


def test_rejects_a_writable_register_on_a_read_only_signal(tmp_path) -> None:
    """A write path that exists in one file and not the other is a trap."""
    p = _minimal_contract(tmp_path)
    import yaml

    d = yaml.safe_load(p.read_text())
    d["modbus"]["registers"][0]["writable"] = True
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ContractError, match="writable"):
        load_contract(p)


def test_the_server_and_the_contract_agree_on_the_mapping(c: Contract) -> None:
    """The server used to hold its own dict of the same mapping, which meant the
    register map existed in two places and could not disagree loudly."""
    from softplc.servers.modbus import REGISTER_TO_SIGNAL

    from_contract = {r.name: r.signal for r in c.registers if r.signal}
    assert from_contract == REGISTER_TO_SIGNAL
