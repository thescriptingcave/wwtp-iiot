# Alarm tuning

Where [`ALARMS.md`](ALARMS.md) records what the alarm engine *is*, this records
how its thresholds were set, what the measurements were, and — at some length —
what did not work.

```bash
python -m alarms.tune          # every rule, every fault it claims
python -m alarms.tune --json   # the numbers below, machine-readable
python -m alarms.tune --rule aeration_do_sagging
```

About 18 minutes for the full matrix. Eight hours of settled measurement per
scenario, after a 9h15m settle.

## Why a tuning tool and not a spreadsheet

`ALARMS.md` records the first finding of the alarm phase: `aeration_do_sagging`
fired on ten of twelve faults *and on the healthy plant*, because its 0.15 mg/L
per hour threshold sat inside the diurnal distribution. It was fixed by hand, in
a throwaway script, from 1 618 samples.

That worked once. Doing it by hand five more times is how you get it wrong twice,
so `alarms/tune.py` is the same measurement for every rule: the healthy
distribution, the fault distribution, and whether the *current* threshold
separates them.

| verdict | meaning |
|---|---|
| `ok` | silent on a healthy plant, fires on what it claims |
| `noisy` | fires on a healthy plant |
| `blind` | never fires on a fault it claims |
| `noisy and blind` | both, and the worst place to be |

## The two harness bugs, which are the most important thing here

Every threshold in this project was wrong before these two were fixed, and both
were invisible because a number came out the other end.

### 1. The plant's startup was in the "healthy" sample

Measurement could not begin until the longest rule lookback had history. With a
20-minute rule that was 15 minutes in, so the first accepted windows contained a
cold basin filling, a blower loop finding its operating point, and a digester
coming up to temperature.

The result was a measured "healthy" DO slope of **−1.513 mg/L/h** against
**−0.427** measured from a settled start. The difference is entirely
commissioning, and a threshold set above the first number is set above a
transient no healthy plant ever experiences.

> **A tuning measurement must exclude the plant's startup, or it will always set
> thresholds too high.**

### 2. The fault was armed at t = 0, so it was over before measurement began

Fixing the first bug made the second visible. The settle point is 9h15m. A storm
lasts 2 hours. Arming the storm at t = 0 meant it was over and gone before the
first measurement, and the tool cheerfully reported that influent flow was
*identical* under a storm and under a healthy sky — to three significant figures.

The tell was that every fault's distribution came back equal to the baseline's.
**An identical distribution is not a finding about the fault; it is a finding
about the harness.**

`alarms/scenarios.py` had the same two bugs for the same reasons, and its
`keep_s` was computed from `stuck_s` and `for_s` rather than `lookback_s()` — so
it kept three hours of history for a rule that wanted nine, and the rule could
never be evaluated at all. A rule that cannot be fed is indistinguishable from a
rule that does not work, and the coverage matrix is exactly where that mistake
is invisible: it reports a number, the number is zero, and zero is an ordinary
result for a rule nobody has debugged.

## The measurements

Thresholds after tuning, with the healthy distribution and the claimed fault's
distribution beside them. All over settled operation, seeded-window limits as
noted.

