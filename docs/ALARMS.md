# Alarms

Phase 4: the alarm engine. Sixteen rules over eleven signals, and a tool that
audits them against the fault library.

```bash
make coverage     # the fault x rule matrix, about eight minutes
make alarms       # run against a live historian
```

## The problem this is built around

**`single_point_threshold` cannot find nine of the twelve faults in
`contracts/fault-scenarios.yaml`.** That is not an opinion; it is a field in the
contract, written there by whoever modelled each fault, and
`sql/00-02` and the lessons that follow it are about why.

The reason is a single physical fact about this plant:

> The aeration basin holds about **40 kg of dissolved oxygen** in 20 000 m³ of
> water. It does not notice a fan.

So a blower trip produces, in order:

```
  fan speed        drops        within 30 s      ← the earliest signal in the system
  DO               sags         1–2 hours         ← the useful alarm
  effluent NH₄     rises        2–6 hours after   ← too late to be the only alarm
```

**Every one of those is detectable by a limit check if you pick the right signal,
and the right signal is the one that tells you least about the process.** A
threshold on fan speed fires in seconds and says "a fan moved". A threshold on
ammonia fires hours later, when the cause is over. Neither is a *process* alarm,
because the process — the oxygen — never leaves its 1.5–3.0 mg/L band. It sags
*within* it and recovers.

That is why `aeration_do_sagging` is a **trend**, and why
`tests/test_alarm_detectors.py::test_a_fast_alarm_and_a_slow_alarm_cannot_be_interchanged`
exists: it asserts the ordering on synthetic data, in seconds, and it asserts that
**a threshold on DO never fires at all** even though the basin genuinely lost
aeration capacity.

## The twelve detection methods

The contract names them. `alarms/base.py` holds the vocabulary and
`alarms/detectors.py` implements ten of them:

| Method | Catches | Notable |
|---|---|---|
| `single_point_threshold` | absences and slow-developing faults | the wrong default; used deliberately in 4 rules |
| `deviation_from_baseline` | sensor drift, cavitation | window mean, so a drifted baseline is not the answer |
| `trend` | 5 of 11 faults | the workhorse; fitted, not differenced |
| `rate_of_change` | fan trips | the earliest signal available |
| `state_change` | equipment stopped | reads `quality`, not `value` |
| `flatline_detection` | `sensor_flatline` | **the detector the deadband hides** |
| `expected_sample_count` | `effluent_tss_stuck` | looks at the count, not the value |
| `cross_validation` | 4 of 11 faults | **the only defence against a wrong number** |
| `ratio_derived_alarm` | digester souring | both terms can look fine |
| `model_based` | — | cheap: the normal band as the expectation |
| `correlation` | — | **declared, not implemented** |
| `oscillation_detection` | — | **declared, not implemented** |

The two missing ones are missing *visibly*. `DETECTION_VOCABULARY` is what the
contract names, `DETECTORS` is what exists, and
`test_every_detector_in_the_contract_is_implemented_or_declared_missing` asserts
the difference is exactly those two. A rule naming an unimplemented detector gets
a distinct error message, because "no such detector" and "not built yet" are
different problems.

## The audit: `make coverage`

This is the part an alarm system almost never has.

Every rule names the faults it claims to catch (`AlarmRule.detects`). Every fault
in the contract names how it *is* and *is not* detectable. The seeder can answer
"did this rule fire?" because it replays the plant model through each fault — so
the tool runs all eleven and reports a matrix.

