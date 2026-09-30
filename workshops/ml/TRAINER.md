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

Everything below is the 3-week panel: 28,728 rows, 57 signals × 504 hours, **22
positive hours, base rate 0.0766%**.

| # | Beat | The number the room should be holding |
|---|---|---|
| 1 | The metric turn | accuracy **0.9995** for a model that learned nothing |
| 2 | The dense panel | **2 of 3** fault kinds have no row in the stored table |
| 3 | The per-signal baseline | F1 **0.500 → 0.800** |
| 4 | The forward window | F1 **1.000**, and it cannot ship |
| 5 | Unsupervised | **0 of 22** fault hours found; 285 of 288 flags are one tag |

**Accuracy never moves.** 0.9995, 0.9997, 0.9998, 1.0000 across the whole session.
Say this out loud at the end. It is the thesis.

---

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

## Beat 3 — the per-signal baseline (tier B, 60 min)

A fault hour's signal logged **326** rows in the preceding 24 h. A quiet ordinary
hour's signal logged **20**. Same panel, same absence, opposite conclusion — the
difference is the signal's own history.

Give them a trailing per-signal median of `n`. F1 **0.500 → 0.800**, precision
still 1.000. Accuracy **0.9998**. Nobody can tell from the accuracy that anything
happened.

**This is where the session turns**, and the thing to say is not "we improved the
model". It is: *the information was in the table the whole time and no model found
it, because nobody gave the model a way to compare a signal to itself.*

Optional, if the room is quick: `CONDUCTIVITY` writes **356 rows in 21 days**
against a 5-second `sample_ms` that would give 360,000. It is near-constant, so a
change-triggered historian almost never records it. **Any tag can be a feature;
only a dense one can be a target.** 22 of 57 tags are under 50% covered.

## Beat 4 — the forward window (tier B, 30 min)

Give them a *leading* 24-hour baseline as well. F1 goes to **1.000**. Recall
**1.000**. Precision **1.000**.

Then ask them to deploy it.

The detector reads tomorrow's row count to decide whether the instrument broke
today. It cannot run on live data. **The score went up and the model got worse**,
and it is the best number anyone saw all day.

Do not reveal that the prototype reported this 1.000 as its headline before anyone
checked. It did, and the plan was built on it. If a participant asks where the
number came from, that is the honest answer and it usually lands harder than the
lesson.

## Beat 5 — unsupervised, and it is not what the plan said (tier B/C, 45 min)

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

## Beat 6 — predictive, and do not buy a GPU (tier C, 45 min)

Predict effluent TSS an hour ahead, held-out fifth:

| Model | MAE |
|---|---|
| **"same hour, previous 3 days"** — three lines of arithmetic | **0.2527** |
| `RandomForestRegressor` on hour-of-day, **random** split | 0.1711 |
| `RandomForestRegressor` on hour-of-day, time split | 0.3325 |
| `RandomForestRegressor` on per-signal features, time split | 0.5171 |
| `LinearRegression`, either way | 2.30 – 3.35 |

**A seasonal naive beats every model.** And a random split nearly halves the error
of the best model (0.1711 → 0.3325) — the same trap `notebooks/08` measures at
61.7 → 494.1 m³/h, reproduced at the modelling level.

**There is nothing left for a sequence model.** 90.0% of the variance is explained
by hour-of-day alone, autocorrelation at lag 24 is **+0.029**, and the sd is 3.63
against an MAE of 0.25. A `Conv1d` or `GRU` would be a ~200 MB dependency to lose
to three lines of arithmetic.

**If someone wants the sequence model anyway, let them build it and let it lose.**
That is the exercise. Do not put `torch` in the environment for the room.

---

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

- **Tiers B and C beyond these five beats are unwritten.** Every number in this
  guide is measured; nothing beyond beat 6 has been run end to end.
- **The 25-week window has never been built in the repo's own compose setup** —
  only in a scratch instance. The `WORKSHOP_*` defaults are 3 weeks for that reason.
- **One fault and the storm genuinely coincide** (a drift on `AERATION:AHU-1:DO`,
  2026-09-27 12:00–13:00, inside the storm). `is_fault` and `is_storm` are
  therefore not disjoint. What a detector *ought* to report for those two hours is
  an open question, and a good one to hand a room.
- **A recurring storm does not exist yet**, so the final week of a 25-week window is
  contaminated by the single storm. Hold out a week from the *middle*.
