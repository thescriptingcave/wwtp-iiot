# Trainer's guide

Read this before you teach. Not the notebooks — this.

The plan is at `~/.opencode/plan/ml-workshop.md` and it is the authority on *why*.
This is the authority on *what happens in the room*, and it exists because **three of
the plan's four headline moments were false when first written** and a fourth was
half a cheat. All four were caught by running them (§4.5, §4.6). Improvise the arc
from the plan and you will teach the room something false, confidently, and they will
go away believing it.

Every number below was measured. None of them is from the plan.

---

## The setup that has to work before anyone arrives

```bash
make setup
make workshop-seed WORKSHOP_DSN='host=127.0.0.1 port=55433 user=wwtp password=... dbname=wwtp_ml'
make workshop-dataset WORKSHOP_DSN='...'
```

Three weeks at 1 s is the default and it is enough: 12.7 M readings, under two
minutes, and it produces every number in this guide. Do **not** use 25 weeks on a
laptop projector. The three `WORKSHOP_*` values must match between the two targets
or the labels are wrong and nothing fails.

The dataset is a **CSV, not a database.** Participants need no Postgres, no
`.env`, and no `docker`. That is deliberate: every support conversation you have
during a modelling workshop should be about the modelling.

---

## The arc, and the number at each step

**Re-measured 2026-10-01, and two of these numbers did not survive.** The plan's
version of beats 3 and 6 was wrong in both directions, and the corrections are the
interesting part. Everything below is what the notebooks now print.

| # | Notebook | The number the room should be holding |
|---|---|---|
| 1 | `01-the-metric-turn` | accuracy **0.9993** for a model that learned nothing; the real forest scores **0.9995** |
| 2 | `02-the-dense-panel` | a query of the stored table keeps **10 of 22** fault hours — two of three classes gone |
| 3 | `03-the-baseline-and-the-noise-floor` | the split-to-split spread is **42×** the difference between two feature sets |
| 4 | `04-the-forward-window` | F1 **0.444 → 0.833**, and the 0.833 needs tomorrow |
| 5 | `05-unsupervised` | **0 of 22** fault hours; 285 of 288 flags are one tag |
| 6 | `06-predictive` | a random split makes the model **5.3×** better, and it is worth nothing |

**Accuracy moves from 0.9993 to 0.833 across six notebooks and is never once the
point.** That is the thesis of the two days.

## Beat 1 — the metric turn (tier A, 45 min)

Load the CSV. `DummyClassifier(strategy="most_frequent")`. Print accuracy. It is
**0.9995**.

Now print the base rate: 22 positives in 28,728 rows. Ask the room what a model has
to do to beat that. The answer is *nothing*, and a `LogisticRegression` on the
imputed features gets **recall 0.000** at accuracy 0.9995 — it is strictly worse
than doing nothing, and its accuracy is identical.

**The moment:** a participant who has been told "pick a metric before you pick a
model" realises they could have picked accuracy, done nothing, and passed review.

**Do not** let them fix it by switching to F1 alone. Make them report precision and
recall too, because the next beat needs a model that is *precise and wrong* — 1.000
precision, 0.333 recall — which is the shape of a real detector in a plant and the
shape they will ship if nobody makes them look.

## Beat 2 — the dense panel (tier A, 45 min)

This is the one that is not in the original plan and it is the biggest lesson.

Query the database the way a participant naturally would, and show them that
`reading_1h` has **no row at all** inside the window of `effluent_tss_stuck` and
`sensor_dead`. Measured: 0 rows, 0 rows, 2 rows.

Then ask why. The historian writes **on change**. A frozen instrument never changes,
so nothing is written, so the evidence is an *absence* — and a table of stored rows
has thrown it away.

Make them cross-join signals against hours and left-join. 28,728 rows. All 22
positives present. 11,635 hours (**40.5%**) have no row at all.

**Then the number that reframes the whole dataset:** of those 11,635 empty hours,
**12 are fault hours — 0.1%.** Absence, on its own, finds nothing.

