# Dashboard stories — a proposal

**Status:** proposal. Nothing here is built. It exists because the current
dashboards answer *"what does this signal do?"* and an operator needs *"is the
plant all right, and if not, where do I look?"*

## What is wrong with the current two

Measured, not asserted:

```
9 panels      timeseries: 7   table: 2   stat: 0
```

| problem | evidence |
|---|---|
| **No at-a-glance layer** | zero `stat` panels. An operator opening a mimic wants four big numbers before any trend. |
| **Paired panels are not relationships** | `Influent and effluent flow` is two independent `avg(value)` series. No ratio, no lag, no difference. You can read *both are going* and nothing else. |
| **No equipment state anywhere** | `RunState 0  equipment 0  PUMP 0  BLW 0  RAS 0` in every query. The contract declares **21** pieces of stateful equipment and none of it is displayed. *Which pump is running* is the first question. |
| **Permit panel returns a boolean** | `round(avg(h.mean),3) AS measured, avg(h.mean) <= 10.0 AS compliant` → `1.06, true`. The useful number is **1.06 of 5 mg/L, 21 % of permit**, and its trend. A boolean has no magnitude and therefore no story. |
| **Titles are nouns** | *Dissolved oxygen* says what the signal is, not what to look for. |
| **The one honest panel is an aside** | *What has stopped reporting* is the only non-trend panel. It is the most truthful thing on the dashboard and it is last. |

## The architectural tension, stated plainly

The dashboards are **generated from `contracts/tags.yaml`**, and that is the cause.

> The generator guarantees consistency. It cannot invent narrative.

It knows 57 signals exist, their units, and their measured bands. It does not know
that **DO ÷ airflow** is a question worth asking — that a blower working hard
while DO sags means something different from both being steady. That knowledge is
a person's, and no amount of deriving it from YAML produces it.

Both naive fixes are wrong:

- **hand-written dashboards** lose the guarantee that ids, units and thresholds
  come from the contract — which is the defect class of thread 22, where a permit
  dashboard read pH from the TSS signal and rendered a plausible wrong number.
- **fully generated** is what exists.

### The proposal: generation *plus* curation

Keep the contract as the source of ids, units, ranges and bands. Add a
**declared list of panels with intent** — a `stories` block in the generator
(or a small `ui/grafana/stories.yaml`) where each entry says the question, the
signals that answer it, and the panel type.

The contract still supplies every id, unit and threshold. The curation supplies
only the *question*. That keeps the guarantee and adds the narrative, and it
makes the questions reviewable in a pull request — which a hand-built layout
never was.

## The spine: eleven questions, already written

The fault library is an operator's question list that was written for a different
reason. Eleven faults, and each is a question a person on shift would ask:

| fault | the question an operator asks |
|---|---|
| `storm_inflow` | is the influent surprising me? |
| `blower_failure` | is the basin still being aerated? |
| `high_ammonia_load` | can the plant treat what it is being given? |
| `do_sensor_drift`, `sensor_flatline`, `sensor_stuck_high` | can I trust these instruments? |
| `sludge_blanket_thickening` | is the clarifier about to fail? |
| `effluent_tss_stuck` | is the permit at risk? |
| `lift_pump_cavitation`, `lift_pump_failure` | is water still being lifted? |
| `digester_souring` | is the digester about to sour? |

**A dashboard organised so those eleven can each be answered in one glance** is a
designed thing. A dashboard listing signals is not. This is the whole proposal.

---

# The stories

Ordered by what an operator does first. **Measurement** column: does this need a
measured settled-healthy distribution before it can carry a band, or is the
number already grounded?

## Dashboard 1 — `WWTP — plant status`

*The question: is anything wrong, and where do I look?* Answers at a glance, no
interaction.

| panel | type | shows | measurement |
|---|---|---|---|
| Permit margin | stat ×5 | NH₄, TSS, pH low, pH high, coliforms — **% of limit used**, with the 30-day sparkline | **no** — permit values are in the contract |
| Signals not reporting | stat | count of signals silent > 1 h, of 57 | **no** — arithmetic on `reading` |
| Aeration on target | stat | DO as % of setpoint, green inside the contract band | **no** — the band is in `signal` |
| Blower / pump state | stat row | 21 `state_equipment` units with `RunState` | **no** — the contract declares them |
| Outstanding alarms | stat | criticals unacknowledged, last 24 h | **no** — `event` |
| Storm active | stat | `SITE:WEATHER:STORM`, with rain rate | **no** |

Everything on this page is grounded already. **It is also the page that would
have answered "what did you create for Grafana?" instantly**, and it is the page
whose absence made the current two look like a list.

## Dashboard 2 — `WWTP — what changed`

*The question: what is different from an hour ago, and is it a problem?*

| panel | type | shows | measurement |
|---|---|---|---|
| Anything out of band, ranked | table | signal, value, band, **how far out**, duration | **no** |
| Biggest movers, 1 h | table | top 10 by \|Δ mean\| vs the previous hour | **no** |
| Fault signatures present | table | the 11 faults, each with a live yes/no/indeterminate | partly — `lift_pump_failure` is **structurally undetectable** (thread 10) and must render *indeterminate*, not *ok* |

That last row matters: **a fault panel that cannot see a fault is a lie**, and
this project has five such faults already documented. The panel has to be able to
say "I cannot tell", and that is a design constraint rather than a detail.

## Dashboard 3 — `WWTP — treatment`

*The question: is the biology working, and what is it costing?*