| Rule | Threshold | Healthy | Claimed fault | Verdict |
|---|---|---|---|---|
| `aeration_blower_speed_drop` | 20 000 rev/min/h | −1 615 .. +1 589 | blower −667 600 .. +585 000 | ok |
| `aeration_do_sagging` | 0.10 mg/L/h down | −0.028 .. +0.049 | blower −0.320 .. +0.273 | ok |
| `aeration_ammonia_rising` | 2.0 mg/L/h up | +0.027 .. +0.044 | ammonia load +3.48 .. +5.68 | ok |
| `influent_flow_ceiling` | 2 000 m³/h | 1 800 .. 1 868 | storm 1 800 .. 5 571 | ok |
| `influent_lift_current_anomaly` | 0.05 above mean | 0 .. +0.039 | cavitation +0.043 .. +68.1 | ok, 10 % margin |
| `secondary_scrape_torque_high` | 50 N·m | 42.9 .. 47.0 | blanket 42.9 .. 58.3 | ok |
| `digester_vfa_high` | 0.6 | 0.355 .. 0.432 | souring 0.355 .. 0.944 | ok |
| `digester_ph_low` | 6.4 | 6.71 .. 7.01 | souring 5.14 .. 7.01 | ok |
| `effluent_tss_coverage` | 3 in an hour | 1 .. 721 | stuck TSS 0 .. 721 | ok |
| `influent_flow_surge` | 400 m³/h/h | −46 .. +40 | storm −2 112 .. +40 | **blind** |
| `lift_pump_flow_lost` | 50 m³/h | 1 731 .. 2 293 | pump failure 1 731 .. 2 378 | **blind** |
| `secondary_blanket_stuck` | no movement | 0 (100 % FP) | flatline 0 | **noisy, unfixable** |
| `aeration_do_xvalidation` | 5 % | — | — | cannot fire in simulation |
| `influent_flow_xvalidation` | 2 % | — | — | cannot fire in simulation |

Every false-positive rate that is not listed as unfixable is **0 %** over eight
settled hours. It was six rules before this work.

## What each threshold was derived from

**`aeration_blower_speed_drop` — 20 000 rev/min per hour.** The steepest change a
healthy blower makes between two samples is about 6 900. That is the control loop
hunting, not the machine. A trip is three orders of magnitude away. The old
value of 1 800 was *inside* the healthy distribution, so the "earliest alarm in
the system" fired on 17 % of a healthy plant and was therefore not an alarm.

**`aeration_do_sagging` — 0.10 mg/L per hour, over a six-hour window.** The span
is half the answer. Fitting the same DO series over three window lengths:

```
span   healthy min   blower trip p01   separation
 3 h       -0.427          -0.797          1.9x
 6 h       -0.110          -0.419          3.8x
 9 h       -0.028          -0.320         11.3x
```

A least-squares slope over a short window is a noise estimator. The deadband-free
DO series moves fast enough that a twenty-minute fit swings through several mg/L
per hour on a perfectly healthy basin.

**`influent_flow_ceiling` — 2 000 m³/h.** The contract's `range_max` is 2 200 and
a healthy plant peaks at 1 868, so the old threshold fired on 17 % of healthy
time. This is the general lesson in its purest form:

> **A threshold copied from a normal band inherits the band's optimism, and a band
> is a specification, not a measurement of what the machine does.**

**`secondary_scrape_torque_high` — 50 N·m.** The old value was 120, the top of
the contract's 10–150 N·m band, which is above everything the fault produces. The
rule never fired once in eleven runs. Same lesson, and it is the second time this
project has hit it.

**`influent_lift_current_anomaly` — 0.05 above the window mean.** Two changes
were needed and only one was a number. The rule passed `direction: high` and the
detector **ignored it**, using `abs()` instead — so a rule documented as "current
far above its normal draw" fired on a large *fall*. A healthy lift pump's current
swings about 40 A either side of its mean, and `abs()` called the downward swing
an anomaly. That was the entire 90 % false-positive rate.

`tests/test_alarm_rules.py::test_no_rule_carries_a_parameter_the_detector_ignores`
now asserts that every key a rule passes is read by its detector. It found two on
its first run — this one and `min_span_s`, below.

## Three things that did not work, and one that cannot

### `min_span_s` was declared and never used

`aeration_do_sagging`'s fix in the previous commit was described as *"a span
rather than a number"*. The span was in `detectors.trend`, in
`AlarmRule.lookback_s`, and in the detector's own test. **It was not in
`alarms/rules.py`**, because a string replacement silently failed to match. So
no rule passed the parameter, no rule got a longer window, and the rule kept
fitting a line over twenty minutes — the exact bug the commit claimed to fix.

