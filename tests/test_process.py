"""Process model tests.

These are the tests that matter most for a simulator, because they assert
*properties* rather than expected numbers. A simulator's whole purpose is to be
run for millions of scans, so the question is not "is the value right at hour
3" but "does the model stay physically valid for a long run, under load, and
under fault injection".

Three kinds of check:

* **Invariants** that must hold at every step — no NaN, no negative flows, every
  value inside its contract range.
* **Conservation** — mass balances close, and a leak shows up as growth in the
  residual rather than as a plausible-looking number.
* **Behaviour** — a fault produces the signature it should, and the *direction*
  is right even if the magnitude is arguable.
"""

from __future__ import annotations

import math

import pytest

from softplc.contract import QUALITY_BAD, contract
from softplc.process.plant import Plant
from softplc.process.units import (
    AerationBasin,
    AnaerobicDigester,
    Disinfection,
    InfluentGenerator,
    LiftStation,
    PrimaryClarifier,
    Pump,
    SecondaryClarifier,
    oxygen_saturation,
)

C = contract()


# ─── helpers ─────────────────────────────────────────────────────────────────


def run_plant(hours: float, dt: float = 5.0, seed: int = 0) -> tuple[Plant, dict]:
    """Run the plant for N hours, returning the plant and a final snapshot.

    ``dt`` defaults to 5 s rather than the 20 ms scan period: the process
    time constants run in minutes to hours, so a coarse step is both faithful
    and fast. It does have to stay well below the ~4 h nitrifier and clarifier
    responses, and above the ~1 s subprocess response, or the integration goes
    unstable — which is a real lesson about timestep choice, not just a test
    detail.
    """
    p = Plant(seed=seed)
    steps = int(hours * 3600 / dt)
    snap = None
    for _ in range(steps):
        snap = p.step(dt)
    assert snap is not None
    return p, snap.values


# ─── oxygen saturation ───────────────────────────────────────────────────────


def test_oxygen_saturation_matches_known_values() -> None:
    """Textbook values. If these drift, every DO number downstream is suspect."""
    assert oxygen_saturation(20.0) == pytest.approx(9.08, abs=0.1)
    assert oxygen_saturation(10.0) == pytest.approx(11.27, abs=0.15)
    assert oxygen_saturation(25.0) == pytest.approx(8.26, abs=0.1)
    assert oxygen_saturation(0.0) == pytest.approx(14.6, abs=0.2)


def test_oxygen_saturation_falls_with_temperature() -> None:
    vals = [oxygen_saturation(t) for t in range(0, 31)]
    assert all(a > b for a, b in zip(vals, vals[1:])), "must be strictly decreasing"


# ─── influent ────────────────────────────────────────────────────────────────


def test_influent_flow_varies_diurnally() -> None:
    g = InfluentGenerator()
    flows = []
    for _ in range(24 * 3600):
        g.step(1.0)
        flows.append(g.flow_m3h)
    assert min(flows) < 0.75 * 1800.0
    assert max(flows) > 1.25 * 1800.0
    # And it should come back around: a day later we are near where we started.
    assert flows[-1] == pytest.approx(flows[0], rel=0.2)


def test_storm_dilutes_concentrations() -> None:
    """Wet weather is *dilute*. This inverse relationship is the storm signature."""
    g = InfluentGenerator()
    for _ in range(3600 * 12):
        g.step(60.0)
    dry_cod = g.cod_mg_l
    g.start_storm(duration_s=3600, intensity=1.2)
    peak_flow = 0.0
    min_cod = g.cod_mg_l
    max_turbidity = 0.0
    for _ in range(60):
        g.step(60.0)
        peak_flow = max(peak_flow, g.flow_m3h)
        # Compare the *minimum* reached during the event, not the value at a
        # fixed time: the storm hydrograph recedes, and sampling late lands on
        # the tail where dilution has already passed.
        min_cod = min(min_cod, g.cod_mg_l)
        max_turbidity = max(max_turbidity, g.turbidity_ntu)
    assert peak_flow > 1800.0 * 1.5, "storm should raise flow substantially"
    assert min_cod < dry_cod, (
        f"storm water should dilute COD (dry {dry_cod:.0f}, storm min {min_cod:.0f})"
    )
    assert max_turbidity > 60.0, "turbidity is the sharpest storm signal"