```
fault                       detected  by rule                       contract says NOT:
-------------------------  ---------  ----------------------------  -----------------
blower_failure                   YES  blower_speed_drop, do_sagging  single_point_threshold
digester_souring                 YES  vfa_high, ph_low              single_point_threshold
effluent_tss_stuck               YES  tss_coverage                  single_point_threshold, deadband
high_ammonia_load                YES  ammonia_rising                single_point_threshold
lift_pump_cavitation             YES  lift_current_anomaly          single_point_threshold, state_change
sensor_flatline                  YES  blanket_stuck                 deadband, ...
sensor_stuck_high                YES  lift_current_anomaly          —
storm_inflow                     YES  flow_surge, flow_ceiling      single_point_threshold
-------------------------  ---------  ----------------------------  -----------------
do_sensor_drift                 by luck  —                            single_point_threshold, deviation...
lift_pump_failure               by luck  —                            —
sludge_blanket_thickening       by luck  —                            single_point_threshold

8/11 of the 11 faults detected (0 never run)
blind spots: sludge_blanket_thickening, lift_pump_failure, do_sensor_drift
caught only incidentally (not a claim, and it does not survive a retune):
    sludge_blanket_thickening, lift_pump_failure, do_sensor_drift
rules that never fired: aeration_do_xvalidation, influent_flow_xvalidation,
    lift_pump_flow_lost, secondary_scrape_torque_high
```

**Three blind spots, and three of them are caught "only incidentally".** That
combination is the most useful thing in the table. Each of those three faults *is*
found in practice — by six rules that were not looking for it. Retune those six,
and it is not. A coincidence standing in front of a blind spot is still a blind
spot, and the report says so rather than letting a green-looking row stand.

The honest reading of the whole table: **eight faults are covered by a rule that
claims them, four rules have never fired, and no claim in this rule set is
currently unverified.**

Three numbers at the bottom, and they are the ones worth reading:

* **blind spots** — faults no rule catches, whether claimed or incidental. Each one
  is a scenario the plant can be in and nobody will be told.
* **rules that never fired** — dead configuration. In an alarm system that is a
  liability, not a neutral: it is one more thing an operator has been trained to
  ignore.
* **contract disagreements** — the contract says a fault is *not* detectable by
  method X, and a rule using X fired on it anyway.

### The one the report has to get right

A fault that was **never run** is not a missed fault. The first version of this
report had no `evaluated` field, and asked for three faults out of eleven reported
*"8 of 11 blind spots"* — because the other eight had no transitions simply by
never having been simulated.

That is worse than reporting nothing, because it invites a rule to be deleted on
the strength of a measurement that was never taken. `coverage.py` now separates
them and the render says `untested` in the same column as `YES` and `no`.

## Three findings the audit produced

### 1. A trend over a short window is a noise estimator

The first full run: `aeration_do_sagging` fired on **ten of eleven** faults, and
six rules fired on almost every fault. The
rule was not miscalibrated by a factor — it was measuring the wrong thing. A
least-squares slope over a 30-minute window of dissolved oxygen is dominated by
sampling noise and by the plant's diurnal cycle, both of which clear a
0.15 mg/L per hour threshold on a perfectly healthy basin.

The fix is not a bigger number. It is a **`min_span_s`**: the rule now requires two
hours of history before it will fit a line, and the diurnal cycle cannot fake a
two-hour fall. `tests/test_alarm_detectors.py` pins both halves — the refusal on a
short window, and the fact that the real fault is still caught.

Re-measured after the fix: `aeration_do_sagging` fires on **one of eleven** —
`blower_failure`, which is the fault it exists for. The incidental catches
disappeared with it, because they were the same bug seen from another angle.

The general lesson, and it is the one I would keep from this whole phase:

> **A rate needs a window long enough that the thing you are measuring is slower
> than the thing you are trying to exclude.**

### 2. The contract's `NOT_detectable_by` entries are too absolute

The coverage report flags three disagreements: `digester_vfa_high` and
`digester_ph_low` are `single_point_threshold` rules, and the contract lists
`single_point_threshold` under `NOT_detectable_by` for `digester_souring`. They
fired. So either the rule is wrong or the contract is.

The rule is right, and the contract is **imprecise**. `NOT_detectable_by` reads as
*"this method cannot find this fault"*, and what it means in every one of those
cases is *"this method alone is not sufficient"* — a threshold on digester pH
finds souring only because souring is slow, has a limit, and the operator is
already watching. Set a high ammonia load and pH is the wrong signal entirely.