It survived because the detector's test builds rules through
`alarms.synthetic.rule()`, which fills in sensible defaults for every parameter.
A test helper that defaults a parameter whose whole purpose is to be
absent-by-default will hide exactly this. The guard is now at the level of the
*rule set*, not the detector.

### The two harnesses disagree by a factor of seven

`aeration.tune` and `scenarios.run_fault` measure the same rules over nominally
the same window and report healthy DO slopes of **−0.005** and **−0.028** mg/L per
hour respectively. A threshold of 0.02, taken from the first, fires on a healthy
plant in the second.

`0.10` is set from the wider of the two, with better than three times the margin
on either side. **The disagreement is not resolved and is the most important open
item on this page.** The likely candidates are the physics step (`SCAN_DT` 0.1 s
in both, so not that) and the recording rate, but I did not isolate it, and
guessing in a document is worse than saying so.

### `lift_pump_failure` has no signature, and that is a contract gap

`pump_fault` sets the pump's `running = False` and `speed = 0`. The lift station
controller starts the other pump, and **both `INFLUENT:LIFT:CURRENT` and
`INFLUENT:LIFT:FLOW` are station totals**, so neither moves. The healthy and fault
distributions are identical to every digit the tool prints.

A single-pump failure is structurally undetectable from the signals this
contract collects. The fix is per-pump signals in `contracts/tags.yaml` — a
contract change, recorded as one rather than made here.

### `secondary_blanket_stuck` fires on 100 % of healthy time and always will

The clarifier blanket genuinely moves less than its own deadband. This is the
deadband's blind spot, it is why the contract lists `deadband` under
`not_sufficient_alone` for `sensor_flatline`, and **no threshold can fix it** — the
quantity a threshold would need is one the database does not record.

The rule says "no movement" rather than "broken", and that is the honest claim.
Closing the gap needs a second observation of the same quantity, which is
`cross_validation`, which the simulation cannot exercise because the plant model
publishes one source.

## The false-positive ratchet

`tests/test_alarm_rules.py::test_a_healthy_plant_raises_almost_nothing` asserts
the *set* of rules that fire on a healthy plant and their count, so a tuned rule
has to be removed deliberately and a regression is a failing test. It was **six
rules**; it is now **five**, and the remaining five are the ones in this document
that are either unfixable, structurally undetectable, or measured at a 10 % margin.

The tests marked `slow` take **18 minutes**, because every scenario settles for
9h15m before measurement begins. That is a real cost and it is the direct
consequence of the second harness bug above: the settle window is set by the
longest rule lookback, so one long-horizon rule makes every scenario slow. A real
deployment would fix that by keeping the settled plant state rather than
re-settling per run, and it is the first thing to change here.

## Exercises

1. `alarms/tune.py` and `alarms/scenarios.py` disagree by a factor of seven on
   the healthy DO slope. Both use `SCAN_DT = 0.1` and `RECORD_DT = 5.0`, and
   both now settle and arm the fault after settling. Read both `measure_run` and
   `run_fault` side by side and find the difference. It is *not* the deadband, the
   physics step, or the window length — all three are the same.
2. `influent_lift_current_anomaly` has a 10 % margin between a healthy 0.039 and
   a cavitation 0.043. Is that margin worth having an alarm for? Decide, then
   check what the *contract* would need to say for you to trust it.
3. `secondary_scrape_torque_high` detects a fault that raises torque from 47 to
   58 against a healthy range of 43–47. Find the threshold that would give a
   1 % false-positive rate, and say what the cost is in detection.
4. `influent_flow_surge` is blind: a storm's flow *slope* never exceeds a healthy
   flow's slope, even though the storm's flow *level* triples. Which of
   `single_point_threshold`, `deviation_from_baseline` and `trend` would find it,
   and what is the false-positive cost of each?
5. `run_fault` now settles for 9h15m per scenario, which is why the slow tests
   take 18 minutes. Make the settle depend on the *rules under test* rather than
   on the whole rule set, and check that `test_the_blower_trip_is_caught_by_a_trend_not_by_a_threshold`
   still passes when only the aeration rules are passed to it.
