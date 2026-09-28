# Testing

What is verified, how, and — the part that matters more — **what is deliberately
not**.

```
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

`-p no:cacheprovider` is not decoration. It keeps pytest from writing to
`.pytest_cache` in the repo root, which the Modbus socket test objects to.

## The current state

| Suite | Count | Needs a database? |
|---|---|---|
| Unit, no database | 672 | no |
| Integration | 46 | yes, and refuses to truncate a seeded one |
| Slow (`-m slow`) | 4 | no — they run the plant model, ~18 min |
| SQL course | 64 queries in 17 lessons | yes |
| OPC UA course | 35 snippets in 9 lessons, each against a **live server** | no — the gate starts its own |
| Extracted queries (`sql/TablePlus/`) | 64 files, run against a live database | yes |
| `mypy` | clean across 57 source files | no |
| `ruff` | clean on the gated packages; 158 tracked findings elsewhere | no |

Per file, for the ones worth naming:

| File | Tests | What it is for |
|---|---|---|
| `test_contract.py` | 63 | the loader's rules: units, ranges, bands, register links, write paths |
| `test_alarm_detectors.py` | 61 | pure functions over hand-built windows |
| `test_modbus.py` | 63 | word order, the register model, the client |
| `test_process.py` | 40 | the chemistry and the control loops, dimensionally |
| `test_scada_contract.py` | 37 | the generated flows — and it *executes their SQL* |
| `test_scanloop.py` | 31 | pacing, metrics, fault propagation |
| `test_faults.py` | 28 | the eleven faults and what each one does to the plant |
| `test_control.py` | 26 | DO control, chlorine dose, SRT |
| `test_spool.py` | 23 | durability across rotation and restart |
| `test_alarm_replay.py` | 22 | rebuilding alarm state from the event log |
| `test_web_page.py` | 22 | the dashboard's data path, its SQL, and its credential boundary |
| `test_readme_claims.py` | 47 |
| `test_opcua_course.py` | 32 |
| `test_opcua_minimal_client.py` | 7 | that the reference client's five safeguards actually fire | the lesson gate's own behaviour, and the claims the course makes |
| `test_extract_sql.py` | 10 | that the numbers this document states are the real ones |
| `test_alarm_engine.py` | 17 | a list for a sink, an injected clock |

**Every number in both tables is asserted by
`tests/test_readme_claims.py`** — each per-file count by `pytest --co`, the
course count against `tools/check_sql.py`'s own output, and `mypy`'s file count.
Which is a claim, and the first version of it was written before the assertions
existed; it is now true and `test_the_documented_test_counts_match_the_suite`
fails if it stops being so.

This table understated the unit suite by 57 % — it said 378 unit tests against
592, 17 integration tests against 46, and `mypy` over 30 source files against
55 — and it said the repository had "no CI configuration and no `Makefile`",
which was false in both halves. That is five documents carrying the same stale
figure, and the reason the checks now sweep every markdown file rather than this
one.

## The gates run in CI, and what each one is

`make check` runs the fast gates locally. `.github/workflows/gates.yml` runs them
on a machine that is not mine, in six jobs — see [`docs/CI.md`](CI.md) for what
each covers and, more usefully, what it deliberately does not.

The line that was here before said "The repository has no CI configuration and no
`Makefile`. The gates above are run by hand." Both halves were false, and the
sentence is worth keeping as an example: **a document that says a control is
missing when it is present is not conservative, it is wrong in the direction that
gets a project trusted.** It was written when it was true, which is the only
defence, and that is not one.

**`ruff` is the one gate that is not clean**, and the table above used to say
"clean on every file the migration touched" — which was true and misleading, the
same trick in miniature. It is now scoped: `make lint` checks the packages that
are clean, `make lint-all` shows everything, and `make lint-debt` fails if the
count in `lint-debt-baseline.txt` goes **up**. 159 findings, tracked openly, unable
to grow.

---

## The four layers, and why each exists

### 1. Unit tests: no sockets, no database

The process model (1 642 lines of chemistry and control), the contract loader,
the deadband, the spool, the scan loop, both protocol *servers* against a fake
client, and the writer's buffering policy.

**These run in about two minutes and need nothing**, which is the property that
makes them worth having: a contributor can check their change in the time it
takes to read the diff.

The writer takes an `execute` callable rather than a connection, so its policy —
when to flush, what a failure costs, what gets counted — is tested with a list and
no database. That decision predates the migration and is the reason the storage
layer was unit-testable while its SQL went unexecuted for three days.

### 2. Integration tests: against a real database, and refused against real data

`tests/integration/test_postgres.py`. 17 tests, skipped loudly when there is no
database.

Every one that asserts a constraint shows it *biting*, and the evidence is the
server's own error message:

```
ERROR: new row for relation "_hyper_7_85_chunk" violates check constraint
       "reading_null_is_not_good"
