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
- **Two address spaces, not one.** The datastore's own `getValues` and the
  Modbus PDU addressing disagree, and both differ from the contract's 4xxxx
  convention. Anything verified through the wrong one looks correct and is not.
  Every check now goes through a real client.
- A measurement against a block populated via the *constructor* does not
  generalise to one populated via `setValues`. "The value looked about right"
  never settles an addressing question; a unique ramp in the block does.
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

## Phase 2b — the first runnable process

The soft PLC (`softplc/main.py`) is the first time the pieces met each other, and
the first time the interesting bugs showed up. None of them were in the
components; every one was in the seams.

- **A server is bound to the event loop it was created on.** The OPC UA address
  space holds state tied to its loop, so `asyncio.run(plc.start())` followed by
  talking to it from a second loop times out — and the log says *connection
  refused/timeout*, which sends you hunting for a network fault that does not
  exist. One long-lived loop on its own thread, driven with
  `run_coroutine_threadsafe`. The Modbus listener still needs a second thread,
  because pymodbus's `StartTcpServer` has no asynchronous form.
- **Staging is not publishing.** `set_value` records a change; the address space
  only moves when `publish()` runs. Forgot, and a connected client watched a
  frozen plant that looked perfectly healthy — the most convincing possible
  wrong answer. Unit tests of the server passed throughout, because the server
  was never asked to do this.
- **A control program that never runs looks exactly like one that works.** The
  blocks were originally invoked beside the scan loop rather than through it,
  so the phase metrics — the only evidence they executed at all — were empty.
  They now run through `ScanLoop.scan_once()`.
- **`--duration` waited on a signal forever.** A bounded smoke test had to be
  killed by hand, and a test you have to kill is a test nobody runs. Shutdown now
  waits on whichever comes first.
- **The alarm signature is an ordering, not a threshold.** Measured on the
  `aeration_loss` scenario: at 2 h DO is 1.73 mg/L while effluent ammonia is
  still 0.96 mg/L; at 6 h DO has collapsed and effluent ammonia is 4.17 mg/L.
  The basin holds ~40 kg of oxygen, so air must be lost long before anyone is
  endangered. The end-to-end test asserts that lag, because an alarm on ammonia
  would have blamed the wrong unit.
- Two PLCs in one test session need their own ports. `Errno 48` is a real
  constraint, not a flake to retry.

## Phase 3a — the deadband and the spool

Two small components, and between them they produced five real bugs. All five
were found by tests that asserted a *property* rather than a case, which is
worth something on its own.

**The deadband**

- `BandRule.for_signal` read `signal.min` and `signal.max`. The field is called
  `range_min`/`range_max`, and `getattr(..., None)` returned `None` for every
  signal in the contract — so every span was `0.0` and relative mode was
  **silently dead across all 57 signals**. A component whose entire job is to
  filter, filtering nothing, with no error anywhere. `getattr` with a default is
  the worst way to read a field: it converts a typo into a silent wrong answer
  instead of an `AttributeError`.
- The baseline is the last **published** value, not the last **seen** one. I had
  written two tests asserting the opposite intuition (that cumulative distance
  matters), and both were wrong. The property that actually matters: a suppressed
  reading must not become the reference, or a signal drifting steadily at 0.6
  with a band of 1.0 never publishes — not at 0.6 per scan, not per hour. It
  simply vanishes while the plant keeps reporting it.
- A deadband of `0.0` does **not** mean "publish everything". It means "publish
  any change at all", which still drops a signal that is sitting still. I had
  assumed otherwise in a test. Worth knowing before calling 0.0 a safe default.
- `deadband_mode` is now validated in the contract loader rather than in the
  gateway. A typo like `relitive` is a startup failure with a line number,
  instead of a runtime surprise in the one component that reads it — which is
  also the component furthest from the file containing the mistake.

**The spool**

- The size cap was checked against the **current file's** size, not the spool's.
  Nine hour-files reached 2.8 MB under a 1 MB limit. A limit that looks
  enforced and bounds nothing is the worst way for a safety limit to fail.
- `st_size` cannot see Python's write buffer, so `refresh()` reported ~130 KB
  less than had been written — and *shrank* the running total in the process.
- A deleted file was credited back its `st_size` while the total had been
  incremented by the logical size. The remainder stayed counted forever, and the
  cap crept upwards a buffer at a time.
- Each of those three passed every smaller test. The test that caught all three
  asserts one property — *the total never exceeds the limit* — over enough hours
  to matter, because three separate near-misses all look fine in isolation.
- Two of my own tests were wrong before the code was. `drain()` correctly
  excludes the hour still being written, and I had written two tests expecting it
  not to. The component was right; my model of it was not. Worth saying plainly,
  because the instinct when a test fails is to change the test, and that instinct
  is wrong exactly when the test was the thing that was wrong.

## Open threads

1. **Modbus wire addressing — resolved, and it took three attempts.** The net
   translation is `PDU = contract address - 40000`, but it is the *composition*
   of three shifts (model index, a one-slot block lead-in, and pymodbus's
   `PDU = index - 1`), any of which can be changed independently. Two earlier
   probes disagreed because they were run against blocks populated differently.
   The one that settled it probed a live server and located returned values in
   the block. Ten xfail'd tests are now passing.
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
