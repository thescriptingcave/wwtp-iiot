# CI

`.github/workflows/gates.yml`. **Seven jobs.** This document says what each one is
for, what it deliberately does **not** cover, and why the exclusions are
exclusions rather than oversights.

It said "five jobs" until a test caught it: `lint-debt` was added after this
sentence was written, which is the ordinary way a number goes stale — the change
was real, the prose was simply not revisited. It then said "six" until
`notebooks` was added. The test now reads the job count out of the workflow
itself rather than naming it, so there is no number here for the next one to
catch.

`make check` runs the fast gates locally in the order that fails fastest. The
workflow is the thin YAML that runs them on a machine that is not mine.

## Why it exists, in one sentence

Until this file existed, every claim in every commit message in this repository
was something I verified by hand, on one machine, at one moment — and a gate
that runs when I remember is a gate that is *sometimes* true, which is worse
than no gate because it is trusted.

## What it cost to write

Writing the file found three things that were broken and that I had been
reporting as fine.

### `make types` had been failing since Phase 2

The Makefile ran

    mypy softplc gateway storage alarms tools

and every mypy invocation I actually ran, in four phases, was over a *subset* of
that list. Running it over the whole thing found two errors in
`tools/opcua_browser.py`: a `try_read` with an unannotated coroutine, and
`main` returning `Any` from a function that declares `int`. Both are now fixed
and the gate covers every Python file in the project.

### `make lint` had been failing the whole time

Worse, and the same mistake. `ruff check .` reported **159 findings** when this
was written; it is **157** now — a `ruff --fix` pass over
`softplc/servers/opcua.py` that took that file from 27 findings to 6, and the
notebook work clearing the sixteen findings it had added.
Almost all of them are `E501` (long lines) and `PLC0415` (function-local imports)
in `softplc/process/units.py`, `softplc/servers/opcua.py` and the Phase 1–2 test
files — debt this project has tracked openly as an open thread since Phase 1
rather than sweeping into a commit claiming to be about something else.

So the ruff gate is now **scoped to the packages that are actually clean**
(`alarms scada ui storage gateway softplc/scanloop.py`) and the debt is measured
separately by a ratchet. The scoping is on the `make help` label, in the
Makefile comment, and in the workflow comment, because a gate that is reported
as passing because it was run over the wrong subset is worse than no gate — and
this project has now produced that mistake twice in one day.

The ratchet (`make lint-debt`, and the `lint-debt` job) fails if the finding
count goes **up** and does not fail if it goes down. A hard gate at 157 would
block every commit, and deleting the debt in one sweeping commit is the thing
this project has deliberately not done five times. The baseline lives in
`lint-debt-baseline.txt`, so lowering it is a deliberate act with a diff that
says so.

### The Node-RED service could not start at all

The `scada` compose service mounted the generated flows read-only at `/data` and
a named volume for the credential at `/data/credentials`. **The credential
volume was nested inside the read-only mount**, so Docker refused:

    mkdirat .../data/credentials: read-only file system

and the container never started. A `run` from the workflow found it. That is the
fourth compose file in this project that has shipped broken, and all four were
found by running `docker compose up` rather than by reading the YAML.

The fix and the reasoning are in `compose.yaml`; the short version is that the
flows are now mounted at `/flows:ro` and the entrypoint assembles `/data/flows.json`
from them, because a named volume over `/data` silently shadows the image's
`settings.js`. See `scada/README.md`.

## The jobs

| Job | Runs | What it is for |
|---|---|---|
| `unit` | every push | ruff (scoped), mypy (everything), pytest without a database, **the lesson courses** |
| `integration` | every push | a real seeded TimescaleDB, the integration suite, the SQL course, the flow SQL, the dashboard queries, the replay |
| `notebooks` | every push | the analysts' database, the eleven notebooks, and every number in their prose |
| `drift` | every push | generated files vs the contract, and `docker compose config` |
| `images` | every push | the three images build, and the two non-core node types resolve |
| `lint-debt` | every push | 157 findings is the baseline; going up fails |
| `nightly` | 04:17 UTC | the fault × rule coverage matrix and the four slow tests |

That is six on every push and one on a schedule. The count is checked by
`tests/test_readme_claims.py::test_no_document_miscounts_the_ci_workflow_jobs`,
which reads it out of `gates.yml` rather than against a number written down here
— and which found the wrong number in *this file* the day it was written, then
found it wrong a second time when the `notebooks` job was added.