```

**Read that carefully — it names the chunk, not the table.** A constraint on a
hypertable lives on every chunk, because a chunk *is* a table. Both names are
correct and neither is a bug.

**The suite refuses to run against a database holding more than 50 000 readings.**
It truncates `reading`; the seeder puts a week in. This guard exists because it
already destroyed one: the first version read `POSTGRES_TEST_DB` for the skip
message and `POSTGRES_DB` for the connection, so the suite connected to and
truncated the developer's real database while appearing to use a scratch one. It
failed as an authentication error, which is a wonderfully misleading message for
a port mistake.

A guard is not a promise in a docstring, and this one is a guard.

### 3. The SQL course checker

`tools/check_sql.py` runs every ````sql` block in `sql/` against a real server.

It fails on any error, on any answer that changes between runs, and on any query
that returns zero rows. Three things about it are the interesting part.

**It cannot change your data.** Every block runs in a transaction that is rolled
back. The lessons contain `INSERT` statements — you are *supposed* to write some
bad-instrument readings — and the first version ran each block three times in
autocommit, so a lesson's `INSERT` executed on run 1 and collided with its own
primary key on run 2, and was reported as a failing query. Same hazard as the
integration suite, in a different tool: **a program that runs SQL from a directory
needs a rollback, not a promise.**

**It distinguishes three kinds of instability.** A float aggregate differing in its
sixteenth digit is arithmetic. A *result ordering* changing is also arithmetic, and
much harder to dismiss. Anything else is real non-determinism and fails.

**It skips illustrative fragments**, marked with `<!-- check: skip -->`. Without
that, a course full of deliberately-wrong examples produces a wall of expected
failures, and a reader who learns to ignore the output learns to ignore real
failures too.

### 4. The drift guard

`tests/test_postgres_schema.py` asserts the contract fits the DDL — by parsing
`schema.sql` and `schema.py` as *text*.

Parsing rather than importing is deliberate: the point is to check the artefact a
reviewer reads, against the contract, not against a Python object that might have
been built from the same wrong assumption.

It includes a sweep for tautological `CHECK` constraints, because one was written
by accident during the migration (`… OR true`) and caught only by that sweep. A
tautological constraint is the one piece of DDL that is actively misleading: it
appears in a diff, it appears in `\d`, and it enforces nothing.

---

## What running the thing found that tests did not

Fifteen bugs before the migration, all found by running against live databases.
They are listed in [`DESIGN.md`](DESIGN.md); the pattern is worth naming:

> **Not one of them would have been found by unit tests, and not one by reading
> the code.**

Six were schema and transport, four were SDK behaviour, five were `compose.yaml`
— and three of those five would each have stopped the stack on a fresh machine.
The most instructive was the `pg_isready` health check: the Postgres image starts
a *temporary* server on a unix socket to run its init scripts, and a health check
without `-h 127.0.0.1` reports healthy during that window. That is the same class
of bug as the Couchbase `/pools/default` endpoint, which answers the string
`"unknown pool"` — not an HTTP error, so it reads as success, and it cannot be
used as a readiness signal at all.

The lesson is not "write more tests". It is that **a startup path is code, and
code has to be run.**

---

## What is deliberately not tested

Stated plainly, because a test suite that implies completeness is lying.

**The chemistry is not validated against a real plant.** Every constant in
`softplc/process/` is plausible and internally consistent. None of it came from an
operating wastewater facility. The aeration DO loop holds 2.0 mg/L with about
**19 % headroom**, which is thin, and the solids balance closes to about **15 %** —
bounded by its own test rather than tuned away, because a mass balance that closes
suspiciously well is usually one that has been fitted.

**No performance testing.** 4.3 million rows is a week. A year is 220 million,
and the 7-day raw retention policy means `reading` never holds more than about
600 000 rows anyway. Nothing here has been loaded at a year's scale, and the
continuous aggregates have not been measured against a realistic refresh rate.

**No test asserts the SQL course's *answers*.** The checker verifies that every
query runs and returns rows. Whether the output shown in a lesson is still what
the query produces is not checked, because the answer changes as the seeder's
random seed changes — and a test that fails when a lesson's illustrative numbers
age out is a test that gets deleted rather than fixed. **This is a real gap**: the
shown outputs in `sql/` were generated from real runs and are correct as of this
commit, and nothing will tell you when they stop being.

**The alarm engine has five of its fifteen rules firing on a healthy plant, and
the remaining five cannot be tuned away.** It was six before the thresholds were
measured; `docs/ALARM-TUNING.md` has the measurement behind every one, and the
count is a ratchet in `test_a_healthy_plant_raises_almost_nothing`, so a
regression is a failing test and a fix is a deliberate edit.