**The honest fix is to change the contract's wording, not to delete a working
rule.** That edit has not been made: a contract is the single source of truth for
the plant, and quietly rewriting it to agree with the code is the failure mode
this project keeps running into. It is a real disagreement and it is recorded here
as one.

### 3. The cross-validation rules cannot fire, and that is correct

`aeration_do_xvalidation` and `influent_flow_xvalidation` never fire, in any of the
eleven runs, and the report says so. The reason is structural: the plant
simulator publishes **one** source, so there is nothing to cross-validate against.

These two rules exist for the *gateway*, which reads Modbus and OPC UA and writes
both with `source` in the primary key. They are the only defence in this project
against a low-word-first float decoding to `2.3e-41` — a value that is finite, in
range, and wrong, and that no check against the value itself can catch. A rule
that never fires in simulation and fires in production is a rule the simulation
cannot test, and the report naming it is the honest outcome.

## The state machine

`alarms/engine.py`. Small, and every part of it is a decision:

* **`for_s` is a physics statement, not a debounce.** "This fault takes at least
  this long to develop." The fan drops in 30 s so its rule has `for_s = 0`; DO
  sags over two hours so its rule has `for_s = 600`.
* **`clear_s` is always positive.** A rule that clears the instant its condition
  goes false chatters when a signal sits on the limit, and a chattering alarm is
  one operators learn to ignore.
* **`critical` latches.** It stays on the operator's list until acknowledged, even
  after the condition has cleared. An alarm that vanishes by itself is one nobody
  has to acknowledge, which is indistinguishable from one that never fired.
  `warning` auto-clears, because a warning that latches is how a mimic diagram
  fills with permanently-acknowledged icons.
* **Re-emission needs two conditions.** The value must have moved
  (`relatch_delta`) *and* `relatch_interval_s` must have elapsed. Movement alone
  produced 9 994 reaffirmations of three alarms in nine hours; a time limit alone
  would mean a static alarm never updates, and the first message an operator sees
  is the one with the least information in it.
* **The transition log evicts reaffirmations first.** The first version dropped
  from the front, which is the obvious thing and the wrong one: the transitions
  lost were the `raised` records at the start of the run, so a coverage report
  over a long run silently lost every fault detection and reported the faults as
  blind spots.

## The deadband's blind spot, and why `flatline_detection` cannot close it

A deadband suppresses readings that have not moved. So a **healthy steady signal
and a failed instrument are the same observation at the database level** — and the
contract says so, listing `deadband` under `NOT_detectable_by` for both
`sensor_flatline` and `effluent_tss_stuck`.

`secondary_blanket_stuck` fires on `sensor_flatline`, which is the point. But look
at what it reports: *movement*, not *broken*. A clarifier blanket that genuinely
does not move 5 mm produces the same output as a frozen probe, and the rule says
so in its own event detail. Closing the gap needs a second observation of the same
quantity — which is `cross_validation`, and which the simulation cannot exercise.

This is the honest limit of the design and it is also the reason
`sql/02-04` exists: thirteen signals produce exactly one reading in a seeded week.

## Thresholds: how they were set

Every threshold in `alarms/rules.py` is derived from a measurement of what a
settled healthy plant actually does, and the measurements — including the two
harness bugs that made every one of them wrong first, and the two rules that
still cannot work — are in [`ALARM-TUNING.md`](ALARM-TUNING.md). Read that before
changing a number here.

## The finding that started it: six rules fired on a healthy plant

Not "might". Measured, on a six-hour baseline run with no fault armed:

| Rule | Why it fires on a healthy plant |
|---|---|
| `aeration_ammonia_rising` | 0.5 mg/L per hour upward is inside the diurnal swing |
| `aeration_blower_speed_drop` | blower speed moves by thousands per hour on the plant's own load cycle |
| `aeration_do_low` | DO reaches 1.40 against a `normal_low` of 1.5 |
| `influent_flow_ceiling` | influent flow reaches 2 249 m³/h against a `range_max` of 2 200 |
| `influent_lift_current_anomaly` | a 25 % deviation from an 1 800 s window mean is well inside a noisy pump's ordinary scatter |
| `secondary_blanket_stuck` | the clarifier blanket genuinely moves less than its deadband |