And the one to end on: **41% of ordinary hours are in exactly the state a broken
instrument is in.** So is a frozen one and a dead one — the same state as each
other. The room has been staring at the answer and it does not work.

## Beat 3 — the feature is right and the experiment cannot prove it (tier B, 60 min)

A fault hour's signal logged **596** rows in the previous 24 h; a quiet hour's
logged **20**. Twenty-nine times, and it is the only thing that separates the two
cases notebook 02 could not.

Then run the model, and it gets **worse**: F1 0.600 → 0.444.

**Do not explain this away.** Run the same model, same features, twenty times,
changing only the split. F1 ranges **0.000 to 0.857** for the naive features and
**0.000 to 0.750** with the baseline. The two means differ by 0.020. The variation
from which hours you tested on is **42× the thing being measured**.

The panel has 22 positives; a 20% test fold has about 4. You want at least 10 to
estimate a recall at all. **This experiment has no power, and no amount of modelling
fixes that — it is a property of the sample.**

**The moment:** someone will want to keep tuning until one split looks good. That is
how you get a feature that scores 0.800 on the fold you tried and 0.000 on the next,
and a number in a slide nobody can reproduce.

## Beat 4 — the best score is the one you cannot use (tier B, 30 min)

Add a 24-hour window looking *forward*. F1 **0.444 → 0.833**, precision 1.000, **zero
false alarms in 9,569 ordinary hours**, and the stuck sensor goes from 0 of 2 to 2
of 2.

It works because every fault hour has future hours where the instrument is writing
again, and a quiet hour usually does not — so the feature is asking "does this
instrument recover?", and for a sustained fault the answer is no.

Then ask the room to deploy it. At 03:00, deciding whether to raise an alarm,
tomorrow's row count does not exist.

**The best number anyone will see all day, and it is unusable.** That is the beat.

## ## Beat 5 — unsupervised, and it is not what the plan said (tier B/C, 45 min)

The plan said unsupervised methods confidently find the storm rather than the
faults. **They find neither.** On the 3-week panel, with no labels used to fit:

| Method | flagged | fault hours hit | storm hours hit |
|---|---|---|---|
| `IsolationForest` 0.1% | 29 | **0** of 22 | 0 of 114 |
| `IsolationForest` 1% | 288 | **0** of 22 | 2 of 114 |
| PCA(3) reconstruction, top 1% | 288 | 1 of 22 | 1 of 114 |

They find **`INFLUENT:LIFT:STARTS` — 285 of its 504 hours, and 285 of the 288
flags.** Median `n` of a flagged hour is 226 against 2 for everything else. It is
flagging the tag that reports on nearly every scan, because it is unlike the other
56.

**The corrected lesson is better than the false one:** an unsupervised method here
does *cross-sectional* work — "this tag is unlike the pack" — while a fault is a
*within-signal* deviation, and nothing in the table separates them, because the
per-signal baseline correctly reports that a fault hour looks exactly like that
signal's normal self. **Anomaly detection finds the odd tag out, not the odd hour.**
Same insight as beat 3, from the other direction.

## Beat 6 — a random split makes a good model out of a clock (tier C, 45 min)

Predict effluent TSS an hour ahead, held-out final fifth:

| Model | MAE |
|---|---|
| `LinearRegression` on hour-of-day | 2.3475 |
| "same hour, previous 3 days" — three lines of arithmetic | 0.4367 |
| `RandomForest` on the panel's own features | 0.3399 |
| **`RandomForest` on hour-of-day** | **0.3304** |
| the same model, **random split** | **0.0627** |

Three findings, and the plan got two of them wrong.

**A linear model on hour-of-day is the worst row in the table** — worse than
guessing the mean. Hour-of-day is a circle: 23:00 and 00:00 are adjacent, and a
straight line through them puts the worst prediction at midday. A tree sees the
wrap; a regression cannot.

**"A seasonal naive beats every model" is false.** A tree on hour-of-day beats it,
0.330 against 0.437. It would have been a satisfying thing to believe and it is not
true.