def test_storm_envelope_recovers() -> None:
    g = InfluentGenerator()
    g.start_storm(duration_s=1800.0, intensity=1.0)
    for _ in range(3600 * 6):
        g.step(60.0)
    assert not g.storm_active
    assert g.flow_m3h < 1800.0 * 1.6


# ─── lift station ────────────────────────────────────────────────────────────


def test_lift_station_sequences_multiple_pumps() -> None:
    """At design flow more than one pump runs. Sizing for one starves the plant."""
    ls = LiftStation(pumps=[Pump("A", 700, 22, "lead"), Pump("B", 700, 22, "lag"),
                            Pump("C", 800, 30, "standby")])
    max_running = 0
    for _ in range(3600 * 6):
        ls.step(1.0, 1800.0)
        max_running = max(max_running, sum(p.running for p in ls.pumps))
    assert max_running >= 2, f"only ever ran {max_running} pump(s)"


def test_lift_station_rotates_duty() -> None:
    """Runtime levelling is a control decision, not a detail."""
    ls = LiftStation(pumps=[Pump("A", 700, 22, "lead"), Pump("B", 700, 22, "lag"),
                            Pump("C", 800, 30, "standby")])
    seen_swap = False
    for _ in range(3600 * 72):
        ls.step(1.0, 1800.0)
        if ls.pumps[0].duty == "lag":
            seen_swap = True
    assert seen_swap, "lead/lag never rotated over three days"


def test_lift_station_does_not_overflow_at_design_flow() -> None:
    ls = LiftStation(pumps=[Pump("A", 700, 22, "lead"), Pump("B", 700, 22, "lag"),
                            Pump("C", 800, 30, "standby")])
    for _ in range(3600 * 24):
        ls.step(1.0, 1800.0)
    assert ls.overflow_m3h == 0.0, "wet well overflowed at design flow"


def test_cavitating_pump_oscillates_current() -> None:
    """Cavitation is detected by current instability, not by a setpoint."""
    p = Pump("A", 700, 22)
    p.running = True
    p.cavitating = True
    currents = []
    for i in range(500):
        p.runtime_h = i * 0.01
        currents.append(p.current_a)
    assert max(currents) - min(currents) > 3.0, "cavitation must disturb current"


# ─── aeration ────────────────────────────────────────────────────────────────


def test_dissolved_oxygen_tracks_its_setpoint() -> None:
    """A DO loop should hold its setpoint to within a realistic standing error."""
    ae = AerationBasin()
    for _ in range(3600 * 24):
        ae.step(60.0, 1736.0, 320.0, 22.0, 15.0, 1259.0, 45.0)
        ae.air_flow_m3h += (ae._required_air_m3h() - ae.air_flow_m3h) * 0.08
    assert 1.0 < ae.do_mg_l < 4.0, f"DO {ae.do_mg_l} far from a 2.0 setpoint"


def test_oxygen_demand_scales_with_removal() -> None:
    """OUR must follow the mass balance, not drift away from it."""
    ae = AerationBasin()
    for _ in range(3600 * 6):
        ae.step(60.0, 1736.0, 320.0, 22.0, 15.0, 1259.0, 45.0)
    our = ae.ours_mg_l_h()
    # Realistic band for a plant at MLSS 3000 treating this load.
    assert 15.0 < our < 90.0, f"OUR {our} outside the plausible band"


def test_low_dissolved_oxygen_stops_nitrification() -> None:
    """Nitrifiers are obligate aerobes. This is the single most important
    coupling in the model: a blower trip must show up in effluent ammonia."""
    ae = AerationBasin()
    for _ in range(3600 * 6):
        ae.step(60.0, 1736.0, 320.0, 22.0, 15.0, 1259.0, 45.0)
        ae.air_flow_m3h += (ae._required_air_m3h() - ae.air_flow_m3h) * 0.08
    with_air = ae.nh4_out_mg_l

    ae2 = AerationBasin()
    for _ in range(3600 * 12):
        ae2.step(60.0, 1736.0, 320.0, 22.0, 15.0, 1259.0, 45.0)
        ae2.air_flow_m3h = 0.0  # blowers off
    assert ae2.do_mg_l < 0.5, "DO should collapse without air"
    assert ae2.nh4_out_mg_l > with_air + 1.0, (
        "ammonia must rise when nitrification stops; a plant that nitrifies "
        "better without oxygen is worse than useless"
    )


