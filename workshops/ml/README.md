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

```bash
make workshop            # 3 weeks at 1 s -> wwtp_ml and workshops/ml/dataset.csv
```

That is the whole of it, and it is a make target rather than a pair of commands
because of how this repository loads its environment.

**Do not hand-write the seeder command.** This page used to say this — in a `bash`
fence, as a command to run, which is why it survived review for as long as it did:

```text
POSTGRES_DB=wwtp_ml tools/py.sh -m storage.seed.main --days 175 ... --reset
```

It is a `text` fence now rather than a `bash` one on purpose. The counter-example has
to be quotable without being runnable, and a fence language is the only thing in
Markdown that distinguishes the two. `tests/test_readme_teaches_no_destructive_command.py`
checks that no `bash` fence anywhere in the docs pairs `POSTGRES_DB=` with
`--reset`, which is the shape of the mistake; a plain grep could not tell this
warning from the advice it replaced.

and it was **wrong in a way that destroys data**. `tools/py.sh` sources
`tools/env.sh`, which sources `.env` *after* the inherited environment -- so the
`POSTGRES_DB=wwtp_ml` in front of the command is overwritten by whatever `.env`
says, which is `wwtp`. That command does not seed the workshop database. It seeds
**the plant's database, with `--reset`.**

`make workshop-seed` passes `--database $(WORKSHOP_DB)` as an *argument* for exactly
this reason: an argument cannot be overridden from the environment. The seeder also
refuses `--reset` without an explicit `--database`, so the mistake fails instead of
wiping. See `storage/seed/main.py::_target_database`.

`WORKSHOP_DSN` is not required either. `make workshop-dataset` derives the
connection string from `.env` the way everything else here does; the old
`WORKSHOP_DSN="host=... port=55433 ..."` was a hand-transcription of information the
Makefile already had, and it is wrong on any machine whose `.env` port is not 55433
-- which is every machine that has not been configured yet.

The three `WORKSHOP_*` values **must match between the seed and the builder**,
because the labels are derived from them rather than read from the data. A mismatch
does not fail: it produces a table whose labels are confidently wrong, which is why
the builder takes `--days`/`--end`/`--every-hours` as arguments rather than
inferring them, and why the defaults live in the Makefile rather than in two places.

### The long window

```bash
make workshop-long                          # 8 weeks -> wwtp_ml25, dataset-8wk.csv
make workshop-long WORKSHOP_LONG_WEEKS=25   # 25 weeks -> dataset-25wk.csv
```

**Eight weeks is the default, not 25, and the reason is arithmetic.** Notebook 03
needs about **10 positives in the test fold** before a recall figure means anything.
The 3-week panel's 22 positives put 4 there. The fault schedule is in hours, so
positives scale with duration at 7.3 a week:

| weeks | positives | in a 20% test fold | power? | disk (bounded) |
|------:|----------:|-------------------:|:-------|---------------:|
| 3 | 22 | 4 | no | 0.7-1.9 GB |
| **8** | **59** | **12** | **yes** | **1.9-8.2 GB** |
| 25 | 183 | 37 | yes | 6.1-25.7 GB |

Eight weeks clears the threshold. Twenty-five puts 37 positives there — three times
what is needed, for up to 25.7 GB.

### What 25 weeks actually costs, measured

Run on 2026-10-01 and stopped at 92.5%, because the disk ran short:

| | projected | measured |
|---|---|---|
| rows | ~25 M | **97.5 M at 92.5%** |
| disk | 6.1 GB | **23 GB at 92.5%** |
| time | ~17 min | **66 minutes to 92.5%** |

Three things about that, all of which cost an hour.

**The projection was wrong by 4x.** Extrapolating from the 3-week database's 731 MB
predicted 6.1 GB; it is at least 23 GB. Extrapolating from one window's row count to
another window's is not arithmetic — and the error is multiplicative in the sampling
rate, which is exactly the knob you would turn to make it cheaper.

**The growth is superlinear and unexplained.** Rows per day went from **142,654 to
602,726** — 4.2x *more* per day on the long window, so 25 weeks stored 4.2x more rows
per day than 8.3x its duration should give. Two measured points only *bracket* the
truth for any intermediate window, which is why the disk column above is a range and
not a number. Why it is superlinear is not explained here, only recorded.

An earlier draft of this file said rows per day *fell* from 998 k to 557 k. It rose,
from 142,654 to 602,726 — wrong in the direction and wrong in both numbers. The
measured values are above, and a test now holds them, because a figure that is right
in shape and wrong in sign is the hardest kind to notice: the sentence still reads
sensibly.

**116 was the wrong unit.** An earlier version of this table said 25 weeks gives "~116
fault hours". 116 is the number of *recurrences* — 4,200 hours at one every 36. Like
for like against the panel's 22 fault *hours*, the answer is 183.

If you need the long window at a coarser `--sample-interval`, the fault *count* is
unaffected, because the schedule is in hours. What changes is `n`, and notebook 03's
29x separation is measured in `n` — at 60 s sampling a quiet signal can write zero
rows in 24 h, which collapses the very baseline the notebook is about. Coarser
sampling buys the fault count and costs the mechanism.

It writes to its **own database and its own CSV**. That is not tidiness: the
`workshop-dataset` recipe originally hardcoded `--out workshops/ml/dataset.csv`, so
`WORKSHOP_WEEKS=25 make workshop` worked and silently replaced the 3-week panel that
every number in `TRAINER.md` and all six notebooks were measured on. Nothing failed.
The output path is now `$(WORKSHOP_PANEL)` and `tests/test_workshop_panel_paths.py`
keeps it that way.

To check what the extra data actually buys:

```bash
uv run --extra workshop python -m workshops.ml.measure_long_window
```

It runs notebook 03's experiment on both panels and prints the one number that
decides whether the comparison means anything: the split-to-split spread within a
single configuration, divided by the difference between two configurations. See
`TRAINER.md` for the measured result.

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

## The six notebooks, and the number at each

Full detail and the failure modes in `TRAINER.md`.

| # | Notebook | The number the room should be holding |
|---|---|---|
| 1 | `01-the-metric-turn` | accuracy **0.9993** for a model that learned nothing; the real forest scores **0.9995** |
| 2 | `02-the-dense-panel` | a query of the stored table keeps **10 of 22** fault hours — two of three classes gone |
| 3 | `03-the-baseline-and-the-noise-floor` | the split-to-split spread is **42×** the difference between two feature sets |
| 4 | `04-the-forward-window` | F1 **0.444 → 0.833**, and the 0.833 needs tomorrow |
| 5 | `05-unsupervised` | **0 of 22** fault hours; 285 of 288 flags are one tag |
| 6 | `06-predictive` | a random split makes the model **5.3×** better, and it is worth nothing |

**Accuracy moves from 0.9993 to 0.833 across all six and is never once the point.**

Three of these are not what the plan said, and the corrections are the useful part:
the "seasonal naive beats every model" claim is false, the per-signal baseline cannot
be shown to help on 22 positives, and unsupervised does not find the storm — it finds
the odd *tag*.

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
