# Clean-machine test plan

A top-to-bottom run of everything in this repository, on a machine that has never
seen it. Execute the steps in order. Each step says what to run, what you should
see, and what to do when it fails.

**Fill this in as you go.** A column for the result is more useful than a note at
the end, because the first thing that fails is usually the first thing after a
step you rushed.

| step | result | notes |
|---|---|---|
| 1 | | |
| 2 | | |
| ... | | |

## Before you start

You need:

- macOS or Linux, Docker Desktop or Docker Engine + Compose v2
- `git`, `make`, and a C compiler (TimescaleDB needs one)
- about **60 GB free** — the plan's heaviest step needs ~19 GB, and Docker's image
  does not shrink when you delete files inside it
- 90 minutes to two hours

Check the prerequisites:

**Step 0** — verify the toolchain.

```bash
git --version
docker --version
docker compose version
make --version
cc --version
```

Expect: git 2.x, docker 24+ with a Compose v2 plugin, GNU make 3.81+, and a C
compiler. **If `cc` is missing the Postgres image will fail to build**, and the
error names a missing shared library rather than a missing compiler.

---

## Phase 1 — Clone and reach a running plant

**Step 1** — clone.

```bash
git clone <this-repo-url> wwtp-iiot
cd wwtp-iiot
```

**Step 2** — confirm you have the expected starting point.

```bash
git log --oneline -1
git status --short
ls Makefile compose.yaml .env.example
```

Expect: one commit line, **no output** from `git status --short` (a clean tree),
and the four files present. Anything in `git status` means you have local edits
before you start, which will make every later difference ambiguous.

**Step 3** — bring up the plant. This is the whole setup.

```bash
make up
```

Expect: about **2m 40s** on a laptop, most of it the seed, ending with readings
landing. It writes a `.env` for you if you do not have one.

**Fail here?** The most common causes, in order:

| symptom | cause | fix |
|---|---|---|
| `error: no project Python` | never installed | `make sync` |
| connection refused on port 5432 | `.env` was never written | `rm .env && make up` |
| `Exec format error` | wrong architecture image | `docker compose build --no-cache` |
| build fails on a C library | no compiler | install one, `make up` again |

**Step 4** — confirm data is actually arriving.

```bash
make query SQL="SELECT count(*) FROM reading"
```

Expect: about **4.2 M**. Far fewer means the scan loop is not running; far more
means the retention policy has not trimmed yet, which is harmless.

**Step 5** — confirm the three protocols answer.

```bash
docker compose ps
```

Expect: `db`, `init-db`, `softplc` and `gateway` all **running**. A service in
`Exited` is the failure — read its name and check `docker compose logs <name>`.

**Step 6** — browse the OPC UA address space.

```bash
uv run python tools/opcua_browser.py browse
```

Expect: a tree with **8 areas, 22 pieces of equipment, 57 signals** — 138 leaves in
total. `browse` prints **structure, no values**. If you see numbers, something is
wrong; that was a documented lie once.

**Step 7** — read and watch one tag.

```bash
uv run python tools/opcua_browser.py read AERATION:AHU-1:DO
uv run python tools/opcua_browser.py watch AERATION:AHU-1:DO
```

Expect: a value, a status, metadata, and then updating values. The dotted spelling
`AERATION.AHU-1.do_mg_l` must reach the same signal.

**Step 8** — run one SQL query by hand.

```bash
make query SQL="SELECT signal_id, count(*) FROM reading_1h GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
```

Expect: five rows, with one busy signal at the top.

---

## Phase 2 — The gates

Run them in this order. Each depends on the one before it, and a failure early on
makes later failures noise.

**Step 9** — lint and types.

```bash
make lint-debt
make types
```

Expect: **155 findings, baseline 155** and **Success: no issues found in 73 source
files**. A count other than 156 means the baseline drifted — the ratchet is a real
check, not decoration.

**Step 10** — unit tests.

```bash
make test
```

Expect: **1152 passed**, no failures. The documented count is checked by a test, so
a different number means either a missing file or a genuine change in count.

**Step 11** — integration tests.

```bash
make integration
```

Expect: **48 passed**. These need the database from step 3 and refuse to truncate a
seeded one.

**Step 12** — the SQL course.

```bash
make sql
```

Expect: **78 queries in 26 files: 78 ok, 0 failed, 0 empty**.

**Step 13** — the OPC UA course.

```bash
make lessons
```

Expect: **87/87 snippets ran**, 22 skipped. The gate starts its own server, so this
works even if the plant is down.

**Step 14** — the analyst notebooks. First run seeds its own seven-day database.

```bash
make notebooks
```

Expect: **11 notebooks, built, executed, outputs and prose numbers agree** after
about **8 minutes**. Every **bold** number in the prose is checked against the run.

**Step 15** — everything at once, as CI runs it.

```bash
make check
```

Expect: **all gates green**. If steps 9–14 each passed, this must too.

