"""The plant: units wired together with mass balance between them.

The individual units in :mod:`softplc.process.units` know nothing about each
other. This module owns the *coupling* — which stream flows where, and what
concentration each unit sees. That separation is deliberate: unit models are
testable in isolation, and the plant model is testable as a mass balance.

Flow path
---------
    InfluentGenerator → LiftStation → PrimaryClarifier → AerationBasin
                                        ↓                      ↑
                              (primary sludge)          SecondaryClarifier ←┘
                                        ↓                      │
                                   Thickener → AnaerobicDigester → biogas → boiler
                                               ↓
                                       Disinfection → effluent

Two return streams close the loops, and both matter:

* **RAS** (return activated sludge): clarifier underflow back to the
  aeration basin. Large flow, high concentration. Without it the basin has no
  inoculum and MLSS collapses.
* **WAS** (waste activated sludge): the rest of the underflow, leaving the
  plant. This is the only solids *outflow*, which is what makes the SRT real.

The mass-balance property this module exists to guarantee
-------------------------------------------------------
For every stream, what goes in equals what comes out plus what accumulates.
Tests assert this to a tolerance rather than trusting it, because a mass balance
that drifts is a model that will eventually produce a permit breach that is pure
arithmetic error rather than process behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from random import Random

from softplc.contract import Contract, contract as get_contract
from softplc.process.units import (
    AerationBasin,
    AnaerobicDigester,
    Disinfection,
    InfluentGenerator,
    LiftStation,
    PrimaryClarifier,
    Pump,
    SecondaryClarifier,
)

#: Absolute tolerance for a mass-balance check, kg/h. Chosen to be loose enough
#: for float drift over a long run and tight enough to catch a real leak.
MASS_BALANCE_TOLERANCE_KG_H = 0.5

#: Fraction of forward flow diverted direct to waste in the primary clarifier.
PRIMARY_DIRECT_WASTE_FRACTION = 0.03


@dataclass(slots=True)
class PlantSnapshot:
    """One scan's worth of plant state, keyed by contract signal id.

    The keys are the ``id`` fields from ``contracts/tags.yaml``, so the snapshot
    can be handed straight to the Modbus/OPC UA servers and the gateway without a
    translation layer — and a renamed tag becomes a loud KeyError rather than a
    silently missing series.
    """

    values: dict[str, float] = field(default_factory=dict)
    states: dict[str, int] = field(default_factory=dict)
    quality: dict[str, int] = field(default_factory=dict)

    def get(self, key: str, default: float = 0.0) -> float:
        return self.values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.values


class Plant:
    """The whole treatment works, wired together.

    One call to :meth:`step` advances the entire plant by ``dt`` seconds, in the
    order the real process imposes. Order matters: the primary clarifier must
    see this scan's influent, the aeration basin must see the primary's
    *output*, and the clarifier must see the basin's *mixed liquor* — not last
    scan's, which would silently insert a full residence time of lag between
    every pair of units.
    """

    def __init__(self, c: Contract | None = None, seed: int = 0) -> None:
        self.c = c or get_contract()
        self.rng = Random(seed)
        self.t_s: float = 0.0

        design = self.c.design_flow_m3h

        # ── units ────────────────────────────────────────────────────────────
        self.influent = InfluentGenerator(design_flow_m3h=design)

        # Lift station sized for peak wet-weather flow, with duty rotation.
        peak = design * self.c.site.get("design", {}).get("peak_factor", 2.5)
        per_pump = peak / 2.6
        self.lift = LiftStation(
            pumps=[
                Pump("PIT-1", per_pump, 22.0, "lead"),
                Pump("PIT-2", per_pump, 22.0, "lag"),
                Pump("PIT-3", per_pump * 1.15, 30.0, "standby"),
            ]
        )

        self.primary = PrimaryClarifier(
            surface_area_m2=self.c.site.get("primary_area_m2", 620.0)
        )
        self.aeration = AerationBasin(volume_m3=20000.0)
        self.secondary = SecondaryClarifier(surface_area_m2=780.0)
        self.digester = AnaerobicDigester(volume_m3=4200.0)
        self.disinfection = Disinfection(volume_m3=900.0)

        # ── mass-balance accumulators, for the tests ─────────────────────────
        self.cumulative_kg: dict[str, float] = {
            "influent_solids_in": 0.0,
            "primary_solids_out": 0.0,
            "primary_sludge_out": 0.0,
            "aeration_solids_out": 0.0,
            "secondary_solids_out": 0.0,
            "effluent_solids_out": 0.0,
            "was_solids_out": 0.0,
            "inflow_m3": 0.0,
            "effluent_m3": 0.0,
            "overflow_m3": 0.0,
            "was_m3": 0.0,
            "reactor_inventory_kg": 0.0,
            "clarifier_inventory_kg": 0.0,
            "wetwell_m3": 0.0,
            "biomass_growth_kg": 0.0,
            "direct_waste_kg": 0.0,
            "direct_waste_m3": 0.0,
        }

        # ── control state the SCADA layer will override ───────────────────────
        self.dose_mg_l: float = 2.0
        self.cl_dose_enable: bool = True

        # Inventory at t=0. Balances are stated as *changes* in stored material,
        # because solids and water that entered and are still sitting in a basin
        # have not been destroyed — a balance that omits this term drifts by
        # exactly the inventory, which looks like a leak but is not one.
        self._initial_reactor_solids_kg = (
            self.aeration.volume_m3 * self.aeration.mlss_mg_l / 1000.0
        )
        self._initial_clarifier_solids_kg = (
            (self.primary.surface_area_m2 * self.primary.blanket_m * 10.0)
            + (self.secondary.surface_area_m2 * self.secondary.blanket_m * 12.0)
        )
        self._initial_wetwell_m3 = self.lift.level_m * self.lift.area_m2
        self.cumulative_kg["reactor_inventory_kg"] = 0.0
        self.cumulative_kg["clarifier_inventory_kg"] = 0.0
        self.cumulative_kg["wetwell_m3"] = 0.0

    # ── the process step ─────────────────────────────────────────────────────

    def step(self, dt: float) -> PlantSnapshot:
        """Advance the whole plant by ``dt`` seconds."""
        dt_h = dt / 3600.0
        self.t_s += dt

        # ── 1. raw water ────────────────────────────────────────────────────
        self.influent.step(dt)
        raw_m3h = self.influent.flow_m3h

        # ── 2. lift station ─────────────────────────────────────────────────
        self.lift.step(dt, raw_m3h)
        pumped_m3h = self.lift.total_outflow_m3h
        lost_m3h = self.lift.overflow_m3h

        # ── 3. primary treatment ────────────────────────────────────────────
        self.primary.step(dt, pumped_m3h, self.influent.tss_mg_l)
        solids_in_kg_h = pumped_m3h * self.influent.tss_mg_l / 1000.0
        solids_to_sludge_kg_h = (
            self.primary.sludge_rate_m3h * self.primary.underflow_solids_pct * 10.0
        )
        # Primary clarifiers waste a few percent of flow direct from the hopper
        # to the digester, ahead of the blanket scraper. It is a genuine
        # outflow of both water and solids, and omitting it leaves the water
        # balance permanently short by that fraction.
        direct_waste_m3h = pumped_m3h * PRIMARY_DIRECT_WASTE_FRACTION
        direct_waste_kg_h = direct_waste_m3h * self.influent.tss_mg_l / 1000.0
        solids_over_kg_h = max(
            0.0, solids_in_kg_h - solids_to_sludge_kg_h - direct_waste_kg_h
        )

        # Forward flow: what leaves over the weir toward aeration.
        forward_m3h = self.primary.forward_m3h

        # ── 4. aeration ─────────────────────────────────────────────────────
        # The DO control loop lives here because it is a *plant* decision: the
        # required air flow depends on oxygen demand, which depends on what the
        # biology is doing, which depends on what the clarifiers returned last
        # scan. Keeping it in the unit would mean the unit guessing at flows it
        # does not own.
        #
        # The waste rate is computed from the target SRT and the concentration
        # the sludge actually leaves at, so the realised SRT matches the
        # commanded one instead of quietly being a third of it.
        self.aeration.return_sludge_conc_mg_l = max(
            500.0, self.secondary.underflow_conc_mg_l
        )
        self.aeration.step(
            dt,
            forward_m3h,
            # The reactor sees *primary effluent*, not raw water. Passing the
            # influent COD here would double-count removal and inflate MLSS.
            self._primary_cod_mg_l(raw_m3h),
            self._primary_nh4_mg_l(raw_m3h),
            self.influent.temp_c,
            self.secondary.return_sludge_m3h,
            self.primary.tss_mg_l,
        )
        self._drive_dissolved_oxygen(dt)

        # Biomass created from removed substrate — a solids *source*, not a
        # solids loss. Tracked so the balance can be stated honestly.
        growth_kg_h = (
            self.aeration.volume_m3
            * self.aeration.cod_removed_mg_l_h
            * 0.65
            * 0.40
            / 1000.0
        )
        self._accumulate_growth(growth_kg_h, dt_h)

        # ── 5. secondary clarification ─────────────────────────────────────
        # RAS is *commanded* as a fraction of forward flow, exactly as an
        # operator would set it. The clarifier then receives forward + RAS, and
        # the underflow concentration falls out of the solids balance rather
        # than being imposed. Getting this direction backwards makes RAS feed
        # itself — the clarifier's inflow is its own output, and the loop grows
        # until an arbitrary clamp hides it.
        ras_m3h = forward_m3h * self.secondary.ras_ratio
        self.secondary.step(
            dt,
            forward_m3h,
            ras_m3h,
            self.aeration.mlss_mg_l,
            self.aeration.nh4_out_mg_l,
            self.aeration.cod_out_mg_l,
            self.influent.temp_c,
            was_m3h=self.aeration.was_m3h,
        )

        # ── 6. disinfection ────────────────────────────────────────────────
        # One call to learn the contact time, then dose, then apply the dose.
        # The dose is solved to hold the target residual, because that is what a
        # plant actually does and it is why the chlorine bill tracks ammonia.
        self.disinfection.step(
            dt,
            self.dose_mg_l if self.cl_dose_enable else 0.0,
            self.secondary.forward_m3h,
            self.aeration.nh4_out_mg_l,
            self.secondary.tss_mg_l,
            self._effluent_turbidity_ntu(),
            self.aeration.ph,
            self.influent.temp_c,
        )
        if self.cl_dose_enable:
            self.dose_mg_l = self.disinfection.dose_for_target_residual(
                self.disinfection.target_residual_mg_l
            )

        # ── 7. digestion ────────────────────────────────────────────────────
        feed_kg_d = solids_to_sludge_kg_h * 24.0 + (
            self.secondary.was_m3h * self.secondary.underflow_conc_mg_l / 1000.0
        ) * 24.0
        self.digester.step(
            dt,
            feed_kg_d,
            self.aeration.ph,
            self.aeration.wtemp_c,
            boiler_demand_kw=1200.0,
        )

        # ── 8. accumulators ─────────────────────────────────────────────────
        k = self.cumulative_kg
        k["influent_solids_in"] += solids_in_kg_h * dt_h
        k["primary_sludge_out"] += solids_to_sludge_kg_h * dt_h
        k["secondary_solids_out"] += solids_over_kg_h * dt_h
        k["effluent_solids_out"] += (
            self.secondary.tss_mg_l * self.secondary.forward_m3h / 1000.0 * dt_h
        )
        k["was_solids_out"] += (
            self.secondary.was_m3h * self.secondary.underflow_conc_mg_l / 1000.0 * dt_h
        )
        k["inflow_m3"] += raw_m3h * dt_h
        k["effluent_m3"] += self.secondary.forward_m3h * dt_h
        k["overflow_m3"] += lost_m3h * dt_h
        k["was_m3"] += self.secondary.was_m3h * dt_h
        k["direct_waste_kg"] += direct_waste_kg_h * dt_h
        k["direct_waste_m3"] += direct_waste_m3h * dt_h
        # Inventory *changes*. Solids that entered and are still in a basin are
        # stored, not destroyed.
        k["reactor_inventory_kg"] = (
            self.aeration.volume_m3 * self.aeration.mlss_mg_l / 1000.0
        ) - self._initial_reactor_solids_kg
        k["clarifier_inventory_kg"] = (
            self.primary.surface_area_m2 * self.primary.blanket_m * 10.0
            + self.secondary.surface_area_m2 * self.secondary.blanket_m * 12.0
        ) - self._initial_clarifier_solids_kg
        k["wetwell_m3"] = self.lift.level_m * self.lift.area_m2 - self._initial_wetwell_m3

        return self.snapshot()

    def _drive_dissolved_oxygen(self, dt: float) -> None:
        """The DO control loop: proportional approach to the required air flow.

        Deliberately a plain proportional filter rather than a full PID. That is
        what most real plants run for DO — a slow outer loop trimming a fast
        inner one — and it is why a real DO loop carries a small standing error.
        Modelling a perfect controller would produce a DO that sits exactly on
        its setpoint forever, which is the giveaway of an unrealistic simulator.
        """
        target = self.aeration._required_air_m3h()
        gain = DO_CONTROL_GAIN_PER_S
        self.aeration.air_flow_m3h += (
            target - self.aeration.air_flow_m3h
        ) * min(1.0, gain * dt)

    # ── derived influent/effluent chemistry ─────────────────────────────────

    def _primary_cod_mg_l(self, raw_m3h: float) -> float:
        """COD leaving the primary clarifier.

        Primary treatment removes *settleable* organics — roughly 30 % of the COD
        and about 60 % of the TSS. The fraction that *passes* is one minus the
        removal. Multiplying by the removal fraction instead — passing only 30 %
        of the influent COD to the reactor — makes the aeration basin treat a
        load a third of the real one, which quietly starves nitrification and
        under-reports oxygen demand.
        """
        return self.influent.cod_mg_l * (1.0 - PRIMARY_COD_REMOVAL)

    def _primary_nh4_mg_l(self, raw_m3h: float) -> float:
        """Ammonia leaving the primary clarifier.

        Ammonia is soluble, so a primary clarifier removes almost none of it
        (a couple of percent, from the solids-associated fraction). This is why
        nitrification has to happen in the aeration basin and cannot be
        substituted by primary treatment. The removal is small; what *passes* is
        close to all of it.
        """
        return self.influent.nh4_mg_l * (1.0 - PRIMARY_NH4_REMOVAL)

    def _effluent_turbidity_ntu(self) -> float:
        """Effluent turbidity, from effluent TSS and a particle-size assumption."""
        return _clamp(self.secondary.tss_mg_l * 0.55, 0.1, 100.0)

    # ── state for the protocol layer ─────────────────────────────────────────

    def snapshot(self) -> PlantSnapshot:
        """Current state, keyed by contract signal id."""
        inf, ae, sec, pri, dig, dis = (
            self.influent,
            self.aeration,
            self.secondary,
            self.primary,
            self.digester,
            self.disinfection,
        )
        lift = self.lift

        v: dict[str, float] = {
            # influent
            "INFLUENT:FLOW:FLOW": inf.flow_m3h,
            "INFLUENT:FLOW:TURBIDITY": inf.turbidity_ntu,
            "INFLUENT:FLOW:NH4_IN": inf.nh4_mg_l,
            "INFLUENT:FLOW:TEMP": inf.temp_c,
            "INFLUENT:FLOW:PH": inf.ph,
            "INFLUENT:FLOW:CONDUCTIVITY": inf.conductivity,
            # lift station
            "INFLUENT:LIFT:WETWELL_LEVEL": lift.level_m,
            "INFLUENT:LIFT:CURRENT": lift.total_current_a,
            "INFLUENT:LIFT:FLOW": lift.total_outflow_m3h,
            "INFLUENT:LIFT:RUNTIME": max(p.runtime_h for p in lift.pumps),
            "INFLUENT:LIFT:STARTS": float(sum(p.starts for p in lift.pumps)),
            # primary
            "PRIMARY:PRI-CL-1:BLANKET": pri.blanket_m,
            "PRIMARY:PRI-SCR-1:TORQUE": pri.torque_nm,
            "PRIMARY:PRI-CL-1:UNDERFLOW": pri.underflow_solids_pct,
            "PRIMARY:PRI-CL-1:SLUDGE_RATE": pri.sludge_rate_m3h,
            "PRIMARY:PRI-CL-1:TEMP": pri.temp_c,
            # aeration
            "AERATION:AHU-1:DO": ae.do_mg_l,
            "AERATION:AHU-1:SETPOINT_DO": ae.setpoint_do_mg_l,
            "AERATION:AHU-1:AIR_FLOW": ae.air_flow_m3h,
            "AERATION:AHU-1:BLOWER_RPM": ae.blower_rpm,
            "AERATION:AHU-1:BLOWER_VALVE": ae.blower_valve_pct,
            "AERATION:AHU-1:NH4_IN": ae.nh4_in_mg_l,
            "AERATION:AHU-1:NH4_OUT": ae.nh4_out_mg_l,
            "AERATION:AHU-1:NO3_OUT": ae.no3_out_mg_l,
            "AERATION:AHU-1:MLSS": ae.mlss_mg_l,
            "AERATION:AHU-1:ORCH": ae.orch_mg_l_h,
            "AERATION:AHU-1:WTEMP": ae.wtemp_c,
            "AERATION:AHU-1:PH": ae.ph,
            "AERATION:AHU-1:SRT": ae.srt_actual_d,
            "AERATION:AHU-1:WASTE_RATE": ae.was_m3h,
            # secondary
            "SECONDARY:SEC-CL-1:BLANKET": sec.blanket_m,
            "SECONDARY:SEC-SCR-1:TORQUE": sec.torque_nm,
            "SECONDARY:SEC-CL-1:OVERFLOW": sec.tss_mg_l,
            "SECONDARY:SEC-CL-1:TEMP": sec.temp_c,
            # effluent
            "EFFLUENT:FLOW:FLOW": sec.forward_m3h,
            "EFFLUENT:FLOW:PH": dis.ph,
            "EFFLUENT:FLOW:RESIDUAL_CL": dis.residual_mg_l,
            "EFFLUENT:FLOW:TSS": dis.tss_mg_l,
            "EFFLUENT:FLOW:TURBIDITY": self._effluent_turbidity_ntu(),
            "EFFLUENT:FLOW:NH4": dis.nh4_mg_l,
            "EFFLUENT:FLOW:TEMP": dis.temp_c,
            "EFFLUENT:FLOW:CONDUCTIVITY": dis.conductivity,
            "EFFLUENT:DIS-CL-2:BACTI": dis.bacti_mpn_100ml,
            # sludge / digestion
            "SLUDGE:THK-1:TS": 2.4,
            "SLUDGE:DIG-1:PH": dig.ph,
            "SLUDGE:DIG-1:ALKALINITY": dig.alkalinity_mg_l,
            "SLUDGE:DIG-1:VFA_ALK_RATIO": dig.vfa_alk_ratio,
            "SLUDGE:DIG-1:TEMP": dig.temp_c,
            "SLUDGE:DIG-1:GAS_FLOW": dig.gas_flow_m3h,
            "SLUDGE:DIG-1:CH4": dig.ch4_pct,
            "SLUDGE:DIG-1:GAS_PRESSURE": dig.gas_pressure_mbar,
            "SLUDGE:GAS-BLR:BOILER_DUTY": dig.boiler_duty_kw,
            # utility + site
            "UTILITY:SITE:PLANT_POWER": self._plant_power_kw(),
            "SITE:WEATHER:AIR_TEMP": inf.air_temp_c,
            "SITE:WEATHER:RAIN": inf.rain_mm_h,
            "SITE:WEATHER:BARO": inf.baro_hpa,
            "SITE:WEATHER:STORM": 1.0 if inf.storm_active else 0.0,
        }

        states: dict[str, int] = {}
        for p in lift.pumps:
            states[p.id] = _pump_state(p)
        states["SEC-CL-1"] = 1
        states["PRI-CL-1"] = 1
        states["DIG-1"] = 1
        # Continuous-duty equipment that has no modelled failure mode yet. They
        # still have to report state: an instrument list that omits half the
        # plant is what makes a status wall of indicators useless.
        for eq in ("SCREEN-1", "SCREEN-2", "GRIT-1", "PRI-SCR-1", "SEC-SCR-1",
                   "THK-1", "RAS-P-1", "RAS-P-2", "DIS-CL-2"):
            states[eq] = 1
        # Blowers: as many running as the airflow demands, the rest standby.
        blowers = ["BLW-1", "BLW-2", "BLW-3", "BLW-4"]
        per_blower = max(1.0, ae.blower_capacity_m3h / len(blowers))
        needed = math.ceil(ae.air_flow_m3h / per_blower)
        for i, b in enumerate(blowers):
            if i < needed:
                states[b] = 1
            elif i < needed + max(0, 3 - needed):
                states[b] = 3  # standby, ready
            else:
                states[b] = 0
        states["GAS-BLR"] = 1 if dig.boiler_duty_kw > 1.0 else 0
        states["CL-DOS-1"] = 1 if self.cl_dose_enable else 0

        return PlantSnapshot(values=v, states=states, quality={})

    def _plant_power_kw(self) -> float:
        """Total plant power. Aeration dominates — typically 50–60 % of it."""
        ae = self.aeration
        # Blower specific power. A roots blower delivering ~100 m³/h against a
        # 5 m submergence needs roughly 7 kW, so ~0.07 kW per m³/h. The earlier
        # value of 0.0115 was an order of magnitude low, which made aeration
        # look like a rounding error in the plant's energy bill.
        aeration_kw = ae.air_flow_m3h * BLOWER_SPECIFIC_POWER_KW_PER_M3H
        lift_kw = sum(
            p.rated_kw * (0.85 if p.running else 0.0) for p in self.lift.pumps
        )
        misc_kw = 180.0 + 60.0 * inf_power_scale(self.influent.flow_m3h)
        return aeration_kw + lift_kw + misc_kw

    # ── storm and fault entry points (used by the fault library) ────────────

    def start_storm(self, duration_s: float = 3600.0, intensity: float = 1.0) -> None:
        self.influent.start_storm(duration_s=duration_s, intensity=intensity)

    def stop_storm(self) -> None:
        self.influent.stop_storm()

    # ── mass balance, for the tests ─────────────────────────────────────────

    def solids_balance_residual_kg(self) -> float:
        """Solids in, minus solids out, minus what the reactor now holds.

        The full balance is::

            in  =  primary_sludge + effluent_TSS + WAS + Δreactor_inventory

        where Δreactor_inventory is the *change* in solids stored in the
        aeration basin. Including the inventory change is what makes this a real
        balance rather than a comparison that happens to be close: solids that
        entered and are still in the reactor have not been destroyed, and a
        balance that ignores them drifts by exactly that amount.
        """
        k = self.cumulative_kg
        out = (
            k["primary_sludge_out"]
            + k["effluent_solids_out"]
            + k["was_solids_out"]
            + k["direct_waste_kg"]
        )
        stored = k["reactor_inventory_kg"] + k["clarifier_inventory_kg"]
        return k["influent_solids_in"] - out - stored + k["biomass_growth_kg"]

    def _accumulate_growth(self, kg_h: float, dt_h: float) -> None:
        """Record biological growth, which creates solids from dissolved substrate.

        A solids balance on an activated-sludge plant cannot close on solids
        alone: organic matter that leaves the water column as CO₂ and water is
        converted into new biomass, so *more* solids leave as WAS than entered
        as suspended solids. Growth is that term, and leaving it out of the
        balance is what makes a correct model look like it is leaking.
        """
        self.cumulative_kg["biomass_growth_kg"] += kg_h * dt_h

    def water_balance_residual_m3(self) -> float:
        """Water in, minus every water out.

        Water leaves the plant only as effluent, overflow, and waste activated
        sludge. The return sludge is *recirculated*, so it is not an outflow —
        and treating it as one is what makes RAS appear to create water.
        """
        k = self.cumulative_kg
        return (
            k["inflow_m3"]
            - k["effluent_m3"]
            - k["overflow_m3"]
            - k["was_m3"]
            - k["direct_waste_m3"]
            - k["wetwell_m3"]
        )


#: Fraction of influent COD removed by primary clarification. Soluble COD is
#: not settled; only the particulate fraction goes.
PRIMARY_COD_REMOVAL = 0.30
#: Fraction of influent ammonia removed by primary clarification. Ammonia is
#: soluble, so this is nearly zero — which is precisely why nitrification cannot
#: be substituted by primary treatment.
PRIMARY_NH4_REMOVAL = 0.02
#: Proportional gain of the DO control loop, per second. A DO loop is slow by
#: design: the biology responds over minutes, and a fast loop just chases
#: measurement noise. The resulting standing error is realistic.
DO_CONTROL_GAIN_PER_S = 0.08
#: Blower specific power, kW per m³/h of air. Roots blowers need roughly 7 kW
#: per 100 m³/h at typical submergence.
BLOWER_SPECIFIC_POWER_KW_PER_M3H = 0.07


def inf_power_scale(flow_m3h: float) -> float:
    return flow_m3h / 1800.0


def _pump_state(p: Pump) -> int:
    from softplc.scanloop import ScanState

    if not p.available:
        return int(ScanState.FAULT)
    if p.running:
        return int(ScanState.RUNNING)
    if p.duty == "standby":
        return int(ScanState.STANDBY)
    return int(ScanState.STOPPED)


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v