### Why the lesson courses run in the `unit` job

`tools/check_lessons.py` runs every Python block in `courses/` against a live OPC
UA server that **it starts itself**, on an ephemeral port. It needs no database,
no seeded week and no compose, so it runs in the `unit` job rather than the
`integration` one.

That placement is the design, not a convenience. A lesson gate that needed a
seeded fortnight to demonstrate a browse would be a gate that ran a fortnight and
then did not run. The gate is stricter than `check_sql.py` for the same reason it
can be: a SQL block that returns no rows is indistinguishable from a correct one,
whereas an OPC UA snippet either connects to a server or it does not.

It is also the gate that keeps the course honest. Writing lesson 01 surfaced a
reference id (`Organizes` where the answer was `HasComponent`) that had been
guessed rather than observed, and a precedence bug in a snippet
(`await f()[:4]`, which slices a coroutine). Neither was findable by reading the
markdown.

### Why the database tests run in CI

The obvious cheaper choice is to skip them, which is what most projects do and
what this one did implicitly by not having CI at all. Those are the tests that
found the continuous aggregates that were never refreshed, and the `SET LOCAL
ROLE` that was a no-op. Skipping them would mean the least useful half of the
suite was the half nobody ran.

The `integration` job therefore seeds **a real week** — about two minutes —
because every query against an empty database returns nothing, and nothing
returning nothing is a pass.

### Why the notebooks get a database of their own

The eleven notebooks in `notebooks/` state numbers — `29.36 %`, `4 239 284
readings` — and a gate checks each one against a real run. That only works if the
rows are the same rows every time, and `wwtp` cannot promise that: it is seeded
relative to *now*, so every re-seed slides every timestamp. The SQL course
depends on exactly that, in 28 places, so pinning it would break the course.

So the `notebooks` job seeds a second database, `wwtp_notebooks`, at a fixed
instant, and the job's two steps are that seeding and then
`python -m tools.check_notebooks`. This is **not** redundancy, and the comment in
the workflow says so: the two audiences need opposite properties from their data
and cannot share it.

The gate is four checks per notebook — the generated `.ipynb` is in step with its
`src/*.md`, the notebook runs top to bottom, every `output` block matches what it
printed, and the seed fingerprint is the pinned one. The third is the one that
matters, and it is the SQL course's rule applied to notebooks: *a lesson that
states a number must state the number the database actually produced.* A cell's
`outputs` array is a record of the last run; an `output` block in markdown is a
claim, which is why the notebooks are generated from markdown rather than
authored as `.ipynb`.

A fifth rule scans every source once and is **not** one of the four, because it
cannot be checked by running anything: all four compare a number against a run on
the machine doing the running, so a number derived from the disk or the clock
passes them and is still wrong on the runner. `hypertable_size()` did exactly
that, in notebook 03's cost table, and CI is how it was found. `NOT_PORTABLE` in
`tools/check_notebooks.py` is the list, and `notebooks/README.md` explains why it
scans code and not prose.

### Why the coverage matrix is nightly

Eighteen minutes, because every scenario settles for 9 h 15 m before measurement
begins. It is a **measurement, not a gate**: it reports how many of the eleven
faults are covered and which rules fire on a healthy plant, and those numbers are
the ones most likely to drift without anybody noticing. The job uploads the JSON
so the trend is visible, and the slow tests run there too — a change that adds a
false positive should fail a build rather than be noticed a fortnight later.

`17 4` rather than `4 4`: every scheduled job in the world is on the hour, and
the ones on the hour queue behind each other's runners.

### What the `images` job asserts, and why it is not just "it builds"

`docker compose build scada` succeeding and the flows loading are different
claims, and only the second one matters. The base image is **prebuilt**, so it
never installs a `package.json` added afterwards, and the runtime then sits at
"Waiting for missing types" forever. The job therefore runs the lookup Node-RED
does:

    ls /data/node_modules | grep -qx node-red-contrib-postgresql

and that is the assertion which would have caught it.

## Two gates that had never passed, and why a local run could not tell

Found by reading `gh run list`, after three consecutive failed pushes. Both are
fixed and both were invisible on a laptop, for reasons worth stating — a gate that
is green locally and red on the runner is a gate that has been telling you
something narrower than you think.

### The setup step checked the roles instead of creating them

```yaml
- run: uv run python -m storage.postgres.roles --check
```