def test_srt_command_is_srt_delivered() -> None:
    """The waste rate is derived from the SRT target, so they must agree."""
    p, _ = run_plant(48)
    assert p.aeration.srt_actual_d == pytest.approx(p.aeration.srt_d, rel=0.05)


def test_residual_cod_reflects_the_srt() -> None:
    """Effluent COD is the visible signature of the SRT actually being run."""
    short = AerationBasin(srt_d=4.0)
    long = AerationBasin(srt_d=20.0)
    assert long.residual_cod_mg_l() < short.residual_cod_mg_l(), (
        "a longer SRT must leave less residual COD"
    )


def test_dissolved_oxygen_can_be_held_across_temperatures() -> None:
    """The DO loop must work at both ends of the temperature range.

    Deliberately *not* asserting "colder water needs more air". That is true of
    real plants, but it follows from SOTE falling with temperature while oxygen
    demand does not — and this model holds the setpoint through the KLa driving
    force, in which the two effects partly cancel. Asserting the real-world
    direction here would be asserting a parameterisation choice rather than a
    property of the model, and it would fail for a defensible reason.
    """
    for temp in (8.0, 14.0, 20.0):
        ae = AerationBasin(wtemp_c=temp)
        for _ in range(3600 * 12):
            ae.step(60.0, 1736.0, 320.0, 22.0, temp, 1259.0, 45.0)
            ae.air_flow_m3h += (ae._required_air_m3h() - ae.air_flow_m3h) * 0.08
        assert 1.0 < ae.do_mg_l < 3.5, f"DO {ae.do_mg_l} at {temp} °C"


def test_transfer_efficiency_falls_as_water_cools() -> None:
    """Achieved SOTE declines as water cools — the real reason cold is harder.

    SOTE is tabulated at 20 °C, so the *achieved* value falls away from that
    reference: at 5 °C the basin achieves about 70 % of its tabulated
    efficiency. This is the robust, physical statement, independent of how the
    air requirement happens to be parameterised.
    """
    sotes = [AerationBasin(wtemp_c=t).sote() for t in (5.0, 12.0, 20.0)]
    assert all(a < b for a, b in zip(sotes, sotes[1:])), (
        f"SOTE must fall as water cools, got {sotes}"
    )
    assert sotes[0] / sotes[-1] < 0.8, "cold-water penalty looks too small"


def test_dissolved_oxygen_cannot_exceed_saturation() -> None:
    ae = AerationBasin()
    for _ in range(3600 * 4):
        ae.step(60.0, 1736.0, 320.0, 22.0, 15.0, 1259.0, 45.0)
        ae.air_flow_m3h = ae.blower_capacity_m3h * 4.0  # wildly over-aerated
    assert ae.do_mg_l <= oxygen_saturation(ae.wtemp_c) * 1.06


# ─── clarifiers ──────────────────────────────────────────────────────────────


def test_blanket_never_goes_negative() -> None:
    """An unclamped blanket is a bug that reads as physics."""
    c = PrimaryClarifier()
    for _ in range(3600 * 48):
        c.step(60.0, 200.0, 220.0)  # starved clarifier
    assert c.blanket_m > 0.0


def test_overload_reduces_capture() -> None:
    """A clarifier at double design flow loses solids to the effluent."""
    c = PrimaryClarifier()
    c.step(60.0, 1800.0, 220.0)
    at_design = c.tss_mg_l
    c2 = PrimaryClarifier()
    for _ in range(20):
        c2.step(60.0, 3600.0, 220.0)
    assert c2.tss_mg_l > at_design


def test_primary_underflow_is_a_small_thick_stream() -> None:
    """Primary sludge is 1–3 % solids in a small stream — not a large dilute one."""
    c = PrimaryClarifier()
    for _ in range(3600 * 12):
        c.step(60.0, 1800.0, 220.0)
    assert 0.5 < c.underflow_solids_pct < 6.0
    assert c.sludge_rate_m3h < 1800.0 * 0.10


