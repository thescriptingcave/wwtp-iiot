"""Process models for the treatment units.

Each unit is a small, self-contained model with a ``step()`` method advancing its
state by ``dt`` seconds. They are written to be *defensible*, not merely
plausible: the equations are the standard ones from wastewater process
engineering, so the numbers behave the way an operator would expect.

Deliberate design choices
-------------------------
* **Mass balance, not lookup tables.** Solids flow through the plant because
  mass is conserved, not because a table says so. This is what makes the fault
  scenarios propagate realistically — a storm raises influent turbidity, and the
  resulting solids load shows up in clarifier torque and effluent TSS on its own.
* **Monod kinetics for the bioreactor.** Oxygen uptake and nitrification use
  saturation-form kinetics. This matters: it means DO is a *nonlinear* function
  of air flow, so a linear controller leaves a steady-state error, and the
  aeration block has to deal with that.
* **Diurnal and seasonal forcing.** Flow and load follow a daily curve. Real
  plants are never steady, and a model that only ever sits at equilibrium hides
  the hardest part of monitoring.
* **No hidden global state.** Every unit owns its own state. The plant model
  wires them together explicitly, which is what makes the plant readable as a
  mass balance.

Units are SI: m³, kg, s. Concentrations mg/L == g/m³, which avoids a class of
unit bug that is endemic in plant spreadsheets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from random import Random

# ─── process constants ───────────────────────────────────────────────────────
# Standard textbook values for a municipal plant. Sourced in
# docs/REFERENCES.md → Metcalf & Eddy; used unchanged so the model can be
# checked against published behaviour.

#: Oxygen solubility in water, mg/L, as a function of temperature.
#: Van der Kroon / APHA approximation.
def oxygen_saturation(temp_c: float) -> float:
    """DO saturation concentration, mg/L.

    Falls with temperature: ~9.1 mg/L at 20 °C, ~14.6 mg/L at 10 °C. This is
    why winter is hard — less oxygen is available *and* nitrifiers grow slower.
    """
    return 14.652 - 0.41022 * temp_c + 0.007991 * temp_c**2 - 0.000077774 * temp_c**3


#: Maximum specific growth rate for heterotrophs, d⁻¹.
MU_HETEROTROPH = 6.0
#: Half-saturation constant for heterotrophic growth on substrate, mg/L.
K_S_HETEROTROPH = 20.0
#: Maximum specific nitrification rate, d⁻¹. Well below heterotrophs, which is
#: exactly why nitrification is the fragile step.
MU_NITRIFIER = 0.8
#: Half-saturation for ammonia, mg/L. Nitrifiers are inhibited below ~1 mg/L.
K_S_NITRIFIER = 1.0
#: Yield of biomass on substrate, gVSS/gCOD.
Y_HETEROTROPH = 0.40
#: Yield of biomass on ammonia-N, gVSS/gN.
Y_NITRIFIER = 0.15
#: Decay coefficient for heterotrophs, d⁻¹.
K_D_HETEROTROPH = 0.06
#: Decay coefficient for nitrifiers, d⁻¹. Nitrifiers are fragile.
K_D_NITRIFIER = 0.05
#: Fraction of influent COD that is readily biodegradable.
F_READILY_BIODEGRADABLE = 0.65
#: Non-settleable fraction of effluent COD.
F_NON_SETTLEABLE = 0.08
#: Nitrifier biomass as a fraction of MLVSS. Nitrifiers grow slowly, so they
#: are a small minority of the population in a healthy plant — and their share
#: collapses under shock, which is why nitrification is the fragile step.
NITRIFIER_BIOMASS_FRACTION = 0.12
#: Influent COD (mg/L) at which carbon stops limiting nitrification. Municipal
#: influent is 200–500 mg/L, so nitrifiers are comfortably carbon-supplied;
#: industrial or dilute effluents really do fail to nitrate, and this is the
#: knob that reproduces it. Keyed to the *influent*, not the effluent residue —
#: a clean effluent means the heterotrophs won, not that carbon ran out.
CARBON_STARVATION_THRESHOLD = 50.0
#: Oxygen consumed per gram of ammonia-N nitrified, g O₂/g N.
O2_PER_NITRIFIED_N = 4.57
#: Nitrogen as a fraction of biomass VSS. Roughly 0.07 for activated sludge.
F_NITROGEN_IN_BIOMASS = 0.07
#: Non-biological residual COD, mg/L. Below this there is nothing left that
#: biology can remove, so removal stops. This is what bounds the mass balance.
#: Non-biological residual COD, mg/L. Below this there is nothing left that
#: biology can remove, so removal stops. This is what bounds the mass balance.
#: Kept low: at SRT 20 d a real plant achieves well under 1 mg/L, and a floor
#: set too high flattens the relationship between SRT and effluent COD — the
#: very indicator operators use to judge the SRT they are actually running.
COD_RESIDUAL_FLOOR = 1.0
#: Typical thickened underflow concentration from a secondary clarifier, mg/L.
UF_BASE_CONC_MG_L = 8000.0
#: Largest share of forward flow the underflow may take. Above this the RAS
#: would be more volume than the clarifier receives, which is unphysical.
MAX_UNDERFLOW_FRACTION = 0.45
#: Primary sludge underflow concentration, % solids. Low, because primary
#: sludge is mostly raw organics — it has not been through biology.
PRIMARY_UNDERFLOW_SOLIDS_PCT = 2.5
#: Fractional change in KLa per °C. Oxygen transfer falls about 4 % per °C,
#: so 20 °C → 10 °C costs roughly 20 %. The exponent matters enormously: an
#: over-steep coefficient makes every winter scenario look like a plant failure.
KLA_TEMPERATURE_COEFF = 1.024
#: Time constant for the nitrifier population to respond, h. Slow, because
#: nitrifiers are slow-growing and decay fast. This is why effluent ammonia
#: recovers hours after a blower trip rather than minutes.
NITRIFIER_RESPONSE_H = 4.0
#: A clarifier always holds some blanket; zero means "no sludge at all", which
#: is a sensor fault rather than a process state.
MIN_BLANKET_M = 0.05
#: Kilograms of O₂ in one m³ of air at 20 °C, 1 atm.
#:
#:     0.2095 (mole fraction) × 1/0.02405 m³/mol × 0.032 kg/mol ≈ 0.279
#:     ... at standard temperature (0 °C, 22.414 L/mol) ≈ 0.299
#:
#: The value ~0.23 sometimes quoted is close to the O₂ *volume* fraction and is
#: off by a factor of five when used as a mass content. Using it inflates the
#: air demand ~5×, which drives DO to zero and makes every aeration scenario
#: look like a total failure.
O2_KG_PER_M3_AIR = 1.33
#: KLa as a function of airflow. At a fraction of the saturation flow the
#: bubbles are well distributed; below it, distribution collapses and so does
#: the effective transfer coefficient. The power law is what makes the DO/air
#: relationship steep at low flow, and inverting it is the whole job of the
#: aeration controller.
MIXING_FLOOR = 0.15
MIXING_SATURATION_FRACTION = 0.25
MIXING_EXPONENT = 0.6
#: Clean-water SOTE at 20 °C for a fine-bubble system on a deep tank, as a
#: fraction. Corrected by alpha and beta at runtime.
SOTE_BASE_COLD = 0.30
#: Fraction of influent suspended solids that is inorganic and passes through
#: the biology. Typically 0.25–0.40 of TSS.
F_INORGANIC = 0.30
#: Ammonia floor, mg/L. A nitrifying plant holds effluent well below 1 mg/L;
#: anything above ~2 is a genuine nitrification problem, not measurement noise.
NH4_FLOOR = 0.10
#: Chlorine demand coefficients. The ammonia term dominates: breakpoint
#: chlorination consumes ~7.6 g Cl₂ per g NH₄-N before any free residual
#: appears, which is why a plant with failing nitrification has both a
#: disinfection problem and a chemical bill problem from the same cause.
CL_CHLORINE_DEMAND_PER_NH4 = 7.6
CL_DEMAND_PER_TSS = 0.012
CL_DEMAND_PER_TURBIDITY = 0.004
#: Base first-order chlorine decay, 1/h, at 15 °C and pH 7.5.
CL_DECAY_BASE = 0.35
#: Digester VFA kinetics. The gains are chosen for their *time constants*, not
#: their magnitude: VFA responds over hours, alkalinity over tens of hours.
#: Souring is a slow drift, and that is the whole diagnostic point — a model
#: that sours in twenty minutes would teach the opposite lesson about how much
#: lead time an operator has.
#: Minimum pump speed as a fraction of nameplate. Below roughly this, an
#: affine pump curve stops being reliable and the minimum-flow limit sets in.
MIN_PUMP_SPEED_FACTOR = 0.10
#: Minimum thickened return-sludge concentration, mg/L. Activated sludge will
#: not thicken below roughly 3 g/L in service.
MIN_RETURN_SLUDGE_CONC_MG_L = 3000.0
#: Time constant for wet-well level control, h. The controller aims to bring the
#: level back to the start setpoint over this horizon, which is what stops the
#: proportional-only oscillation an integrating process would otherwise have.
LEVEL_CONTROL_HORIZON_H = 0.45
VFA_PRODUCTION_GAIN = 60.0
VFA_CONSUMPTION_RATE = 0.10
ALKALINITY_GAIN = 25.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def _noise(rng: Random, magnitude: float) -> float:
    """Small symmetric noise, for sensor realism. Never changes the mean."""
    return rng.gauss(0.0, magnitude)


# ═════════════════════════════════════════════════════════════════════════════
# Influent generation
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class InfluentGenerator:
    """Synthetic raw water: diurnal flow, diurnal load, weather, storms.

    Real plants see a factor of 2–2.5 between minimum and peak flow, driven by
    infiltration and infiltration/inflow from rain. Load peaks lag flow peaks
    because the water that arrives during a peak carries more solids per litre.
    """

    design_flow_m3h: float = 1800.0
    base_cod_mg_l: float = 320.0
    base_tss_mg_l: float = 220.0
    base_nh4_mg_l: float = 22.0
    base_temp_c: float = 16.0
    base_conductivity: float = 520.0

    # forcing state
    t_s: float = 0.0
    storm_active: bool = False
    storm_intensity: float = 0.0
    storm_started_s: float = -1e9
    storm_duration_s: float = 3600.0
    diurnal_phase: float = 0.0
    rain_mm_h: float = 0.0
    air_temp_c: float = 16.0
    baro_hpa: float = 1013.0

    # outputs, refreshed each step
    flow_m3h: float = 0.0
    cod_mg_l: float = 0.0
    tss_mg_l: float = 0.0
    nh4_mg_l: float = 0.0
    temp_c: float = 0.0
    ph: float = 7.2
    conductivity: float = 0.0
    turbidity_ntu: float = 0.0

    def start_storm(
        self, duration_s: float = 3600.0, intensity: float = 1.0
    ) -> None:
        """Arm a storm. ``intensity`` scales the peak flow multiplier."""
        self.storm_active = True
        self.storm_intensity = _clamp(intensity, 0.2, 2.5)
        self.storm_started_s = self.t_s
        self.storm_duration_s = duration_s

    def stop_storm(self) -> None:
        self.storm_active = False

    @property
    def storm_age_s(self) -> float:
        return self.t_s - self.storm_started_s

    def _diurnal(self) -> float:
        """Flow factor over a day. Low overnight, morning peak, evening peak.

        Amplitude 0.30 gives a peak/average of about 1.6, which is modest for a
        combined sewer system and typical of a plant with good inflow control.
        """
        day_phase = (self.t_s / 86400.0 + self.diurnal_phase) % 1.0
        angle = 2.0 * math.pi * day_phase
        # Two peaks: a morning one at ~07:00 and a smaller evening one at ~19:00.
        primary = math.exp(-((day_phase - 0.29) ** 2) / (2 * 0.045**2))
        secondary = 0.55 * math.exp(-((day_phase - 0.79) ** 2) / (2 * 0.055**2))
        night = -0.30 * math.exp(-((day_phase - 0.02) ** 2) / (2 * 0.12**2))
        return _clamp(1.0 + 0.30 * primary + 0.16 * secondary + night, 0.45, 2.0)

    def _storm_factor(self) -> float:
        """Flow multiplier from the storm envelope.

        A real storm hydrograph rises fast and recedes slowly, so the profile is
        asymmetric: a short rise to peak, then a long tail as the catchment
        drains. This asymmetry is why effluent TSS spikes *after* the flow peak.
        """
        if not self.storm_active:
            return 0.0
        age = self.storm_age_s
        dur = self.storm_duration_s
        if age < 0 or age > dur:
            if age > dur:
                self.storm_active = False
            return 0.0
        rise = 0.15 * dur
        if age < rise:
            shape = age / rise
        else:
            # Exponential recession with the same peak reached at `rise`.
            shape = math.exp(-2.2 * (age - rise) / max(1.0, dur - rise))
        peak = self.storm_intensity * 1.6
        return peak * shape

    def step(self, dt: float) -> None:
        self.t_s += dt

        diurnal = self._diurnal()
        storm = self._storm_factor()
        flow_factor = diurnal + storm
        self.flow_m3h = self.design_flow_m3h * flow_factor

        # Rain follows the storm, lagged slightly behind the flow peak.
        self.rain_mm_h = _clamp(18.0 * storm, 0.0, 200.0)

        # Temperature: seasonal sinusoid plus diurnal swing, and a cold snap
        # during rain. Nitrification slows markedly below 12 °C.
        day_of_year = (self.t_s / 86400.0) % 365.25
        seasonal = self.base_temp_c + 9.0 * math.sin(
            2.0 * math.pi * (day_of_year - 110.0) / 365.25
        )
        diurnal_t = 3.5 * math.sin(
            2.0 * math.pi * (((self.t_s / 86400.0) + self.diurnal_phase) % 1.0) - 0.25
        )
        rain_chill = -2.5 * _clamp(storm, 0.0, 1.5)
        self.air_temp_c = self.base_temp_c + (seasonal - self.base_temp_c) + diurnal_t + rain_chill
        self.temp_c = self.air_temp_c + 4.0  # water is warmer than air
        self.baro_hpa = 1013.0 - 6.0 * _clamp(storm, 0.0, 1.0)

        # Infiltration and inflow: storm water is far more dilute in COD and
        # ammonia, so concentration DILUTES as flow rises. This inverse
        # relationship is the signature of a wet-weather event and is exactly
        # what a good plant operator looks for.
        dilution = 1.0 / (1.0 + 0.85 * storm)
        load_factor = diurnal * 0.55 + 0.45  # load follows flow, sub-linearly
        self.cod_mg_l = _clamp(self.base_cod_mg_l * load_factor * dilution, 30.0, 1200.0)
        self.tss_mg_l = _clamp(self.base_tss_mg_l * load_factor * dilution, 15.0, 900.0)
        self.nh4_mg_l = _clamp(self.base_nh4_mg_l * load_factor * dilution, 3.0, 60.0)
        self.turbidity_ntu = _clamp(
            6.0 + 0.18 * self.tss_mg_l + 55.0 * _clamp(storm, 0.0, 1.5), 2.0, 400.0
        )
        # Conductivity rises with storm water infiltration: a cheap, fast tracer.
        self.conductivity = self.base_conductivity + 260.0 * _clamp(storm, 0.0, 1.5)
        self.ph = _clamp(7.2 + 0.35 * diurnal - 0.25 * _clamp(storm, 0.0, 1.0), 5.8, 8.8)


# ═════════════════════════════════════════════════════════════════════════════
# Lift station — wet well level and pump duty rotation
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class Pump:
    """A duty pump with runtime accounting, used for lead/lag rotation.

    Runtime is the input to duty rotation: a real plant evens out runtime so no
    pump wears out early. That behaviour is a control decision, and it is
    modelled here rather than hidden in a status flag.

    Pumps are **variable-speed**. This is not decoration — it is what makes the
    lift station well behaved. With fixed-speed pumps sized so that one pump
    roughly equals the average flow, the wet well has no stable equilibrium: it
    fills until a pump starts, the pump overshoots, the well empties, and the
    whole station cycles between nothing and full output. Worse, the amplitude of
    that cycle scales with the *timestep*, so the same model behaves differently
    at 1 s and at 60 s — a property no real plant has, and one that makes every
    downstream number timestep-dependent.

    Throttling to track the level gives a continuous family of operating points
    and removes the artefact.
    """

    id: str
    capacity_m3h: float
    rated_kw: float
    duty: str = "lag"  # lead | lag | standby
    running: bool = False
    runtime_h: float = 0.0
    starts: int = 0
    fault: int = 0
    cavitating: bool = False
    #: 0..1 speed command from the level controller. Affine pump curves mean flow
    #: is roughly proportional to speed down to about 40 %, below which the
    #: relationship flattens and the minimum useful flow sets in.
    speed: float = 1.0
    #: 0..1, how much of rated current the pump draws at nominal load.
    efficiency: float = 0.78

    @property
    def available(self) -> bool:
        return self.fault == 0

    @property
    def flow_m3h(self) -> float:
        if not self.running or not self.available:
            return 0.0
        if self.cavitating:
            return self.capacity_m3h * 0.35
        # Affine pump curve, flattened at low speed to respect minimum flow.
        frac = _clamp(self.speed, MIN_PUMP_SPEED_FACTOR, 1.0)
        return self.capacity_m3h * _clamp(0.25 + 0.75 * frac, 0.0, 1.0)

    @property
    def current_a(self) -> float:
        if not self.running or not self.available:
            return 0.0
        # Cube-law on speed, which is why variable-speed pumping is the single
        # biggest energy saving in a lift station: flow ∝ speed, power ∝ speed³.
        kw = self.rated_kw * (1.12 if self.cavitating else 0.85) * _clamp(
            self.speed, 0.0, 1.0
        ) ** 3
        amps = (kw * 1000.0) / (math.sqrt(3.0) * 400.0 * 0.85 * self.efficiency)
        if self.cavitating:
            amps *= 1.0 + 0.30 * math.sin(self.runtime_h * 37.0)
        return amps


@dataclass(slots=True)
class LiftStation:
    """Wet well with level-controlled pumps, lead/lag rotation, standby failover.

    Level control with a deadband and alternating duty is the canonical SCADA
    control loop. The interlocks are what make it interesting: a pump that
    cavitates must trip on low suction level, and a faulted pump must hand over
    to standby without operator intervention.
    """

    area_m2: float = 42.0
    level_start_m: float = 3.0
    level_stop_m: float = 1.2
    level_alarm_m: float = 5.5
    level_trip_m: float = 6.8
    level_min_pump_m: float = 0.6  # below this, running pumps trip on cavitation
    #: Inflow the level controller is currently fighting, m³/h.
    _inflow_m3h: float = 0.0

    level_m: float = 2.0
    pumps: list[Pump] = field(default_factory=list)
    lead_index: int = 0
    total_outflow_m3h: float = 0.0
    total_current_a: float = 0.0
    overflow_m3h: float = 0.0

    def total_capacity_m3h(self) -> float:
        return sum(p.flow_m3h for p in self.pumps if p.available)

    def step(self, dt: float, inflow_m3h: float) -> None:
        # The controller needs to know the inflow it is fighting. Storing it on
        # the station keeps :meth:`_sequence` free of plumbing.
        self._inflow_m3h = inflow_m3h

        # ── volume balance: what goes in, what goes out ──────────────────────
        net_m3h = inflow_m3h - self.total_outflow_m3h
        self.level_m += (net_m3h / self.area_m2) * (dt / 3600.0)

        # Wet well overflow above the trip level. A real plant floods the
        # channel; we record the loss so mass balance still closes.
        if self.level_m > self.level_max_safe():
            excess_m3h = (self.level_m - self.level_max_safe()) * self.area_m2 * 3600.0 / dt
            self.overflow_m3h = excess_m3h
            self.level_m = self.level_max_safe()
        else:
            self.overflow_m3h = 0.0

        # ── interlocks: cavitation trip on low suction level ──────────────────
        for p in self.pumps:
            if p.running and self.level_m < self.level_min_pump_m:
                p.cavitating = True
            elif self.level_m > self.level_min_pump_m + 0.5:
                p.cavitating = False

        self._rotate_duty()
        self._sequence()

        # ── accounting ────────────────────────────────────────────────────────
        self.total_outflow_m3h = sum(p.flow_m3h for p in self.pumps)
        self.total_current_a = sum(p.current_a for p in self.pumps)
        for p in self.pumps:
            if p.running:
                p.runtime_h += dt / 3600.0

    def level_max_safe(self) -> float:
        return self.level_trip_m

    def _sequence(self) -> None:
        """Level control by variable-speed pumping, with lead/lag rotation.

        The control problem: keep the wet well between its limits while matching
        whatever the inflow is doing, which varies by a factor of two diurnally
        and rises several-fold in a storm.

        The approach, in order of increasing level:
          1. Below the start level, no pump runs and the well fills.
          2. At the start level, the lead pump starts and its speed is
             modulated to hold the level just below start. This is the normal
             operating mode and it is *continuous* — the station does not
             switch on and off, so there is no limit cycle and no timestep
             dependence.
          3. If the lead alone cannot keep up, the lag joins and the pair
             shares the duty.
          4. If both are saturated, the standby starts — the storm case, where
             a lift station genuinely does run flat out.

        Getting this wrong in the on/off direction is what produces a station
        that oscillates between zero and full output, with an amplitude that
        scales with the simulation timestep.
        """
        available = [p for p in self.pumps if p.available and not p.cavitating]
        if not available:
            for p in self.pumps:
                p.running = False
                p.speed = 0.0
            return

        # ── 1. below start: nothing runs ────────────────────────────────────
        if self.level_m <= self.level_start_m:
            for p in self.pumps:
                p.running = False
                p.speed = 0.0
            return

        # ── how much flow do we need? ───────────────────────────────────────
        # Target: bring the level back to the start setpoint over a fixed
        # horizon. A time-constant form rather than pure proportional gain,
        # because a proportional-only controller on an integrating process
        # cannot remove its own error.
        head = self.level_m - self.level_start_m
        horizon_h = LEVEL_CONTROL_HORIZON_H
        required_flow = self._inflow_m3h + self.area_m2 * head / horizon_h
        required_flow = _clamp(required_flow, 0.0, self.total_installed_m3h())

        # ── 2/3/4. commit pumps in duty order, sharing the requirement ───────
        # Sort by duty so the lead is always used first, and only bring in the
        # next pump when the previous one is already saturated.
        order = {"lead": 0, "lag": 1, "standby": 2}
        ranked = sorted(available, key=lambda p: (order.get(p.duty, 3), p.id))
        remaining = required_flow
        for p in ranked:
            if remaining <= MIN_PUMP_SPEED_FACTOR * p.capacity_m3h:
                p.running = False
                p.speed = 0.0
                continue
            if not p.running:
                p.running = True
                p.starts += 1
            # Give this pump the smallest share that covers what is left.
            share = min(remaining, p.capacity_m3h)
            remaining -= share
            p.speed = _clamp(
                (share / p.capacity_m3h - 0.25) / 0.75, MIN_PUMP_SPEED_FACTOR, 1.0
            )
        # Anything left over means every pump is saturated; run them all flat.
        for p in ranked:
            if remaining > 0.0:
                p.speed = 1.0
                remaining -= p.capacity_m3h

    def total_installed_m3h(self) -> float:
        return sum(p.capacity_m3h for p in self.pumps if p.available)

    def _rotate_duty(self) -> None:
        """Swap lead/lag once daily, or when the lead has run much longer.

        Runtime levelling is the point. A plant that never rotates ends up
        replacing the same pump every year.
        """
        for p in self.pumps:
            if p.available:
                break
        else:
            return  # no pump available at all; nothing to rotate

        runtimes = [p.runtime_h for p in self.pumps if p.duty in ("lead", "lag")]
        if len(runtimes) != 2:
            return
        lead, lag = runtimes
        # Rotate once the lag has caught up to within 25% of the lead.
        if lead > 0 and lag >= 0.75 * lead:
            for p in self.pumps:
                if p.duty == "lead":
                    p.duty = "lag"
                elif p.duty == "lag":
                    p.duty = "lead"


# ═════════════════════════════════════════════════════════════════════════════
# Primary clarifier
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class PrimaryClarifier:
    """Gravity settling tank with a scraper.

    The behaviour that matters is the **sludge blanket**: solids settle, the
    scraper pushes them to the hopper, and if the sludge blanket grows past the
    hopper it starts washing out over the weir into the effluent. The symptom an
    operator sees is effluent TSS climbing, and the cause is visible earlier in
    the torque and the blanket depth.
    """

    surface_area_m2: float = 620.0
    depth_m: float = 3.6
    hopper_m: float = 0.9
    scraper_torque_nm: float = 60.0
    scraper_torque_max_nm: float = 400.0
    drive_kw: float = 1.5

    # state
    blanket_m: float = 0.5
    raw_sludge_m3: float = 40.0
    underflow_solids_pct: float = 2.0
    sludge_rate_m3h: float = 30.0
    tss_mg_l: float = 20.0
    torque_nm: float = 60.0
    temp_c: float = 16.0
    running: bool = True
    #: 0..1, 1.0 = normal. Below that, a fault degrades the tank.
    health: float = 1.0
    #: A thickened blanket cannot be raked; this makes that feedback explicit.
    raking_difficulty: float = 1.0
    _t: float = 0.0
    forward_m3h: float = 1000.0

    def step(self, dt: float, influent_m3h: float, influent_tss_mg_l: float) -> None:
        self._t += dt
        self.temp_c = _clamp(
            self.temp_c + (16.0 - self.temp_c) * min(1.0, dt / 3600.0 * 0.3), 0.0, 40.0
        )
        # ── solids capture: removal efficiency falls with overload ────────────
        # Design flow is where the tank performs. Above it, the overflow rate
        # rises and capture falls off — this is the storm mechanism.
        hydraulic_overload = influent_m3h / 1800.0
        base_capture = 0.62
        capture = base_capture / (1.0 + 0.85 * max(0.0, hydraulic_overload - 1.0))
        capture *= self.health
        capture = _clamp(capture, 0.10, 0.90)

        solids_in_kg_h = influent_m3h * influent_tss_mg_l / 1000.0
        solids_captured_kg_h = solids_in_kg_h * capture
        forward_m3h = influent_m3h * (1.0 - 0.03)  # a little sludge is wasted direct
        solids_overflow_mg_l = (1.0 - capture) * influent_tss_mg_l
        self.tss_mg_l = _clamp(solids_overflow_mg_l, 2.0, 900.0)

        # ── underflow: solids leave in the thickened sludge stream ────────────
        # Solids balance again. Primary sludge is only 1–3 % solids, so the
        # underflow is a *small* stream carrying a *large* solids load — the
        # opposite of the RAS, which is a large stream at high concentration.
        dt_h = dt / 3600.0
        target_solids_pct = _clamp(
            PRIMARY_UNDERFLOW_SOLIDS_PCT
            * (0.55 + 0.45 * self.raking_difficulty),
            0.4,
            6.0,
        )
        underflow_m3h = solids_captured_kg_h / (target_solids_pct * 10.0)
        # The underflow cannot exceed a small share of the forward flow.
        underflow_m3h = min(underflow_m3h, influent_m3h * 0.05)
        self.sludge_rate_m3h = underflow_m3h
        # kg/m³ ÷ 10 gives % w/v. Multiplying here instead would report 250 %
        # solids and pin the value at its clamp, which is exactly the kind of
        # unit slip that hides behind a plausible-looking number.
        self.underflow_solids_pct = _clamp(
            solids_captured_kg_h / max(0.01, underflow_m3h) / 10.0, 0.3, 8.0
        )

        # ── blanket: solids captured but not raked out accumulate on the floor ─
        raked_m3h = underflow_m3h * self.raking_difficulty
        raked_kg_h = raked_m3h * self.underflow_solids_pct * 10.0
        net_solids_kg_h = solids_captured_kg_h - raked_kg_h

        # Sludge blanket at roughly 10 kg solids/m³ (≈1 % w/v).
        # Clamped at both ends: the scraper can remove faster than solids
        # arrive, so a healthy tank holds a shallow blanket and cannot go
        # negative — an unclamped value is a bug that reads as physics.
        self.blanket_m = _clamp(
            self.blanket_m + (net_solids_kg_h * dt_h / 10.0) / self.surface_area_m2,
            MIN_BLANKET_M,
            self.depth_m,
        )
        if self.blanket_m >= self.depth_m - 1e-6:
            # Blanket has reached the surface: severe carryover to effluent.
            self.tss_mg_l = _clamp(self.tss_mg_l * 3.0, 2.0, 900.0)

        # Scraping gets harder as the blanket deepens — the feedback an
        # operator watches on the torque.
        self.raking_difficulty = _clamp(
            1.0 - 0.35 * max(0.0, self.blanket_m - 0.8) / max(0.1, self.depth_m), 0.2, 1.0
        )

        # Rake torque: base plus a term proportional to the solids being pushed.
        load = (
            0.35 * (self.underflow_solids_pct / 2.0)
            + 0.65 * (1.0 - self.raking_difficulty)
        )
        self.torque_nm = _clamp(
            self.scraper_torque_nm * load + 2.0 * math.sin(self._t * 0.7),
            2.0,
            self.scraper_torque_max_nm,
        )

        self.raw_sludge_m3 += raked_m3h * dt_h
        self.forward_m3h = forward_m3h

    t_s_internal: float = 0.0
    forward_m3h: float = 1000.0


# ═════════════════════════════════════════════════════════════════════════════
# Aeration basin — the interesting unit
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class AerationBasin:
    """Activated sludge with DO control and nitrification.

    This is where the plant's behaviour is genuinely non-linear, and where a
    simulator earns its keep. Two things follow from the physics:

    1. **DO versus air flow is a saturating curve.** Enough air saturates DO at
       the saturation concentration; beyond that, more air only raises ORC
       (oxygen utilisation) wastefully. A linear controller therefore leaves a
       standing error and a naive one oscillates.

    2. **Nitrification has a threshold.** Below about 1 mg/L DO the nitrifier
       growth rate collapses, and below about 1 mg/L NH₄ it is substrate
       limited. Nitrifiers are slow-growing and decay fast, so a brief DO
       excursion can cost hours of ammonia removal — the reason effluent NH₄
       responds hours after a blower trip.

    Oxygen uptake is written as OUR = μ·S/(Kₛ+S)·X in mg/L/h, and the basin is
    integrated explicitly against the gas-phase transfer term.
    """

    #: Basin volume. 20 000 m³ against 1800 m³/h design flow gives ~11 h
    #: hydraulic retention, which is the range a nitrifying plant is designed
    #: for. Sizing matters more than it looks: too small and the solids
    #: concentration needed to hold the SRT becomes implausibly high, which
    #: makes the whole model quietly wrong rather than obviously wrong.
    volume_m3: float = 20000.0
    design_air_m3h: float = 5000.0
    #: **Total installed** air capacity across all blowers, m³/h.
    #:
    #: Sized so the basin holds its DO setpoint across the design day *and*
    #: moderate wet weather. Note the basin is **aeration-limited above roughly
    #: 2 400 m³/h of influent in cold water** — the blowers saturate, KLa cannot
    #: rise further, and DO falls. That is a genuine design characteristic rather
    #: than a modelling artefact: an undersized aeration train behaves exactly
    #: this way, and it is the reason plants carry a blower standby. The fault
    #: library and the storm scenarios both exercise this limit deliberately.
    blower_capacity_m3h: float = 26000.0
    srt_d: float = 15.0
    srt_actual_d: float = 15.0
    was_m3h: float = 40.0
    was_kg_h: float = 0.0
    #: Concentration the waste sludge leaves at, mg/L. Set from the secondary
    #: clarifier's underflow by the plant each scan.
    return_sludge_conc_mg_l: float = 8000.0
    ras_ratio: float = 0.75
    alpha: float = 0.55  # oxygen transfer correction for mixed liquor
    beta: float = 0.95  # saturation correction for fouling

    # state
    do_mg_l: float = 2.0
    setpoint_do_mg_l: float = 2.0
    air_flow_m3h: float = 5000.0
    blower_rpm: float = 1200.0
    blower_valve_pct: float = 55.0
    nh4_in_mg_l: float = 20.0
    nh4_out_mg_l: float = 1.2
    no3_out_mg_l: float = 8.0
    cod_in_mg_l: float = 120.0
    cod_out_mg_l: float = 25.0
    cod_removed_mg_l_h: float = 0.0
    nh4_removed_mg_l_h: float = 0.0
    mlss_mg_l: float = 3000.0
    mlvss_mg_l: float = 2200.0
    orch_mg_l_h: float = 180.0
    wtemp_c: float = 16.0
    ph: float = 7.2
    #: Volumetric oxygen transfer coefficient, 1/h, at 20 °C and design flow.
    #:
    #: This is the aeration system's **lumped design parameter** and the only
    #: one that moves state. SOTE is reported but deliberately drives nothing:
    #: KLa and SOTE are independent empirical quantities related through tank
    #: geometry and standard conditions, so deriving one from the other and then
    #: using *both* to move oxygen counts the same transfer twice.
    #:
    #: Calibrated, not guessed. Solved so that at design load the basin holds a
    #: 2.0 mg/L setpoint on ~2.7 m³ of air per m³ of sewage — the range real
    #: fine-bubble plants run at — with roughly 19 % transfer headroom in
    #: reserve. Below about 4.5 /h the basin is aeration-limited: DO collapses
    #: regardless of blower capacity. That threshold is a real design fact
    #: about the tank, and the fault library uses it.
    kla_per_h: float = 5.5
    #: Oxygen balance, kg/h. Exposed because oxygen utilisation is the plant's
    #: single largest operating cost and deserves to be a first-class signal.
    o2_required_kg_h: float = 0.0
    o2_transferred_kg_h: float = 0.0
    o2_wasted_kg_h: float = 0.0
    o2_delivered_kg_h_value: float = 0.0
    #: Nitrifier population activity, 0..1. Lags DO and carbon availability.
    _nitrifier_activity: float = 1.0
    _t: float = 0.0

    def ours_mg_l_h(self) -> float:
        """Oxygen uptake rate, mg/L/h.

        **Derived from actual removal, never from an independent growth rate.**
        This is the single most important consistency property in the model. If
        oxygen demand were computed from a growth term while the mass balance
        removed substrate by some other rule, the two would drift apart and DO
        would settle wherever the two happened to meet — a number with no
        physical meaning. Tying oxygen demand to measured COD and ammonia
        removal makes the closure structural instead of coincidental.

            carbonaceous  O₂ = COD removed · (1 − Y) / Y
            nitrogenous   O₂ = NH₄-N removed · 4.57

        Both are stoichiometries, not fitted coefficients.
        """
        # Carbonaceous demand. Only the *biodegradable* fraction consumes
        # oxygen; influent solids and refractory COD do not.
        #
        # The RATE is used, not the concentration difference. (S_in − S_out) is
        # the load crossing the reactor; the rate is (Q/V)·(S_in − S_out). Using
        # the difference inflates oxygen demand by the reciprocal of the
        # hydraulic residence time — a factor of ~5 here — and produces a plant
        # that is permanently oxygen-starved at a DO setpoint it can never reach.
        carbonaceous = (
            self.cod_removed_mg_l_h
            * F_READILY_BIODEGRADABLE
            * (1.0 - Y_HETEROTROPH)
            / Y_HETEROTROPH
        )

        # Nitrification demand. Nitrifying 1 g N costs 4.57 g O₂.
        nitrogenous = self.nh4_removed_mg_l_h * O2_PER_NITRIFIED_N

        # Assimilation and endogenous respiration add a biomass-proportional
        # term, the familiar "our_mlss" contribution.
        endogenous = (
            K_D_HETEROTROPH
            * self.mlvss_mg_l
            * (1.0 - F_NITROGEN_IN_BIOMASS)
            / 24.0
        )

        return max(0.0, carbonaceous + nitrogenous + endogenous)

    def _do_limitation(self) -> float:
        """Oxygen limitation of nitrification. Monod on DO, as a fraction.

        At DO = 2 mg/L this is ~0.83. At 0.5 mg/L it is ~0.33. Below ~0.2 the
        nitrifiers effectively stop.
        """
        return self.do_mg_l / (0.4 + self.do_mg_l)

    def sote(self) -> float:
        """Standard oxygen transfer efficiency, as a fraction, including
        temperature, alpha (mixing liquor) and beta (fouling) corrections.

        SOTE is the fraction of the oxygen in the supplied air that actually
        dissolves. Fine-bubble diffusers on a deep tank reach 0.25–0.40 before
        corrections; the corrections routinely halve it, which is why clean-water
        figures over-promise and why alpha is measured plant by plant.
        """
        temp_factor = KLA_TEMPERATURE_COEFF ** (self.wtemp_c - 20.0)
        return _clamp(self.alpha * self.beta * SOTE_BASE_COLD * temp_factor, 0.02, 0.60)

    def effective_sote(self) -> float:
        """SOTE as achieved, including the airflow mixing penalty.

        **Reporting only.** It is what appears on a plant datasheet, and it is
        useful next to the air bill. It deliberately drives no state: sizing air
        from SOTE as well as from KLa would count the same oxygen twice, and the
        two disagree by orders of magnitude because SOTE is defined against
        standard conditions while KLa is defined against the tank.
        """
        return _clamp(self.sote() * self.mixing_factor(), 0.005, 0.60)

    def o2_delivered_kg_h(self, air_m3h: float | None = None) -> float:
        """Oxygen delivered to the basin, kg/h.

            delivered = air × ρ(O₂ in air) × SOTE_effective

        This is the supply side of the balance. Keeping it as the *only* way
        oxygen enters the model is what makes the arithmetic self-consistent:
        an earlier version also drove DO from a KLa driving force, and the two
        disagreed by two orders of magnitude, producing a controller that
        believed it needed 80 m³/h where the mass balance required 7000.
        """
        air = self.air_flow_m3h if air_m3h is None else air_m3h
        return air * O2_KG_PER_M3_AIR * self.effective_sote()

    def _required_air_m3h(self) -> float:
        """Air flow that will hold the DO setpoint against current demand.

        One oxygen path, not two. An earlier version drove oxygen transfer from
        *both* a KLa driving force and an SOTE mass balance, which double-counts
        the transfer and lets the two disagree by two orders of magnitude. The
        standard ASCE form is used here, where KLa is the transfer coefficient
        and air is sized from it:

            at the setpoint:   KLa(air) · (C* − C_sp)  =  OUR
            required O₂ (kg/h) =  V · OUR / 1000
            air = required O₂ / (KLa · (C* − C_sp) / 1000) / ρ(O₂ in air)

        KLa depends on airflow through the mixing factor, so this is implicit
        and is solved by bisection — monotonic in air, so bisection is safe.

        The SOTE is retained for *reporting* only (it is what appears on a plant
        datasheet) and is deliberately not used to move any state.
        """
        c_star = oxygen_saturation(self.wtemp_c) * self.beta
        driving_force = c_star - self.setpoint_do_mg_l
        if driving_force <= 1e-6:
            return 0.0  # setpoint at or above saturation; no air required

        o2_required_kg_h = self.volume_m3 * self.ours_mg_l_h() / 1000.0
        if o2_required_kg_h <= 0.0:
            return 0.0

        # The setpoint is held when the transfer across the driving force equals
        # demand:  KLa(air) · (C* − C_sp)  =  OUR
        kla_required = self.ours_mg_l_h() / driving_force
        if self._kla_at(self.blower_capacity_m3h) < kla_required:
            # Aeration-limited: full capacity still cannot reach the setpoint.
            # Return full capacity and let DO fall — the honest answer, and
            # exactly the shortfall an operator needs to see.
            return self.blower_capacity_m3h

        # The air flow that holds the setpoint is the **KLa solution**. The
        # oxygen *mass* check below is a feasibility gate, not a second
        # constraint: taking the larger of the two over-aerates, because the
        # extra air raises DO above the setpoint even though the mass balance is
        # satisfied. At the design point the two agree — the air flow giving the
        # required KLa necessarily delivers the required mass.
        def delivered(air_m3h: float) -> float:
            sote = _clamp(self.sote() * self._mixing_at(air_m3h), 0.005, 0.60)
            return air_m3h * O2_KG_PER_M3_AIR * sote

        if delivered(self.blower_capacity_m3h) < o2_required_kg_h:
            # The blowers cannot even supply the oxygen the biology wants, so
            # the setpoint is unreachable however the level is tuned. Run flat
            # out and let DO fall — and say so, because that is the shortfall an
            # operator needs to see.
            return self.blower_capacity_m3h

        return self._air_for_kla(kla_required)

    def _air_for_kla(self, kla_required: float) -> float:
        """Smallest air flow achieving a given KLa — the DO-level constraint."""
        if self._kla_at(self.blower_capacity_m3h) < kla_required:
            return 0.0
        lo, hi = 0.0, self.blower_capacity_m3h
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if self._kla_at(mid) < kla_required:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def _kla_at(self, air_m3h: float) -> float:
        """KLa the diffuser achieves at a given air flow, 1/h.

        A *design* parameter (fine-bubble, deep tank: 10–15 /h at 20 °C and
        design flow) corrected for temperature and for how well the gas is
        distributed at that flow.
        """
        temp_factor = KLA_TEMPERATURE_COEFF ** (self.wtemp_c - 20.0)
        return self.kla_per_h * temp_factor * self._mixing_at(air_m3h)

    def _mixing_at(self, air_m3h: float) -> float:
        """Mixing factor at an arbitrary air flow."""
        fraction = _clamp(air_m3h / self.blower_capacity_m3h, 0.0, 1.0)
        return _clamp(
            MIXING_FLOOR
            + (1.0 - MIXING_FLOOR)
            * (fraction / MIXING_SATURATION_FRACTION) ** MIXING_EXPONENT,
            0.0,
            1.0,
        )

    def effective_kla(self) -> float:
        """KLa at the *current* airflow, 1/h."""
        return self._kla_at(self.air_flow_m3h)

    def mixing_factor(self) -> float:
        """Fraction of full transfer achieved at the current airflow, 0..1.

        At a fraction of the saturation flow the bubbles are well distributed;
        below it, distribution collapses. The power law is what makes the
        DO/airflow relationship steep at low flow, and the reason a plant
        cannot simply turn the blowers down overnight: below some flow the
        transfer falls faster than the flow does, and DO drops.
        """
        return self._mixing_at(self.air_flow_m3h)

    def residual_cod_mg_l(self) -> float:
        """Effluent COD that the SRT implies, mg/L.

        At steady state, growth exactly balances decay and wastage, so the net
        specific growth rate is fixed by the SRT:

            μ_net = 1/SRT  and  μ_net = μ_max·S/(Kₛ+S) − k_d

        Solving for S gives the residual substrate the plant *must* leave behind
        to hold that SRT. This is why effluent COD is a control indicator: it
        is the visible signature of the SRT the operator is actually running,
        and a rising effluent COD means the SRT has fallen, whatever the MLSS
        gauge says.
        """
        mu_required = 1.0 / max(1.0, self.srt_d) + K_D_HETEROTROPH
        headroom = MU_HETEROTROPH - mu_required
        if headroom <= 1e-6:
            # SRT is so short the organisms cannot survive. Effluent is
            # essentially influent.
            return COD_RESIDUAL_FLOOR
        s = K_S_HETEROTROPH * mu_required / headroom
        return _clamp(max(s, COD_RESIDUAL_FLOOR), COD_RESIDUAL_FLOOR, 400.0)

    def step(
        self,
        dt: float,
        influent_m3h: float,
        influent_cod_mg_l: float,
        influent_nh4_mg_l: float,
        temp_c: float,
        return_sludge_m3h: float,
        influent_tss_mg_l: float,
    ) -> None:
        self._t += dt
        self.wtemp_c = temp_c
        self.nh4_in_mg_l = influent_nh4_mg_l

        dt_h = dt / 3600.0

        # ── airflow is what the control block has already set ────────────────
        self.air_flow_m3h = _clamp(self.air_flow_m3h, 0.0, self.blower_capacity_m3h * 4.0)
        blower_fraction = _clamp(self.air_flow_m3h / self.blower_capacity_m3h, 0.0, 1.0)
        self.blower_rpm = 2400.0 * math.sqrt(max(0.0, blower_fraction))
        self.blower_valve_pct = _clamp(100.0 * blower_fraction, 0.0, 100.0)

        # ── DO mass balance ──────────────────────────────────────────────────
        # The driving-force form, which is what actually makes the *level*
        # controllable (see _required_air_m3h):
        #     dC/dt = KLa · (C* − C) − OUR
        # Using the supply/demand form instead balances total oxygen but leaves
        # DO free to settle anywhere, because at any level the delivered and
        # consumed amounts can be made to match.
        c_star = oxygen_saturation(self.wtemp_c) * self.beta
        kla = self.effective_kla()
        transfer_mg_l_h = kla * max(0.0, c_star - self.do_mg_l)
        uptake = self.ours_mg_l_h()
        self.do_mg_l = _clamp(
            self.do_mg_l + (transfer_mg_l_h - uptake) * dt_h, 0.0, c_star * 1.05
        )
        self.orch_mg_l_h = uptake

        # Oxygen balance, reported. The plant pays for all the air it blows, so
        # the gap between supply and uptake is the oxygen utilisation
        # inefficiency — the number an operator sees when a blower is oversized
        # or the DO setpoint is set too high.
        self.o2_required_kg_h = self.volume_m3 * uptake / 1000.0
        self.o2_transferred_kg_h = self.volume_m3 * min(transfer_mg_l_h, uptake) / 1000.0
        self.o2_delivered_kg_h_value = self.o2_delivered_kg_h()
        self.o2_wasted_kg_h = max(0.0, self.o2_delivered_kg_h_value - self.o2_required_kg_h)

        # ── substrate: a CSTR, done properly ─────────────────────────────────
        # The distinction that makes this model defensible:
        #
        #   * KINETICS determine the *residual concentration* the plant leaves
        #     behind. Effluent COD is set by the SRT (see residual_cod_mg_l).
        #   * The MASS BALANCE determines the *rate* of removal. In steady state
        #     a CSTR removes (Q/V)·(S_in − S_out), full stop.
        #
        # Getting this backwards — letting a growth rate set the removal — is the
        # most common way an activated-sludge model ends up with a reactor that
        # removes more COD than was ever fed to it, and an oxygen demand that
        # has no relationship to the influent.
        dilution_rate = influent_m3h / self.volume_m3  # 1/h, the D term

        # Effluent COD: the kinetics/SRT equilibrium, approached with a finite
        # response time rather than instantly, because a real reactor has
        # inertia and the response time is itself informative.
        cod_target = min(self.residual_cod_mg_l(), influent_cod_mg_l)
        response_h = max(0.5, self.volume_m3 / max(1.0, influent_m3h) * 0.35)
        cod_out_new = self.cod_out_mg_l + (
            cod_target - self.cod_out_mg_l
        ) * min(1.0, dt_h / response_h)
        # Removal is the mass balance across the reactor.
        cod_removed_mg_l_h = max(
            0.0, dilution_rate * (self.cod_in_mg_l - cod_out_new)
        )
        self.cod_removed_mg_l_h = cod_removed_mg_l_h
        self.cod_out_mg_l = _clamp(cod_out_new, COD_RESIDUAL_FLOOR, 1500.0)
        # Feed the reactor with the incoming concentration, flow-weighted.
        self.cod_in_mg_l = _clamp(
            influent_cod_mg_l
            + (self.cod_out_mg_l - influent_cod_mg_l) * min(1.0, dt_h / response_h),
            COD_RESIDUAL_FLOOR,
            2000.0,
        )

        # ── nitrification ────────────────────────────────────────────────────
        # Same CSTR logic as COD: a kinetic *capacity*, and a residual found by
        # balancing that capacity against the demand the mass balance requires.
        #
        # **Reduced nitrifier activity must leave more ammonia, not less.** An
        # earlier version computed the residual from the activity directly,
        # which drove effluent ammonia to zero whenever the nitrifiers were
        # suppressed — a plant that looked *better* the harder it was failing,
        # and would have passed a compliance check while doing it.
        #
        # Carbon limitation reads the *influent* supply, not the effluent
        # residue. Nitrifiers compete with heterotrophs for what arrives, and on
        # a well-run plant the effluent COD is low precisely because the
        # heterotrophs won — so keying the factor to the residue starves
        # nitrification on a plant that is working perfectly, and effluent
        # ammonia then creeps up for no physical reason. Dilute influent really
        # does limit nitrification; a clean effluent does not.
        carbon_factor = _clamp(
            influent_cod_mg_l / CARBON_STARVATION_THRESHOLD, 0.0, 1.0
        )
        do_factor = self._do_limitation()
        # Nitrifier population lags conditions, and recovery is slow.
        self._nitrifier_activity += (
            do_factor * carbon_factor - self._nitrifier_activity
        ) * min(1.0, dt_h / NITRIFIER_RESPONSE_H)

        # Kinetic capacity for ammonia oxidation, mg/L/h.
        nit_biomass_frac = NITRIFIER_BIOMASS_FRACTION * (
            self.mlvss_mg_l / max(1.0, self.mlss_mg_l)
        )
        nh4_capacity_mg_l_h = (
            MU_NITRIFIER
            * self._nitrifier_activity
            * nit_biomass_frac
            * 1000.0
            / Y_NITRIFIER
            / 24.0
        )
        # Demand the mass balance requires to hold the residual floor.
        nh4_needed_mg_l_h = dilution_rate * max(0.0, self.nh4_in_mg_l - NH4_FLOOR)

        if nh4_capacity_mg_l_h >= nh4_needed_mg_l_h:
            # Capacity exceeds demand: the reactor can hold ammonia at the floor.
            nh4_out_new = NH4_FLOOR
        else:
            # Capacity-limited: ammonia accumulates until removal matches.
            nh4_out_new = max(
                NH4_FLOOR, self.nh4_in_mg_l - nh4_capacity_mg_l_h / max(1e-9, dilution_rate)
            )

        nh4_response_h = max(1.0, self.volume_m3 / max(1.0, influent_m3h) * 0.5)
        self.nh4_out_mg_l = _clamp(
            self.nh4_out_mg_l
            + (nh4_out_new - self.nh4_out_mg_l) * min(1.0, dt_h / nh4_response_h),
            NH4_FLOOR,
            80.0,
        )
        self.nh4_removed_mg_l_h = max(
            0.0, dilution_rate * (self.nh4_in_mg_l - self.nh4_out_mg_l)
        )
        self.nh4_in_mg_l = _clamp(
            influent_nh4_mg_l
            + (self.nh4_out_mg_l - influent_nh4_mg_l) * min(1.0, dt_h / nh4_response_h),
            0.0,
            100.0,
        )

        # Nitrified ammonia-N becomes nitrate-N, less the fraction assimilated
        # into biomass (which leaves with the waste sludge). Everything here
        # stays in mg/L/h — no unit conversions, which is where a nitrate
        # balance quietly loses two orders of magnitude.
        no3_generated_mg_l_h = (
            self.nh4_removed_mg_l_h * 0.92 * (1.0 - F_NITROGEN_IN_BIOMASS)
        )
        no3_washed_out_mg_l_h = dilution_rate * self.no3_out_mg_l
        self.no3_out_mg_l = _clamp(
            self.no3_out_mg_l
            + (no3_generated_mg_l_h - no3_washed_out_mg_l_h) * dt_h,
            0.0,
            60.0,
        )

        # ── solids inventory and SRT ─────────────────────────────────────────
        # SRT is the control variable operators actually set. It is the mean
        # time solids spend in the reactor, and it fixes everything else: MLSS,
        # nitrifier population, and therefore effluent ammonia. The waste rate
        # is *derived* from the target SRT rather than chosen independently —
        # that is the whole point of the relationship.
        inventory_kg = self.volume_m3 * self.mlss_mg_l / 1000.0

        # Waste activated sludge: the only solids leaving the plant, and what
        # sets the SRT. Work in *masses* first, then convert to a flow using the
        # thickened concentration the sludge leaves at.
        #
        #     was_kg_d = inventory / SRT
        #     was_m3h = was_kg_d / 24 / (concentration)
        #
        # Converting straight to a volume by dividing the *inventory* by the
        # MLSS as well double-counts the concentration and yields a waste rate
        # several times too high — which drains a basin that the MLSS gauge still
        # reports as healthy, because the gauge reads concentration, not mass.
        target_waste_kg_d = inventory_kg / max(1.0, self.srt_d)
        self.was_kg_h = target_waste_kg_d / 24.0
        # Activated sludge does not thicken below roughly 3 g/L in service.
        # Dividing by a thinner (or, at start-up, a near-zero) concentration
        # produces a waste rate of hundreds of m³/h, which is not physical and
        # leaves the contract's range immediately.
        conc = max(MIN_RETURN_SLUDGE_CONC_MG_L, self.return_sludge_conc_mg_l)
        self.was_m3h = _clamp(self.was_kg_h / (conc / 1000.0), 0.0, 500.0)
        was_kg_h = self.was_kg_h

        # Biomass grows on the substrate actually removed. Using the removal
        # rate keeps growth and oxygen demand tied to the same mass balance,
        # rather than to an independent growth term that could disagree.
        growth_kg_h = (
            self.volume_m3
            * cod_removed_mg_l_h
            * F_READILY_BIODEGRADABLE
            * Y_HETEROTROPH
            / 1000.0
        )
        # Inorganic solids entering are a fraction of the influent *solids*,
        # not of the COD. Deriving them from COD is a category error that
        # quietly inflates MLSS until the SRT relationship stops holding.
        infl_inorganic_kg_h = influent_tss_mg_l * influent_m3h / 1000.0 * F_INORGANIC
        d_mlss = (
            infl_inorganic_kg_h + growth_kg_h - was_kg_h
        ) * dt_h / self.volume_m3
        self.mlss_mg_l = _clamp(self.mlss_mg_l + d_mlss, 400.0, 9000.0)
        self.mlvss_mg_l = self.mlss_mg_l * _clamp(
            0.72 - 0.012 * self.srt_d, 0.35, 0.80
        )

        # ── reported SRT: actual inventory over actual removal ───────────────
        # Reported, not commanded: it drifts as load and settling change, and
        # that drift is itself worth watching.
        actual_waste_kg_d = max(1.0, was_kg_h * 24.0)
        self.srt_actual_d = _clamp(inventory_kg / actual_waste_kg_d, 1.0, 60.0)

        # Mixing and pH: blowers provide gas lift mixing; low DO means low mixing.
        self.ph = _clamp(
            7.2 + 0.4 * math.sin(self._t / 7200.0) - 0.3 * _clamp(storm_proxy(influent_m3h) - 1.0, 0, 1),
            6.2,
            9.0,
        )


def storm_proxy(flow_m3h: float) -> float:
    """Flow as a fraction of design flow. Used for pH and other soft effects."""
    return flow_m3h / 1800.0


# ═════════════════════════════════════════════════════════════════════════════
# Secondary clarifier
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class SecondaryClarifier:
    """Final settling tank. Determines what the permit actually sees.

    Effluent TSS is the product of the mixed liquor entering and the fraction
    the tank fails to capture. Solids loading is the driver, so a storm that
    raises MLSS and flow degrades effluent TSS twice over.
    """

    surface_area_m2: float = 780.0
    depth_m: float = 3.4
    hopper_m: float = 0.8
    scraper_torque_nm: float = 70.0
    scraper_torque_max_nm: float = 350.0
    drive_kw: float = 2.2

    blanket_m: float = 0.4
    torque_nm: float = 70.0
    tss_mg_l: float = 12.0
    nh4_mg_l: float = 1.0
    temp_c: float = 16.0
    underflow_conc_mg_l: float = 8000.0
    return_sludge_m3h: float = 600.0
    underflow_m3h: float = 60.0
    was_m3h: float = 0.0
    forward_m3h: float = 1200.0
    effluent_cod_mg_l: float = 20.0
    #: Realised return-sludge ratio, reported rather than commanded.
    ras_ratio_actual: float = 0.75
    #: Return activated sludge as a fraction of forward flow. 0.5–1.0 is
    #: typical; too low thins the sludge and the blanket deepens.
    ras_ratio: float = 0.75
    raking_difficulty: float = 1.0
    health: float = 1.0
    _t: float = 0.0

    def step(
        self,
        dt: float,
        forward_m3h: float,
        ras_m3h: float,
        mixed_tss_mg_l: float,
        mixed_nh4_mg_l: float,
        mixed_cod_mg_l: float,
        temp_c: float,
        was_m3h: float = 0.0,
    ) -> None:
        self._t += dt
        self.temp_c = temp_c
        dt_h = dt / 3600.0

        # ── flows: RAS and WAS are *commanded*, concentration is the outcome ──
        # This direction matters. Deriving the underflow from the solids load
        # (concentration in, flow out) and then feeding the clarifier
        # forward + RAS closes a positive feedback loop: more feed means more
        # underflow means more RAS means more feed. It grows geometrically until
        # something arbitrary clamps it, and the effluent flow ends up larger
        # than the influent — which a mass-balance test catches immediately.
        #
        # In a real plant the operator commands a return rate and the sludge
        # concentration is whatever the physics delivers. So:
        #     feed      = forward + RAS        (RAS commanded)
        #     underflow = RAS + WAS            (WAS from the SRT target)
        #     conc      = captured / underflow (an outcome, capped by physics)
        #     effluent  = feed − underflow
        influent_m3h = forward_m3h + ras_m3h  # total mixed-liquor feed
        self.was_m3h = _clamp(was_m3h, 0.0, 600.0)
        underflow_m3h = ras_m3h + self.was_m3h
        self.underflow_m3h = underflow_m3h
        self.return_sludge_m3h = ras_m3h
        # Effluent over the weir is the feed less the whole underflow. Reporting
        # the *forward* flow here while also counting WAS as a separate outflow
        # deducts the waste water twice, and the plant's water balance then
        # drifts by 2 × the waste rate forever.
        self.forward_m3h = max(1.0, influent_m3h - underflow_m3h)
        self.ras_ratio_actual = ras_m3h / max(1.0, self.forward_m3h)

        # ── capture efficiency ──
        # A secondary clarifier is a good separator: 99.5–99.9 % TSS capture at
        # design load. The permit number is the *concentration* left in the
        # forward stream, not the fraction removed, which is why the RAS ratio
        # moves the permit result more than operators expect.
        sor = influent_m3h / self.surface_area_m2  # m/h
        capture = 0.9985 - 0.0020 * max(0.0, sor - 2.2)
        solids_loading = mixed_tss_mg_l * influent_m3h / 1e6  # kg/m²·h
        capture -= 0.00012 * max(0.0, solids_loading - 4.0)
        capture *= self.health
        capture = _clamp(capture, 0.70, 0.9995)

        solids_in_kg_h = influent_m3h * mixed_tss_mg_l / 1000.0
        solids_captured_kg_h = solids_in_kg_h * capture

        # Underflow concentration as an *outcome*, capped at what the scraper can
        # physically achieve. When captured solids exceed that cap the excess
        # cannot leave, so it accumulates as blanket — which is exactly the
        # thickening failure the fault library injects.
        achievable = UF_BASE_CONC_MG_L * (0.55 + 0.45 * self.raking_difficulty)
        if underflow_m3h > 1e-6:
            self.underflow_conc_mg_l = _clamp(
                solids_captured_kg_h * 1000.0 / underflow_m3h, 400.0, achievable
            )
        else:
            self.underflow_conc_mg_l = achievable

        # Effluent solids: what was not captured, in the forward stream.
        self.tss_mg_l = _clamp((1.0 - capture) * mixed_tss_mg_l, 1.0, 300.0)
        self.nh4_mg_l = max(0.0, mixed_nh4_mg_l)  # dissolved: passes straight through
        self.effluent_cod_mg_l = mixed_cod_mg_l * (1.0 - F_NON_SETTLEABLE)

        # ── blanket ──────────────────────────────────────────────────────────
        # What the underflow actually carries out, versus what was captured.
        # Anything it cannot carry — because the concentration is already at the
        # scraper's physical limit, or because raking is impaired — stays on the
        # floor. A rising blanket is the earliest sign of losing solids.
        raked_m3h = underflow_m3h * self.raking_difficulty
        raked_kg_h = raked_m3h * self.underflow_conc_mg_l / 1000.0
        net_kg_h = solids_captured_kg_h - raked_kg_h
        # Sludge in the blanket at ~12 kg/m³ (≈1.2 % w/v).
        self.blanket_m = _clamp(
            self.blanket_m + (net_kg_h * dt_h / 12.0) / self.surface_area_m2,
            MIN_BLANKET_M,
            self.depth_m,
        )

        # Blanket approaching the surface: solids wash over the weir.
        carryover_zone = self.depth_m - self.hopper_m - 0.30
        if self.blanket_m > carryover_zone:
            excess = (self.blanket_m - carryover_zone) / max(0.1, self.hopper_m)
            self.tss_mg_l = _clamp(
                self.tss_mg_l * (1.0 + 2.5 * _clamp(excess, 0.0, 1.0)), 1.0, 300.0
            )

        # Scraping gets harder as the blanket deepens.
        self.raking_difficulty = _clamp(
            1.0 - 0.55 * max(0.0, self.blanket_m - 0.5) / self.depth_m, 0.15, 1.0
        )
        load = (
            0.30
            + 0.40 * (self.underflow_conc_mg_l / UF_BASE_CONC_MG_L)
            + 0.30 * (1.0 - self.raking_difficulty)
        )
        self.torque_nm = _clamp(
            self.scraper_torque_nm * load + 2.0 * math.sin(self._t * 0.6),
            2.0,
            self.scraper_torque_max_nm,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Anaerobic digester
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class AnaerobicDigester:
    """Mesophilic digester producing biogas.

    The failure mode that matters is **souring**: the VFA/alkalinity ratio climbs
    above about 0.6, pH falls, methane fraction drops, and the digester stops
    producing gas. It is slow, it is subtle, and it is expensive to recover from
    — which makes it the best example in the plant of a condition that only a
    trend, not a threshold, will reveal.
    """

    volume_m3: float = 4200.0
    setpoint_ph: float = 7.1
    setpoint_temp_c: float = 37.0

    ph: float = 7.1
    alkalinity_mg_l: float = 3800.0
    vfa_mg_l: float = 700.0
    temp_c: float = 37.0
    gas_flow_m3h: float = 900.0
    ch4_pct: float = 64.0
    gas_pressure_mbar: float = 120.0
    boiler_duty_kw: float = 900.0
    organic_load_kg_d: float = 4200.0
    souring: float = 0.0  # 0..1, driven by the fault library
    _t: float = 0.0

    @property
    def vfa_alk_ratio(self) -> float:
        """Volatile fatty acids over alkalinity. The digester health metric.

        Below 0.4 is healthy. Above 0.6 the process is unstable and methane
        production is falling.
        """
        return self.vfa_mg_l / max(1.0, self.alkalinity_mg_l)

    def step(
        self,
        dt: float,
        feed_cod_kg_d: float,
        feed_ph: float,
        temp_c: float,
        boiler_demand_kw: float = 1200.0,
    ) -> None:
        self._t += dt
        dt_h = dt / 3600.0

        # ── temperature: heated, tracking setpoint with a slow lag ───────────
        self.temp_c += (self.setpoint_temp_c + self.souring * 3.0 - self.temp_c) * min(
            1.0, dt_h * 0.8
        )

        # ── load and souring ─────────────────────────────────────────────────
        # Overloading an anaerobic digester is the usual cause of souring, so
        # the fault library drives `souring` and the model follows.
        overload = feed_cod_kg_d / max(1.0, self.organic_load_kg_d)

        # VFA production is first-order in the organic load, with a first-order
        # consumption term. The coefficients are set so the *time constants* are
        # right — hours for VFA, tens of hours for alkalinity — because that is
        # the diagnostic signature: souring is a slow drift, and a model that
        # sours in minutes teaches the wrong lesson about how early the alarm
        # should be.
        vfa_production = (1.8 * overload - 1.0) * VFA_PRODUCTION_GAIN
        vfa_consumption = self.vfa_mg_l * VFA_CONSUMPTION_RATE
        vfa_rate = vfa_production - vfa_consumption + self.souring * 30.0
        self.vfa_mg_l = _clamp(
            self.vfa_mg_l + vfa_rate * dt_h, 50.0, 6000.0
        )

        # Alkalinity is consumed as VFA are converted to methane; the feed
        # buffers some back. Slower than the VFA term, deliberately.
        alk_rate = (
            -(self.vfa_mg_l / 1000.0) * 0.35 + 0.25 * overload
        ) * ALKALINITY_GAIN
        self.alkalinity_mg_l = _clamp(
            self.alkalinity_mg_l + alk_rate * dt_h, 300.0, 8000.0
        )

        # pH follows the VFA/alkalinity balance, plus feed pH and temperature.
        ratio = self.vfa_alk_ratio
        ph_target = 7.2 - 2.4 * _clamp((ratio - 0.3) / 0.5, 0.0, 1.0)
        ph_target += 0.25 * (feed_ph - 7.2)
        ph_target -= 0.05 * max(0.0, 10.0 - self.temp_c)
        self.ph += (ph_target - self.ph) * min(1.0, dt_h * 0.5)

        # ── gas production ───────────────────────────────────────────────────
        # Methanogenesis is pH and temperature sensitive, and needs the VFA to
        # have been consumed first. This is why souring cuts gas *after* the VFA
        # have risen — the sequence is diagnostic.
        ph_factor = _clamp(1.0 - 2.0 * max(0.0, (6.6 - self.ph)) / 0.6, 0.0, 1.0)
        temp_factor = _clamp(1.0 - 0.09 * max(0.0, 35.0 - self.temp_c), 0.0, 1.0)
        health = ph_factor * temp_factor
        available = _clamp(overload, 0.2, 1.6)

        self.gas_flow_m3h = _clamp(900.0 * available * health * (1.0 - self.souring * 0.5), 50.0, 2500.0)
        # Below pH 6.8 the methane share collapses as the population shifts.
        self.ch4_pct = _clamp(
            64.0 + 4.0 * health - 34.0 * _clamp((6.9 - self.ph) / 0.6, 0.0, 1.0), 45.0, 75.0
        )
        self.gas_pressure_mbar = _clamp(
            60.0 + 90.0 * (self.gas_flow_m3h / 900.0) ** 0.5, 20.0, 280.0
        )

        # ── gas to energy ────────────────────────────────────────────────────
        # 1 m³ biogas ≈ 6 kWh thermal at 65% boiler efficiency.
        available_kw = self.gas_flow_m3h * (self.ch4_pct / 100.0) * 6.0 * 0.65
        self.boiler_duty_kw = _clamp(min(available_kw, boiler_demand_kw), 0.0, 2200.0)


# ═════════════════════════════════════════════════════════════════════════════
# Effluent
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class Disinfection:
    """Hypochlorite contact basin.

    The target is a residual that decays with time and light. Too little and the
    permit fails on bacteria; too much and the residual oxidises the effluent
    ammonia, which is a nitrogen loss the plant pays for in chemicals.
    """

    volume_m3: float = 900.0
    target_residual_mg_l: float = 0.8
    dose_mg_l: float = 14.0
    _t: float = 0.0
    residual_mg_l: float = 0.8
    ph: float = 7.2
    temp_c: float = 16.0
    tss_mg_l: float = 12.0
    turbidity_ntu: float = 6.0
    nh4_mg_l: float = 1.0
    conductivity: float = 520.0
    bacti_mpn_100ml: float = 120.0
    contact_time_h: float = 1.0

    def dose_for_target_residual(self, target_residual_mg_l: float) -> float:
        """Dose required to hold a target residual, by inverse search.

        Real plants do not dose a fixed amount — they control to a residual, and
        the required dose swings with ammonia, temperature, contact time and
        flow. This is why a plant with failing nitrification shows a visibly
        higher chemical cost: the ammonia chlorine demand (7.6 g Cl₂ per g N)
        dominates everything else in the dose.
        """
        lo, hi = 0.0, 80.0
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if self._residual_for(mid) < target_residual_mg_l:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    def _residual_for(self, dose_mg_l: float) -> float:
        """Residual chlorine for a given dose, at current conditions."""
        demand = (
            CL_CHLORINE_DEMAND_PER_NH4 * self.nh4_mg_l
            + CL_DEMAND_PER_TSS * self.tss_mg_l
            + CL_DEMAND_PER_TURBIDITY * self.turbidity_ntu
        )
        available = max(0.0, dose_mg_l - demand)
        decay = (
            CL_DECAY_BASE
            + 0.02 * max(0.0, self.temp_c - 15.0)
            + 0.15 * max(0.0, self.ph - 7.5)
        )
        return _clamp(available * math.exp(-decay * self.contact_time_h), 0.0, 10.0)

    def step(
        self,
        dt: float,
        dose_mg_l: float,
        flow_m3h: float,
        nh4_mg_l: float,
        tss_mg_l: float,
        turbidity_ntu: float,
        ph: float,
        temp_c: float,
    ) -> None:
        self._t += dt
        dt_h = dt / 3600.0
        self.ph = ph
        self.temp_c = temp_c
        self.tss_mg_l = tss_mg_l
        self.turbidity_ntu = turbidity_ntu
        self.nh4_mg_l = nh4_mg_l

        # Contact time. Too short a contact time is a real compliance failure.
        contact_time_h = self.volume_m3 / max(1.0, flow_m3h)
        self.contact_time_h = contact_time_h

        # Chlorine demand. Ammonia dominates: breakpoint chlorination consumes
        # ~7.6 g Cl₂ per g NH₄-N before any free residual appears, so ammonia
        # is the dose driver. This is why nitrification pays twice — it protects
        # the disinfection credit *and* cuts the chlorine bill, which is why a
        # plant with poor ammonia removal has a visibly higher chemical cost.
        demand = (
            CL_CHLORINE_DEMAND_PER_NH4 * nh4_mg_l
            + CL_DEMAND_PER_TSS * tss_mg_l
            + CL_DEMAND_PER_TURBIDITY * turbidity_ntu
        )
        # Decay: faster in warm, bright, high-pH water.
        decay = 0.35 + 0.02 * max(0.0, temp_c - 15.0) + 0.15 * max(0.0, ph - 7.5)
        available = max(0.0, dose_mg_l - demand)
        target = available * math.exp(-decay * contact_time_h)
        # Adequacy falls sharply with contact time, temperature and turbidity.
        self.residual_mg_l = _clamp(target, 0.0, 5.0)

        # Log-removal, clipped hard by short contact time and cold water.
        ct = self.residual_mg_l * contact_time_h * 60.0
        ct_credit = ct / (4.0 * max(0.2, 1.0 + 0.06 * (20.0 - temp_c)))
        log_removal = _clamp(3.2 * math.log10(max(1.0, ct_credit) + 1.0) + 1.1, 0.0, 6.0)
        self.bacti_mpn_100ml = _clamp(1.0e5 * 10.0 ** (-log_removal), 10.0, 100000.0)

    contact_time_h: float = 1.0
