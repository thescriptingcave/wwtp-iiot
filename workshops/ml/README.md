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

| weeks | positives | in a 20% test fold | power? | readings generated |
|------:|----------:|-------------------:|:-------|------------------:|
| 3 | 22 | 4 | no | 12,694,439 |
| **8** | **62** | **12** | **no — 13x** | **33,843,069** |
| 25 | 183 | 37 | no — untested | 97,566,270 at 92.5% |

Eight weeks puts 12 positives in the test fold and **still is not enough** — the
split-noise ratio only falls from 42x to 13x. See below; it is the interesting
result rather than the disappointing one.

### What the long window costs, and three things I got wrong about it

Measured on 2026-10-01, by building it:

| | 3-week | 8-week (default) | 25-week |
|---|---|---|---|
| readings generated | 12,694,439 | **33,843,069** | 97,566,270 at 92.5% |
| rows per day | 142,654 | **604,341** | 557,522 at 92.5% |
| fault hours on the panel | 22 | **62** | ~183 |
| split noise / effect | 42x | **13x** | not built |
| database after the build | 854 MB | **1,040 MB** | 23 GB (interrupted) |

**The 23 GB I warned you about was an interrupted build.** `reading` carries
`drop_after: '7 days'`, which TimescaleDB evaluates against `now()` — so it trims
itself continuously, and a build that runs long enough for retention to keep up
stays small. The 25-week run hit 23 GB because I killed it at 92.5% with raw data
piling up faster than a once-daily retention job could delete it. A completed build
lands far lower. **Do not size a disk from an interrupted build.**

**"Superlinear growth" was a claim from two points, and the third point refutes it.**
Rows per day went 142,654 -> 604,341 -> 557,522. It rose 4.2x between 3 and 8 weeks
and then *fell* 8%. That is not a power law; it is a curve I only sampled three
times. The earlier claim that the growth is "superlinear and unexplained" was an
effort to sound rigorous about two measurements, and it named a shape the data does
not have.

**The panel does not depend on the part that gets deleted.** The builder reads
`reading_1h`, which carries no retention policy; `reading` is the 1-second table
that gets trimmed. After the build, `reading` held 5 days of 56 and `reading_1h`
held all 56, summing to 33,843,057 readings — exactly the panel. So the CSV stays
rebuildable, and **the CSV is the artefact worth keeping.**

Verified on the built panel: it sums to 33,843,057 of the 33,843,069 readings the
seeder reported, 12 short, which is the bucket boundary rather than lost data.

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

## Does the per-signal baseline actually work? Yes, and it took 419 positives

Notebook 03 says the feature is right and that the 3-week panel cannot show it. That
was tested rather than asserted, at three sample sizes:

| | 3 wk / 36 h | 8 wk / 36 h | 18 wk / 12 h |
|---|---|---|---|
| positives | 22 | 62 | **419** |
| in a 20% test fold | 4 | 12 | **84** |
| split noise / effect | **42x** | **13x** | **1x** |
| naive mean F1 | 0.345 | 0.416 | **0.463** |
| + per-signal baseline | 0.365 | 0.456 | **0.615** |
| difference | +0.020 | +0.040 | **+0.152** |
| the separation | 29.3x | 29.5x | **25.6x** |

**It works.** The difference is +0.152 against a within-configuration spread of
0.225: the effect is finally the same size as the noise, which is what having power
means. On the held-out final week it is **0.438 -> 0.579**, recall 0.292 -> 0.458.

The third column is what 18 weeks buys, and note **how** it was bought: recurrence,
not duration. 18 weeks at a 12 h fault rate is 19 GB and 46 minutes, where 54 weeks
at 36 h is 55 GB and does not fit on a laptop.

**Notebook 04's best number goes the other way.** The forward window falls 0.833 ->
0.684 and its spread across folds drops to 0.149, so most of its apparent
superiority was a small-sample artefact -- which is precisely what notebook 04 says
about it, and equally something it could not show.

### What the denser recurrence costs

Both measured, neither fatal:

- **The separation fell 29.3x -> 25.6x.** At 12 h a stuck sensor is often still
  broken from the previous fault, so it never gets to write and `base_24` has less
  contrast to work against. It holds.
- **`row_written` is no longer 0% for the silent faults** -- 5% for
  `effluent_tss_stuck` and 7% for `sensor_dead`, against 0% at 36 h. A preceding
  fault has ended and the signal writes again. The "no row at all" property -- the
  one this whole dataset exists to expose -- is being **eroded by the recurrence
  that bought the sample size.** Worth a paragraph in the room.

Reproduce it with:

```bash
make workshop-long WORKSHOP_LONG_WEEKS=18 WORKSHOP_HOURS=12
uv run --extra workshop python -m workshops.ml.measure_long_window \
  --panel workshops/ml/dataset.csv --panel workshops/ml/dataset-18wk.csv
```

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