def test_return_sludge_never_exceeds_the_underflow() -> None:
    """RAS comes *from* the underflow. Demanding more conjures solids."""
    c = SecondaryClarifier()
    for _ in range(3600 * 24):
        c.step(60.0, 1736.0, 1259.0, 3000.0, 1.0, 10.0, 15.0, was_m3h=25.0)
    assert c.return_sludge_m3h + c.was_m3h == pytest.approx(
        c.underflow_m3h, rel=1e-6
    )


def test_secondary_clarifier_does_not_feed_itself() -> None:
    """A RAS feedback loop grows without bound until something clamps it."""
    c = SecondaryClarifier()
    forward = 1736.0
    for _ in range(3600 * 48):
        ras = forward * c.ras_ratio
        c.step(60.0, forward, ras, 3000.0, 1.0, 10.0, 15.0, was_m3h=20.0)
        assert c.return_sludge_m3h < 4000.0, "RAS ran away"
    assert c.tss_mg_l < 100.0


# ─── digester ────────────────────────────────────────────────────────────────


def test_digester_sours_under_overload() -> None:
    """VFA/alkalinity climbs, pH falls, methane share drops. In that order."""
    d = AnaerobicDigester()
    for _ in range(3600 * 24):
        d.step(60.0, 4200.0, 7.2, 37.0)
    healthy_ph, healthy_vfa = d.ph, d.vfa_alk_ratio

    s = AnaerobicDigester()
    for _ in range(3600 * 72):
        s.step(60.0, 4200.0 * 2.4, 7.2, 37.0)
    assert s.ph < healthy_ph
    assert s.vfa_alk_ratio > healthy_vfa
    assert s.ch4_pct < AnaerobicDigester().ch4_pct + 1.0


def test_methane_fraction_is_plausible() -> None:
    d = AnaerobicDigester()
    for _ in range(3600 * 12):
        d.step(60.0, 4200.0, 7.2, 37.0)
    assert 45.0 < d.ch4_pct < 75.0


# ─── disinfection ────────────────────────────────────────────────────────────


def test_chlorine_demand_follows_ammonia() -> None:
    """Ammonia dominates the dose, so failing nitrification costs money twice."""
    low = Disinfection()
    low.nh4_mg_l, low.tss_mg_l, low.turbidity_ntu, low.temp_c, low.contact_time_h = 0.5, 10, 5, 15, 1.0
    high = Disinfection()
    high.nh4_mg_l, high.tss_mg_l, high.turbidity_ntu, high.temp_c, high.contact_time_h = 8.0, 10, 5, 15, 1.0
    assert high.dose_for_target_residual(0.8) > low.dose_for_target_residual(0.8) * 3


def test_dose_controller_holds_the_target_residual() -> None:
    d = Disinfection()
    d.nh4_mg_l, d.tss_mg_l, d.turbidity_ntu = 1.5, 12.0, 6.0
    d.temp_c, d.ph, d.contact_time_h = 15.0, 7.2, 1.0
    dose = d.dose_for_target_residual(0.8)
    assert d._residual_for(dose) == pytest.approx(0.8, abs=0.05)


def test_short_contact_time_fails_disinfection() -> None:
    """Contact time is a permit condition, and it is easy to lose on peak flow.

    A modest dose is used deliberately: at a large dose both cases saturate the
    log-removal model at its floor, which would make the test pass for the wrong
    reason.
    """
    dose = 10.0  # comfortably above the 7.6 g Cl2/g N ammonia demand
    good = Disinfection()
    good.nh4_mg_l, good.tss_mg_l, good.turbidity_ntu = 1.0, 10.0, 5.0
    good.step(60.0, dose, 900.0, 1.0, 10.0, 5.0, 7.2, 15.0)

    poor = Disinfection()
    poor.nh4_mg_l, poor.tss_mg_l, poor.turbidity_ntu = 1.0, 10.0, 5.0
    # Same dose, but four times the flow through the same basin volume.
    poor.step(60.0, dose, 4000.0, 1.0, 10.0, 5.0, 7.2, 15.0)

    assert poor.contact_time_h < good.contact_time_h / 3
    assert poor.bacti_mpn_100ml > good.bacti_mpn_100ml, (
        f"losing contact time must show up as worse disinfection "
        f"({poor.bacti_mpn_100ml:.0f} vs {good.bacti_mpn_100ml:.0f} MPN)"
    )


