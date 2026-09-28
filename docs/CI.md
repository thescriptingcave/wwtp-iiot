# CI

`.github/workflows/gates.yml`. **Six jobs.** This document says what each one is
for, what it deliberately does **not** cover, and why the exclusions are
exclusions rather than oversights.

It said "five jobs" until a test caught it: `lint-debt` was added after this
sentence was written, which is the ordinary way a number goes stale — the change
was real, the prose was simply not revisited. `tests/test_readme_claims.py` now
asserts it.

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
was written; it is **158** now, the difference being a `ruff --fix` pass over
`softplc/servers/opcua.py` that took that file from 27 findings to 6.
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
count goes **up** and does not fail if it goes down. A hard gate at 159 would
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
| `drift` | every push | generated files vs the contract, and `docker compose config` |
| `images` | every push | the three images build, and the two non-core node types resolve |
| `lint-debt` | every push | 158 findings is the baseline; going up fails |
| `nightly` | 04:17 UTC | the fault × rule coverage matrix and the four slow tests |

That is five on every push and one on a schedule, and the count is asserted by
`tests/test_readme_claims.py::test_no_document_says_the_ci_workflow_has_five_jobs`
— which found the wrong number in *this file* the day it was written.

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
* **The pre-existing lint debt** is measured, not fixed. 158 findings, tracked
  in `lint-debt-baseline.txt`.
* **`tests/test_scanloop.py::test_pace_divides_by_speed_so_a_backfill_is_not_throttled`**
  fails in a full run and passes alone. It is a wall-clock flake and it is not
  fixed; it is the only test known to be order-dependent, which is itself worth
  knowing.