The five are not untuned — they are impossible, and the document says which way
each one is impossible: `secondary_blanket_stuck` because a deadband makes a
healthy steady signal and a failed instrument the same observation;
`lift_pump_flow_lost` because both lift signals are station totals and the
controller compensates; `influent_flow_surge` because a storm's flow *slope*
never exceeds a healthy flow's; and the two `cross_validation` rules because the
simulator publishes one source, so there is nothing to cross-validate.

**Four rules have never fired**, so their thresholds are unverified in the other
direction — a threshold nobody has seen fire may be unreachable rather than
tuned.

**The coverage matrix is a simulation, not a plant.** It answers "given a plant
that behaves this way, does this rule fire?" — which is the question about the
*rules*. It says nothing about real fouling, real instrument failure, or whether
any of this would help anybody, and `docs/ALARMS.md` says so in the place somebody
would otherwise skip past it.

**The dashboard's *rendering* is untested.** `ui/web` has 22 tests in
`test_web_page.py` and they cover the data path: the generated read model is in
step with the contract, every signal id the pages name exists, all four SQL
statements run against a live database, the credential cannot reach a browser,
and there is no second write path.

What is *not* covered is the JSX. **There is no TypeScript test runner**, so
"the page renders" is a manual claim — established by `npm run build`,
`next start`, and a `curl` per route, with the container verified healthy and its
client bundle grepped for the password. That is a materially worse position than
the other twelve components are in, and the fix is a test runner rather than
another assertion about source text. `ui/web/README.md` says so in the place
somebody would look.

**The two alarm harnesses disagree by a factor of seven, and nobody has explained
why.** `alarms.tune` and `alarms.scenarios.run_fault` measure the same rules over
the same window and report healthy DO slopes an order of magnitude apart. Every
threshold in the project is set from the wider of the two, with 3× the margin —
so the thresholds' provenance is a guess. This is the first item on the open
list in [`LEARNING-LOG.md`](LEARNING-LOG.md) and the only one that undermines
work already done.

**The gateway cannot reconnect to Postgres, and did not for five hours.** The
writer opens one connection at construction and never re-establishes it
(`grep -c reconnect storage/postgres/writer.py` is **0**), so a database restart
or a terminated backend stops every write *permanently* — the only recovery is a
container restart. It was found with `failures: 29702` in the gateway's own
status line while the container reported **healthy**, because the health check
verifies that the gateway *cannot* `TRUNCATE reading` and never tests that a
write succeeds.

No data was lost — the spool held 515 files and delivered 134 078 rows on
restart, and the history is continuous — so the durability layer held under an
outage the layer above it could not survive. But `pending: 2 613 776` overstated
the data at risk by three orders of magnitude, because it counts an in-memory
buffer whose contents were also on disk. **A status line that blurs "queued in
RAM" from "exists only in RAM" invites the wrong conclusion in both
directions.**

**`source` is always `opcua`, so the cross-protocol defence is not implemented.**
`poll_once` merges both protocols into one dict ("OPC UA wins on conflict") and
`SpoolRecord` has no source field, so `drain()` hardcodes `"opcua"`. Since
`source` is part of the primary key and the README's answer to a wrong Modbus
word order is to compare two independent observations of the same quantity,
**that defence is absent rather than weak** — there is only ever one source.
Every unit test passes, because the writer's and the spool's tests each test
their own half and neither knows the provenance does not survive the trip. Found
by a walkthrough and one `SELECT DISTINCT source`. Three open threads, all in
[`LEARNING-LOG.md`](LEARNING-LOG.md).

**The CI gates are only as good as the machine they run on, and the slow half runs
nightly rather than per push.** The 18-minute coverage matrix and the four slow
tests are in the `nightly` job, because the whole `unit` job is 2 m 12 s and a
gate that takes 20 minutes is a gate people learn to skip. So a change that adds a
false positive is caught within a day rather than on the push.

---

## Conventions

**Test names say what is true, not what is called.** `test_a_failed_batch_is_kept_and_retried_in_order`
rather than `test_writer_retry`. The name is the first line of documentation and
it is read far more often than the body.

**Docstrings carry the reasoning, including the measured error strings.** A test
that asserts a specific Postgres error message is asserting a *behaviour*, and
the message is how the database reports it. Three of them name constraints
explicitly, so a rename shows up as a failure.

**`xfail` with a reason, not `skip`.** The ten Modbus offset tests were `xfail` for
a while because the arithmetic had been checked and the wire had not. When the
wire test passed, they were not deleted — they were un-marked, and they are the
reason the offset is documented where the arithmetic happens rather than in a
comment somewhere else.

**Comments say why the obvious thing is wrong.** `Reading` has a `ts: float` and
`as_row()` converts it to an aware `datetime`, because a parameterised `INSERT`
quietly coerces a float and `COPY` does not. That asymmetry cost a debugging
round, and the comment is the reason it will not cost another.
