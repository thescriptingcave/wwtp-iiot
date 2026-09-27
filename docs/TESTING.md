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
| Unit | 378 | no |
| Integration | 17 | yes, and refuses a seeded one |
| SQL course | 57 queries | yes |
| Alarm detectors | 58 | no — pure functions over hand-built windows |
| Alarm engine | 17 | no — a list for a sink, an injected clock |
| Alarm rules | 12 fast + 4 slow | the slow ones run the plant model |
| Contract ↔ schema | 11 | no — asserts against the DDL text |
| `mypy` | clean across 30 source files | no |
| `ruff` | clean on every file the migration touched | no |

The repository has **no CI configuration and no `Makefile`**. The gates above are
run by hand. That is a real gap, listed at the bottom rather than hidden.

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

**The alarm engine exists but six of its fifteen rules fire on a healthy plant,
and three faults are caught only incidentally.** Both are measured and both are in
[`ALARMS.md`](ALARMS.md); the false-positive count is a ratchet in
`test_a_healthy_plant_raises_almost_nothing`, so a regression is a failing test and
a fix is a deliberate edit. Four rules have never fired, so their thresholds are
also unverified.

**The coverage matrix is a simulation, not a plant.** It answers "given a plant
that behaves this way, does this rule fire?" — which is the question about the
*rules*. It says nothing about real fouling, real instrument failure, or whether
any of this would help anybody, and `docs/ALARMS.md` says so in the place somebody
would otherwise skip past it.

**The dashboard is untested.** `ui/web` has no test suite. It is four services
and a Next.js app, and it is the least verified part of the project.

**No CI.** No workflow, no pre-commit, no `Makefile`. The gates in the table above
are run by hand, which means they are run when somebody remembers. A three-line
workflow running pytest, mypy, ruff and `check_sql.py` against a service
container is the highest-value missing piece, and it is missing on purpose
because doing it properly is a separate piece of work rather than something to
half-do at the end.

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