`--check` reports and changes nothing. On a fresh service container there is
nothing to report *on*, so the step created no roles and then printed
`all 2 roles are as declared` — **true, and empty**. Two minutes later the SQL
course failed with `role "wwtp_gateway" does not exist`, and then with
`password authentication failed` for a role that had never been created.

A laptop never sees this because `init-db` does all three things — schema, group
roles, login roles — and CI reproduced only the first two. The step is now
**apply, then check**, and the check is a separate step so it is an assertion
rather than the same command twice.

### The lesson gate's port allocator only fails on Linux

`82/87 snippets ran`, five failures, all `address already in use` on port 48500.
`free_port()` asked the OS for port 0 and, when the answer came back below its own
floor, returned the constant floor instead — so every such snippet got the same
port. **macOS's ephemeral range starts at 49152 and Linux's at 32768**, so the
branch cannot fire on a Mac. The fix for a constant was a constant behind a
platform-dependent branch.

It now walks up from the floor until a bind *succeeds*, and remembers every port
it has handed out so a snippet that leaks its server cannot cause the next
snippet's failure. `tests/test_lessons_gate_ports.py` reproduces the Linux
ephemeral range on a Mac by substituting `socket.socket`, and asserts the premise
— that macOS cannot take that branch — so the next reader of a green local run
learns why it is not evidence.

## A gitignored file, and a gitignored file's assumptions

`tools/env.sh` is sourced by bash at the top of **every** recipe, and it ended in
a bare `. .env`. `.env` is gitignored, so CI — a checkout, with no credentials and
no local configuration — has never had one, and *every* target failed with

```
tools/env.sh: line 43: /…/.env: No such file or directory
make: *** [Makefile:NNN: some-target] Error 1
```

`make lint` failed too, which has nothing to do with the environment. It also
failed four of `tests/test_makefile_env.py`, because that file invokes the real
`$(PY)` wrapper and so is a genuine user of `env.sh` on a machine without `.env`.

`env.sh` now warns on stderr and carries on. **A gitignored file cannot be a hard
dependency of every recipe**, and a warning that names `cp .env.example .env` is
more use than a shell error about a file the reader has never heard of.

The second half is the part worth keeping. `test_the_makefile_loads_env_for_every_recipe`
asserted `^set -a$` at the top of `env.sh`, and failed on the corrected file the
moment `set -a` moved inside an `if [ -f … ]`. The guard was matching the file's
*shape*. The correct repair — widen it to `^\s*set -a\s*$` — and the tempting one
— unindent the file until the old regex matches again — look the same from a
distance, and only one of them leaves the behaviour intact.

Fixing `env.sh` is not the same as fixing its tests. `tests/test_makefile_env.py`
had a `pytest.skip` for "`POSTGRES_PORT` is not in `.env`" sitting *after* the
`read_text()` that raises `FileNotFoundError` when there is no `.env` at all — so on
a runner the three tests failed and the skip was unreachable. The guard has to come
first, and the tests that need a file to demonstrate anything now ask for one
explicitly. What runs everywhere instead is
`test_a_missing_env_is_a_warning_and_not_a_failure`, which builds its own
`.env`-less root and deletes nothing.

The unit suite is now exercised in both shapes: `make test` with `.env`, and the
same pytest invocation under `env -i` with the workflow's variables and no `.env` at
all. Running the gate the way the runner runs it costs nothing, and it found a
fourth instance of this class that no log had — see below.

## What CI does not cover, stated plainly

* **The flows' JavaScript is not executed.** Their *SQL* is, against the same
  database, by `tests/test_scada_contract.py`. Running Node-RED in CI to test
  three generated flows costs more than it is worth at this size, and the honest
  thing is to say which half is covered rather than let "flows tested" mean the
  weaker half.
* **`alarms serve` is never started.** Its state machine is covered by unit
  tests and its behaviour by the slow scenarios, but the process itself has only
  ever been run by hand.
* **`ui/web` has no tests at all** — the least verified part of the project, and
  `docs/LEARNING-LOG.md` has it as an open thread.
* **The pre-existing lint debt** is measured, not fixed. 157 findings, tracked
  in `lint-debt-baseline.txt`.
* **`tests/test_scanloop.py::test_pace_divides_by_speed_so_a_backfill_is_not_throttled`**
  fails in a full run and passes alone. It is a wall-clock flake and it is not
  fixed; it is the only test known to be order-dependent, which is itself worth
  knowing.