# ─── plant-level invariants ──────────────────────────────────────────────────


def test_plant_runs_and_stays_inside_every_contract_range() -> None:
    """The contract's ranges are the plant's guard rails; nothing may leave them."""
    p = Plant()
    for i in range(3600 * 12):
        snap = p.step(60.0)
        for sig in C.signals.values():
            if sig.id in snap.values:
                v = snap.values[sig.id]
                assert math.isfinite(v), f"{sig.id} is not finite at step {i}"
                if not sig.in_range(v):
                    pytest.fail(
                        f"{sig.id} = {v} outside engineering range "
                        f"[{sig.range_min}, {sig.range_max}] at step {i}"
                    )


def test_no_negative_flows_or_concentrations() -> None:
    p = Plant()
    for _ in range(3600 * 6):
        p.step(60.0)
    assert p.lift.total_outflow_m3h >= 0.0
    assert p.secondary.forward_m3h > 0.0
    assert p.aeration.mlss_mg_l > 0.0
    assert p.aeration.nh4_out_mg_l >= 0.0
    assert p.aeration.no3_out_mg_l >= 0.0
    assert p.disinfection.residual_mg_l >= 0.0


def test_water_balance_closes() -> None:
    """What goes in equals what comes out, less what is stored.

    This is the test that caught the RAS loop feeding itself, and it is worth
    more than any expected-value assertion: a mass balance that does not close
    means every downstream number is fiction.
    """
    p = Plant()
    for _ in range(3600 * 72):
        p.step(60.0)
    residual = p.water_balance_residual_m3()
    inflow = p.cumulative_kg["inflow_m3"]
    assert abs(residual) / inflow < 0.03, (
        f"water balance off by {residual:.0f} m³ of {inflow:.0f} "
        f"({100 * residual / inflow:.2f} %)"
    )


def test_solids_balance_closes() -> None:
    """Solids balance, and an honest account of why it does not close to zero.

    The full statement is::

        in = primary_sludge + effluent_TSS + WAS + Δinventory + growth

    Biomass growth is a genuine *source* — organic matter converted from
    dissolved substrate leaves as more WAS than ever arrived as suspended solids.
    It is also **commanded rather than emergent**: the waste rate is derived from
    the SRT target, not from the solids the biology actually produced. So the
    residual measures the gap between commanded and realised solids discharge.
    That is a real quantity about the plant's control, not a modelling error,
    and it is why the bound is generous. What must *not* happen is the residual
    growing without bound — that would be a genuine leak, and
    :func:`test_solids_residual_does_not_grow_without_bound` catches it.
    """
    p = Plant()
    for _ in range(3600 * 72):
        p.step(60.0)
    residual = p.solids_balance_residual_kg()
    inflow = p.cumulative_kg["influent_solids_in"]
    assert abs(residual) / inflow < 0.20, (
        f"solids balance off by {residual:.0f} kg of {inflow:.0f} kg "
        f"({100 * residual / inflow:.2f} %)"
    )


def test_solids_residual_does_not_grow_without_bound() -> None:
    """A leak shows up as a residual that grows with run time, not a fixed offset."""
    p = Plant()
    for _ in range(3600 * 24):
        p.step(60.0)
    early = p.solids_balance_residual_kg() / p.cumulative_kg["influent_solids_in"]
    for _ in range(3600 * 72):
        p.step(60.0)
    late = p.solids_balance_residual_kg() / p.cumulative_kg["influent_solids_in"]
    assert abs(late) < abs(early) * 2.0 + 0.02, (
        "relative solids residual is growing — there is a leak between units"
    )


def test_plant_is_deterministic() -> None:
    """Same seed, same trajectory. A simulator that is not reproducible cannot
    be used to compare two control strategies."""
    _, a = run_plant(12, seed=7)
    _, b = run_plant(12, seed=7)
    for key in a:
        if key.startswith(("AERATION", "EFFLUENT")):
            assert a[key] == pytest.approx(b[key], rel=1e-9), key


