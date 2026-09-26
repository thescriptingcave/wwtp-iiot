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


def test_every_signal_has_its_equipment_known_or_is_site_level(c: Contract) -> None:
    """Signal ids are AREA:UNIT:EQUIP. The middle component must resolve."""
    for sig in c.signals.values():
        if sig.equipment in c.equipment:
            continue
        # Site/weather and influent/flow are pseudo-units with no equipment.
        assert sig.equipment in {"SITE", "WEATHER", "FLOW", "LIFT"}, (
            f"{sig.id}: unit {sig.equipment!r} is neither declared equipment "
            "nor a known pseudo-unit"
        )


# ─── design rule 1: tag by identity, field by value ───────────────────────────


def test_no_numeric_field_names(c: Contract) -> None:
    """Guards the cardinality trap: a value must never become a tag."""
    for m in c.measurements.values():
        for sig in m.signals:
            has_alpha = any(c.isalpha() for c in sig.field)
            assert has_alpha, f"{sig.id}: field {sig.field!r} looks like a value"


def test_measurement_tags_are_identity_only(c: Contract) -> None:
    """The InfluxDB tag set must contain no value-bearing column."""
    for name in c.measurements:
        tags = c.measurement_tags(name)
        assert "instrument_id" in tags
        assert "site" in tags
        for tag in tags:
            assert not tag.startswith(("value", "val", "reading", "measurement_"))


def test_signal_ids_are_unique(c: Contract) -> None:
    assert len(c.signals) == len(set(c.signals))


def test_table_field_pairs_are_unique(c: Contract) -> None:
    """A duplicate (table, field) silently overwrites in InfluxDB."""
    seen: set[tuple[str, str]] = set()
    for m in c.measurements.values():
        for sig in m.signals:
            key = (m.name, sig.field)
            assert key not in seen, f"duplicate {key}"
            seen.add(key)


# ─── design rule 2: identity lives in the contract, not InfluxDB ──────────────


def test_signals_do_not_embed_metadata_in_influx_fields(c: Contract) -> None:
    """No field may be a serial number, calibration date, or location.

    Those belong in Couchbase. If one appears here it is a schema decision
    that will not scale.
    """
    forbidden = ("serial", "calibrat", "location", "lat", "lon", "vendor", "model")
    for m in c.measurements.values():
        for sig in m.signals:
            for bad in forbidden:
                assert bad not in sig.field.lower(), (
                    f"{sig.id}: field {sig.field!r} looks like metadata — it "
                    "belongs in Couchbase, not InfluxDB"
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
            "registers": [
                {
                    "address": 40000,
                    "name": "R",
                    "type": "float32",
                    "word_order": "big",
                    "unit_id": 1,
                }
            ]
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