**An alarm system whose rules fire on a healthy plant is worse than no alarm
system.** Every alarm it raises is a false one, and operators learn to ignore the
panel within a shift — at which point the eight faults this rule set *does* detect
are being ignored too.

The cause is the same in most of these rows: **the thresholds were written from
engineering judgement and from the contract's normal bands, and neither of those
is a measurement of what a healthy plant actually does.** A rule built on a
`range_max` inherits that range's optimism, and two of these signals reach or
exceed it in normal operation.

`aeration_do_sagging` was the seventh and the worst, and it is the one that was
fixed — see §3.1. The method that fixed it is the method for the other six:
**measure the healthy distribution, then put the threshold above it.** For DO that
was a 1 618-sample sweep and took two minutes.

`tests/test_alarm_rules.py::test_a_healthy_plant_raises_almost_nothing` is a
ratchet: it asserts the *set*, so a newly-tuned rule has to be removed
deliberately, and the *count*, so a regression is obvious. Reaching zero is the
next piece of work and it is measurement rather than design.

## What is not built

* ~~**Acknowledgement from an operator.**~~ **Done.**
  `alarms/replay.py` rebuilds the state from the event log and
  `scada/flows/02-annunciator.json` now writes an `alarm_acknowledged` row. The
  two halves are separate on purpose and the reason is the interesting part:

  * the **engine** decides whether something is happening now, and holds only
    what it has evaluated;
  * the **replay** holds what the log says is outstanding, and the panel reads
    that.

  They cannot disagree, and a Node-RED flow restart loses nothing, because nothing
  was in memory to lose. See `alarms/replay.py` for the design decision it
  forced: acknowledgement is per *rule* and clears when the condition clears,
  because an operator acknowledges what they can see on a panel, and a recurrence
  is a new event that deserves a new acknowledgement.
* **`correlation` and `oscillation_detection`.** Declared in the contract, named
  in the vocabulary, absent from the registry.
* **Alarm suppression and shelving.** Two blowers tripping at once produce two
  alarms, and an operator facing four unacknowledged criticals cannot rank them.
  A real system has a deadband on the *annunciator* as well as on the signal.
* **State persistence.** The engine holds its state in memory, so a restart loses
  which alarms were latched. The `event` table has the history but nothing
  replays it into the state machine.
* **The deadband in this measurement.** `alarms/scenarios.py` records every sample
  at the record rate rather than applying the contract deadband, so no rule has to
  survive a six-minute gap between DO readings. That flatters the rules and it is
  recorded here rather than buried. `make alarms` runs the real path.
* **Five false positives remain**, listed above and measured in
  `ALARM-TUNING.md`. Six became five; none of the remaining five is fixable by
  tuning — two are structurally undetectable, one is the deadband's blind spot,
  and two sit on thresholds the contract's own normal bands made unreachable.
* **The two harnesses disagree by a factor of seven** on the healthy DO slope.
  `aeration_do_sagging`'s threshold is set from the wider measurement, so it is
  safe, but the disagreement is unexplained and is the most important open item
  in the tuning write-up.
* **Three blind spots.** `sludge_blanket_thickening`, `lift_pump_failure` and
  `do_sensor_drift` are found only incidentally, by rules that do not claim them.
  Two of the rules that *should* find them — `secondary_scrape_torque_high` and
  `lift_pump_flow_lost` — have never fired. A rule that is written, claimed and
  unverified is the worst state for an alarm rule: it looks like coverage on the
  strength of its `detects` list and provides none. **This is the next thing to
  fix, and it is a tuning problem rather than a design one** — the signatures are
  known, the signals are right, and the thresholds have not been checked against
  measured baselines the way the aeration ones now have been.