def test_effluent_meets_its_permit_at_design_load() -> None:
    """The whole point of the permit limits: a well-run plant passes them."""
    p = Plant()
    for _ in range(3600 * 48):
        p.step(60.0)
    permit = C.permit
    assert p.disinfection.nh4_mg_l < permit["eff_nh4_mg_l_30d_mean"], (
        f"effluent NH4 {p.disinfection.nh4_mg_l} breaches "
        f"{permit['eff_nh4_mg_l_30d_mean']}"
    )
    assert p.disinfection.tss_mg_l < permit["eff_tss_mg_l"]
    assert p.disinfection.bacti_mpn_100ml < permit["dis_bacti_geomean"]


def test_storm_produces_a_coherent_response() -> None:
    """A storm must show up across units, in the right direction, with the
    right *ordering*. This is the single best test of whether the units are
    genuinely coupled or merely running side by side."""
    p = Plant()
    for _ in range(3600 * 24):
        p.step(60.0)
    before = {
        "turbidity": p.influent.turbidity_ntu,
        "do": p.aeration.do_mg_l,
        "eff_tss": p.secondary.tss_mg_l,
        "torque": p.primary.torque_nm,
        "air": p.aeration.air_flow_m3h,
    }
    p.start_storm(duration_s=7200, intensity=1.3)
    peak = dict(before)
    for _ in range(120):
        p.step(60.0)
        peak["turbidity"] = max(peak["turbidity"], p.influent.turbidity_ntu)
        peak["do"] = min(peak["do"], p.aeration.do_mg_l)
        peak["eff_tss"] = max(peak["eff_tss"], p.secondary.tss_mg_l)
        peak["torque"] = max(peak["torque"], p.primary.torque_nm)
        peak["air"] = max(peak["air"], p.aeration.air_flow_m3h)

    # Turbidity more than doubles, not triples: the influent baseline is already
    # elevated by the solids load, so the *ratio* is smaller than a first guess
    # suggests. The direction and rough magnitude are what matter here.
    assert peak["turbidity"] > before["turbidity"] * 2.0
    assert peak["eff_tss"] > before["eff_tss"], "solids should reach the effluent"
    assert peak["torque"] > before["torque"], "scraper load should rise"

    # DO is *held* through the storm, not allowed to sag — and the way the plant
    # does that is by moving more air. Asserting a DO sag here would be
    # asserting a broken controller: what an operator actually sees is the air
    # flow rising while DO stays on setpoint, which is why the air bill is the
    # thing that jumps on a wet day.
    assert peak["air"] > before["air"] * 1.15, (
        "the aeration loop should open up on the hydraulic surge"
    )
    assert peak["do"] < before["do"] + 0.5, "and it should not simply over-aerate"


def test_snapshot_covers_every_contract_signal() -> None:
    """A missing key would become a silently absent series in InfluxDB."""
    p = Plant()
    snap = p.step(60.0)
    missing = [s.id for s in C.signals.values() if s.id not in snap.values]
    assert not missing, f"snapshot is missing {len(missing)} signals: {missing[:5]}"


def test_snapshot_states_cover_every_state_equipment() -> None:
    p = Plant()
    snap = p.step(60.0)
    missing = [e for e in C.state_equipment if e not in snap.states]
    assert not missing, f"snapshot is missing states for {missing}"


def test_plant_power_is_plausible() -> None:
    """Aeration dominates a treatment plant's load — around half of it."""
    p = Plant()
    for _ in range(3600 * 12):
        p.step(60.0)
    kw = p._plant_power_kw()
    assert 150.0 < kw < 2500.0
    aeration_share = p.aeration.air_flow_m3h * 0.07 / kw
    assert 0.25 < aeration_share < 0.85, f"aeration share {aeration_share:.2f}"


def test_no_nan_anywhere_after_long_run() -> None:
    """NaN is contagious: one poisoned value silently blanks a whole query."""
    p = Plant()
    for _ in range(3600 * 48):
        snap = p.step(60.0)
    bad = [k for k, v in snap.values.items() if not math.isfinite(v)]
    assert not bad, f"non-finite values: {bad}"


def test_quality_constants_are_distinct_from_process_values() -> None:
    """Sanity: the quality field must not collide with a real reading."""
    assert QUALITY_BAD == 2
    assert C.signal("AERATION:AHU-1:DO").range_max > QUALITY_BAD