| panel | type | shows | measurement |
|---|---|---|---|
| Nitrification | timeseries | NH₄ in and out on one axis, with **removal %** as a second axis | **no** for the values; **yes** for a removal-% band |
| Carbon removal | timeseries | BOD/TSS in vs out, removal % | depends on what the contract has |
| DO vs airflow | timeseries ×2 | DO against its setpoint; **air per kg NH₄ removed** | **YES** — specific air use has no measured healthy distribution |
| Sludge settleability | timeseries | secondary blanket, overflow solids | **yes** for a rising-rate band |
| Digester health | timeseries | VFA/alk ratio against the measured 0.2–0.3 / >0.5 | **no** — already measured |
| Return sludge | timeseries | RAS flow and rate | **YES** |

## Dashboard 4 — `WWTP — influent & lift`

*The question: what is arriving, and is the plant coping?*

| panel | type | shows | measurement |
|---|---|---|---|
| Flow, NH₄, turbidity | timeseries ×3 | with the storm flag as a **vertical annotation**, not a separate line | **no** |
| Wet well | timeseries | level, pump current, starts per hour | **YES** for a starts/hour band |
| Pump state | stat row | PIT-1/2/3 and BLW-1..4 | **no** |
| Load vs capacity | timeseries | current load against the contract's `design_flow_m3h` | **no** — the number is in the contract |

## Dashboard 5 — keep `WWTP — overview`

Unchanged. It is the plant's geography and it does that job.

## Dashboard 6 — rework `WWTP — discharge permit`

*The question: am I compliant, and by how much?*

| panel | type | shows | measurement |
|---|---|---|---|
| Margin, not boolean | table | parameter, unit, measured, limit, **% of limit used**, 30-day trend sparkline | **no** |
| Worst hour in 30 days | table | the single worst value per parameter, with its timestamp | **no** |
| Trend of % of limit | timeseries | the margin over 30 days — the shape is the story | **no** |

The existing panel is *Permit compliance, from the contract* and it returns
`1.06, true`. This replaces the boolean with the margin and adds the worst-hour
row, which is the number a regulator would ask for.

---

# What needs measuring first

Seven panels want a band that does not exist yet. Each needs a
settled-healthy distribution the way the alarm thresholds did, recorded in
`docs/ALARM-TUNING.md`:

| quantity | why it is not obvious |
|---|---|
| specific air use (kg O₂ / kg NH₄ removed) | depends on load; needs the *distribution*, not a number |
| clarifier blanket rising rate | the signal is a level, the question is a slope |
| return sludge rate | no contract band exists |
| wet-well pump starts per hour | a count per interval, and the deadband hides small changes |
| nitrification removal % | bounded 0–100, but the *good* band is not 100 % |
| carbon removal % | only if the contract has an influent carbon signal to compare against |
| anything out of band, ranked | **no measurement** — uses the contract's own bands |

**Rule I would hold to:** the seven panels above ship with the number and *no
band* until measured, or they do not ship. A band drawn from a specification
rather than a measurement is the exact defect `docs/ALARM-TUNING.md` exists to
document, and putting one on a dashboard would be the first invented threshold in
the project.

---

# Estimate

Stated as work, not calendar, because the design decisions need you and the
measurement passes are mine.

| # | work | size | notes |
|---|---|---|---|
| 1 | **Agree the story list** | **you + me, ~1 hour** | the panel list above, minus or plus panels. This is the part that cannot be parallelised and it is the part that decides whether the result is good. |
| 2 | Curation layer in the generator | **M, 1 day** | `stories.yaml`; ids/units/bands still resolved from the contract; a test that every id named exists — the thread-22 guard, reused |
| 3 | `stat` panels + margin maths | **S, half a day** | the `% of limit used` arithmetic, thresholds as cell colours from contract limits |
| 4 | Equipment state grid | **S, half a day** | needs a `state_equipment` → `reading` path; the contract has the units, nothing displays them |
| 5 | Story 2: "what changed" | **M, 1 day** | plus the **indeterminate** state for the five undetectable faults — the hard part is honesty, not SQL |
| 6 | Story 3: treatment, relationships | **M, 1 day** | two-axis panels, removal %, and the derived-metric SQL |
| 7 | Rework the permit dashboard | **S, half a day** | margin table, worst-hour row, margin trend |
| 8 | **Measurements for the 7 bands** | **M, 1–2 days** | reuse `alarms/tune.py`; `settle_s_for()` already excludes startup. Can overlap 6 and 7. |
| 9 | Tests | **M, 1 day** | every panel's SQL against a live DB; every id against the contract; a band-present-or-explicitly-absent assertion |
| 10 | Docs | **S, half a day** | `docs/ALARMS.md` cross-refs, the tuning doc, `ui/grafana/README.md` |
| | **total** | **≈ 8–9 days** | of which ~5 are mine and ~1 is yours |

### Where I would cut if you want less

- **1 + 2 + 3 + 4 + 10 ≈ 3 days** gives you Dashboard 1 (`plant status`) alone.
  That single page fixes every complaint in the table at the top — no big numbers,
  no equipment state, boolean permit — and it needs **no new measurements**.
  **That is the 80 % of the value for 35 % of the work**, and it is what I would
  build first.
- The relationship panels (6) and the measurement pass (8) are where the real
  plant-engineering content is, and the measurement pass is the only item that
  can change its own answer twice.

### The honest risk

Item 1. The panel list above is my proposal, not a derivation, and **the generator
cannot tell me it is wrong.** If the story is wrong, 8 days of very solid code
produces a beautiful dashboard that answers questions nobody has. That is the same
failure as the current one, one level up, and only you can catch it.

So: agree the list first, build Dashboard 1, look at it, and then decide whether
items 5–8 are worth it.
