# The machine-learning workshop

A third track, after the SQL course and the analyst notebooks. Those two teach
**tools**; this one teaches **the moment a score stops being evidence**, using this
plant's data as the thing that does it.

Nothing here is written yet. `build_dataset.py` is, and it exists because the
prototype it came from found that the obvious way to get a modelling dataset does
not work.

## The one hard finding, and everything else follows from it

**The stored hourly table has no row at all inside the window of two of the three
instrument faults.** Measured on a 25-week seed:

| Fault | Rows in the stored window | Of possible |
|---|---|---|
| `do_sensor_drift` | 2 | 90 minutes |
| `effluent_tss_stuck` | **0** | 120 minutes |
| `sensor_dead` | **0** | 60 minutes |

A classifier fitted on that table is not a weak classifier. It is one with no
positive examples for two thirds of the classes, and every number it reports is a
statement about the sample rather than about the plant.

So `build_dataset.py` does not query the hourly table. It **crosses every signal
with every hour** and left-joins, so an absent hour becomes a row with `n = 0`.
The panel is 57 × 4,200 = 239,400 rows, 194 of them positive — a base rate of
**0.081%**, and every injected fault present as a row.

## Build it

The dataset is a separate database, not the plant's, and it is not built into
`wwtp` by accident:

```bash
# 1. seed a 25-week window with a fault every 36 hours, at 60 s sampling
POSTGRES_DB=wwtp_ml tools/py.sh -m storage.seed.main \
    --days 175 --sample-interval 60 --fault-every-hours 36 \
    --storm-after 36 --end 2026-09-29T00:00:00Z --reset

# 2. emit the tidy table
WORKSHOP_DSN="host=127.0.0.1 port=55433 user=wwtp dbname=wwtp_ml" \
    uv run --extra workshop python -m workshops.ml.build_dataset --days 175
```

`--days`, `--end` and `--every-hours` **must match the seed**, because the labels
are derived from those three numbers rather than read from the data. A mismatch
does not fail: it produces a table whose labels are confidently wrong, which is why
the CLI takes them rather than inferring them.

## The three row states, and why two columns

A stored hour can be in one of three states, and all three are kept
distinguishable:

| State | `row_written` | `value_is_null` | Means |
|---|---|---|---|
| a value | 1 | 0 | the instrument is answering |
| a NULL value | 1 | 1 | `sensor_dead` — it stopped answering |
| no row | 0 | 1 | `effluent_tss_stuck` — it never changed, so nothing was written |

Collapsing the last two into one "missing" column throws away the distinction
between a dead instrument and a frozen one — **which is the distinction the fault
schedule exists to teach** — and throws it away silently, because the frame is
still rectangular and the labels are still valid.

Read this row before designing anything:

| Class | hours | `row_written` | `value_is_null` | median `n` |
|---|---|---|---|---|
| `do_sensor_drift` | 10 | 100% | 0% | 17 |
| `effluent_tss_stuck` | 8 | 0% | 100% | 0 |
| `sensor_dead` | 4 | 0% | 100% | 0 |
| **normal** | 28,706 | 60% | **41%** | 2 |

**41% of ordinary hours are in exactly the state a broken instrument is in**, and
`sensor_dead` and `effluent_tss_stuck` are in the *same* state as each other. The
row state is necessary and nowhere near sufficient.

The drift also inverts the intuition: median `n` of **17 against a normal median of
2**. A drifting value keeps changing, so the change-triggered historian keeps
writing, so the fault makes the instrument *louder*. A detector looking for quiet
finds the stuck sensor and misses the drift; one looking for busy finds the drift
and fires on every diurnal swing.

## What the per-signal baseline is for

11,639 empty hours in that panel, of which **12 are fault hours — 0.1%.** Absence
on its own is nearly worthless, which is why a `RandomForest` on the naive features
reaches only F1 **0.500** on a held-out fifth. What separates a fault hour from a
quiet hour is that *the same signal* was busy yesterday: a fault hour's signal
logged 326 rows in the preceding 24 h, a quiet hour's signal logged 20.

One trailing median per signal takes F1 from **0.500 to 0.800**, precision still
1.000. Held-out fifth, read back from the emitted CSV so no database is involved:

| features | accuracy | recall | precision | F1 |
|---|---|---|---|---|
| naive (`n`, `row_written`, `value_is_null`) | 0.9997 | 0.333 | 1.000 | 0.500 |
| **causal — what the builder emits** | 0.9998 | 0.667 | 1.000 | **0.800** |
| the same plus a **forward** 24 h window | 1.0000 | 1.000 | 1.000 | **1.000** |

**The 1.000 is available and it is worth nothing.** A detector that reads tomorrow's
row count already knows whether the instrument broke today, and cannot be run on
live data. `0.800` is the honest number; `1.000` is the cheat. The builder emits
only the causal features, deliberately, and the workshop adds the forward window as
an exercise and watches the score go up.

Read the accuracy column across all three rows: 0.9997, 0.9998, 1.0000. It does
not notice any of it, and neither does a `DummyClassifier`.

## Files

| File | What it is |
|---|---|
| **[`TRAINER.md`](TRAINER.md)** | **Read this before teaching.** The arc, the number at each of six beats, three of which are not in the original plan, and a list of things that will go wrong |
| `build_dataset.py` | The builder. Pure functions over a frame, IO in `main()`, so `tests/test_workshop_dataset.py` runs without a database |
| — | 15 tests, in `tests/test_workshop_dataset.py`: the panel is dense, the baseline cannot see the hour it judges, the two row states stay apart, the storm is not a fault |

```bash
make workshop             # seed and build
make workshop-notebooks   # the claim-checking gate, same five checks as `make notebooks`
```

`make workshop-notebooks` is the analyst gate on the workshop track: every `output`
block is a claim checked against a real run, and every **bold** number in the prose
is checked against what the notebook printed. The two gates that do not apply are
`sql`-fence execution and the seed fingerprint — a CSV has no query blocks and a
derived panel has no fingerprint — and they are switched **off** rather than left to
pass vacuously.

## The six beats, and the number at each

Full detail and the failure modes in `TRAINER.md`.

| # | Beat | The number the room should be holding |
|---|---|---|
| 1 | The metric turn | accuracy **0.9995** for a model that learned nothing |
| 2 | The dense panel | **2 of 3** fault kinds have no row in the stored table |
| 3 | The per-signal baseline | F1 **0.500 → 0.800** |
| 4 | The forward window | F1 **1.000**, and it cannot ship |
| 5 | Unsupervised | **0 of 22** fault hours found; 285 of 288 flags are one tag |
| 6 | Predictive | a seasonal naive beats every model; **90%** of variance is the clock |

**Accuracy never moves** — 0.9995, 0.9997, 0.9998, 1.0000 across all six. That is
the thesis of the two days.

## Known limitation, recorded rather than fixed

`--storm-after` arms **one** storm, at the end of the window, so the final week is
contaminated by it. That is wrong for a "score your detector on a held-out week"
exercise. The fix is a recurring storm in the seeder, not a change here; until
then, hold out a week from the *middle* of the window.

A second, measured: **one fault and the storm coincide.** On the 3-week seed a
`do_sensor_drift` on `AERATION:AHU-1:DO` runs 2026-09-27 12:00–13:00, inside the
storm's 12:00–14:00. `is_storm` and `is_fault` are therefore **not** disjoint, by
design — the storm is a covariate, not a fourth fault — and a workshop should
discuss what a detector *ought* to report for those two hours rather than quietly
dropping them. `schedule.recurring_faults` deliberately does not avoid the storm:
a schedule that silently dodges it is one whose event count is not the number you
asked for.