**90.0% of the variance is hour-of-day**, and the baseline is already within 9.8% of
a standard deviation. A `Conv1d` or `GRU` would be a ~200 MB dependency learning the
rest of a small band. **That is the argument against buying a GPU, made of
measurements rather than of principle.**

And the one that matters: **the random split is 5.3× better, and the improvement is
entirely an illusion.** With a random split the hour being predicted has neighbours
from the same day in the training set. It is not forecasting, it is interpolating
between two values it has already seen.

## Things that will go wrong

**Someone will `dropna()` and get a clean dataset and a worse model.** Let them.
It is beat 2, and `dropna` is the same mistake `notebooks/02` makes at the SQL
level. The empty hours are not missing data; they are the measurement.

**Someone will reach for `TimeSeriesSplit` as the fix for the leakage.** It is not.
The leakage in this dataset is not temporal overlap, it is *the label is 0.081% of
rows* and *the features are all but the answer*. A perfect split fixes neither.

**Someone will use accuracy because it is the easiest.** They all will, at least
once. That is beat 1 and it is not a failure of teaching.

**The room will want the storm.** It is one 2-hour event in 3 weeks and it is the
largest single anomaly in the data, so the instinct is to study it. It is a
covariate. `is_storm` exists so they can find that out by looking rather than by
being told.

---

## What has *not* been validated

Say this if you are asked, because it is true:

- **Beat 3's remedy was tested, and it works.** The 18-week panel at a 12 h
  recurrence gives **419 positives**, and the experiment finally has power:

  | | 3 wk / 36 h | 8 wk / 36 h | 18 wk / 12 h |
  |---|---|---|---|
  | positives | 22 | 62 | **419** |
  | in a 20% test fold | 4 | 12 | **84** |
  | split noise / effect | 42x | 13x | **1x** |
  | naive mean F1 | 0.345 | 0.416 | **0.463** |
  | + per-signal baseline | 0.365 | 0.456 | **0.615** |
  | difference | +0.020 | +0.040 | **+0.152** |
  | the separation | 29.3x | 29.5x | **25.6x** |

  **The per-signal baseline does help.** The difference is +0.152 against a
  within-configuration spread of 0.225 -- the effect is the same size as the noise,
  which is what having power means. On the held-out final week it is 0.438 -> 0.579,
  recall 0.292 -> 0.458. Notebook 03 was right, and it could not prove it.

  **And notebook 04's best number collapses.** The forward window went 0.833 -> 0.684,
  and its spread across folds fell to 0.149. Most of its apparent superiority was a
  small-sample artefact, which is exactly what notebook 04 says about it -- it just
  could not show that either.

  Two things the trainer should know changed:

  - **The mechanism weakened slightly**: the separation fell 29.3x -> 25.6x. A 12 h
    recurrence means a stuck sensor is often *still* broken from the previous fault,
    so it never gets to write and `base_24` has less contrast to work with. It holds.
  - **`row_written` is no longer 0% for the silent faults** -- 5% for
    `effluent_tss_stuck` and 7% for `sensor_dead`, against 0% at 36 h. Denser
    recurrence means a preceding fault has ended, so the signal writes again. The
    "no row at all" property is being eroded by the recurrence that bought the
    sample size.

- **No sequence model was fitted, and that is the recommendation.** Notebook 06 shows
  90% of the variance is the clock and a tree captures it. A `Conv1d` or `GRU` here
  would be bought to lose, and the honest test is a room building one anyway and
  watching it.
- **One fault and the storm genuinely coincide** (a drift on `AERATION:AHU-1:DO`,
  2026-09-27 12:00-13:00, inside the storm). `is_fault` and `is_storm` are therefore
  not disjoint, and what a detector *ought* to report for those two hours is an open
  question worth handing a room.
- **A recurring storm does not exist yet**, so the final week of a 25-week window is
  contaminated by the single storm. Hold out a week from the *middle*.
- **The classification beats are measured on 7 positive hours in the test fold.** Beat
  4's 0.833 is 5 of 7. Notebook 03 explains why that number should not be trusted
  even though it is the highest in the set, and the room should be told so before
  they see it, not after.
