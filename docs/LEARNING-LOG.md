# Learning log

Append-only. One entry per phase, in the learner's own words where possible.
The point is to record *what surprised me*, because that is what needs revisiting
before a design decision is made again under different assumptions.

Format: what was built, what was expected, what actually happened, what it cost.

---

## Phase 0–1a — Contract and scan loop

**Expected:** a YAML contract and a PLC-shaped loop. Two days.

**What happened:** the contract was quick. The loop was quick. The process model
was where the time went, and almost all of it went into dimensional analysis.

**Learned:**

- A *series* of errors came from unit confusion, not logic. Oxygen uptake computed
  in mg/L·d⁻¹ but consumed as mg/L·h⁻¹. A waste rate that divided the reactor
  inventory by concentration *twice*, giving a realised SRT of 5.6 d against a
  commanded 15 d — while the MLSS gauge read healthy, because the gauge reads
  concentration and the error was in mass. These are invisible in code review
  and obvious the moment units are written out.
- Oxygen content of air is 1.33 kg/m³. The figure 0.232 is roughly the *volume*
  fraction; using it as a mass content made air demand 5× high and left the basin
  permanently oxygen-starved.
- **Balancing oxygen is not controlling dissolved oxygen.** Sizing airflow so
  delivered equals consumed is indifferent to the DO *level* — it will hold 8 mg/L
  while the controller believes it holds 2. Only the driving force `KLa(C* − C)`
  fixes the level. This one cost several iterations and is the single most
  useful thing learned in the project.
- Driving oxygen transfer from *both* a KLa driving force and an SOTE mass
  balance double-counts it; the two disagree by ~100×. KLa and SOTE are
  independent empirical quantities related through tank geometry, not two
  descriptions of one number.
- A solids balance on activated sludge **cannot** close on solids alone: organic
  matter converted to biomass leaves as more WAS than arrived as suspended
  solids. Growth is a source term, not an error.
- Nitrification was inverted: reduced nitrifier activity drove effluent ammonia
  *down*, so the plant looked better the harder it failed — and would have passed
  a compliance check while doing it. Also, capacity was ~50× too high, which
  hid the same thing behind a plant that nitrified no matter what.
- Primary clarifier removal fractions were applied as pass-through multipliers,
  sending 2 % of ammonia to the reactor. The *fraction removed* is not the
  *fraction that passes*.
- A blanket deepens when solids are captured faster than the scraper removes
  them. Reducing capture makes it *shallower*, because less sludge arrives to
  accumulate. The intuitive fault injection was backwards.
- Two clarifier blankets could go negative. An unclamped value reads as physics
  until you know what the physical range is.

**Cost:** roughly two-thirds of the elapsed time on this project, and every
single real bug was found by a test or a balance, never by reading the code.

---

## Phase 1b — Timestep continuity

**Expected:** run at several `dt` and get similar answers.

**What happened:** the plant ran at 1 s and collapsed at 60 s. Aeration went to
zero, nitrification died, contact time read 900 h.

**Learned:**

- The lift station was the cause, not the aeration. With fixed-speed pumps sized
  so one pump roughly equalled average flow, the wet well had **no stable
  equilibrium**: it filled, overshot, emptied, cycled. Worse, the *amplitude
  scaled with the timestep* — a property no real plant has, and it made every
  downstream number timestep-dependent.
- Variable-speed pumps with a time-constant level controller fixed it. This is
  what real plants do for exactly this reason. The wet well then held its band
  across a 60× range of `dt` with no overflow.
- A 120-hour run caught a regression that a 24-hour test missed: lowering the COD
  floor to make the SRT signal visible starved the nitrifier carbon factor, so
  effluent ammonia crept from 0.25 to 6.10 mg/L on a plant working perfectly. The
  factor was reading the effluent residue; a clean effluent means the
  heterotrophs won, not that carbon ran out.
- **Run the long test.** The processes here have time constants of hours. A test
  that settles for 20 minutes will pass on a model that drifts.

---

## Phase 2 — Fault library

**Learned:**

- Sensor faults must corrupt only the *published* value. Applied to the process
  they are undetectable by definition — the process *is* wrong. Applied to the
  reporting layer they produce exactly what a historian must survive.
- A drifting probe that stays inside its normal band is the case that justifies
  model-based detection. The first draft used a bias large enough to trip a
  high alarm, which quietly made the "undetectable by threshold" claim false.
- The blower scenario is the most valuable output: air pins at full remaining
  capacity, DO decays over one to two hours (limited by the basin's ~40 kg DO
  inventory, not by biology), and effluent ammonia follows two to six hours
  after. **An operator alarming on ammonia gets about three hours less warning.**
  The test asserts the *ordering* of the two responses, because that is what an
  alarm design is judged on.
- The compound scenario is the honest stress test: a drifting DO probe over a
  blower trip reports 0.70 while the true value is 0.00. The instrument masks
  the severity of the fault it should reveal.

---

## Phase 3 — Protocols

**Learned:**

- `pymodbus` 3.15 replaced `ModbusSlaveContext` with `ModbusDeviceContext`,
  removed the sequential block's `getValues`/`setValues`, and made
  `ModbusServerContext` serve only `ModbusSimulatorContext`. Detaching the
  register model from the library was what made this survivable. The version is
  now pinned to 3.6.6.
- With a non-zero base address, `ModbusSequentialDataBlock.getValues(0, n)`
  returns an **empty list** rather than an error, so every register silently
  read as zero. Found by a test that read a published value back, not by
  reading the source.
- `StartTcpServer` blocks forever with no shutdown hook. It must be a **daemon
  thread**: a non-daemon thread hangs the interpreter on exit, and an asyncio
  task cannot cancel it because the work has already left the event loop.
- Building the OPC UA address space in two passes put variables in a folder
  named after the area and left equipment objects as empty siblings. Every path
  a client constructs from the contract resolved to the wrong node — exactly the
  failure OPC UA exists to prevent, caused by not modelling the hierarchy.
- `get_child("Name")` resolves a browse name in **namespace 0**, so a property
  created in the plant namespace reports `BadNoMatch`, which reads like a
  missing property rather than a naming error.
- `publish()` counted changed nodes and never wrote them. A silent no-op
  indistinguishable from a dead subscription.

---

## Open threads

1. **Modbus wire offset is unverified.** pymodbus reserves register 0, and two
   measurements disagreed about whether a client requests `index` or `index + 1`.
   Not guessed. The register model is fully verified independently; the
   wire-level round trip is marked `xfail` rather than asserted on a guess.
   Resolve with one instrumented experiment against a known block.
2. **OPC UA engineering range is not enforced on the wire.** `EUInformation` is
   advisory in the base specification and `asyncua` does not enforce it, so a
   client can write 99 mg/L to a 0.5–6.0 setpoint. Write *permission* **is**
   enforced by the protocol. The gateway must reject out-of-range writes.
   Recorded in `docs/SECURITY.md` as a known gap.
3. **Aeration is near its limit.** `kla_per_h = 5.5` holds 2.0 mg/L with ~19 %
   transfer headroom. Below ~4.5 the basin is aeration-limited. That is a real
   design fact, but the margin is thin for storm scenarios.
4. **Solids balance closes to ~15 %** and that is honest: the waste rate is
   commanded from the SRT target rather than emergent, so the residual measures
   commanded-vs-realised discharge. What must not happen is growth without
   bound, and that has its own test.