**Step 16** — the drift gate.

```bash
make contract
```

Expect: one line per signal, and **no disagreement** between the contract and the
consumers. This is the cheapest gate in the file and the one most likely to catch a
real bug.

---

## Phase 3 — The ML workshop

Needs no database and no Docker beyond what step 3 gave you.

**Step 17** — build the workshop dataset.

```bash
make workshop
```

Expect: **28,728 rows = 57 signals × 504 hours**, **22 positives**, and a
`workshops/ml/dataset.csv` of about **3.3 MB**. Under two minutes.

**Step 18** — the workshop gate.

```bash
make workshop-notebooks
```

Expect: **6 notebooks, built, executed, outputs and prose numbers agree**.

**Step 19** — open it, as a participant would.

```bash
make workshop-url
make workshop-open
```

Expect: a URL on **port 8898** and a browser opening. Then, in the notebook:

- every cell runs top to bottom with no errors
- `workshops/ml/dataset.csv` is visible in the file tree

**Fail here?** If the notebooks cannot find the panel, run `make workshop-has-data`.

**Step 20** — check the claim notebook 03 makes, against the panel yourself.

```bash
uv run --extra workshop python -m workshops.ml.measure_long_window
```

Expect: **42x**, and the verdict **`NO POWER`**. That is correct for 22 positives.
If it prints anything else, the panel is not the one the notebooks were written
against.

---

## Phase 4 — Optional: the heavy build

Only if you have the disk and the time. Everything above is complete without it.

**Step 21** — build the longer window that answers notebook 03's question.

```bash
df -h .
make workshop-long WORKSHOP_LONG_WEEKS=18 WORKSHOP_HOURS=12
```

Expect: **46 minutes**, about **19 GB**, ending with **172,368 rows = 57 signals ×
3,024 hours** and **419 positives**. Read the free-space figure **before** you
start; this is the only step that can fill a disk.

**Step 22** — measure it.

```bash
uv run --extra workshop python -m workshops.ml.measure_long_window \
  --panel workshops/ml/dataset.csv --panel workshops/ml/dataset-18wk.csv
```

Expect: **1x** and the verdict **`POWER`**, with the per-signal baseline at mean F1
**0.615** against **0.463** naive. This is the finding the whole workshop exists
for.

**Step 23** — reclaim the space.

```bash
docker compose exec -T db psql -U wwtp -d postgres -c "DROP DATABASE IF EXISTS wwtp_ml25"
docker builder prune -af
df -h .
```

Expect: the free space to come back. **If it does not**, Docker Desktop → Settings →
Resources → **Reclaim disk space**; on macOS the Postgres volume lives inside a
sparse image that does not shrink on its own.

Keep `workshops/ml/dataset-18wk.csv` if you want to repeat step 22 — it is 19.6 MB
and the database is scaffolding. `reading` is trimmed to seven days by retention
anyway; the panel is rebuilt from `reading_1h`, which is not.

---

## Phase 5 — Tear down and prove it is clean

The point of a clean-machine test is to prove the setup is reproducible, which
means proving the *absence* of state is also reproducible.

**Step 24** — stop everything and delete the volumes.

```bash
make down
docker volume ls | grep wwtp
```

Expect: **no wwtp volumes**. This deletes the seeded week and the pinned notebooks
database — that is what it is for.

**Step 25** — start again from nothing.

```bash
make up
make query SQL="SELECT count(*) FROM reading"
```

Expect: **~4.2 M readings** again, in about 2m 40s. If this works, the setup is
reproducible. If it does not, something survived `make down` and you have found a
real bug.

**Step 26** — confirm the working tree is still clean.

```bash
git status --short
```

Expect: **no output**. A dirty tree after a full run means something wrote into the
repository. The known-benign exceptions are `.env` (gitignored) and the panel CSVs
(gitignored).

**Step 27** — one final gate pass, from the rebuilt plant.

```bash
make check
make notebooks
make workshop-notebooks
```

Expect: all green, **11** analyst notebooks and **6** workshop notebooks. This is
the real end-to-end result: everything works after a full teardown and rebuild.

---

## What "pass" means

The run is a pass when:

1. steps 1–20 pass on a machine that started with nothing;
2. step 25 reproduces the plant after `make down`;
3. step 27 is green;
4. `git status --short` is empty at step 26.

**A run where every gate passes but step 26 is dirty has still found a bug.**

## If something fails

Record, in this order:

1. the step number and the **exact command**
2. the **exact error text**, not a paraphrase — most of the traps in this repository
   produce an error that names the wrong thing
3. `git log --oneline -1` and `docker compose ps`
4. whether it passed on a second machine and not this one

Then read `docs/GETTING-STARTED.md` § Troubleshooting before filing anything. A
large share of clean-machine failures are a missing compiler, a stale `.env`, or a
port already in use, and all three are answered there.

**Do not "fix" a gate to make it pass.** If a count differs from the documented one,
that is the finding.