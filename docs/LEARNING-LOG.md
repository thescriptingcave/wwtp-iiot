# Learning log

Append-only. One entry per phase, in the learner's own words where possible.
The point is to record *what surprised me*, because that is what needs revisiting
before a design decision is made again under different assumptions.

Format: what was built, what was expected, what actually happened, what it cost.

---

## Phase 5b — Acknowledgement, CI, triage

**Expected:** wire up the alarm acknowledgement, add CI, and write a web page.
Roughly a day each.

**What happened:** the acknowledgement took most of it, and the reason is that
it forced a design question the codebase had been avoiding for two phases. The
other two items were shorter than expected and found more.

**Learned:**

- **An acknowledgement that does not survive a restart is not an
  acknowledgement.** `AlarmEngine.acknowledge()` was written, tested, and called
  by nothing; and the Node-RED flow shipped a comment saying it "closed the gap".
  It could not acknowledge anything, because an acknowledgement is a *write* to
  `event` and no flow performed one. Rebuilding state from the event log forces
  the question the state machine had dodged: **is an acknowledgement per rule or
  per occurrence?** Per occurrence (`event.id`) means an operator acknowledges a
  flapping alarm on every flap — a rule cycling every thirty seconds produces
  2 880 acknowledgements a day and a panel nobody works through. Per rule
  *forever* means an alarm that is genuinely new an hour later is silenced by an
  acknowledgement for something that is not the same. So: per rule, cleared when
  the condition clears. And the test that pins it
  (`test_a_recurrence_is_a_new_alarm_and_does_not_inherit_the_acknowledgement`)
  exists because inheriting would be a **confident false negative**, which is the
  worst thing an alarm system can produce — the alarm system suppressing a
  real fault using the very mechanism built to suppress nuisance.
- **The obvious definition of "active" is wrong for exactly the alarms this
  project is about.** `cleared_at is None` looks right and is not: a `critical`
  **latches**, so the engine never writes an `alarm_cleared` for one at all, and
  an acknowledged critical has `cleared_at is None` forever. The test caught it
  — two active alarms where there should have been one — and the fix is three
  separate concepts: `resolved` (off the panel, for either of two independent
  reasons), `latched` (the condition never cleared), and `on_panel`.
- **`make_interval(hours => %s)` is a type error and the error message does not
  say so.** It takes an `int`; psycopg sends a `float`; the reply is `function
  make_interval(hours => double precision) does not exist` with a hint about
  matching argument types, which reads as a schema problem. `(interval '1 hour' *
  %s)` is the same intent and also accepts the fractional lookback of 0.5 h that
  you want when debugging.
- **The flow SQL is in a dialect nothing else speaks.** `node-red-contrib-postgresql`
  uses `$name` placeholders. `psql` and libpq understand `$1`, not `$name`.
  **psycopg3 does not understand `$1` at all** — it uses pyformat, and given
  `$1` with a parameter it says `the query has 0 placeholders but 1 parameters
  was passed`, which is a message about the *driver*, not the query, and sends
  you looking in the wrong place. So there was exactly one implementation in the
  world that could run the four flow queries, it was a node inside a container,
  and it reported failures as a red box. Translating to `%s` in a test made it
  the second, and **running it immediately found a real bug**: `$rule` inside
  `jsonb_build_object` has no inferable type
  (`could not determine data type of parameter $3`) and needs `::text`. The same
  parameter in a `VALUES` list infers fine from the column types; the ones
  inside a function call have nothing to infer from. This is the
  `test_grafana_dashboards.py` lesson arriving a commit later than it should
  have — the two `test_scada_*.py` files were written *before* it and had no SQL
  execution in them at all.
- **A test that cries wolf about `LATERAL` is a test people learn to ignore.**
  The "flows only touch reviewed tables" test reported
  `query touches 'LATERAL', which is not in the reviewed set`, because a regex
  cannot tell a keyword from a table. Fixed by teaching it the difference
  rather than by adding `LATERAL` to the allow-list, which would have hidden the
  real check.
- **Writing the CI file found that `make lint` and `make types` had been failing
  for four phases**, because every invocation in this project was over a subset
  of the packages the Makefile names. Two mypy errors in `tools/opcua_browser.py`
  and 159 ruff findings, the latter mostly `E501` and `PLC0415` in
  `softplc/process/units.py`. The lesson is not "run more checks" — it is that
  **a gate reported as passing because it was run over the wrong subset is worse
  than no gate**, and this project produced that mistake twice in one day. The
  ruff gate is now scoped to what is clean and the debt has a ratchet
  (`lint-debt-baseline.txt`) that fails only if the count goes *up*.
- **`docker compose run scada` found the fourth broken compose file in this
  project.** The credential volume was nested inside a read-only bind mount, so
  the service could not start at all
  (`mkdirat .../data/credentials: read-only file system`). The second attempt —
  a named volume over `/data` — works and silently *shadows the image's
  `settings.js`*, so every setting in it is ignored and nothing says so. Both
  are recorded in `compose.yaml` because the reasons are the useful part.
- **Three JSON arrays cannot be concatenated with `sed`.** The first attempt at
  assembling the three generated flows into one `flows.json` stripped the
  leading `[` and trailing `]` and produced `Extra data: line 212 column 5` —
  invalid JSON, from a shell script, in a container, at deploy time. JSON arrays
  have no line structure to key off. `node -e` is in the image already because
  it is the runtime, and the output is valid by construction.
- **The base image ships a root-owned `/data/flows.json` and the container runs
  as `node-red`.** So `writeFileSync` fails with `EACCES` while writing a
  temporary and renaming over it succeeds — the *directory* is owned by
  `node-red`, so creating and replacing are allowed even though opening the
  shipped file for writing is not. Without the rename the runtime starts
  **healthy**, serves the base image's example flow, and logs nothing. A
  generated file that cannot replace the generated file is invisible. That is
  the sixth time in this project that the only difference between working and
  apparently working was ownership.

## Phase 5c — The custom dashboard

**Expected:** a `package.json`, three pages, an hour.

**What happened:** the hour was the easy part. The interesting finding is that
**the test caught the exact bug this log already documents, in code I had built
and run ten minutes earlier.**

**Learned:**

- **The permit page named two signals that do not exist.** `EFFLUENT:FLOW:BOD` and
  `EFFLUENT:FLOW:NH4_IN`, written from memory of what a permit page usually
  shows. The page compiled, started, and rendered "no data" for both — correctly,
  because a query for a signal that is not there returns nothing rather than
  failing. The real ids are `EFFLUENT:FLOW:NH4` and
  `EFFLUENT:FLOW:TURBIDITY`.

  That is **thread 22, happening again**, and the repetition is the lesson. The
  first time, a Grafana dashboard read pH from the TSS signal. The second time, a
  page I wrote *after writing the test for exactly that* did it anyway. So the
  lesson is not "write the test" — the test existed, in
  `tests/test_web_page.py`, and caught it on the first run. **The lesson is that
  the test has to be in the default suite rather than something you remember to
  run**, because the failure mode is not a wrong number appearing on a page
  nobody checked; it is a plausible number on a document-shaped page, produced by
  the same person who wrote the test.
- **`$1` is not a universal placeholder, and now it has happened twice.** `pg`
  (node-postgres) speaks the server-side protocol and takes `$1`. psycopg3 does
  not — it uses pyformat, and given `$1` with a parameter it says `the query has
  0 placeholders but 1 parameters was passed`, which is a message about the
  *driver* and sends you looking in the wrong place. The Node-RED flows' `$name`
  dialect had the same property one commit earlier.

  So the project now has **two** SQL dialects that exactly one runtime in the
  world can execute, and both have a Python test that translates. That is the
  pattern; the first occurrence was an anecdote.

  And the translation has a subtlety worth recording: bare `%s` binds *left to
  right*, while `$n` binds *by number*, and in `trend()` the two are not in the
  same order — the numbers follow the function signature
  (`trend(signalId, hours)`) and `make_interval(hours => $2::int)` appears
  **above** `a.signal_id = $1` in the text. Translating `$1`→`%s`, `$2`→`%s`
  silently swapped them and produced `operator does not exist: text = smallint`
  in a query that was correct. The translation uses `%(p1)s` / `%(p2)s` now, so
  it is faithful to the numbering rather than to the text.
- **A setpoint's `normal_low` and `normal_high` are the same number.** Two of the
  57 signals have a zero-width band: `AERATION:AHU-1:SETPOINT_DO` at 2.0–2.0 and
  `SITE:WEATHER:STORM` at 0.0–0.0. Shading a zero-height region draws a *line*,
  and for the setpoint that line sits exactly on the commanded value — so the
  panel would say "outside band" for a setpoint sitting precisely where it was
  told to sit. A zero-width band is a rendering bug and the fix belongs in the
  generator, because the contract is right: a setpoint does not have a healthy
  range.
- **The Dockerfile said "the browser cannot keep a secret" while the compose file
  handed it a database URL.** `NEXT_PUBLIC_POSTGRES_URL` was a build argument. It
  was a host and a port, so nothing leaked, and a reviewer reading the name has no
  way to know which it is — the *pattern* is one argument from the password. The
  connection details are runtime environment variables now, `lib/db.ts` is marked
  `server-only` (which throws at build time if a client component imports it), and
  three tests assert the absence.
- **Three tests failed on their first run because they read the comments.** Three
  separate assertions — no `NEXT_PUBLIC_`, no `'use server'`, no `NEXT_PUBLIC_` in
  the compose service — matched the *explanations* of why those things are wrong.
  The fix was to strip comments before scanning, which is the right direction: a
  false positive is a comment to reword and a false negative is a credential leak.
  The same test also sliced the compose file to the next comment banner, so it was
  asserting on **Grafana's** environment and would have passed no matter what the
  web service said.
- **`as const` proved a branch unreachable that I still wanted.** The permit
  page's `'none'` case (a parameter with no limit at all) narrowed every
  candidate to `never` under `as const`, because no entry in the list currently
  has neither a min nor a max. `tsc` was right about the list and wrong about the
  code. An explicit `PermitParameter` interface keeps the branch, and the compiler
  still catches a typo'd signal id — which is the check that matters.
- **A read-only role and a read-only filesystem are free.** The page has no write
  path at all, so `wwtp_ui` in `wwtp_reader` costs nothing and is exactly the
  right grant, and `read_only: true` makes "this service cannot be talked into
  writing" a property of the container rather than a claim in a comment. It needed
  a tmpfs for `.next/cache`, which is the whole cost.
- **Verified the gap handling by breaking the data on purpose.** 40 readings
  inserted with 6 removed: the response contained **two** `<path>` elements, which
  is a break rather than a straight line across the gap. Then deleted the probe
  rows. A sparkline's correctness is a thing you can only see by looking at the
  path data, and it was the only assertion in this phase that no test makes.

- **`COPY` is not a shell, and the web image could not be built at all.** The
  Dockerfile ended with

      COPY --from=build --chown=app:app /app/next.config.* ./ 2>/dev/null || true

  BuildKit parsed `2>/dev/null` and `|| true` as two more source paths and failed
  with `cannot copy to non-directory: .../app/true`. The sixth Dockerfile or
  compose file in this project that ships broken.

  **And the first one found by a CI job rather than by running `docker compose
  up`** — the `images` job was written the day before and would have caught it on
  the first push. That is the whole return on the CI work, in one line: the
  failure mode is unchanged, but it now costs ninety seconds instead of shipping.

  The explicit name is better anyway. A glob that matches nothing copies nothing
  and the app starts on Next's defaults, which is the "it came up" failure for the
  seventh time. A missing file is a loud build error.

**What is still weak, and it is the weakest part of the project:** there is no
TypeScript test runner, so "the JSX renders" is a manual claim — `npm run build`,
`next start`, a `curl` per route, all four 200, zero errors in the log. The data
path is tested from Python; the rendering is verified by running it and written
down in `ui/web/README.md`. That is a materially worse position than the other
twelve components are in, and the honest fix is a test runner, not another
assertion about the source text.

## Phase 6b — The review before the push

**Expected:** a read-through before pushing, to catch anything embarrassing.

**What happened:** five false claims in `README.md`, a security document
asserting a regression that had been fixed two phases earlier, and three tests of
mine that were passing for the wrong reason.

**Learned:**

- **The headline claim was false.** `README.md` said `contracts/tags.yaml` holds
  the plant *and* the eleven faults and six scenarios, and that "all" of the PLC,
  gateway, database, alarm engine, dashboards and tests "read that one file". There
  are **two** contract files. `fault-scenarios.yaml` holds the faults, and four
  source files read that instead. It was the first paragraph after the diagram —
  the first thing a reviewer reads — and it was wrong about the project's
  central design claim.
- **`docs/SECURITY.md` said the database was unprotected.** A whole section titled
  "One database, so one credential — and that is a regression", asserting the
  gateway "can `DROP TABLE`" and that the three-role arrangement was "not
  implemented here" — while printing the exact SQL that has existed since Phase
  3g. It was also out of date in the other direction: `scada` and `grafana`
  authenticate as the owner and do not need to, which was nowhere.
  **A security document that understates its own protections is as dangerous as
  one that overstates its own risks.** A reader assessing this project would have
  reached the opposite conclusion about the part of it that is the most carefully
  built.
- **A count in prose is a measurement or it is nothing** — and it is now a
  *documented rule with a test*, because I replaced "all of them read one file"
  with "nineteen source files read the first" and **that was wrong in the same
  commit**. `git grep -l 'tags.yaml'` gives twelve; grepping the loader too gives
  twenty-five, four of which are a comment, a JSON import and a page footer. So
  the README now names consumers by role and prints the command, and
  `tests/test_readme_claims.py` asserts the *structure* rather than a number.
- **Three of my own tests were passing for the wrong reason, and it took a live
  database to find out.** While writing the corrected security section I checked
  "the gateway cannot change the contract" with `UPDATE signal SET name = name`.
  It was **refused** — because `signal` has no `name` column. Two more the same
  way (`signal.eu` does not exist either; it is `unit`). So three security claims
  had green ticks on them for entirely the wrong reason.
  `REFUSALS` in `tests/integration/test_postgres_roles.py` now matches each
  refusal against the phrase the server must give, so a typo'd column name turns
  a test red instead of leaving it green for ever. **A test that cannot fail is
  worse than no test, because it is trusted.**
- **And the mirror image: the allowed set is a decision, not the complement of the
  refused one.** The same test also asserts what the gateway *may* do, and its
  first version read `reading_1m` — which is refused, on purpose, because
  `login_role.py` says the gateway does not need the rollups. The test was
  asserting the opposite of the design. Both directions are now asserted, and the
  refusal carries a note saying why.
- **The stale number is not in one file, it is in five.** The same "57 queries"
  was in `README.md`, `docs/GETTING-STARTED.md`, `docs/TESTING.md`,
  `docs/adr/0001-…md` and `scada/README.md`; "31 tests" in a sixth; "five jobs" in
  `docs/CI.md` and the learning log. **A count is only checked in the document you
  happened to read**, so the tests now sweep every markdown file — and
  `test_no_document_says_the_ci_workflow_has_five_jobs` failed on its own
  correction note, because it could not tell a *claim* from a *quotation*. That
  is three times in one review that a check caught the record of a mistake rather
  than the mistake. Double-quoted text is now excluded, with the reason written
  down: a number in quotation marks is somebody being cited.
- **The ratchet was checked by hand and was wrong.** Adding the new test file added
  two findings; `lint-debt-baseline.txt` was not moved with it. So
  `test_the_lint_debt_baseline_matches_the_files` now counts the tree and compares,
  and a ratchet nobody re-reads is a ratchet that is wrong on the day somebody adds
  a file.

## Phase 6c — The rest of the documents

**Expected:** `docs/TESTING.md` to be the last one, since it is the newest.

**What happened:** it was the worst of the three, and then `make check` failed
for a reason that had nothing to do with documentation.

**Learned:**

- **`docs/TESTING.md` said the repository had "no CI configuration and no
  `Makefile`."** Both false, and it is the document whose entire job is telling a
  reader what is and is not verified. It also said "the dashboard is untested"
  (22 tests), understated the suite by 57 %, and said six alarm rules fire on a
  healthy plant when it is five.

  The shape of it is identical to `SECURITY.md` claiming the database was
  unprotected, and the pairing is the finding: **one understated a protection and
  the other a verification, and both are wrong in the direction that makes a
  project look worse rather than better.** Neither is cautiousness. Both are prose
  written once and never revisited.
- **The dashboard correction had to be careful not to overclaim in the other
  direction.** "The dashboard is untested" is false, but the replacement is not
  "the dashboard is tested": 22 tests cover the *data path* and nothing covers the
  JSX, because there is no TypeScript test runner. Swapping one overclaim for
  another is the failure mode this whole review is about, so the correction says
  precisely which half is covered.
- **A course query had rotted, and `make check` is what found it.** One lesson
  came back *empty*, with the runner warning that an empty result in a seeded
  database is usually a wrong signal id. It was not a wrong signal id: it filtered
  `ts >= now() - interval '6 hours'`, and **a seeder writes history up to the
  moment it runs**, so six hours later the window contains nothing. It is now
  anchored to `(SELECT max(ts) FROM reading)`, which `01-beginner/README.md`
  already taught and this query did not follow.

  The generalisation is the lesson: **in a historian, `now()` and "the end of the
  data" are different questions**, and a query that conflates them returns an empty
  set rather than an error on any database that is not being written to at that
  moment. Two more lessons use `now() - interval '1 hour'` and will rot the same
  way; both are `check: skip` because they are *meant* to come back empty, and
  that is recorded rather than changed — because a reader cannot otherwise tell a
  clock problem from a lesson.
- **A test can fail on its own fixture, and that is the point.**
  `test_the_documented_test_counts_match_the_suite` asserts `docs/TESTING.md`'s
  per-file table *including a row for itself*. Adding fourteen tests to that file
  made its own row wrong. A stale row in a table about staleness would be a poor
  joke, so the failure is announced instead.
- **A check cannot tell a claim from a quotation, and it bit four times.** The
  sweep tests each caught their own correction note — `docs/CI.md` quoting "five
  jobs", the learning log quoting "57 queries" and "31 tests", and
  `docs/TESTING.md` quoting its old sentence over two lines. So there is one
  shared rule now (`_claims_only`): strip double-quoted text and code spans from
  the **whole document** before matching, because a per-line regex cannot match a
  quotation that wraps.

  The cost is real and stated in the helper: a document *could* hide a wrong claim
  inside quotation marks. That is worth paying, because the alternative is a test
  that cannot be fixed without deleting the explanation of what it is for.

## Phase 6d — Writing the verification steps, and running them

**Expected:** to write `docs/VERIFYING.md` — a step-by-step script somebody else
could follow. About an hour.

**What happened:** writing the steps found a false claim, a **live unrecovered
outage** on the running stack, and **a design defence that is not implemented.**

**Learned:**

- **The gateway had not written a row for about five hours, and never would have
  again.** Its own status line said `failures: 29702`,
  `last_error: 'the connection is closed'`, `pending: 2613776`, and the container
  was **`healthy`**.

  `storage/postgres/writer.py` opens one connection at construction and never
  re-establishes it — `grep -c reconnect` is **0**. So a database restart or a
  terminated backend stops every write permanently, and the only recovery is a
  container restart.

  The health check is a **privilege** check, not a liveness check: it verifies the
  gateway *cannot* `TRUNCATE reading`, which is a genuinely good check, and it
  passes. It never tests that a write succeeds. **The one component whose entire
  job is not losing data was invisible to the orchestrator**, and the only reason
  I found it was reading its logs while writing unrelated documentation.

- **No data was lost, and the compensating control did exactly what it is for.**
  The spool held 515 files throughout and delivered 134 078 rows on restart; the
  history is continuous hour by hour across the gap. So the durability design
  held under a five-hour database outage that the code above it could not survive.
  Which is a strange and rather good sentence: **the layer built for the
  disaster worked, and the layer built for the ordinary case did not.**

- **`pending: 2 613 776` overstated the data at risk by three orders of
  magnitude.** It is `len(writer._rows)` — an in-memory buffer fed by *live*
  polling, whose contents were also on disk in the spool. The status line showed
  2.6 M queued for data that was never at risk, and there was no way for an
  operator to tell "queued in RAM" from "exists only in RAM". **The distinction is
  the entire point of the number**, and a status line that blurs it is worse than
  one that omits it, because it invites the wrong conclusion in both directions.

- **`source` is always `opcua`, so the project's answer to Modbus's silent
  corruption is not implemented.** The chain, all of it verified by reading the
  code and a real spool file:

  1. `poll_once` polls both protocols and merges them into **one dict**. On
     conflict, *"OPC UA wins"*, because it carries a StatusCode and Modbus does
     not. Reasonable decision, and the wrong place to make it.
  2. So by `_publish`, **the protocol a reading came from is no longer
     information.**
  3. `SpoolRecord` has four fields — `ts`, `signal`, `value`, `quality`. There is
     **no source field.** A real record off the volume:
     `{"ts":1790534563,"s":"SECONDARY:SEC-SCR-1:TORQUE","v":44.84732813,"q":0}`
  4. `drain()` consequently writes `source="opcua"` unconditionally.

  And that matters because the README's stated answer to the Modbus word-order
  trap is *"the only defence is to compare two independent observations of the
  same physical quantity, which is why `source` is part of the primary key."*
  A wrong word order gives `2.3e-41` — finite, in-range, undetectable. **There is
  only ever one source in practice**, so the defence is not merely weak, it is
  absent, and Modbus is a *fallback* rather than a second observation.

  **Every unit test passes**, because the writer's tests and the spool's tests
  each test their own half and neither knows the provenance does not survive the
  trip. That is the answer to "why did this survive four phases": the seam
  between two well-tested components is where the bug lives, and no test in
  either component's suite can see it. It took a walkthrough and a
  `SELECT DISTINCT source` to find.

  Not fixed here. The fix is a field on `SpoolRecord` — which changes the on-disk
  format, so it needs a migration story — plus a decision about whether the
  primary key survives it. Both deserve their own commit.

- **`make watch` did not work, and neither did any document that showed it.**
  `make watch SIGNAL=AERATION:AHU-1:DO` printed `Not found: AERATION:AHU-1:DO`, and
  so did the identical command in `README.md` and `docs/GETTING-STARTED.md`.

  The address space is `Area → Equipment → Variable` and a variable's browse name
  is its contract *field* (`do_mg_l`), so the tool wanted
  `AERATION.AHU-1.do_mg_l`. Every other part of this project identifies a signal
  by `AREA:UNIT:FIELD` — the contract, the database, the Node-RED tag list, the
  flows, both dashboards — and **the diagnostic tool was the one place that did
  not**. Which is the worst place for it, because the diagnostic tool is what a
  person reaches for *when something is not working*. The one command most needed
  on a bad day is the one that does not run.

  Three documents and a `make help` line, all wrong the same way, for four
  phases, and no test caught it because every test that touched the browser
  passed a path the test itself chose.

  `resolve()` now tries the dotted form first and falls back to walking the tree
  for the `SignalId` property. Reading that property is itself a trap worth
  recording: it is a **namespace-qualified child** (`get_child(f"{ns}:SignalId")`),
  because a bare name resolves in namespace 0 and answers `BadNoMatch`, which
  looks exactly like a missing property. My first attempt read it as an attribute
  and reported "Not found" for a signal that was right there.

  And the second test is about the *documentation* rather than the tool:
  **every signal id any document tells a reader to type must be one the contract
  declares.** The bug was in four documents, so testing the tool alone would have
  left them broken.

- **Writing the steps found a false claim too, and it was a good one to find.**
  The README said "**Nineteen** of the registers deliberately use low-word-first
  ordering while their neighbours use high-word-first". It is **two**. The error
  was in the more interesting direction: it made the *common* case sound like the
  dangerous one, so a reader would hunt for the trap in the wrong sixteen
  registers. And `docs/DATA-FLOW.md` illustrated it with two addresses,
  `40101` and `40103`, that **do not exist** — the real ones are `40100` and
  `40102`, and both are `big`. Four lines of hand-written YAML about generated
  data, wrong three ways, in the one document that traces a scan end to end.

  Step 3.2 of the plan printed `0 low-word-first, 19 high-word-first`, which was
  wrong in a way that pointed straight at the sentence. Both counts are now
  asserted, and there is a general test: **every 5-digit address in
  `DATA-FLOW.md` must be a real register address.**

## Phase 6e — "Is this still true?", and a one-line bug with two phases of blast radius

**Expected:** answer a yes/no question about whether the dashboard exists.

**What happened:** the answer was no, and following it up found a bug that made
the dashboard unable to authenticate **at all** — in a module written for exactly
one purpose, generalised in its signature and not in its behaviour.

**Learned:**

- **The question was the review.** *"There is no Next.js dashboard yet. `ui/web`
  has a Dockerfile and no application, because Phase 5 has not been written."* It
  was true when written and false for four phases. It was in
  `docs/GETTING-STARTED.md`, so a reader following the document was told a
  working page did not exist — **which is worse than never having written it**,
  because they follow the document and conclude the project is unfinished.

  Asking one question cost nothing and found it. Every stale claim in this
  project's review was found by *doing* — running a step, counting a thing,
  asking whether a sentence was still true — and not one by reading. That is now
  six documents and about a dozen claims, and the pattern is completely
  consistent: **prose describing a state the code has left behind.**

- **`login_role.py` read the password from the literal string
  `GATEWAY_DB_PASSWORD`, for every role.** Adding `wwtp_ui` to the `init-db`
  command generalised `--name` and nothing else, so `init-db` created `wwtp_ui`
  with **the gateway's password** while the `web` service was handed
  `WEB_DB_PASSWORD`. The dashboard answered

      FATAL: password authentication failed for user "wwtp_ui"

  and `init-db` had logged `role wwtp_ui exists; password and LOGIN refreshed`
  immediately beforehand, which is what made it look impossible: the thing that
  refreshed the password set it to the wrong value and said so.

  **A parameter generalised in the signature and not in the behaviour.** `--name`
  implied a generalisation that the body did not honour, and nothing tested the
  second role — the only test touching `login_role` passed the password as an
  argument, which is the one thing a human never does. There is now a
  `LOGIN_PASSWORDS` table, `apply()` logs **which variable each role's password
  came from**, and a test proves two roles with *different* passwords both
  authenticate and neither accepts the other's.

- **The new log line immediately exposed a second problem it was not written
  for.** It printed

      password for wwtp_gateway taken from POSTGRES_PASSWORD (set GATEWAY_DB_PASSWORD …)

  because `GATEWAY_DB_PASSWORD` was unset in `.env` and the fallback is the
  **owner's** password. That fallback has been documented since Phase 3g as "a
  working configuration that defeats the point of the separate credential" — and
  until this commit it was completely silent. **A documented fallback that nothing
  reports is the same as an undocumented one**, and the fix was not to remove the
  fallback (a reader should not be stopped by a security nicety) but to make it
  say so on every run.

- **`init-db` is a one-shot service, so changing a password in `.env` does
  nothing.** It is `service_completed_successfully`, so an already-initialised
  stack keeps the old credentials and the symptom is an authentication failure
  that looks like a code bug. Now in `docs/GETTING-STARTED.md` with the fix:

      docker compose up -d --force-recreate init-db && docker compose restart web

  That is an operational trap rather than a code bug, and the only defence is
  documentation — which is why the auth-failure branch of the getting-started
  page now leads with it.

- **Setting the documented, correct variable *broke* the gateway.** With the
  password bug fixed, the next attempt was to give each service its own
  credential — which `.env.example` has recommended since Phase 3g. It failed at
  once:

      FATAL: password authentication failed for user "wwtp_gateway"

  Because **there were two notions of "the password" and they disagreed.**
  `login_role.py` set the role's password from `GATEWAY_DB_PASSWORD` while
  `schema.dsn()` connected with `POSTGRES_PASSWORD`, and compose passed *both* to
  the gateway. The stack had only ever worked because both variables were unset,
  so both paths used the owner's password and **matched by coincidence** — which
  is why the first bug hid the second.

  Now: **one variable, one meaning.** Each service gets exactly one
  `POSTGRES_PASSWORD`, taken from its own `.env` variable, and
  `${VAR:?set VAR in .env}` makes it **required**. A silent fallback to the owner's
  password is a working configuration that defeats the point of a scoped
  credential, and it defeats it invisibly — so the fallback is gone rather than
  documented. `${A:-${B}}` is not used, because Compose cannot nest it (this
  project has been bitten by that already) and a required variable is the honest
  answer anyway.

- **And `docker compose restart` does not re-read the environment.** After
  changing a password, `restart` reuses the container's existing env, so the
  service never sees the new value and the symptom is an authentication failure
  that looks exactly like a code bug. It cost an hour of "the fix did not work".
  `--force-recreate` is required, and both `init-db` (one-shot) *and* the services
  need it. In `.env.example` and `GETTING-STARTED.md`.

- **And `docs/ARCHITECTURE.md` was missing a service.** The new test asserts the
  service table against `compose.yaml`, and immediately found that **`scada` was
  in compose and absent from the table**. So the table had *two* problems: a row
  describing `web` as unwritten, and a row that was never there.

## The new tests, and what each one is for

* `test_the_architecture_service_table_matches_compose` — every service compose
  declares appears in the table. It **cannot** catch a wrong *description*, only a
  row that has outlived its service. That limit is stated in the docstring,
  because a test that claims more than it checks is the thing this project keeps
  finding.
* `test_getting_started_does_not_deny_a_service_exists` — the sentence this whole
  thread started from, and that `GETTING-STARTED.md` says how to start the thing
  it used to deny.
* `test_two_login_roles_authenticate_with_different_passwords` — the behavioural
  version of the password bug, over the network, checking both directions.

**And one uncomfortable note about how the password bug was nearly misdiagnosed.**
I tested the credentials with `psql` from inside the database container and *both*
passwords authenticated. The loopback rule in `pg_hba.conf` is `trust`, so **no
password was ever checked** and the test proved nothing. The same shape as a
permission-denied test that passes because the SQL was invalid: **a check that
cannot fail is worse than no check, because it is trusted.** The real test has to
come from a different container, over the network, and that is where the answer
finally became `FATAL: password authentication failed`.

## Phase 6f — "What did you create for Grafana, and is it running?"

**Expected:** a two-paragraph answer.

**What happened:** the answer was "yes, and it works", reached after twenty
minutes of chasing a **401 that was not a datasource fault**, and one wrong
guess about Grafana's provisioning log that cost most of them.

**Learned:**

- **Proved it properly, by wiping the volume.** `admin` / `replace-me` — the pair
  in `.env` — returned `401`, and `admin` / `admin` returned `200`, because I had
  reset it during the previous ten minutes of chasing. So the answer to "is the
  password coming from `.env`" was **no**, and the evidence is that
  `GF_SECURITY_ADMIN_PASSWORD=replace-me` *is* present in the container
  (`docker inspect` shows it) and Grafana **discards it**.

  The reason is one file: `/var/lib/grafana/grafana.db`. The variable is only read
  when Grafana *creates* the admin user. After that the password is a row in that
  database, and no environment variable will change it — which means the
  documented flow (set it in `.env`, `docker compose up`) works perfectly on a
  fresh clone and **cannot ever work twice on the same volume.**

  Which is the cleanest statement of the defect I have: it is not that the
  documentation is wrong, it is that **it is only true once.** A reader who
  follows it, changes the password, and cannot log in has no way to learn why,
  because the instruction they followed was correct and the outcome is not.

  The fix is to wipe the volume, which costs nothing here **because every bit of
  Grafana's state is provisioned from files in git** — and wiping it is also the
  honest test of the "a fresh clone has a working datasource" claim. It is:

      datasources: 1  TimescaleDB  uid=wwtp-postgres  url='db:5432'
                    health: {"message":"Database Connection OK","status":"OK"}
      dashboards:  2  WWTP — discharge permit, WWTP — overview, folder WWTP

  with no manual step of any kind. So the claim is now demonstrated rather than
  asserted, which is the standard this whole review has been holding.

- **And the symptom pointed the wrong way, which is the part that costs time.**
  `401` on every API call reads as *wrong password*. It was *stale password*. The
  difference is one `docker volume rm`, and nothing in the response says which.

- **`GRAFANA_ADMIN_PASSWORD` only applies when Grafana creates its admin user.**
  Afterwards the password lives in Grafana's own database and the environment
  variable is **ignored**. So changing it in `.env` and running
  `docker compose up` produces a Grafana you cannot log into, and every symptom
  points the wrong way: `401` on every API call, which reads as "wrong password"
  rather than "stale password". `GETTING-STARTED.md` said to set the variable and
  said nothing else, which is the whole gap.

  The recovery is `grafana cli admin reset-admin-password`, and the better check
  is to wipe the volume — **every bit of Grafana's state here is provisioned from
  files in git**, so a fresh volume is the honest test of the "a fresh clone has a
  working datasource" claim.

- **Grafana's provisioning log is quiet, and I read its silence as a fault.** At
  boot it prints

      starting to provision dashboards
      finished to provision dashboards

  **whether or not it inserts anything**, and the per-file insert lines are
  `level=debug`, off by default. There was also no `provisioning.datasource` line
  in nine hours of log. Both look like failures and neither is one; I was about
  to conclude the dashboards were not provisioned when they were, twice.

  The lesson is specific and I have written it down: **to check whether
  provisioning happened, ask Grafana.** `GETTING-STARTED.md` now has the
  `api/search` command next to the log output it is easy to misread.

- **`${POSTGRES_HOST}` in a datasource provisioning file *is* interpolated.** I was
  fairly sure it was not — Grafana's documented form is `$__env{VAR}` — and was
  about to "fix" a datasource whose `url` came back as the literal
  `${POSTGRES_HOST}`. It came back as `db:5432`. **Nearly-fixed is still a
  regression**, and the cost of finding out was one API call I should have made
  first.

- **And I made the project's own headline mistake, in the project's own
  dashboard.** Checking the datasource with a hand-written query:

      db query error: pq: column "value" does not exist

  `reading_1h` has `mean`, not `value`. That is **thread 22** — a valid query
  against a valid table, failing loudly, and the generated dashboards do not have
  it because the generator and its tests know the column names. Written from
  memory, in thirty seconds, by the person who wrote the post-mortem.

  The redeeming detail is that it failed **loudly**. The quiet version of this bug
  is a green compliance panel, and the loud version cost me one query.

## Phase 6g — The reader was looking at a different application

**Expected:** nothing. I had just verified both dashboards and the datasource
through Grafana's own API.

**What happened:** the reader logged into `localhost:3000`, which is **another
project of theirs**, and reported that Grafana had created no datasource and no
dashboards.

**Learned:**

- **Port 3000 is a `next-server` v16.3.5 belonging to
  `HAPI-FHIR-JPA-Server-Starter`.** It returned **HTTP 200**, it had a **login
  page**, and it had no datasource and no dashboards. Which is *exactly* what
  "Grafana is broken" looks like when you are looking at the wrong application.

  The mechanism is dull and worth recording anyway: `.env.example` ships
  `GRAFANA_PORT=3000`, that default is *correct*, and I had set
  `GRAFANA_PORT=3002` in this machine's `.env` after finding 3000 taken. So the
  documentation said 3000, the container listened on 3002, and a reader who
  trusted the document went to an unrelated dev server. **A default that is
  usually right is wrong exactly when the machine is busy, and that is the only
  time anyone needs the document.**

- **The fix is to stop printing ports, not to print the right one.** Markdown
  cannot interpolate a shell variable into a URL, so the guide now says *find it*:

      docker compose port grafana 3000

  and every runnable `curl` in the two follow-along documents became
  self-substituting — `"http://127.0.0.1:${WEB_PORT:-3001}/api/health"`. Same
  trick the Grafana prose already used, still pasteable (no trailing comment, the
  rule from two commits ago), and **always right**.

- **And `make dashboards` was a no-op, correctly.** `git status --short
  ui/grafana/` came back empty, meaning the regenerated files are byte-identical to
  the committed ones. That is the drift gate working: the dashboards in git already
  matched `contracts/tags.yaml`. Had a threshold been changed in the contract, the
  files would differ there and Grafana would pick them up inside its 30-second
  rescan.

  Worth stating because the reader reasonably expected running the generator to
  make something appear in the UI. **The generator writes files; Grafana reads
  them at boot and every 30 seconds after.** Neither step is "log in and it is
  there", and conflating them is what made this look like a failure.

- **A test, and it needed narrowing twice.**

  `test_the_guide_never_hardcodes_a_port_a_reader_must_visit` first flagged the
  paragraph that *explains* the mistake, because it contains the URL I got wrong.
  Forcing the correction to be deleted is a trade this project has now refused
  five times, so the rule became: **instruction-shaped lines only** — `On <http…>`
  or a line starting with `curl`/`open`/`wget`. Prose that mentions a URL is not
  an instruction.

  Then it flagged `curl -s http://127.0.0.1:3001/api/health`, which was a *correct*
  catch — a copy-pasteable command with a number that moves — and led to the
  self-substituting form above.

  It also flagged its own correction note a third time, via a *quote-pairing* bug:
  `_claims_only` strips `"…"` with a regex, and one unbalanced quote earlier in
  the file shifts every subsequent pairing, so a later quoted number survives the
  strip. **A regex over prose is a parser with no error handling**, and this is
  the third time that has cost time. The fix was to reword the sentence rather than
  the stripper, because a document that has to be worded to satisfy a regex is a
  document shaped by its tests.

## Phase 6h — "The SQL files should have a `.sql` extension"

**Expected:** an argument about file extensions.

**What happened:** the extension was right and the observation was pointing at a
real gap three questions away, and building the right thing found four defects —
one of them in the file I had written two minutes earlier.

**Learned:**

- **The lessons are `.md` and always will be.** 109 fenced blocks, **19,000 words
  of prose teaching them**. `02-01_ctes.md` is a thousand words of explanation
  with five queries in it, and the explanation *is* the course. The line that
  settled it is in `check_sql.py`:

      The line number is what makes a failure report useful.
      "One lesson has a bad query" is not actionable; "01-04 line 61" is.

  Loose `.sql` files could only report *a file*, and a course has many queries per
  lesson.

- **And 64 of 117 blocks run.** The other 53 are fragments, `<placeholder>`
  values, or queries *meant* to come back empty. A folder of 53 files that fail
  when opened is worse than no folder. So `sql/TablePlus/` holds the 64, and each
  carries the line number so a failure in a SQL client maps back to a place in a
  lesson.

- **The generator produced 62 files while the runner reported 64, and nothing
  noticed.** The two runnable queries in index pages (`sql/README.md`,
  `01-beginner/README.md`) were skipped by a `README.md` guard. A generator that
  quietly drops two is worse than one that is short, so the two counts are now
  asserted equal.

- **Naming was "nearest heading above", and the first version compared titles
  alphabetically.** It emitted two different files both called
  `01-the-question.sql` for two different queries. Only offsets mean "nearest".

- **My own README became a 65th runnable query.** `sql/TablePlus/README.md` lives
  inside the course and contains ```` ```sql ```` fences, so the generator extracted
  its own documentation. Caught by
  `test_the_tableplus_readme_does_not_become_a_query` on its first run, then
  **again** when I added the corrected version with a fresh unmarked fence. The
  generator was right twice and the document was wrong twice, which is the correct
  ratio for a rule this easy to forget.

- **The header had no blank line before the query, so "the body is the lesson's
  query byte for byte" had nothing to split on** and reported an empty body for
  every file. Valid SQL — a `--` comment ends at the newline — and useless for
  the check that is supposed to prove the copy is faithful.

- **One of the 64 is an `INSERT`, and I had recommended a read-only credential.**
  `00-03_quality_is_data/02-writing-some.sql` writes three readings — one `Good`,
  one `Uncertain`, one `Bad` — because `quality` is a per-reading column and the
  only honest way to show what its values mean is to write some. My test called
  `fetchall()` and reported it as a failure; the README had already told the reader
  to use `wwtp_ui`. **A third of a SQL course is not `SELECT`**, and both the test
  and the advice assumed it was. The README now says 63 of 64 are reads, and says
  what to do about the one that is not.

- **And the generated directory was being scanned by the thing that generates it.**
  Adding `sql/TablePlus/README.md` made the course 22 files instead of 21, which
  broke two count assertions. The fix is not to keep the counts in step — it is
  for `check_sql.py` to exclude its own output. **A generator whose output is
  scanned by its source is a cycle, and the answer is always to exclude the
  output.**

## Phase 0–1a — Contract and scan loop

**Expected:** a YAML contract and a PLC-shaped loop. Two days.

**What happened:** the contract was quick. The loop was quick. The process model
was where the time went, and almost all of it went into dimensional analysis.

**Learned:**

- A *series* of errors came from unit confusion, not logic. Oxygen uptake computed
  in mg/L·d⁻¹ but consumed as mg/L·h⁻¹. A waste rate that divided the reactor
  inventory by concentration *twice*, giving a realised SRT of 5.6 d against a
  commanded 15 d — while the MLSS gauge read healthy, because the gauge reads
  concentration and the error was in mass. These are invisible in code review
  and obvious the moment units are written out.
- Oxygen content of air is 1.33 kg/m³. The figure 0.232 is roughly the *volume*
  fraction; using it as a mass content made air demand 5× high and left the basin
  permanently oxygen-starved.
- **Balancing oxygen is not controlling dissolved oxygen.** Sizing airflow so
  delivered equals consumed is indifferent to the DO *level* — it will hold 8 mg/L
  while the controller believes it holds 2. Only the driving force `KLa(C* − C)`
  fixes the level. This one cost several iterations and is the single most
  useful thing learned in the project.
- Driving oxygen transfer from *both* a KLa driving force and an SOTE mass
  balance double-counts it; the two disagree by ~100×. KLa and SOTE are
  independent empirical quantities related through tank geometry, not two
  descriptions of one number.
- A solids balance on activated sludge **cannot** close on solids alone: organic
  matter converted to biomass leaves as more WAS than arrived as suspended
  solids. Growth is a source term, not an error.
- Nitrification was inverted: reduced nitrifier activity drove effluent ammonia
  *down*, so the plant looked better the harder it failed — and would have passed
  a compliance check while doing it. Also, capacity was ~50× too high, which
  hid the same thing behind a plant that nitrified no matter what.
- Primary clarifier removal fractions were applied as pass-through multipliers,
  sending 2 % of ammonia to the reactor. The *fraction removed* is not the
  *fraction that passes*.
- A blanket deepens when solids are captured faster than the scraper removes
  them. Reducing capture makes it *shallower*, because less sludge arrives to
  accumulate. The intuitive fault injection was backwards.
- Two clarifier blankets could go negative. An unclamped value reads as physics
  until you know what the physical range is.

**Cost:** roughly two-thirds of the elapsed time on this project, and every
single real bug was found by a test or a balance, never by reading the code.

---

## Phase 1b — Timestep continuity

**Expected:** run at several `dt` and get similar answers.

**What happened:** the plant ran at 1 s and collapsed at 60 s. Aeration went to
zero, nitrification died, contact time read 900 h.

**Learned:**

- The lift station was the cause, not the aeration. With fixed-speed pumps sized
  so one pump roughly equalled average flow, the wet well had **no stable
  equilibrium**: it filled, overshot, emptied, cycled. Worse, the *amplitude
  scaled with the timestep* — a property no real plant has, and it made every
  downstream number timestep-dependent.
- Variable-speed pumps with a time-constant level controller fixed it. This is
  what real plants do for exactly this reason. The wet well then held its band
  across a 60× range of `dt` with no overflow.
- A 120-hour run caught a regression that a 24-hour test missed: lowering the COD
  floor to make the SRT signal visible starved the nitrifier carbon factor, so
  effluent ammonia crept from 0.25 to 6.10 mg/L on a plant working perfectly. The
  factor was reading the effluent residue; a clean effluent means the
  heterotrophs won, not that carbon ran out.
- **Run the long test.** The processes here have time constants of hours. A test
  that settles for 20 minutes will pass on a model that drifts.

---

## Phase 2 — Fault library

**Learned:**

- Sensor faults must corrupt only the *published* value. Applied to the process
  they are undetectable by definition — the process *is* wrong. Applied to the
  reporting layer they produce exactly what a historian must survive.
- A drifting probe that stays inside its normal band is the case that justifies
  model-based detection. The first draft used a bias large enough to trip a
  high alarm, which quietly made the "undetectable by threshold" claim false.
- The blower scenario is the most valuable output: air pins at full remaining
  capacity, DO decays over one to two hours (limited by the basin's ~40 kg DO
  inventory, not by biology), and effluent ammonia follows two to six hours
  after. **An operator alarming on ammonia gets about three hours less warning.**
  The test asserts the *ordering* of the two responses, because that is what an
  alarm design is judged on.
- The compound scenario is the honest stress test: a drifting DO probe over a
  blower trip reports 0.70 while the true value is 0.00. The instrument masks
  the severity of the fault it should reveal.

---

## Phase 3 — Protocols

**Learned:**

- `pymodbus` 3.15 replaced `ModbusSlaveContext` with `ModbusDeviceContext`,
  removed the sequential block's `getValues`/`setValues`, and made
  `ModbusServerContext` serve only `ModbusSimulatorContext`. Detaching the
  register model from the library was what made this survivable. The version is
  now pinned to 3.6.6.
- With a non-zero base address, `ModbusSequentialDataBlock.getValues(0, n)`
  returns an **empty list** rather than an error, so every register silently
  read as zero. Found by a test that read a published value back, not by
  reading the source.
- `StartTcpServer` blocks forever with no shutdown hook. It must be a **daemon
  thread**: a non-daemon thread hangs the interpreter on exit, and an asyncio
  task cannot cancel it because the work has already left the event loop.
- **Two address spaces, not one.** The datastore's own `getValues` and the
  Modbus PDU addressing disagree, and both differ from the contract's 4xxxx
  convention. Anything verified through the wrong one looks correct and is not.
  Every check now goes through a real client.
- A measurement against a block populated via the *constructor* does not
  generalise to one populated via `setValues`. "The value looked about right"
  never settles an addressing question; a unique ramp in the block does.
- Building the OPC UA address space in two passes put variables in a folder
  named after the area and left equipment objects as empty siblings. Every path
  a client constructs from the contract resolved to the wrong node — exactly the
  failure OPC UA exists to prevent, caused by not modelling the hierarchy.
- `get_child("Name")` resolves a browse name in **namespace 0**, so a property
  created in the plant namespace reports `BadNoMatch`, which reads like a
  missing property rather than a naming error.
- `publish()` counted changed nodes and never wrote them. A silent no-op
  indistinguishable from a dead subscription.

---

## Phase 2b — the first runnable process

The soft PLC (`softplc/main.py`) is the first time the pieces met each other, and
the first time the interesting bugs showed up. None of them were in the
components; every one was in the seams.

- **A server is bound to the event loop it was created on.** The OPC UA address
  space holds state tied to its loop, so `asyncio.run(plc.start())` followed by
  talking to it from a second loop times out — and the log says *connection
  refused/timeout*, which sends you hunting for a network fault that does not
  exist. One long-lived loop on its own thread, driven with
  `run_coroutine_threadsafe`. The Modbus listener still needs a second thread,
  because pymodbus's `StartTcpServer` has no asynchronous form.
- **Staging is not publishing.** `set_value` records a change; the address space
  only moves when `publish()` runs. Forgot, and a connected client watched a
  frozen plant that looked perfectly healthy — the most convincing possible
  wrong answer. Unit tests of the server passed throughout, because the server
  was never asked to do this.
- **A control program that never runs looks exactly like one that works.** The
  blocks were originally invoked beside the scan loop rather than through it,
  so the phase metrics — the only evidence they executed at all — were empty.
  They now run through `ScanLoop.scan_once()`.
- **`--duration` waited on a signal forever.** A bounded smoke test had to be
  killed by hand, and a test you have to kill is a test nobody runs. Shutdown now
  waits on whichever comes first.
- **The alarm signature is an ordering, not a threshold.** Measured on the
  `aeration_loss` scenario: at 2 h DO is 1.73 mg/L while effluent ammonia is
  still 0.96 mg/L; at 6 h DO has collapsed and effluent ammonia is 4.17 mg/L.
  The basin holds ~40 kg of oxygen, so air must be lost long before anyone is
  endangered. The end-to-end test asserts that lag, because an alarm on ammonia
  would have blamed the wrong unit.
- Two PLCs in one test session need their own ports. `Errno 48` is a real
  constraint, not a flake to retry.

## Phase 3a — the deadband and the spool

Two small components, and between them they produced five real bugs. All five
were found by tests that asserted a *property* rather than a case, which is
worth something on its own.

**The deadband**

- `BandRule.for_signal` read `signal.min` and `signal.max`. The field is called
  `range_min`/`range_max`, and `getattr(..., None)` returned `None` for every
  signal in the contract — so every span was `0.0` and relative mode was
  **silently dead across all 57 signals**. A component whose entire job is to
  filter, filtering nothing, with no error anywhere. `getattr` with a default is
  the worst way to read a field: it converts a typo into a silent wrong answer
  instead of an `AttributeError`.
- The baseline is the last **published** value, not the last **seen** one. I had
  written two tests asserting the opposite intuition (that cumulative distance
  matters), and both were wrong. The property that actually matters: a suppressed
  reading must not become the reference, or a signal drifting steadily at 0.6
  with a band of 1.0 never publishes — not at 0.6 per scan, not per hour. It
  simply vanishes while the plant keeps reporting it.
- A deadband of `0.0` does **not** mean "publish everything". It means "publish
  any change at all", which still drops a signal that is sitting still. I had
  assumed otherwise in a test. Worth knowing before calling 0.0 a safe default.
- `deadband_mode` is now validated in the contract loader rather than in the
  gateway. A typo like `relitive` is a startup failure with a line number,
  instead of a runtime surprise in the one component that reads it — which is
  also the component furthest from the file containing the mistake.

**The spool**

- The size cap was checked against the **current file's** size, not the spool's.
  Nine hour-files reached 2.8 MB under a 1 MB limit. A limit that looks
  enforced and bounds nothing is the worst way for a safety limit to fail.
- `st_size` cannot see Python's write buffer, so `refresh()` reported ~130 KB
  less than had been written — and *shrank* the running total in the process.
- A deleted file was credited back its `st_size` while the total had been
  incremented by the logical size. The remainder stayed counted forever, and the
  cap crept upwards a buffer at a time.
- Each of those three passed every smaller test. The test that caught all three
  asserts one property — *the total never exceeds the limit* — over enough hours
  to matter, because three separate near-misses all look fine in isolation.
- Two of my own tests were wrong before the code was. `drain()` correctly
  excludes the hour still being written, and I had written two tests expecting it
  not to. The component was right; my model of it was not. Worth saying plainly,
  because the instinct when a test fails is to change the test, and that instinct
  is wrong exactly when the test was the thing that was wrong.

## Phase 3b — the InfluxDB schema, and the bug it hid

The line-protocol encoder is small. Writing the tests for it took far longer than
writing it, and the reason is instructive: the first version of the schema was
wrong in a way that only a test looking for *collision* would find.

- **Six signals, one series.** I keyed the series on (area, equipment,
  measurement, eu). But the contract's `measurement` is a *group* — every
  aeration signal is `measurement: aeration` — so `AERATION:AHU-1:DO`,
  `SETPOINT_DO`, `NH4_IN`, `NH4_OUT`, `NO3_OUT` and `MLSS` all resolved to the
  *same* series key. InfluxDB identifies a field by series + field + timestamp,
  so those six would have written to the same field at the same timestamps and
  overwritten each other. No error, no warning, and a chart that looks entirely
  plausible because five of the six had silently vanished.
  The fix is the project's own rule, applied properly: `field` (`do_mg_l`,
  `nh4_out_mg_l`) is the identity of a signal and belongs in the tags.
  `measurement` is a grouping for querying, not an identity.
- **The measurement is now the stage**, so `SELECT mean(value) FROM aeration`
  is one scan over one part of the plant, rather than a UNION of eleven
  measurements. A measurement-per-signal — what most tutorials do — is the
  mistake here, and it is the mistake the tag design exists to prevent.
- **Two implementations of one specification is worth it.** The encoder and the
  test-only decoder disagreed until escape handling was made symmetric: the
  decoder split on the first literal space, and an escaped `\ ` inside a tag
  value *is* a literal space. The result parsed into a right measurement with
  nonsense tags and no error at all. Comparing against a hardcoded expected
  string would not have found it; a second implementation written from the
  specification did, immediately.
- Line protocol has no escaping of its own, so escaping has to be total. Spaces
  matter as much as commas: an unescaped space ends the tags section and the
  remainder is parsed as a field.

## Phase 3c — the gateway's two protocol readers

Written against a live plant rather than a mock, which is the only reason four
bugs showed up. All four produced *plausible numbers* rather than errors.

- **The Modbus plan indexed registers by position, not by offset.** The plan
  stored register names and worked out where each sat from its index in the
  list. That is correct exactly when every register in a window is the same
  width — and this contract mixes float32 pairs with single-word integers. The
  fifth float in a window sits at offset 8, not 4. The client read the wrong
  register and reported a confident `7.4e14`. The plan now stores `(name,
  offset)`. The lesson generalises: any index derived from *position* rather
  than from the thing's own address is a bug waiting for the first irregular
  element.
- **The OPC UA client hardcoded the plant's browse name as `PLANT-A`.** It is
  actually `PLANT-A: Northgate Water Reclamation Facility`. All 57 lookups
  failed and the reader reported "connected, zero signals", which looks exactly
  like a plant with no instruments. The reader now *finds* the plant object by
  the site id from the contract. A browse name in a client is a second copy of a
  fact the contract owns.
- **Browse names are `sig.field` verbatim.** The client lower-cased them, which
  looks right, matches the server's own `path` construction at a glance, and
  resolves nothing.
- **A stale `parts[2:]` slice.** After removing two leading path elements, the
  slice offset was left behind, so the reader asked for the leaf and got
  `BadNoMatch` 57 times. Slicing a list you have just edited is the sort of thing
  that reads as obviously-correct code. Passing the list whole makes the mistake
  impossible.
- The end-to-end test could not link a Modbus register to the signals behind it,
  because the contract does not. I had written a heuristic that string-matched
  equipment names — a second, wrong mapping, of exactly the kind that hides a
  real bug. The test now states the mapping explicitly for four registers and
  uses OPC UA for the full-path leg, where signal ids arrive natively. The gap
  is still a gap; it is now visible rather than papered over.

## Phase 3d — running the database, and everything it got wrong

Every storage module in this project was unit-tested with an injected callable.
That is the right design, and it is also why the SQL text, the SDK calls and the
schema assumptions had **never been executed**. They all passed. Six of them were
wrong. This is the most valuable section of this log, and none of it could have
been found by reading documentation, because the documentation describes
InfluxDB 1.x and 2.x.

### The bugs

1. **The write API is asynchronous, and drops the last batch silently.** Two
   writes each returned `True` and neither point was in the database. The spool is
   then deleted on the strength of that success, so the data is lost twice over.
   The SDK logs one line — *"cannot schedule new futures after interpreter
   shutdown"* — and the caller has already been told everything is fine. Fixed
   with `SYNCHRONOUS`. **This is the worst bug in the project and it was invisible
   to every test.**

2. **At most two fields per point.** The schema had three (`value`, `quality`,
   `source`). The error is *"Could not parse entire line. Found trailing
   content"*, which reads like a typo in a line protocol rather than a schema
   limit. `source` became a tag.

3. **A table's tag set and column types are fixed by its first write.** InfluxDB
   2.x let any point carry any tags. InfluxDB 3 tables have a schema, so every
   signal must write the *same six tag keys*, differing only in values.

4. **`field` is a reserved word in InfluxQL.** The tag that fixed the series
   collision was named `field`, and `SELECT field, value FROM aeration` fails with
   *"invalid SELECT statement, expected field at pos 7"*. Quoting works, so the
   alternative was a column called `"field"` in every lesson of the `sql/` track.
   Renamed to `signal`.

5. **There is no way to write a non-finite value** — and that turned out to be the
   database being *right*. `NaN`, `+Inf` and a quoted `"nan"` are all refused,
   because the column's type is fixed on first write. What *is* writable is a
   point with a quality and **no value**, and it lands as `value` NULL with
   `quality = 2`.

   That is the representation this project has argued for since Phase 1, and the
   database will not accept the alternatives. A failed instrument is stored as
   *present and invalid* — distinguishable both from "no data at all" and from "a
   number that happens to be wrong". InfluxDB 2.x would have accepted a string in
   a float column, or a NaN that plots as a gap and reads as a process that
   stopped. **The refusal is the feature**, and it is the single best argument
   this project has for the database it chose.

6. **A write that adds a column to an existing table is accepted, and makes the
   table permanently unreadable.** Success response, then every read fails with
   *"column types must match schema types, expected Float64 but found
   Dictionary(Int32, Utf8)"*. Marked `xfail`: it reproduced twice by hand and in
   the suite, but the trigger is order-dependent, so a test asserting it would be
   asserting a race. The mitigation needs no understanding of the trigger — the
   encoder emits exactly two fields and cannot be made to emit a third.

### Things that only running it could tell you

- `DOCKER_INFLUXDB_INIT_*` is InfluxDB **1.x/2.x**. InfluxDB 3 has no setup mode;
  the database must be started with `--host-id`, an object store and a bearer
  token, and initialised with `influxdb3 create database` / `create token`.
- InfluxDB 3 is on **Quay, not Docker Hub**, and publishes **no semver tags** —
  17 tags, all `latest` or commit SHAs. So the "pin by tag, it is more readable"
  advice in `docs/SECURITY.md` is impossible here. That inverted my own argument,
  which is the useful part.
- It serves on **8181**, not 8086.
- The official `influxdb_client` SDK **cannot query it**. It posts to
  `/api/v2/query`, which InfluxDB 3 does not serve; the answer is 404 with a
  message about the endpoint, so it looks like a wrong URL. SQL lives at
  `GET /query?q=...&db=...`.
- `ORDER BY` accepts `time` and nothing else.
- A `WHERE time >= '4000000000000'` filter is rejected: *"is not a valid
  timestamp"*. Timestamps in a filter must be RFC 3339, while timestamps in a
  write are integers. The two forms are not interchangeable and nothing says so.

### The conclusion, and it is not a comfortable one

Query results on this **InfluxDB 3 Core** build are **not deterministic**.
`WHERE quality >= 1` returned rows on one call and nothing on the next, with no
writes in between; `WHERE signal = 'do_mg_l'` — the most basic query this project
could ask — returned nothing while the rows were demonstrably present.

So two integration tests are `xfail` with that reason, and the recommendation is
recorded rather than hidden: **InfluxDB 3 Enterprise, with the home-use licence
key the project already assumes, is the path forward.** The schema work above
transfers unchanged — it is all InfluxDB 3, and Core is the same engine. What
Enterprise buys is a query engine that returns the same answer twice.

The eight stable integration tests stay: synchronous writes, the NULL-for-broken
sensor representation, the two-field limit, the fixed tag set, one table per
process stage, and the round trip through real SQL. Those are the facts the schema
depends on, and they are verified.

## Phase 3e — Couchbase, and the assumption that was not worth making

The Couchbase half had a running container and **zero executed SDK calls** for its
entire life. I wrote down that this was the same position the InfluxDB half was in
an hour before it hid six bugs, and that the assumption it would be fine was not
worth making. It was not fine.

### Four bugs in `storage/couchbase/client.py`

1. `ClusterOptions(username=…, password=…)` no longer exists. It requires an
   `authenticator`, and the old keywords raise `missing 1 required positional
   argument: 'authenticator'`.
2. `wait_until_ready(timeout=30)` raises `'int' object has no attribute
   'total_seconds'`. It wants a `timedelta`.
3. `couchbase.queries` is not a module. `QueryOptions` is in `couchbase.options`.
4. **The default collection name did not exist.** All 80 documents failed with
   `AmbiguousTimeoutException` whose `retry_reasons` was
   `{'key_value_collection_outdated'}` — a message that never mentions
   collections, scopes, or the fact that the name asked for is not there. It reads
   like a network timeout, and the SDK spends its retry budget before the caller
   sees anything.

   Fixed by writing to the scope's `_default` collection, which always exists.
   That is not a concession: the keys are already namespaced by kind
   (`equipment::`, `tag::`, `site::`, `event::`) and every document carries
   `doc_type`, so a second level of collection names would be redundant.

**Every one of the four failed loudly.** A wrapper written from memory is a wrapper
that has not been run, and the saving grace was that the SDK named what was wrong
in each case. Worth remembering on the days when a silent failure would have been
much more expensive.

### Two compose bugs, and both would have stopped the stack

- **The healthcheck was unauthenticated.** `/pools/default` answers **200** to an
  authenticated request and **401** to an unauthenticated one, so
  `curl -fsS http://localhost:8091/pools/default` fails forever on a perfectly
  healthy cluster. Because the gateway waits on `service_healthy`, a cosmetic bug
  in a healthcheck became a stack that never starts. Now authenticated, and
  pointed at `/nodes/self` — `/pools/default` also answers the *string*
  `"unknown pool"` before the cluster exists, which is not an HTTP error and so
  reads as success, making it useless as a readiness signal.
- **Nothing initialised the cluster.** On a fresh volume the server starts and
  every query fails, because the cluster and the bucket do not exist. The first
  attempt put a `cluster-init` script in the couchbase container's `command`, which
  **replaces the image's entrypoint** — and the entrypoint *is* the server, so the
  container sat waiting forever for a node it had never launched. Correct shape is
  a separate one-shot `couchbase-init` service, with the writers depending on
  `service_completed_successfully`.

  Getting even that right took four attempts, because the CLI wants **different
  flags per subcommand** — `cluster-init` takes `--cluster-username`/
  `--cluster-password`, `bucket-*` takes `--username`/`--password`, and `-u`/`-p`
  are deprecated aliases of the *latter*, so they are the wrong thing to reach for
  first. The credential flags are per-subcommand, not global. And
  `cluster-init` defaults to `http://127.0.0.1:8091`, which inside a separate
  container is its own loopback and nothing else.

  The init service ends by *proving* the bucket exists and exiting non-zero if it
  does not, because a one-shot init that prints an error and exits 0 is worse than
  one that fails loudly. It caught its own bug on the first run.

### N1QL reads: two more causes, and neither was the code

For a long while every query failed — `cluster.query(...)` returned successfully
and then iterating `.rows()` raised for *every* statement, including
`SELECT COUNT(*) FROM bucket`. I marked it `xfail` and moved on. It was worth
another look, because I had assumed the environment was at fault, and the
assumption turned out to be wrong twice.

1. **The Query service was never enabled.** `cluster-init` with no `--services`
   brings the cluster up with `kv` alone. Community Edition accepts exactly three
   service combinations, and the error message helpfully lists them: `data`,
   `query,data,index`, or `query,fts,data,index`. The project's compose file was
   doing the first thing, so **N1QL could never have worked in this stack at all.**

2. **Port 8093 was never published.** This one has a long fuse. The SDK reaches
   the KV service on 11210, so every `get` and `upsert` succeeds and the stack
   looks perfectly healthy. The cluster topology then dispatches each query to
   **8093**, which the host cannot see, and the query fails after the SDK's entire
   retry budget with *"Streaming operation failed"* — a message that mentions
   neither the network nor ports. **Only a read ever fails.** A write-only test
   suite would never have found it, and neither would a healthcheck.

Both are fixed in `compose.yaml`, and the seven passing tests now include the
N1QL read path. Neither cause was in the project's code, which is the useful part:
the environment was not broken, the *configuration* was, and only a read that
actually ran could tell the difference.

A related trap on the way: immediately after a bulk seed a query needs
`scan_consistency="request_plus"`. The default sees neither the new documents nor
the new index, and fails with a `ServiceUnavailableException` that never mentions
indexing.

### A test that was wrong in an instructive way

The read tests first failed with *two* rows for one tag. The second was a
leftover document under a test-only key from an earlier run — the test had
prefixed its keys to avoid collisions, and then queried by the ``id`` **field**,
which is not the key.

The fix was to query by key, which is the production pattern anyway: a tag read
out of InfluxDB already carries its identity, and the key *is* the identity. But
the underlying point is worth keeping: **`id` is a field, so querying by it can
return one row per document that merely shares the value.** A document store where
the key and the id field can disagree is a document store that will eventually be
asked a question it answers wrongly.

Also: `SELECT META().id AS key` is a parse error. `key` is reserved.

### The app user: four guesses, then one query

The gateway authenticates as a bucket-scoped application user, and creating it is
part of initialisation. Four plausible spellings were all rejected:

    --roles=wwtp              -> unknown, malformed or role parameters are undefined
    --roles=bucket_admin:wwtp -> the same
    --roles=wwtp[wwtp]        -> the same
    --roles=manage_bucket:wwtp-> the same

I stopped guessing and asked the cluster what roles it actually has:

    [c.role.name for c in r.users().get_roles()]   -> admin, ro_admin, bucket_full_access

Three roles, and none of them is any of the four I had tried. Assignment also uses
**bracket** syntax rather than a colon, which nothing in the error messages hints
at:

    bucket_full_access[wwtp]   SUCCESS
    bucket_full_access         ERROR: unknown role

**The lesson is the one I keep relearning in this project: when four plausible
guesses all fail, the answer is not a fifth guess, it is a different question.**
Every other bug in this log came from a wrong assumption about a system I had not
asked anything. Here, the system could be asked, in one line, and the four
failures had been the cost of not asking sooner.

The scoped user is now what the tests run against, and the two properties
`docs/SECURITY.md` claims are asserted rather than hoped for: writes to its own
bucket succeed, and `users().get_all_users()` is **denied**.

## Phase 3f — the SQL course, and how wrong it was

`sql/` was nine empty directories. It is now the foundations and beginner stages,
and the process of writing them found more about the dialect than a month of
reading would have.

### The course is checked, because a course of untested SQL is not a course

`tools/check_sql.py` extracts every ```sql block and runs it against a live
InfluxDB. It runs each query **five times**, because "this query is broken" and
"this database is having a bad minute" are indistinguishable from the outside —
both are an exception.

So outcomes are classified. A consistent *parse* error is a real dialect violation
and fails. Anything intermittent is reported and not failed, because a course that
cannot be checked is a course nobody checks.

### Six constructs I used that this dialect does not have

Every one of them is in every SQL-for-metrics tutorial, and every one is standard
SQL:

| Written | Error |
|---|---|
| `time_bucket(INTERVAL '1 hour', time)` | parsing error — no `INTERVAL`; use `1h` |
| `now() - INTERVAL '24 hours'` | parsing error — use `now() - 24h` |
| `max(CASE WHEN source='opcua' THEN value END)` | parsing error — **no `CASE` at all** |
| `ORDER BY signal` | invalid ORDER BY, expected TIME column |
| `signal IN ('a','b')` | parsing error — no `IN` |
| `HAVING count(value) >= 30` | parsing error — no `HAVING` |
| `WHERE time >= (SELECT max(time) …)` | invalid conditional expression |

**The absence of `CASE` is the one that reshapes the course.** The pivot idiom —
`mean(CASE WHEN signal='do_mg_l' THEN value END)` — is how every tutorial puts two
signals side by side in one row, and it is a parse error here. The dialect's answer
is **long format**: `GROUP BY time(10m), signal` returns one row per bucket per
signal, stacked. Which turns out to be the honest shape anyway, because that is how
the data is stored and the aggregates you want are all computed in it without any
reshaping. You reshape in the application.

`GROUP BY time(1h)` also needs a **tag** named alongside it, or it parses and then
fails at planning — one of the least helpful failure modes in the whole exercise,
because the statement is valid and the complaint is about the data.

Everything is written up in `sql/_shared/DIALECT.md`, which is now the most useful
file in `sql/`.

### The value of running it

Writing the lessons from the contract produced SQL that was *plausible, idiomatic
and wrong in six ways*. Not one of the six would have been found by reading
InfluxDB's documentation, because the documentation describes 1.x and 2.x.

And the two bugs in this section that were mine rather than the dialect's: a
`SELECT SELECT` left by a bulk string replacement, and a `SELECT`-by-field test
that returned two rows because a document from an earlier run shared an `id`. Both
were found by the checker and the integration suite respectively, which is the
argument for having both.

## Phase 3g — Leaving InfluxDB and Couchbase

**What was built.** The storage layer ported from InfluxDB 3 plus Couchbase to
one PostgreSQL 16 database with TimescaleDB 2.30. Four services instead of seven,
no licence key, and a `sql/02-intermediate` stage that was previously unwritable.

**What was expected.** A translation exercise. Replace the encoder, delete the
document writer, adjust the queries, done in an afternoon.

**What actually happened.** The port was a day. The interesting part was
afterwards, and it split into four things I did not expect.

### The justification for two databases ran backwards

The README said the project "stores its own history in two databases chosen for
two different jobs". I believed that and had written it.

Counting the shapes killed it: **80 documents, three key-sets, exactly one shape
each, zero heterogeneity.** A document store's value is flexibility across
heterogeneous documents, and there were none to be flexible about. It was a
relational dataset being stored as documents because a document store was
available.

Worse, the split had no other reason. The two stores could not be joined, and
*that* was the only reason there were two. The argument for the document store
was "these are genuinely different jobs", and the reason they were in different
jobs was the first decision — the time-series choice. **A design decision was
being defended with a justification derived from itself.**

**Cost of being wrong:** the entire `sql/02` stage, and one security property that
had been verified and worked — a bucket-scoped application user that could not
administer the cluster. See `docs/SECURITY.md`.

### A foreign key found a modelling error immediately

The first `INSERT` into the new `signal` table failed:

```
ERROR:  insert or update on table "signal" violates foreign key constraint
DETAIL:  Key (equipment_id)=(FLOW) is not present in table "equipment".
```

Fifteen signals were naming a holder — `FLOW`, `LIFT`, `SITE`, `WEATHER` — that
is not a piece of equipment, because `Signal.equipment` was doing two jobs at
once: *"the asset id"* and *"whatever the middle of the signal id happens to
be"*.

Nothing had complained for the whole life of the project, because **nothing could
have**: a tag said `equipment=FLOW` and there was no constraint anywhere capable
of saying there is no such asset. The two concepts are now `equipment`
(nullable) and `holder` (derived from the id), and 24 signals correctly have
`equipment = NULL`.

This is the strongest argument I have for the migration and it took ten minutes
to find. **A constraint found a modelling error that a year of querying had not.**

### The first design rule was reverse-engineered from a write rejection

*"Tag by identity, field by value"* was in the design document as a principle. It
is not a principle. It is what you deduce from InfluxDB 3 rejecting a write with
*"Detected a new tag in write"* — an implementation detail of one database,
promoted to a law and then defended as though it were about data.

Nobody noticed, because the rule was **correct**. It was correct for a reason that
had nothing to do with data modelling, and that is the dangerous shape: a rule
that is right for the wrong reason survives every challenge that tests its
conclusions rather than its premises.

### Four grants that were not obvious, and a stale image that hid all of them

`wwtp_writer` was granted `INSERT` on `reading` and nothing else. That is the
obvious minimum, it is what a person writes from a description, and it produces:

    ERROR:  permission denied for table reading

with no hint about what is missing. Three more privileges were needed and all
three were found by **running the stack**:

* `SELECT` — `ON CONFLICT DO UPDATE` reads the conflicting row to decide whether
  to update it.
* `UPDATE` — and then writes it. The one that looks like a mistake on a
  historian, and the gateway's re-send path genuinely needs it.
* `USAGE` on `event_id_seq` — `event.id` is `BIGSERIAL`.

And then a fourth problem, which is the one I would write down: **the container
image was stale.** I applied the grants to the database, re-ran `init-db`, and
the failure persisted, because `init-db` was running the *image's* copy of
`roles.py` from before the edit. I checked the grants by hand, saw them missing,
and spent several minutes on a privileges problem that was a build problem.

That is the **third** time a stale image has cost me time this session. The
lesson is not "remember to rebuild" — it is that **`docker compose run` does not
rebuild, and a container is a snapshot of the code as it was**, which is exactly
the kind of fact that feels like it should be impossible to get wrong and is not.

### Three bugs in my own new code, all the same shape

Every one was found by running the thing rather than by reading it.

**`COPY` and `INSERT` disagreed about a timestamp.**
`InvalidDatetimeFormat: invalid input syntax for type timestamp with time zone:
"1.0"`. A parameterised `INSERT` quietly coerces a float epoch; `COPY` parses its
input as text and refuses. One row type, one representation, both transports
agreeing — much cheaper to design out than to diagnose.

**A failed write became a denial-of-service tool aimed at the database.** A failed
flush keeps its rows (correct — the spool is the durable copy), so the buffer is
still over the row bound, so the next `add` triggers another flush. At one poll
per second over 57 signals: **57 doomed round trips per second, against a
database that is already unwell.** Found by a test I had written for a different
reason. Fixed with a retry deadline.

**The test suite deleted a week of seeded data.** The integration fixture read
`POSTGRES_TEST_DB` for its skip message and `POSTGRES_DB` for the connection, so
it connected to and truncated the real database while appearing to use a scratch
one. It failed as an *authentication error*, which is a wonderfully misleading
message for a port mistake.

That last one is now a guard, not a promise: the suite refuses to run against a
database holding more than 50 000 readings. And `tools/check_sql.py` was making
the same class of mistake in a different place — it executed the course's own
`INSERT` statements, so a lesson collided with its own primary key on run 2 and
was reported as a failing query. Every block now runs in a transaction that is
rolled back.

**The pattern across all three, and the reason I am writing it down:** a program
that runs SQL on somebody else's behalf needs a rollback, not a promise. Twice I
wrote the promise.

### And one thing about floating point that I did not expect at all

While writing `sql/01-03` I noticed `avg()` returning a different answer on
consecutive runs. Chased it down:

```
 6391.155254170624
 6391.155254170615
 6391.155254170614
 6391.1552541706105
```

Floating-point addition is not associative, so `sum()` depends on the order rows
are added in; Postgres aggregates in parallel by default and does not pin the
plan. That much is arithmetic and unremarkable.

**The part I did not expect is that the *order of the result* moves.** Two signals
whose daily means differ by 10⁻¹² produce two different orderings across fifteen
runs, from identical data, under `ORDER BY mean_value DESC`. A changed number is a
diff somebody investigates. A changed ranking reads as "the database is broken"
and is much harder to dismiss.

The fix is to round in the `ORDER BY` as well as the `SELECT`, and to tie-break on
a stable column. It is now lesson 01-03, and the course checker distinguishes
value jitter from order instability from real non-determinism, because treating
those three the same is what made the original tool useless.

**What it cost to find:** about ninety minutes, and only because I was writing an
example output by hand and the numbers did not match what I had written down.
Three other drafts of that lesson contained invented numbers as well. The checker
now runs every query in the course, which is the only reason those were caught
before anyone read them.


### And then I ran the actual stack, and found three more

Every entry above this one came from tests or from a single component. Bringing
up `docker compose up -d` and then *watching it* found three more bugs in about
twenty minutes. All three had been invisible to 384 passing tests.

**`docker compose up` could not start.** `ui/web` has a Dockerfile and no
`package.json`, because Phase 5 has not been written. The build died with
`failed to calculate cache key: "/package.json": not found`. This is the fourth
time this project has shipped a compose file that cannot start, and the third
time the cause was found by running it rather than by reading it. The service is
now behind a `ui` profile with a comment explaining that the Dockerfile is
correct and the app is missing.

**The gateway wrote nothing, and reported itself healthy.** The spool is designed
around hourly files, and `pending()` deliberately excludes the file currently
being written — so **nothing reached the database for up to an hour.** Worse, the
`rotate_s` parameter existed, was documented, was configurable, and was **never
read**: the rotation keyed on the wall-clock hour from the record's own
timestamp.

An hour of un-drainable data is invisible in every test that does not run a real
stack for an hour. It is also an hour of data lost to any crash, in the one
component whose entire job is not losing data. Rotation is now on elapsed time,
defaulting to 60 seconds, which is the project's maximum exposure and says so in
the constant's docstring.

**The plant ran 29 times too fast and starved its own Modbus server.** This is
the one worth reading twice.

`softplc/main.py` steps the loop with `scan_once()` — it has to, because it
interleaves physics, control and publishing around each scan — and therefore never
calls `ScanLoop.run()`, which is where the pacing lived. The line it used instead:

```python
await self._step(sim_dt)
# No extra pacing: the scan loop already accounts for its own period,
# and adding a second sleep would double-count the timing.
await asyncio.sleep(0)
```

`sleep(0)` yields; it does not wait. The comment was a plausible-sounding
rationalisation of a real bug, and **the comment is the lesson**. The PLC ran at
**1452 scans/second against a 50 Hz target**, pegged a core, and starved the
Modbus thread serving it so badly that the gateway's polls took **10.7 seconds**
and the link dropped after three failures. Every one of the 384 tests passed,
because every test drives `run()` or `scan_once()` and never the path that was
broken.

A comment asserting *where* a responsibility lives deserves exactly the same
scepticism as one asserting *what* it does. "The scan loop already accounts for
it" was a claim about a call graph, and nobody checked the call graph.

Two things came out of the fix that are worth having regardless:

* `ScanLoop.pace()` is now the single source of truth for the scan period, so
  every path that steps the loop paces itself the same way.
* It takes a `speed` argument, because `scan_ms` is a period in *plant* time and
  the sleep has to be in wall time. Without that, the backfill tests time out
  rather than failing a comparison — which is how the first fix broke
  `test_end_to_end.py` and why the division is now pinned by its own test.


## Phase 4 — the alarm engine, and the tool that told me it was wrong

**What was built.** Ten detectors, fifteen rules, a state machine, and a coverage
audit that runs the fault library through the plant model and reports a matrix of
fault against rule. `docs/ALARMS.md` is the write-up.

**What I expected.** Writing fifteen thresholds, and then — the part I had been
looking forward to — discovering that several of them caught the same fault.

**What happened.** The thresholds were not the problem. The *tool* was, and it
found four things in itself before it found anything in the rules.

### The report called six untested faults "blind spots"

I asked for three of the eleven faults. The report said *"8 of 11 blind spots"* —
because the other eight had no alarm transitions simply by never having been
simulated.

This is the worst kind of bug a report can have, because it is not wrong, it is
**confidently reporting a measurement that was never taken**, and the natural
response to "your rule set misses these eight faults" is to delete a rule. The
report now has an `evaluated` field and prints `untested` in the same column as
`YES` and `no`.

### Then it called six incidentally-caught faults "not blind spots"

I over-corrected. The first version said a fault was not a blind spot if *any*
rule fired on it, which felt like evidence. It is not:

    sludge_blanket_thickening   "found" by six rules that were not looking for it

Retune those six and the fault is invisible again. A coincidence standing in
front of a blind spot is still a blind spot, and the report now says so, with the
incidental catches listed separately as what they are.

### The transition log was discarding the evidence

The bounded transition list dropped from the front. The transitions it lost were
the `raised` records at the *start* of the run — so a coverage report over a long
run silently lost every fault detection and reported those faults as blind spots.

The bound is only safe if what it discards is the least valuable thing, and "a
repetition of an alarm already known to be active" is that by a wide margin. It
now evicts reaffirmations first.

### And the engine was emitting 9 992 identical events

The first full run: 9 994 of 10 000 transitions were reaffirmations of *three*
alarms, one every five seconds, for nine hours. Re-emission was gated on the value
having moved, and on a noisy signal it always has.

A rate limit alone would have been the wrong fix — an alarm whose value is
static would then never update, and the first message an operator sees is the one
with the least information in it. Both conditions now apply.

### Then the rules, which is the part I would have got wrong on my own

`aeration_do_sagging` fired on **ten of eleven faults**. Not miscalibrated by a
factor — measuring the wrong thing. A least-squares slope over a 30-minute window
of dissolved oxygen is dominated by sampling noise and by the plant's diurnal
cycle, both of which clear a 0.15 mg/L per hour threshold on a perfectly healthy
basin.

I did not fix it by picking a bigger number. I measured:

    healthy plant, 1 618 three-hour windows:  steepest fall -0.427, p5 -0.401
    blower trip,    same measurement:         p1 -0.797

and set the threshold at 0.6, above every healthy sample and below 99 % of the
fault's. Re-measured: it now fires on **one of eleven**, the one it exists for.
The fix is a `min_span_s` rather than a number, because the diurnal cycle cannot
fake a two-hour fall and noise cannot either.

**And the general lesson, which is the one I am keeping:**

> A rate needs a window long enough that the thing you are measuring is slower
> than the thing you are trying to exclude.

### And then: six rules fire on a healthy plant

Which is the most embarrassing thing in this phase, and the most useful. The
thresholds were written from engineering judgement and from the contract's normal
bands, and **neither of those is a measurement of what a healthy plant actually
does.** A rule built on a `range_max` inherits that range's optimism, and two of
the six fire because the plant routinely reaches or exceeds a value the contract
calls a maximum.

An alarm system whose rules fire on a healthy plant is worse than no alarm system,
because every alarm it raises is a false one. This is documented as a ratchet in
`test_a_healthy_plant_raises_almost_nothing` rather than quietly tuned away, and
it is the top item in `docs/ALARMS.md`.

### And the contract is wrong about `single_point_threshold`

Three times, the coverage report flagged a rule using a method the contract says
cannot find that fault — and the rule fired.

The rules are right. The contract's `NOT_detectable_by` reads as *"this method
cannot find this fault"* and means *"this method alone is not sufficient"*. A
threshold on digester pH finds souring only because souring is slow and has a
limit. Set a high ammonia load and pH is the wrong signal entirely.

**The fix is to change the contract's wording, and I have not made it.** A contract
is the source of truth for the plant, and quietly editing it to agree with the code
is the failure mode this project keeps running into. It is recorded as a
disagreement rather than resolved, which is the honest state.

### Four grants that were not obvious, and a stale image that hid all of them

`wwtp_writer` was granted `INSERT` on `reading` and nothing else. That is the
obvious minimum, it is what a person writes from a description, and it produces:

    ERROR:  permission denied for table reading

with no hint about what is missing. Three more privileges were needed and all
three were found by **running the stack**:

* `SELECT` — `ON CONFLICT DO UPDATE` reads the conflicting row to decide whether
  to update it.
* `UPDATE` — and then writes it. The one that looks like a mistake on a
  historian, and the gateway's re-send path genuinely needs it.
* `USAGE` on `event_id_seq` — `event.id` is `BIGSERIAL`.

And then a fourth problem, which is the one I would write down: **the container
image was stale.** I applied the grants to the database, re-ran `init-db`, and
the failure persisted, because `init-db` was running the *image's* copy of
`roles.py` from before the edit. I checked the grants by hand, saw them missing,
and spent several minutes on a privileges problem that was a build problem.

That is the **third** time a stale image has cost me time this session. The
lesson is not "remember to rebuild" — it is that **`docker compose run` does not
rebuild, and a container is a snapshot of the code as it was**, which is exactly
the kind of fact that feels like it should be impossible to get wrong and is not.

### Three bugs in my own new code, all the same shape

**`dwell_for` had its subtraction inverted** — `condition_since - raised_at`
instead of `raised_at - condition_since`. Found by a test asserting the dwell
equals the dwell that was asked for.

**`has_table_privilege` was written as `if cur.execute(...) or cur.fetchone()[0]`**
and therefore always true, so the credential check reported that the gateway held
every privilege on every table. Caught because the first output was obviously
wrong, which is the only reason it was caught at all.

**`SET LOCAL ROLE` is a no-op in autocommit**, so six tests in the role suite
passed *against the owner* — meaning they were checking that the owner cannot do
things the owner can obviously do. A test that passes for the wrong reason is
worse than one that fails, because it is a green tick.

**The gateway's health check took three forms and was wrong twice.** First a shell
`&& ... || ...` chain, which was unreadable enough that I had to work out its
behaviour three times and which redirected stderr to `/dev/null` — so a real
connection failure reported itself as *"scoped"*. Then a folded YAML scalar (`>-`),
which collapsed the newlines and put `try:` mid-line. Now a literal block (`|-`)
with the logic in Python, and it can say *which* of several things went wrong.

A health check is the one piece of diagnostic tooling nobody is ever allowed to
be clever, and I was clever three times.

The common shape: **all three were assertions about a thing, where the assertion
and the thing were wired to each other incorrectly, and nothing failed loudly.**
That is the same lesson as the unthrottled scan loop and the wrong comment in
`softplc/main.py`, arrived at for the fourth and fifth time. It is now a
pattern I look for on sight: *a test that can only fail if the thing it names has
the same name.*

---

### The two most worth learning were the two I automated

Asked whether the charts were too basic — they were, and the fix is a curation
layer and about eight days of work. Then asked what Node-RED was for and what
there was to learn about OPC UA, and the honest answer to the second question was
**nothing at all**.

Measured, not asserted:

```
sql/ course:  21 lessons
OPC UA:        0 lessons
Node-RED:      0 lessons
```

Every mention of either protocol in the whole repository was one of four things:
an architecture line, a command to run, a `45 nodes assembled` verification
message, or a glossary entry. 500 lines of OPC UA server, a client, a browser
tool, 370 lines of tests — and not one sentence that taught anything.

The cause is the same cause as the dashboards, one level down. The SQL course
works because the SQL is hand-written and runs against a live database: **the
artefact is the lesson.** The flows and the address space did not work that way.
`scada/build_flows.py` is 1 147 lines of Python generating 275 lines of
JavaScript — four times more code deciding the flows than code inside them — and
the Node-RED editor is switched off, so the two things that teach Node-RED
(dragging nodes onto a canvas, reading messages in the debug sidebar) are both
disabled. The address space is generated from `contracts/tags.yaml` for the same
reason, and the thing that teaches OPC UA is *browsing a hierarchy by hand*,
which is exactly what the generator removed.

> I optimised for correct and reproducible, and treated teachable as a property
> of the documentation rather than of the build.

`docs/ARCHITECTURE.md` said, in the "what is deliberately not here" section, that
excluding MQTT avoided hiding "the protocol behaviour the project exists to
teach". The reasoning was right and the outcome was the opposite, and the sentence
should have been evidence rather than a claim. It now says so.

### The gate caught two things I had written from memory

`tools/check_lessons.py` runs every Python block in a course against a server it
starts itself. It is stricter than `check_sql.py` for the reason it can be: a SQL
block returning no rows is indistinguishable from a correct one, whereas an OPC UA
snippet either connects or it does not.

Lesson 01 failed twice on its first run, and both failures were mine:

* **`await root.get_references()[:4]`** slices the *coroutine*, not the list.
  `TypeError: 'coroutine' object is not subscriptable`. A precedence mistake that
  is invisible in a code block and unmissable in a running one.
* **`Organizes` where the answer is `HasComponent`.** I wrote that the eight
  areas are `Organizes` references from reading the code's intent, and the real
  answer is 16 references — `35 Organizes x1`, `40 HasTypeDefinition x1`,
  `46 HasProperty x6`, `47 HasComponent x8`. The two `get_children()` hides are
  the interesting ones: an inverse reference to the parent, and the type
  definition. Which is a *better* lesson than the one I had written, and I would
  never have found it by reading the markdown.

The generalisation, and it is the same shape as the thread about `has_table_privilege`
and `dwell_for`: **I can write plausible text about a system faster than I can
observe it, and nothing complains.** A lesson is the most dangerous artefact in
this repository for exactly that reason — it is prose that looks like
verification. Hence the gate, and hence pasting real output instead of writing it.

### Four things this OPC UA implementation gets wrong, now written down

Writing the course meant reading the server properly, and four claims did not
survive:

1. **Every numeric value is a `Double`.** `_variant_type()` takes an engineering
   unit and ignores it. The storm flag — `{Boolean}` in the contract — is
   published as a floating-point number, and a pH and a m³/h are
   indistinguishable by type. The module docstring claims a typed address space;
   that is currently true only of `RunState`.
2. **`Bad` is never published.** `publish()` maps quality to `Good`/`Uncertain`,
   and `grep -c 'StatusCodes.Bad' softplc/servers/opcua.py` is **0**, while the
   docstring says a failing sensor reports `Bad`. The historian's whole honesty
   argument rests on that distinction.
3. **The engineering range is not enforced on the wire** — already a known gap in
   `SECURITY.md`, now the subject of a lesson instead of a caveat.
4. **`RunState` is 0 until the process model drives it.** A bare `OpcUaServer` —
   a unit test, or the snippet gate — publishes 22 pieces of equipment all
   reading `0`, which is indistinguishable from 22 stopped motors. This is the
   deadband problem again in a different costume: **a value that is always zero
   is indistinguishable from a value that is genuinely zero**, which is why the
   historian records *when a signal last changed* and why there is a "What has
   stopped reporting" panel next to every trend.

### Two smaller ones, in my own new code

**`exec` does not allow top-level `await`.** The gate's docstring claimed it did,
with a comment explaining that the snippet was "compiled inside a coroutine". It
is not; `exec` compiles as a module. `ast.PyCF_ALLOW_TOP_LEVEL_AWAIT` would work
but makes `exec` return a coroutine that has to be awaited separately, and getting
that wrong passes a snippet without running a line. Wrapping the source in an
`async def` is the version that fails loudly. I wrote an explanatory comment
asserting a behaviour I had not tested, which is the same mistake as the wrong
comment in `softplc/main.py` that is already in this log.

**`len(Counter(...))` is the number of distinct keys.** The lesson-gate test
asserted "16 references" and got 4. The lesson itself does not use it, so this
was a bug in the test rather than the teaching — but it is the sort of thing that
becomes a lesson the moment somebody copies the snippet.

### A fresh server reports a plant that is exactly on the edge of healthy

Lesson 03 set out to check one sentence in `softplc/servers/opcua.py`:

> **StatusCodes.** Every value carries its quality. A failing sensor reports
> `Bad` rather than a plausible number, which is the thing that makes a
> historian honest.

Chasing "which value carries a failing sensor" turned up four things, and the
first is worse than the sentence it came from.

**A server that has measured nothing reports `Good` at the bottom of every
healthy band.** `_add_signal` constructs each variable at `sig.normal_low`:

```
do_mg_l                1.5       1.5         3.0
blower_rpm           600.0     600.0      1800.0
flow_m3h              200.0     200.0      2200.0
```

All `Good`, all with a `SourceTimestamp` of *now*, because `asyncua` stamps it at
construction. So a client connecting before the first publish sees dissolved
oxygen on the floor of its band, four blowers at 600 rpm — which to any
threshold means **running** — and influent at 200 m³/h. There is no way to tell
this from a working plant.

This is the project's own "plausible wrong number" class, from thread 22 where
the permit dashboard read pH from the TSS signal. Except there the number came
from a wrong column; here **it comes from the constructor**, and it is the
server's default state rather than an edge case. The sharpened lesson, which I
had not previously stated anywhere: **an invented number that sits *inside* the
expected range is harder to catch than one that does not**, because every
downstream check is calibrated to the expected range. The permit bug was caught
eventually; this one would not be.

**`Bad` cannot reach the wire.** `publish()` is
`Good if quality == 0 else Uncertain` — one branch for two states. The full
round trip:

```
plant model on the wire gateway recovers value
Good        Good        Good              2.0
Uncertain   Uncertain   Uncertain         2.0
Bad         Uncertain   Uncertain         2.0
```

The gateway is the one part that does this correctly, using a raw batch `read`
that preserves every `StatusCode`. The status is lost on the way *out*. The
consequences run downhill: the gateway's `if quality == QUALITY_BAD: continue`
branch is unreachable against this server, and the two arms of
`quality_from_status` can never both fire, so that mapping is only covered by
unit tests that build the status by hand.

**Nothing produces a `Bad` in the first place.** I assumed the fault engine
produced one and had to check. `softplc/process/plant.py:469` is the *only*
construction of a `PlantSnapshot` and passes `quality={}` unconditionally. The
sole writer of a non-zero quality in the entire simulation is
`softplc/faults/engine.py:458`, and it writes `QUALITY_UNCERTAIN`. So all four
sensor faults — drift, flatline, stuck-high, effluent TSS — degrade to
`Uncertain`, and `QUALITY_BAD` is a constant with no producer. The docstring
describes a capability the code does not have.

**And the obvious read throws the reason away with the value.**
`read_data_value()` defaults to `raise_on_bad_status=True`, so an `Uncertain`
value arrives as `UaStatusCodeError` with the number still in the response. A
client that catches and discards loses the value *and* the status — precisely
what `gateway/clients/opcua_client.py` spends a paragraph warning against, and
arrived at by obeying the API. `tools/opcua_browser.py` calls it with the
default at two sites, so **the tool you would reach for to see a fault cannot
show one.**

### A test I wrote backwards, caught by reading it rather than running it

`test_the_lesson_claims_the_browser_tool_would_raise_on_a_degraded_value`
asserted `assert not bare` — that the browser does *not* call
`read_data_value()` with the default. The lesson says it *does*, and the lesson
is right. The test was the thing that was wrong, and it was wrong in the
direction that would have hidden the bug: a green tick next to a claim that the
tool is broken.

The whole family of tests in `tests/test_opcua_course.py` asserts that a
**defect is still present**, which is the inverse of every other test in this
repository. That inversion is deliberate and it is fragile in a way worth naming:
a test that fails when someone fixes a bug is a test that punishes the fix. It is
only defensible because the failure message says what to do — *"the bug lesson 03
teaches has been fixed and the lesson is now stale"* — so the person who fixed it
is told the documentation needs updating rather than left with a red build and no
explanation. Sixteen tests here now work that way, and each one names the lesson
it protects.

### The gate caught four more, and one of them was mine again

Two snippets in lesson 03 failed on `NameError: name 'parent' is not defined` and
three more on `node_id` and `QUALITY_UNCERTAIN` — because a snippet that reuses a
variable from the previous block is not runnable by a reader who starts at that
block. The gate enforces self-containment for free, and the fix in each case was
to make the snippet stand alone rather than to establish a convention.

The other two were source excerpts from `opcua.py`, `plant.py` and
`faults/engine.py` that I had written as runnable code. They got
`<!-- check: skip -->` with a reason on the line above, which is the convention
`check_sql.py` already uses.

I also wrote *"22 units, nineteen ids"* in lesson 02 when it is **eighteen** —
22 − 4, and I had not done the subtraction. Both numbers are now asserted so the
prose and the table cannot drift apart again. Three findings in three lessons
from writing plausible text faster than I could observe it, and the gate caught
all three. That is the argument for the gate, and it is also an argument against
my own instincts.

### The project chose OPC UA for a reason it then declined to use

Lesson 04 went looking for the feature the docstring sells second:

> **Subscriptions.** A client subscribes once and receives changes. Modbus makes
> the client poll, which means deadbanding, change detection and data volume all
> become the client's problem.

```
$ git grep -c create_subscription -- '*.py'
tools/opcua_browser.py:1
```

**One hit, in the tool built for a human to look around.** Not the gateway, not
the server, not the soft PLC. And the gateway — the client this project actually
wrote — polls:

```python
# gateway/clients/opcua_client.py:184,199
async def poll(self) -> OpcUaPollResult:
    response = await self._client.uaclient.read(params)
```

A batch read of all 57 nodes, `poll_interval_s: float = 1.0`, forever, whether
or not anything moved. So the project picked OPC UA, built a real address space,
and then consumed it exactly as Modbus would require. The single best argument
for the protocol is the one argument the protocol is not being used for here, and
`gateway/deadband.py` is 241 lines of well-tested code doing by hand — on the
wrong side of the wire — the job a `DataChangeFilter` does natively.

Three comments describe machinery that is not there:

* The class docstring says the server "keeps one internal subscription with a
  data-change filter and pushes updates through a single writer callback." There
  is no internal subscription. `OpcUaServer` holds `self._dirty: set[str]` and
  `publish()` walks it. The *advice* about per-value subscriptions is right; the
  implementation is not the thing it names.
* `mark_dirty` says it is "called from the scan loop, which knows what changed
  because the deadband said so." It is called from `set_value()`. The scan loop
  has no deadband — `softplc/main.py:236-238` filters on **exact equality**, so
  every distinct float is published however far below the contract's deadband it
  falls. The only deadband is client-side, downstream.
* Which means the sentence's own conclusion — *"pushing only real changes is what
  makes an OPC UA subscription cheaper than Modbus polling"* — describes a saving
  that has not been taken.

**And the cost side, which the one-paragraph pitch never mentions.** Measured
with a real subscription, DO ramping 0.01 per publish:

```
  client A (no filter):  41 notifications, last value 2.39
  client B (deadband 0.02): 2 notifications, last value 2.0
  the server says: 2.39
```

Two clients, same server, same node, same instant, both receiving `Good`.
Client B is **16 % low** and there is no field in its notification saying so. A
filter is negotiated *per subscription*, not a property of the data, so two
clients can hold genuinely different beliefs about one value with nothing to
reconcile them — and a deadband does not queue, summarise or timestamp what it
drops, so a historian on a filtered subscription has no record that DO moved in
nineteen steps at all.

The other measurement worth keeping: **41 notifications carrying 3 distinct
values.** Thirty-eight of them said nothing new. A filterless subscription
reproduces the "data volume is the client's problem" complaint the docstring
raises *about Modbus*, which is the kind of result that makes the comparison
unreliable in the direction that flatters the thing you built.

### A measurement I could not reproduce, and what I did about it

The first run of that comparison printed 41 / 41 / 41 — the deadband appearing to
suppress nothing, which contradicted everything I expected. The second run, on a
different code path, printed 41 / 1 / 1. Rather than pick the one I liked, I
re-ran the first pattern exactly, and got 41 / 1 / 1.

So the first script was buggy and its output was wrong, and I had two options:
publish the number that told the story I wanted, or go back and find out which
one the server actually does. The rule this project already has — *a number in
prose is a measurement or nothing* — only means anything if the measurement is
reproducible, and a number I cannot reproduce twice is a number I do not have.

What I actually had was a script with a bug and no way to tell which of two
contradictory outputs was the bug. The fix was to re-run the *first* pattern
verbatim rather than to keep refining the second one, because the second one was
the one I had just written and the first one was the one that surprised me. A
surprising result deserves more scepticism than a confirming one, not less.

Lesson 04's numbers come from the reproduced pattern, and the divergence between
them is now a test: `tests/test_opcua_course.py` asserts that nothing in
`gateway/`, `softplc/main.py` or `softplc/servers/opcua.py` creates a
subscription, and that the server-side filter is still exact equality. Both
assert that the *defect* persists, so fixing it fails the build with a message
saying the lesson is now stale.

### A security control that nothing can reach, and a write surface that does nothing

Lesson 05 was written to measure one number in `docs/SECURITY.md` — that a
client can write 99 mg/L to a DO setpoint whose range is 0.5–6.0. Confirmed:

```
before: 2.0
wrote 2.5, in range  -> 2.5
wrote 99.0, out of range -> 99.0
```

99.0 accepted, 16× the contract's maximum. And the guarantee on the other side
of the same docstring is real: writing a measurement gives
`BadUserAccessDenied`, enforced by the protocol, not by this project. Worth
saying plainly, because the rest of this entry is a list of things that are not
guaranteed, and it would be easy to leave the impression that OPC UA is
unreliable. It is not, here.

**Then the finding I did not expect.** The range *is* checked, in
`OpcUaServer.write_value()` at `opcua.py:481`, which raises `ua.UaError` with
the range in the message. And:

```
$ git grep -n write_value -- '*.py' | grep -v '^tests/'
softplc/servers/opcua.py:407:            await entry.node.write_value(
softplc/servers/opcua.py:447:            await node["state"].write_value(
softplc/servers/opcua.py:452:    async def write_value(self, signal_id: str, value: float) -> None:
```

Two calls, both the server writing *out*. Line 452 is the definition. **The
function has no caller.** A wire write is handled by `asyncua` setting the node's
value directly and never routes through it. The docstring calls the range check
"a courtesy for in-process callers"; there are no in-process callers either, so
it is dead code that reads like a security control — which is worse than not
having it, because the next person to read the file will believe writes are
checked.

**And the writes are inert regardless.** I had assumed that fixing the range
check would make writes meaningful, and checked before writing the lesson.
`softplc/main.py:131` assigns `self._space = await self.opcua.start()` — so
`self._space` *is* the OPC UA address space, and the filter at line 236 compares
the node against the model and overwrites on any difference. The model produces
2.0 for `SETPOINT_DO` forever, so every client write is silently reverted within
one scan cycle.

Worse: the DO controller computes `driving_force = c_star - self.setpoint_do_mg_l`
at `units.py:918`, reading **its own dataclass field**. No code path runs from a
client write to a control decision. So a client can write 99 mg/L, read it back,
see `Good`, and be entirely mistaken — the write surface is decorative. Two
writable signals out of 57, neither connected to anything, and the project's only
real write path is the Node-RED flow's **Modbus** write.

What keeps this from being alarming is the direction it fails: an absurd setpoint
is ignored, not applied. A control system that *accepted* 99 mg/L would be a far
worse bug. But that is luck, not design, and nothing in the repository says so.

**The consequence for `SECURITY.md` is the real output of this lesson.** The gap
is documented as "the engineering range is not enforced on the wire." That is
true and it is much narrower than the truth. The actual finding is that **no
write reaches anything at all** — so a reader triaging by the security document
would fix the range check, find the write still does nothing, and have no way to
know that was always the case. A security document that understates a gap is worse
than one with no gap, because it is the document somebody trusts when deciding
what to fix first.

### A test about absence, and the two ways it can lie

`test_the_lesson_claims_the_range_checking_write_path_has_no_caller` walks the
AST for calls to `write_value` and asserts there are none. It is a test about
*absence*, which is a different kind of thing and I got it wrong twice:

* It flagged `node["state"].write_value(...)` as a caller, because that is an
  `ast.Subscript` and my filter only recognised `ast.Attribute`. A false
  positive on the very finding it exists to protect.
* My own lint pass then removed the `noqa` markers as "unused" and moved an
  `import ast` into a function body, which broke it the other way.

Both were caught because the test was run rather than reasoned about. The general
point is the one this repository keeps relearning: **a claim about what is not
there is exactly as easy to get wrong as a claim about what is**, and slightly
harder to notice, because the failure mode is a passing test.

### Loop affinity fails silently, which is why the comments are scars

Lesson 06 is the first of the six where the *code* is mostly right, so the
findings are about the prose. But the measurement that justifies the whole
architecture is the most useful thing in the course so far, and it took nine
lines:

```
  time.sleep(0.3), not awaited          305.0 ms, ticked   5 times
  await asyncio.sleep(0.3)              301.2 ms, ticked  32 times
  await asyncio.to_thread(blocking)     304.9 ms, ticked  33 times
```

Same 300 ms. **Five ticks, or thirty-two.** The timing column is identical in all
three rows, which is the part worth keeping: the damage is invisible in how long
the blocking call took and visible only in *what else failed to happen meanwhile*.
That is why this class of bug gets misdiagnosed as a network problem — there is
no slow call to find.

**The loop-affinity failure raises nothing at all.** `softplc/main.py:77` says the
OPC UA server holds state bound to its creating loop, so talking to it from a
second loop "produces a connection timeout that looks like a networking fault."
I had assumed that meant an exception somewhere. It does not:

```
  publish() on the owning loop   -> 0, no error
  publish() on the wrong loop    -> no exception, no log, no bad return
Task was destroyed but it is pending!
task: <Task pending name='Task-4' coro=<InternalServer._set_current_time_loop() ...
```

`publish()` succeeds, returns a count, and the server's internal tasks are
orphaned and garbage-collected mid-flight. The comment is a scar from a real
incident, and the scar is the only evidence — there was never an exception to
point at.

**A heading that its own body contradicts.** `ARCHITECTURE.md:108` reads *"One
event loop, two protocols, one thread."* The process has **two loops and three
threads**: the main thread on `asyncio.run(_run(args))`, `softplc-loop` running
its own `run_forever()`, and `modbus-tcp` on a daemon thread. The body text
describes the second loop and the second thread, two paragraphs below the
heading. A heading cannot be fixed by reading further down, which is what makes it
the wrong place for the one false word.

By this point it is a pattern rather than a slip: **in the protocol work the prose
describes an intended design, the code describes the built one, and they differ.**
Lessons 02, 03, 04, 05 and 06 all found the same shape. What makes it worth naming
is that the *body* comments are consistently excellent — `modbus_server.py:180`
("A daemon is the honest mechanism"), `stop()` ("this only clears the running
flag"), `set_equipment_state` ("staging is not a convenience here, it is the only
correct way to cross that boundary") — so the failure is confined to
summary-level claims. Someone writing an honest comment about a mechanism they had
to debug, and an optimistic one about the architecture around it.

### Two constraints of the gate, found by tripping over both

Lesson 06's cross-loop snippet failed twice before it ran, and both failures are
now documented in `tools/check_lessons.py`:

* **`address already in use`.** The runner starts a server on 48400 for every
  snippet, so a snippet that starts a *second* server has to pick another port.
* **`asyncio.run() cannot be called from a running event loop`.** A snippet is
  already inside the runner's loop and `asyncio.run` creates a new one.

The second one improved the lesson rather than merely unblocking it: the
demonstration needed two loops anyway, and the runner's own loop is a perfectly
good "wrong loop". So the fix was to make the snippet do the thing it was
teaching rather than to work around the runner. Worth noticing that a tooling
constraint and a teaching requirement turned out to be the same requirement —
which is a decent argument for building the gate before writing the lessons.

### The security document ranks the wrong asset, and its top fix is already here

Lesson 07 was going to be about certificates. The two lines the server prints on
every single start —

```
No encrypting policy available, password may get transferred in plaintext
Endpoints other than open requested but private key and certificate are not set.
```

— turned out to be the least interesting part, because `docs/SECURITY.md` already
says it plainly: OPC UA encryption "**Not enforced** — Anonymous, `None` security
policy", authentication "**Not enforced**". A client confirms it:
`security_policy : SecurityPolicyNone`, `user_certificate : None`.

(I also had to correct myself: I had remembered a `LOGIN_PASSWORDS` table as being
OPC UA users. It is `storage/postgres/login_role.py` — Postgres roles, a different
protocol. The OPC UA browser is explicitly anonymous, and says so in a docstring.)

**So what the lesson found instead is a structural problem, and it is the first
finding in seven lessons that runs opposite to the pattern.** Everything in 02–06
was *documentation* overstating code that was right or harmlessly broken. Here the
prose is careful, the threat model is explicit about its scope ("There is no real
plant. Nothing here controls anything that matters"), and the *code* leaves an
entire asset class outside the model.

`SECURITY.md` ranks what matters in a real plant:

> 1. **Integrity of the control path.** Someone able to write a setpoint can
>    change what the plant does. This is the asset worth defending.

**Nobody can.** Lesson 05 established it: the write path is unwired, the scan loop
overwrites everything, the DO controller reads its own field. The number-one asset
is the one asset not exposed, because nothing is connected to it.

What *is* exposed, from an anonymous connection with no username, password or
certificate:

```
plant: PLANT-A: Northgate Water Reclamation Facility
  DesignFlow_m3h                     = 1800.0
  Permit_eff_nh4_mg_l_30d_mean       = 10.0
  Permit_eff_tss_mg_l                = 30.0
  Permit_eff_ph_min                  = 6.0
  Permit_eff_ph_max                  = 9.0
  Permit_dis_bacti_geomean           = 200.0
areas enumerable        : 8
measured signals walked : 57
```

The complete compliance envelope — every number needed to discharge into this
plant's permit without tripping it. **Confidentiality of that envelope is nowhere
in a threat model whose top-ranked risk is control-path integrity.**

And the list of "what this would need before facing a real network" opens with:

> 1. **Write handlers on OPC UA** that reject out-of-range values… Closes gap 1.

A write handler that rejects out-of-range values is `OpcUaServer.write_value()`.
It exists, it is correct, it has tests, and **it has no caller.** The top security
improvement on the list is already written, already unreachable, and the document
does not know it. Completing it would not achieve the stated outcome either, since
closing gap 1 only stops out-of-range writes reaching a control loop that is not
listening.

**The bind address, in no document at all.** `git grep -n '0\.0\.0\.0' -- '*.md'`
returns nothing outside this course. The server binds `0.0.0.0:4840` and
`compose.yaml:79` publishes `"${OPCUA_PORT:-4840}:4840"`, which is every host
interface; loopback-only is a two-token difference. So "anyone who can reach the
port" is unresolvable by reading the project — on a laptop, the local network; on
a server in a rack, the internet, depending on a firewall nobody here has
written.

And `compose.yaml:71` sets up the expectation it then does not meet: *"publishing
to the host is a separate, deliberate decision made per port below"* — and the
port below publishes to all interfaces, which is a default rather than a decision.
A reader has to know the Docker publishing syntax to notice.

**The genuinely two-sided lesson**, which is the part worth taking to a real
plant: the protocol's greatest feature is also its greatest reconnaissance
surface. A Modbus attacker gets 4 314 integers and has to guess. An OPC UA
attacker gets the map, the labels and the permit limits, and never guesses. That
is not an argument against OPC UA — it is an argument that **an OPC UA server is
only as safe as its authentication, and authentication is the one thing that
cannot be retrofitted**, because a client that expects `None` will talk to a
`Sign` endpoint and a client that requires `Sign` will not talk to this server at
all. Tightening later breaks every client that connected while it was loose. That
argument is not in `SECURITY.md`, and it is the real reason to do it at the start
rather than at the end.

### A test about a fact's absence, which failed on itself

`test_the_lesson_claims_the_bind_address_is_in_no_markdown_file` scanned every
markdown file for `0.0.0.0` and asserted none contained it. It failed — on
**lesson 07, which quotes the endpoint in order to complain that nobody documents
it.**

That is a correct failure and the wrong scope. The claim worth protecting is
narrower: the project's *own* documentation never mentions the bind address. So
the test now excludes `courses/`, and the lesson says so in prose — that the only
markdown naming it is the document complaining about it, "which is the usual fate
of a fact that only appears in the document complaining about it."

It is the third time in this course a claim about what is *not* there has been the
hardest kind to get right, after the dead `write_value()` caller and the missing
`create_subscription`. Three for three, and the failure mode is always a test that
passes when it should fail or a lesson that documents a gap by filling it.

### The largest generated artifact is the one nobody can review

Lesson 08 was written to argue against a decision I made, and it is the strongest
of the seven. Measured first:

```
  depth 0:   14
  depth 1:   27
  depth 2:  132
  depth 3:  513
  depth 4:    0
  total   :  686  from 57 signals and 22 pieces of equipment
  that is 12.0 addressable nodes per signal
```

Depth 3 is 513, which is 57 × 9 exactly. 500 lines of Python produce 686
addressable nodes, and the hand-written alternative is 686 lines of
`add_variable` calls plus 57 drift opportunities. **Generation was the right
call** — the Modbus map, the Node-RED tags, the Grafana panels and the SQL course
all read the same YAML, and that is worth more than everything below.

The cost is one number and one structure.

**The number:** the `drift` CI job checks five generated artifacts — the tag
list, the flows, the dashboards, the extracted queries, the web read model. Each
is written to disk and diffed. The address space, at 686 nodes the largest
generated thing in the repository, **is not among them, because it has no file.**
It is built at startup, served and discarded. So the one generated output big
enough to be worth reviewing is the one nobody can review, and its 24 tests assert
*shape* at runtime, which is not the same as a person having agreed with it.

**The structure:** 57 signals, **1 property set, 1 DataType**.

```
  signals walked: 57
  distinct property sets : 1 -> [57]
  distinct DataTypes     : 1 -> {'Double': 57}
```

A hand-written tree would have had 57 independent chances to be wrong. This has
exactly one, and it is not a chance — it is a decision that cannot be half-made.
Generation is *better* when the decision is right and **worse** when it is wrong,
because the blast radius is the whole plant and because a systematic fault looks
identical to a systematic success: 57 identical, well-formed, wrong signals, and
nothing in the output distinguishes "correct by design" from "wrong by design".

So I enumerated the decisions in `build_address_space` and `_add_signal`. About
fourteen, none of which a reader would call decisions because they sit in
plumbing-looking code. **Three are unambiguously wrong and a fourth is wrong
whenever nothing drives the plant** — the `normal_low` initial value, the bare
`Int32` `EngineeringUnits`, `_variant_type` returning `Double` whatever the unit,
and `RunState` initialised to 0. All four were found in this course, by walking
the running server. None was found by reading the generator, and three are in
code I wrote.

**The generalisation, and it is the one I would keep:** every single defect in
eight lessons was found by *running* something rather than by reading something.
Lesson 02's reference ids, lesson 03's fabricated `normal_low`, lesson 04's
notification counts, lesson 05's accepted 99.0, lesson 06's five ticks, lesson
07's anonymous walk. The documentation defects were found by reading; the code
defects were found by observing. That is not a coincidence about this project, it
is what happens when the reviewable surface and the defective surface are
different surfaces.

**The fix is neither hand-writing nor stopping.** This project has already solved
the exact problem four other times: serialise the address space to a file, add a
sixth `--check` step to the existing drift job, and let the serialised form be the
review surface. A pull request changing `_add_signal` would then show 57 rows
saying `DataType: BaseDataVariableType → AnalogItemType` — a reviewable diff —
instead of a Python function. It would have caught decisions 6, 8, 9, 11 and 12
immediately, because they all surface as a *value in a table* rather than a line
in a loop. It would also fix lesson 01's `RunState = 0`, which is only visible as
a value. A day's work, specified by a lesson rather than found by an outage.

### I forgot a constraint I had written down forty minutes earlier

Lesson 08's first snippet started its own server on 48400 and failed with
`address already in use` — the exact gotcha I had added to
`tools/check_lessons.py`'s docstring in lesson 06, in the same session, in the
same file. The fix was better than the workaround: the gate injects `server`, so
the snippet asks the address space that already exists instead of building a
second one.

A constraint I had just written down and did not look up. It is a small thing and
it is the same shape as everything else in this log: I produced a correct artifact
and then consumed it from memory rather than from the artifact.

### The one field a client would use to date a reading is destroyed on every write

Lesson 09 was the capstone: write a real client rather than describe one, because
the project's own thesis is that the artefact is the lesson. That turned out to
matter more than expected — **the safeguard I designed was wrong, and writing the
code is what proved it.**

The safeguard: every variable in this server is constructed at its `normal_low`,
so a fresh server reports DO at 1.5 mg/L with a `Good` status (lesson 03). A
client that prints `1.5` is lying by omission, so the client records when it
connected and compares `SourceTimestamp` against that. It worked — on a fresh
server, every value came back `[unmeasured]`, correctly.

Then I published a real value and it still said `[unmeasured]`:

```
  before anything is published:  1.5 (UNMEASURED)  Good
  after one real publish of 2.4:  2.4 (UNMEASURED)  Good
```

I nearly recorded that as a subtlety of timestamp comparison. It is not. Checking
the field directly:

```
  fresh server            value=1.5  SourceTimestamp=1790564165.416149
  after publish of 2.4    value=2.4  SourceTimestamp=None
  after publish of 9.9    value=9.9  SourceTimestamp=None
```

`asyncua` stamps `SourceTimestamp` at construction. `publish()` writes

```python
ua.DataValue(ua.Variant(entry.value, ua.VariantType.Double), StatusCode=status)
```

with **no `SourceTimestamp`**, so the field is *cleared* on every write. **The
only field a client could use to know whether a value is a real reading is
destroyed by the server's own write path.** Fix: `SourceTimestamp=ua.DateTime.now()`,
one line, and `tests/test_opcua_minimal_client.py::test_publish_clears_the_source_timestamp`
fails the moment it is applied and says so.

This is the fourteenth finding and the only one that is neither a documentation
error nor a local mishap. Lessons 02, 04, 05 and 06 were prose overstating
working code. Lessons 01, 03 and 08 were design decisions nobody reviewed. This
one is **in the design**: the server's publish path cannot tell a client when a
value was measured, so no client can date a reading, and a client that tries has
to invent a three-state verdict — `measured`, `unmeasured`, `untimestamped` —
where the third state is the honest answer for every real reading on this server
today.

**And the lesson about the lesson:** my first client had a two-state verdict and
treated a missing timestamp as `unmeasured`. That reports a genuine 2.4 mg/L as
never-measured, forever — a safeguard that fires on 100 % of real data, which is a
safeguard that gets switched off. Writing the code found that; describing the
code would not have. The test that caught it is
`test_a_value_published_after_connecting_is_measured`, and its docstring says it
is the test that caught the bug.

### The fix was wrong the first time, and wrong invisibly

Applying the one-line fix was the easy part. The interesting part is that the
obvious implementation of it does not work:

```python
SourceTimestamp=ua.DateTime.now()      # what I wrote first
```

`ua.DateTime.now()` returns a **naive local** datetime, and `asyncua` encodes one
against a UTC epoch:

<!-- asyncua's encoder, quoted — the branch that makes a naive datetime wrong -->
<!-- check: skip -->
```python
# asyncua/ua/ua_binary.py, datetime_to_win_epoch
if dt.tzinfo is None:
    ref = FILETIME_EPOCH_AS_DATETIME        # 1601-12-31, treated as UTC
else:
    ref = FILETIME_EPOCH_AS_UTC_DATETIME
```

So the machine's UTC offset becomes part of the value and the client reads it back
hours out. Measured with the naive version in place:

```
  wall clock          1790566160.150795
  constructor stamp   2026-09-28 03:30:49.777213+00:00    delta  -0.1s
  published stamp     2026-09-27 20:30:49.814630+00:00    delta  -7.0h
```

**Seven hours, and completely invisible in the published value.** It looks like an
ordinary timestamp, the server raises nothing, and the error appears only as a
client-side time disagreeing with the wall clock. The fix is
`datetime.now(timezone.utc)`, wrapped in a `_utcnow()` helper so the trap is
documented where the call is.

I found this because the test written for the *original* finding compares a
published timestamp against `time.time()` — not because I reasoned about
`tzinfo`, which I did not. That is the second time in this course that the
obvious implementation of a fix produced a plausible wrong value, after a
`Counter` that counted distinct keys rather than entries. **Neither was findable
by reasoning about the fix**, and both were found by a test that *measures* the
thing rather than asserting what it ought to be.

**A guard that punishes its own documentation.** The test I wrote first asserted
`"ua.DateTime.now()" not in source` — and then failed, because my explanatory
comments in the server *name the function they forbid*, on purpose. A test that
greps for a string punishes the documentation of its own trap. It now parses the
AST and inspects the real call graph, so a comment may name the forbidden
function and the guard still means what it says.

Both guards were **verified by mutation**: I reintroduced the naive form, a bare
`datetime.now()`, and a missing `SourceTimestamp` in turn, and confirmed each one
fails. A guard never seen to fire is a guard nobody knows works — the same
argument as the `Bad`-quality test in lesson 03.

### A residual the fix does not remove

There is a case the client still cannot resolve, and the honest thing is to say
so rather than pick a winner. A genuine measurement published a moment *before*
the client connected carries a timestamp older than the connection — exactly like
the constructor's placeholder. **One sample cannot tell them apart.** Only
watching the value move settles it, which is what the client now tracks and what
a subscription does naturally.

So three verdicts survive the fix, and the middle one is the interesting one:

| verdict | condition | meaning |
|---|---|---|
| `measured` | stamped at/after we connected, **or** seen to move | a real reading |
| `unmeasured` | stamped before we connected, never seen to move | placeholder, *or* a reading from just before we arrived |
| `untimestamped` | no stamp | a server that does not timestamp; age unknown |

`unmeasured` is the right direction to be wrong in — "I cannot vouch for this"
rather than "this is fine". A client that guessed optimistically would be the
pH-read-from-the-TSS-signal bug wearing a different hat, and the whole course is
an argument against guessing in the optimistic direction.

The shipped client is `tools/opcua_minimal_client.py`, ~200 lines with five
safeguards, and it exists because the existing `tools/opcua_browser.py` is
demonstrably less correct: it calls `read_data_value()` with the default, so it
**crashes on the first degraded sensor** (lesson 03), and it prints a plausible
`1.5` for a plant that has never been measured. Both are defects in the tool
whose job is to be trusted by a person looking at a plant. Its output against an
undriven server:

```
    AERATION:AHU-1:DO                                1.5 [unmeasured]  Good
    AERATION:AHU-1:AIR_FLOW                         2000 [unmeasured]  Good
    INFLUENT:FLOW:FLOW                               200 [unmeasured]  Good
    SITE:WEATHER:STORM                                 0 [unmeasured]  Good
```

Four numbers, all `Good`, all wrong, and the client says so about every one.

## What nine lessons added up to

The findings sort into three kinds, and only the third survives a careful read:

1. **Documentation that overstated working code** — 02, 04, 05, 06. Fixed by
   editing prose; all found by running something.
2. **Design decisions nobody reviewed** — 01, 03, 08, 09. Placeholders at
   `normal_low`, a cleared `SourceTimestamp`, 686 generated nodes with no
   artifact and three wrong decisions in fourteen. None is a typo.
3. **A threat model that does not match the code** — 07. The top-ranked asset is
   inert, the top remediation exists unwired, and the exposed asset is not in the
   list.

**The reviewable surface and the defective surface are different surfaces.** A
project that reviews only the reviewable one finds documentation bugs forever and
design bugs never. That is the finding I would keep from all nine lessons, and
it is not about OPC UA — it is about what happens when you optimise for
*correct and reproducible* and treat *teachable* as a property of the docs.

The course is now complete: nine lessons, 35 runnable snippets, 37 tests, and
fourteen findings. Three are worth doing this week and none is hard — set
`SourceTimestamp` in `publish()` (one line), serialise the address space and add
a fifth `--check` beside the four that exist, and bind loopback in `compose.yaml`
(two tokens per port).

## The two cheap fixes from the course's list, and what they exposed

### 686 nodes, now a file

Lesson 08 recommended publishing the address space. Done:
`contracts/address-space.json`, written by `tools/opcua_address_space.py`, and a
**sixth** step in the `drift` job that already had five.

The value is visible in the file itself. All 57 signals read `"data_type":
"Double"`, which is lesson 02's wart as a row rather than a claim. All 57 read
`"constructed_from": {"field": "normal_low", "value": ...}`, which is lesson 03's
worst finding — the value the constructor invents, in a table, where a YAML diff
of `contracts/tags.yaml` cannot show it, because the contract does not say the
value is *used this way*. And all 22 pieces of equipment read
`"run_state_constructed_as": 0`, which is lesson 01's closing example.

The gate names the line and quotes both sides, because "the files differ" on a
686-node file pushes the work back onto whoever the gate was built to help:

```
contracts/address-space.json is out of step with the address space the server
builds: line 253: committed '"data_type": "Double"', built '"data_type": "Int32"'
```

Verified by mutation — changing `_variant_type` to return `Int32` makes
`--check` exit 1, and a test does exactly that rather than asserting the gate
would work.

**Two things I got wrong writing it.** `_describe` read `DataType` from every
node, and a Folder has no such attribute, so the walk died on the first area
folder with `BadAttributeIdInvalid` — the server's correct answer, and my
assumption that every node is a Variable. And `render` wanted `sort_keys=True` for
determinism, which would have alphabetised 686 nodes and destroyed the tree order
that is the entire reason a human would read the file. Determinism comes from the
contract's ordering, not from sorting the output.

### Five ports, one prefix

`4840:4840` publishes on *every* host interface. The fix is `${HOST_BIND:-
127.0.0.1}` in front of all five, with `HOST_BIND=0.0.0.0` as a documented opt-out
in `.env.example` — a **default, not a policy**, because someone demoing the plant
on a laptop they trust should not have to edit compose. The warning sits next to
the opt-out, because a reader who sets it without opening `SECURITY.md` should
still be told what it exposes.

### A guard with a regex narrower than the prose it guarded

While wiring the serialiser in I noticed `README.md` said **"five CI jobs"** and
the workflow has six. The test written to catch exactly that,
`test_no_document_says_the_ci_workflow_has_five_jobs`, **passed** — because its
pattern was `\b(?:five|5) jobs\b` and the README said "five **CI** jobs".

That is the third guard in this project that was green and wrong, and the
interesting part is that widening the regex immediately found a second instance:

```
README.md:176:  The five CI jobs, and the three broken things writing the file found
courses/opcua/08-generated.md:274:  ...was checked by five CI jobs.
```

The second one **I wrote four commits earlier, in lesson 08**, in the same
session, while documenting the very problem the course exists to expose. A guard
with a regex narrower than the prose it guards is worse than no guard, because it
is green *and* it is cited as evidence that the claims are checked.

The pattern now allows an adjective between the number and the noun. And the
lesson's own sentence had to be reworded rather than the test narrowed again,
which is the correct direction to resolve that conflict.

### And a gate that punished its own documentation

The source guard for the timezone fix asserted `"ua.DateTime.now()" not in source`
and failed, because the server's comments name the function they forbid, on
purpose. It now walks the AST and inspects the real call graph, so a comment may
name the forbidden function and the guard still means what it says. Both that
guard and the live one are verified by mutation.

Three guards, three ways of being confidently wrong, all found by looking rather
than by trusting a green tick. That is now the single most repeated lesson in
this log, and it is worth more than any of the fourteen findings: **a test that
has never been seen to fail is an assumption wearing a tick.**

---

## Thread triage

Twenty-two threads were open at the end of Phase 5a. Triaged, they are not
twenty-two items of work — they are **five things to do, six properties of the
design that are not going to change, and eleven that are finished.** The
distinction matters because a list of twenty-two undifferentiated open items is
a list nobody reads, and the two most valuable entries in it (the factor of
seven, and the untested lesson outputs) were buried under nine resolved ones.

| # | Thread | Verdict |
|---|---|---|
| 1 | Modbus wire addressing | **done** |
| 2 | OPC UA engineering range not enforced | limitation — a property of the specification |
| 3 | Aeration near its limit | limitation — a design fact, with the margin stated |
| 4 | Solids balance closes to ~15 % | **decided** — commanded-vs-emergent, with a guard test |
| 5 | Contract links signals to registers | **done** |
| 6 | Storage against live databases | **done** |
| 7 | Lint debt in the older files | **decided** — now ratcheted, cannot grow |
| 8 | Deadband makes "no data" ambiguous | **decided** — cost stated; revisit when the purpose is known |
| 9 | Database credentials | **done** |
| 10 | False-positive alarm rules | **part done** — six resolved, five impossible; one has a fix |
| 11 | `NOT_detectable_by` was an absolute | **done** |
| 12 | The course's shown outputs are untested | **open** — a real gap |
| 13 | `ui/web` has no source | **open** — Phase 5b |
| 14 | Modbus link shares the scan loop's thread | limitation — comfortable now, surfaces on smaller hardware |
| 15 | A parameter declared in three places, used in none | **done** — and the guard found a second |
| 16 | The two harnesses disagree by a factor of seven | **open** — the highest-value item in the repository |
| 17 | Continuous aggregates were never refreshed | **done** |
| 18 | Two harness bugs that made every threshold wrong | **done** |
| 19 | The slow tests take 18 minutes | **open** |
| 20 | A field called `unit` that held an area | **done** |
| 21 | Three Node-RED failures that present as "it started" | **done** — and a fourth, found by writing the CI |
| 22 | Three mistakes in one permit query | **done** |

### The five that are still work

1. **The factor of seven (16).** Unexplained. Until it is, no threshold in
   `contracts/` is trustworthy to better than a factor of two, and the fix is in
   the harness rather than in the rules.
2. **Per-pump signals in the contract (10).** The only way to make
   `lift_pump_failure` detectable, because both lift signals are station totals
   and the controller compensates.
3. **The course's shown outputs (12).** `tools/check_sql.py` proves every query
   runs; nothing proves the numbers printed in a lesson are still the numbers the
   query returns.
4. **The settle window (19).** One long-horizon rule makes every scenario slow.
5. **`ui/web` (13).** Now Phase 5b.

### The six that are limitations, and why they stay open forever

Threads 2, 3, 4, 7, 8 and 14 are not tasks and will not be closed by writing
code. Each is recorded because something else depends on it:

* **2** — `EUInformation` is advisory in the base specification and `asyncua` does
  not enforce it. The only defence is the gateway refusing the write, and
  `docs/SECURITY.md` says so. This will never be "fixed"; it is what OPC UA is.
* **3** — `kla_per_h = 5.5` holds 2.0 mg/L with about 19 % headroom. Below ~4.5
  the basin is aeration-limited. A real design fact with a thin margin for storm
  scenarios, and the honest response is to size the plant differently rather than
  retune a number.
* **4** — the waste rate is *commanded* from the SRT target, so the residual
  measures commanded-vs-realised discharge rather than closing to zero. What must
  not happen is growth without bound, and that has its own test. **Closing this to
  0 % would be the bug.**
* **7** — 159 lint findings, nearly all cosmetic, in files that are correct. Now
  ratcheted by `lint-debt-baseline.txt`, so the number can only go down.
* **8** — a deadband makes a healthy steady signal and a failed instrument the
  same observation, and thirteen signals show exactly one reading across a seeded
  week. Periodic key-value reporting is the industry answer; it would roughly
  triple the row count, which is a trade with a cost rather than a free
  improvement, and **the right answer depends on what the data is for, which I do
  not currently know.** That sentence is the reason it is still open.
* **14** — one scan per 20 ms and a blocking Modbus server thread share a
  container. It is the same shape as the bug in thread 5 and it would surface
  first on a smaller machine.

## Open threads, in full


1. **done.** **Modbus wire addressing — resolved, and it took three attempts.** The net
   translation is `PDU = contract address - 40000`, but it is the *composition*
   of three shifts (model index, a one-slot block lead-in, and pymodbus's
   `PDU = index - 1`), any of which can be changed independently. Two earlier
   probes disagreed because they were run against blocks populated differently.
   The one that settled it probed a live server and located returned values in
   the block. Ten xfail'd tests are now passing.
2. **limitation.** **OPC UA engineering range is not enforced on the wire.** `EUInformation` is
   advisory in the base specification and `asyncua` does not enforce it, so a
   client can write 99 mg/L to a 0.5–6.0 setpoint. Write *permission* **is**
   enforced by the protocol. The gateway must reject out-of-range writes.
   Recorded in `docs/SECURITY.md` as a known gap.
3. **limitation.** **Aeration is near its limit.** `kla_per_h = 5.5` holds 2.0 mg/L with ~19 %
   transfer headroom. Below ~4.5 the basin is aeration-limited. That is a real
   design fact, but the margin is thin for storm scenarios.
4. **decided.** **Solids balance closes to ~15 %** and that is honest: the waste rate is
   commanded from the SRT target rather than emergent, so the residual measures
   commanded-vs-realised discharge. What must not happen is growth without
   bound, and that has its own test.
5. **done.** **The contract now links signals to Modbus registers — resolved.** Each
   register carries an optional `signal:`, validated at load: it must name a real
   signal, no two registers may claim one, and a writable register may not sit on
   a read-only signal. Fourteen of nineteen registers are linked; the other five
   — heartbeat, fault code, state bitfield, and the two halves of a 32-bit pump
   runtime — are deliberately unlinked, because they are not measurements. The
   contract loader *rejects* a contract where no register has one, so a gateway
   that cannot publish is a startup error rather than a process that runs and
   quietly stores nothing.

   The server used to hold this mapping as a literal dict in Python. That was two
   copies of the register map — the YAML a client read, and this one the server
   read — which could not disagree loudly: a register renamed in the contract
   would keep working on the server and break every client. It is derived from the
   contract now, and a test asserts the two agree.
6. **done.** **Storage runs against live databases — resolved, by removing the problem.**
   Seventeen integration tests against InfluxDB 3 and Couchbase found **fifteen
   bugs**: six in the InfluxDB schema and transport, four in the Couchbase SDK
   wrapper, and five in `compose.yaml`, three of which would each have stopped
   the stack from starting at all.

   Two of those five were found only by writing a *read*: the Query service was
   never enabled, and port 8093 was never published, so every write and every
   `get` succeeded while every query failed. **A write-only test suite would have
   found neither.** That observation outlived the databases it was made about.

   The blocker this thread existed for — InfluxDB 3 Core's non-deterministic
   planner, two `xfail`s and nine intermittently-failing course queries — was not
   fixed. It was **removed**, along with the licence key that would have fixed it.
   The replacement is 17 integration tests against Postgres that are all passing,
   all of which assert a constraint by showing it bite.

   The lesson from the whole exercise, and it is not about either database:
   **not one of the fifteen would have been found by unit tests, and not one by
   reading the code.** Three would have been found by `docker compose up`.

7. **decided, ratcheted.** **Lint debt in the older test files.** `ruff check tests/`
   reports ~60 findings in the test files alone, nearly all import ordering:
   function-local imports, unused unpacked variables. The files are correct and
   the findings are cosmetic. Left visible rather than swept in a commit that
   claims to be about something else.

   **Updated in Phase 5b.** The total across the project is 159, not 60, and the
   extra ones are mostly `E501` and `PLC0415` in `softplc/process/units.py` and
   `softplc/servers/opcua.py` — source, not tests. What changed is not the debt
   but the *gate*: `make lint` runs `ruff check .` and **has been failing the
   whole time**, while every ruff invocation in this project for four phases was
   over a subset of the packages. So the gate is now scoped to the packages that
   are clean, and the debt is measured by a ratchet against
   `lint-debt-baseline.txt` that fails only if the count goes **up**.

   Which is a better answer than "fixed them" would have been, and not only
   because 159 findings is a lot of churn to bury in a commit about something
   else. The ratchet is a *standing* change: it is the only thing here that
   stops the next twenty findings from appearing silently, and it is now in CI.

8. **decided.** **The deadband makes "no data" ambiguous, and thirteen signals show it.** A row
   exists when the value moved, so a signal whose value never moves produces one
   row, forever. Thirteen of the 57 signals produced exactly **one** reading across
   a seeded week. Nothing in the schema objects: the signal is present, the
   foreign key is satisfied, the row is valid.

   The industry answer is periodic key-value reporting — force a reading every N
   minutes regardless of movement. This project does not do it because it would
   roughly triple the row count, and that is a trade with a cost rather than a
   free improvement. Whether it is the right trade depends on what the data is
   for, and I do not currently know the answer. `sql/02-04` states the problem
   rather than papering over it.

9. **done.** **Database credentials — resolved.** The old gateway authenticated to Couchbase
   with a bucket-scoped user, verified to be unable to administer the cluster.
   The Postgres migration replaced that with one shared password owning the
   database, and I recorded it as a deliberate regression on the grounds that a
   documented gap beats a silent fix.

   That reasoning was right for the two *protocol* gaps — OPC UA's
   `EUInformation` is advisory by specification and Modbus has no authentication
   at all, so those are properties of the technology — and wrong for this one,
   which is a grant that had not been made yet. **A documented omission is a
   teaching point; a documented regression is debt somebody agreed to pay.** It is
   paid: `wwtp_writer` and `wwtp_reader` are group roles, the gateway is a
   `LOGIN` role in `wwtp_writer` and nothing else, and it cannot DELETE a
   reading, TRUNCATE, DROP a table or UPDATE the contract. Each proved by
   attempting it and reading the refusal.

   The password has to be composed as a quoted literal, because **DDL cannot be
   parameterised** — `CREATE ROLE ... PASSWORD %s` is a syntax error, and the
   first version failed with exactly that.

10. **partly done.** **False-positive alarm rules — six resolved, five impossible.** Every
    threshold is now derived from a measurement of what a *settled* healthy plant
    does, and the measurements are in `docs/ALARM-TUNING.md`. Six rules fired on a
    healthy plant; five do, and none of the five is fixable by tuning:

    * `secondary_blanket_stuck` — the clarifier blanket genuinely moves less than
      its own deadband. **A deadband makes a healthy steady signal and a failed
      instrument the same observation**, and no threshold can fix a quantity the
      database does not record.
    * `lift_pump_flow_lost` — `pump_fault` stops one pump and the controller
      starts the other, and **both lift signals are station totals**. A
      single-pump failure is structurally undetectable from the signals the
      contract collects. The fix is per-pump signals in `contracts/tags.yaml`.
    * `influent_flow_surge` — a storm's flow *slope* never exceeds a healthy
      flow's slope, though the storm's flow level triples.
    * the two `cross_validation` rules — the simulator publishes one source, so
      there is nothing to cross-validate.

    The general lesson, hit three times: **a threshold copied from a normal band
    inherits the band's optimism, and a band is a specification rather than a
    measurement of what the machine does.**

11. **done.** **The contract's `NOT_detectable_by` — resolved, and it was an absolute.**
    The coverage report disproved three of its eleven entries, and none of the
    three was a mistake about the method: a limit check *does* find
    `digester_souring`, it finds it days late, and days late is adequate for a
    fault that develops over days and useless for one that develops in an hour.

    Renamed to **`not_sufficient_alone`**, which can only be read one way, and the
    three faults where a threshold provably fires late now say so under a new
    `expects.threshold_eventually_fires` note. The coverage report distinguishes an
    *expected* late detection from a real contradiction, which is why it no longer
    says "either the contract is wrong or the rule is" three times about three
    faults where the contract was fine.

12. **open.** **The SQL course's shown outputs are not tested.** `tools/check_sql.py`
    verifies that every query runs and returns rows. Whether the output printed in
    a lesson is still what the query produces is unchecked, because the answers
    change as the seeder's random seed changes. Every shown output in `sql/` was
    generated from a real run and is correct as of this commit, and nothing will
    tell me when that stops being true. A test that fails when a lesson's
    illustrative numbers age out is a test that gets deleted rather than fixed, so
    the right fix is pinning the seeder's seed and checking the outputs — which is
    a piece of work, not a patch.

13. **open.** **`web` had no source.** `ui/web/Dockerfile` was correct and
    `ui/web/package.json` did not exist, because Phase 5 had not been written. The
    service sat behind a `ui` profile so the rest of the stack started, and every
    document's service count was accurate — while anything that implied a
    dashboard was available was not.

    **The Grafana half is done** (`ui/grafana/dashboards/`, two dashboards
    generated from the contract, provisioned rather than clicked into existence,
    with two tests that run queries against a live database). The `ui/web` half
    is this phase's last item.

    Which is worth noting as a *process* failure rather than a scheduling one: the
    `ui` profile existed, the Dockerfile existed, and every document counted the
    service correctly, so the gap was consistent everywhere it was mentioned. **A
    placeholder that is accounted for is the easiest kind of missing thing to
    forget** — there was never a moment where anything looked wrong.

14. **limitation.** **The soft PLC's Modbus link is single-threaded against its own scan loop.**
    Even correctly paced at 50 Hz, one scan per 20 ms and a blocking Modbus
    server thread share a container. It is comfortable now, but it is the same
    shape as the bug in thread 5 and it would surface first on a smaller machine.
    The fix is a separate concern — a scan budget check, or a lower default rate
    — and is not done.

15. **done.** **A parameter declared in three places and used in none.** `min_span_s` — the
    thing the previous commit called `aeration_do_sagging`'s fix — was honoured by
    the detector, honoured by `lookback_s`, and tested in the detector, and was
    **not passed by a single rule**, because a string replacement silently failed
    to match. The rule was still fitting a line over twenty minutes.

    It survived because the detector's test builds rules through
    `alarms.synthetic.rule()`, which supplies defaults for every parameter — so a
    helper that defaults the one parameter whose whole purpose is to be *absent by
    default* hides exactly this. The guard is now on the rule set, and it found a
    second ignored parameter on the same run: `direction` on
    `deviation_from_baseline`, which used `abs()` — so a rule documented as "lift
    pump current far *above* its normal draw" also fired on a large fall, and that
    single ignored parameter was a 90 % false-positive rate.

16. **open, and the highest-value item here.** **The two harnesses disagree by
    a factor of seven.** `alarms.tune` and
    `alarms.scenarios.run_fault` measure the same rules over the same window and
    report healthy DO slopes of −0.005 and −0.028 mg/L per hour. A threshold from
    the first fires on a healthy plant in the second. It is set from the wider
    measurement, with 3× the margin on either side, and the disagreement is
    **unexplained** — which is the honest place for it.

17. **done.** **The continuous aggregates were never refreshed.** `schema.sql` created
    `reading_1m` and `reading_1h`; nothing populated them, ever. A seeded week of
    4 290 000 readings produced a *2-row* `reading_1h` and every query in a stage
    of the course returned nothing. Nothing failed and no test caught it, because
    a continuous aggregate is a table *and a definition* and the definition does
    not fill the table.

18. **done.** **Two harness bugs that made every threshold wrong.** The plant's *startup*
    was in the "healthy" sample, and the fault was armed at t = 0 — so with a
    9h15m settle a two-hour storm was over before the first measurement, and the
    tool reported influent flow as identical under a storm and under a healthy
    sky. The tell was that every distribution equalled the baseline's: **an
    identical distribution is a finding about the harness, not about the fault.**

19. **open.** **The slow tests now take 18 minutes.** The settle window is set by the
    longest rule lookback, so one long-horizon rule makes every scenario slow.
    Making it depend on the rules under test is exercise 5 of `03-04`.

20. **done.** **A field called `unit` that held an area.** Every measurement in the contract
    declared its area in a key called `unit`, and the loader passed it into
    `Signal.unit`, so `signal.unit` was `"AERATION"` for every aeration signal.
    The database, the OPC UA engineering units and the Modbus scaling were all
    *correct*, because they all read `Signal.eu` — three consumers right, one
    wrong, and no test, because the wrong one was the one nothing read.

    Found by `scada/generate_tags.py`, the first consumer to *render* a unit. The
    lesson is about what a test suite covers: **a bug in an attribute nobody
    reads is not a bug that has not happened, it is a bug waiting for the first
    consumer that does.**

21. **done.** **Three Node-RED failures that all present as "it started".** The base image
    never installed the two non-core nodes (it is prebuilt; its `npm install` ran
    against *its* package.json), so the runtime sat at "Waiting for missing types"
    indefinitely with no error and no exit. `settings.js` was copied to
    `/opt/node-red/data` while the runtime reads `/data`, so every setting in it
    was silently ignored. And the hand-written `flows_cred.json` closed three
    braces for four opens, which Node-RED reported as a JSON parse error in a log
    line about a file the reader did not know existed.

    The common shape: **all three are things that start successfully.** A flow
    that imports, a runtime that boots, and a container that is healthy are all
    the same claim — "it came up" — and none of them is "it works". The same
    lesson as the stale `rotate_s` in the gateway, and the reason `make scada`
    now exists as a target that actually starts the thing.

22. **done.** **Three mistakes in one permit query, and the first two were only
    findable by running it.** The dashboard is generated from the contract, which made the
    numbers right — and the SQL still had `avg(value)` against `reading_1h`,
    which has `mean`. It read pH from the *TSS signal* (there is an
    `EFFLUENT:FLOW:PH`), and named a CTE `window`, which is reserved.

    The pH one is the instructive failure: it is a valid query against a valid
    table, and **nothing in the database objects to it**, because a unit of
    measure is carried by a signal's identity and by nothing on the row. A wrong
    signal id in a query is a wrong number, not an error — and the only thing
    that catches it is checking the identity against the contract, which is now
    `test_every_query_names_a_signal_the_contract_declares`.

    The other two failed loudly, and the general point is the same as the Node-RED
    entrypoint: **a loud failure is a gift.** The quiet version of this bug would
    have been a green compliance panel.

## What I would do next, in order

Retriaged after Phase 5b. Three of the eight are finished and one is being
finished now, so the list is short enough to actually mean something.

1. **Find the factor of seven** (thread 16). Two harnesses, same rules, same
   window, healthy DO slopes an order of magnitude apart. Until that is explained
   no threshold here is trustworthy to better than a factor of two, and the fix is
   in the harness rather than in the rules. **This is the first thing to do and
   the only thing on this list that undermines work already done.**
2. **Per-pump signals in the contract** (thread 10). `lift_pump_failure` is
   structurally undetectable because both lift signals are station totals and the
   controller compensates. A `contracts/tags.yaml` change, and the only way to
   close that fault.
3. **Pin the seeder's seed and check the course's shown outputs** (thread 12).
   Every query in `sql/` is proved to run and return rows. Nothing proves the
   numbers printed in a lesson are still the numbers its query returns, and they
   will not stay right by accident.
4. **Make the settle window depend on the rules under test** (thread 19), which
   takes the slow suite from 18 minutes back under two. Lower priority than it
   looks: the suite is slow, not broken, and CI runs the slow half nightly.
5. **A read-only Postgres role for the SCADA service.** It authenticates as the
   owner while the gateway authenticates as `wwtp_gateway` and cannot delete a
   reading. The mimic is read-only by construction, but "the flows are read-only"
   is a claim about the flows and not about the credential. This one is a real
   privilege-escalation gap and it is the only item here that is about security
   rather than about correctness.

Dropped from the list, and why:

* ~~**A CI workflow.**~~ **Done** — `.github/workflows/gates.yml`, seven jobs. And
  writing it found that `make lint`, `make types` and `make test` **had all been
  failing, or not doing what their labels said**, the whole time: I had been
  reporting the subsets that pass. See `docs/CI.md`.
* ~~**Phase 5 — Grafana.**~~ **Done** in 5a. Two dashboards, provisioned from the
  contract, with their queries executed against a live database in the test
  suite.
* ~~**Phase 5 — the Next.js page.**~~ **Done** in 5b. See `ui/web/README.md`.
* ~~**`ui/web` has no tests.**~~ Partly addressed: the page is generated from the
  contract and the contract test that would have caught a wrong signal id there
  is the same one that caught it in a Grafana query (thread 22). What is still
  missing is anything that *executes* the page.

### Done in this phase, and no longer on the list

* **The alarm engine itself.** Ten detectors, fifteen rules, a state machine, and
  a coverage audit reporting a matrix of fault against rule.
* **The database roles.** `wwtp_writer` and `wwtp_reader` are group roles; the
  gateway is a `LOGIN` role in `wwtp_writer` and nothing else. It cannot DELETE or
  TRUNCATE a reading, and each of those is proved by attempting it and reading the
  refusal. The password has to be composed as a quoted literal because **DDL
  cannot be parameterised**.
* **Every alarm threshold, measured** (thread 10). Six false positives became five,
  and the five are impossible rather than untuned.
* **The contract's detection vocabulary** (thread 11). `not_sufficient_alone`,
  `threshold_eventually_fires`, and a report that tells an expected late detection
  apart from a real contradiction.
* **`sql/03-advanced`** — continuous aggregates, chunks, retention, `EXPLAIN`. The
  stage could not be written before, because the rollups it teaches were empty.
* **`make check`**, which the CI item is a thin wrapper around.
* **Alarm acknowledgement** (thread 13 of the old numbering, now done). The
  engine rebuilds its acknowledgements from the event log, the annunciator flow
  writes `alarm_acknowledged`, and the design question it forced — per rule or
  per occurrence — is answered and pinned by a test.
* **`.github/workflows/gates.yml`** — and the two gates that had been failing, and
  the compose service that could not start at all. See `docs/CI.md`.

---

## The notebook series, part one: a gate that was green and checking nothing

Building `notebooks/02-three-kinds-of-nothing.ipynb` took four attempts and
produced six bugs, four of which are worth writing down because **none of them
failed loudly**. The pattern across all four is the same, and it is the reason this
file exists.

### The gate found zero claims and reported success

The lesson's expected output was written in bare fences:

    ```
    4,287,657 readings across 57 signals, one week
    ```

`tools/check_notebooks.py` looked for ` ```output `. It found **nothing**, and
returned success. The notebook had eleven executable cells, ran clean against a
live database, and had **no checkable claim in it at all**.

This is worse than having no gate, because it looked like verification. A gate that
finds zero problems because it found zero problems is indistinguishable, from the
outside, from a gate that found zero problems because the work is correct.

Fixed two ways, because the first fix was not enough:

* an **untagged opening fence is now a build error**, naming the line and the two
  tags to use;
* `tests/test_readme_claims.py` asserts that `build_one` raises on one, and that
  the tagged version is buildable — so the guard cannot pass vacuously the way the
  gate did.

And the untagged check itself had a bug: matching `^```$` finds closing fences too,
so a correctly tagged lesson still failed the build. `untagged_openings()` now
alternates open/closed the way a markdown parser does. **A bare regex cannot count
fences.** That was learned by a build error pointing at a correctly tagged file,
which is the only way anyone learns it.

### The checker compared only the first line of each block

Fixed the zero-claims bug, wrote `17` where the lesson said `13`, ran the gate, and
it passed.

```output
signals with 5 or fewer rows in a week: 13
```

was the first line of the block, and it still matched, so the altered line below it
was never examined. Then the mutation that found it was a **row count four lines
into a table** — `30718` → `99999` — because that is the case a reader would never
write by hand and I therefore would never have noticed by reading.

Now every line of every `output` block is checked, with elision markers (`...`,
`|---`) skipped. And the error names **the line the lesson claims**, not the line
the notebook printed, because the lesson is what is wrong and that is what the
reader has to go and fix.

### My own guards tested a copy of the logic and passed while it was broken

Two tests, both green, both meaningless:

```python
def test_the_output_checker_would_catch_a_wrong_number():
    printed = _normalise(...)          # the test's own variable
    assert all(line in printed for line in good.split("\n"))
```

It called `_normalise`, re-implemented the `in` check, and asserted against its own
list. Reverting the real checker to first-line-only left it passing. **A guard that
tests a copy of the logic is a guard that tests nothing** — and the way it was
found was to revert the production code and watch the test stay green, which is the
only test of a test that is worth running.

Rewritten to call `check_outputs` and `build_one` directly, with `tmp_path`
supplying a real source file. Both mutations now fail the test.

### The pandas unit trap that a ratio hides

`np.diff(index.view("int64")) / 1e9` is the standard idiom for converting a
`DatetimeIndex` to seconds. On pandas 3.x, `.view("int64")` returns
**microseconds**.

It did not raise. It reported `typical_s = 0.06` for a one-minute series and
`span = 0.0 h` for twelve hours — and `coverage` came out at a confident
**100.0 %**, because the same wrong divisor cancelled between numerator and
denominator.

> **A ratio that survives a unit error is the dangerous kind.** Absolute quantities
> fail loudly and get fixed; a ratio that comes out plausible-looking is the one
> that ships.

`describe()` now uses `Timedelta.total_seconds()`, and the unit is written down
once.

### Coverage measured against itself is always 100 %

The first `describe()` inferred the expected sampling interval from the data. A
series decimated to every seventh point reported **full coverage**, because after
decimation the gaps *are* regular. The metric was measuring the data against itself
and could not fail.

`expected_s` is now required for a coverage figure — the contract's `sample_ms`, or
a rate the question implies — and without it the field is **omitted from the
stamp** rather than invented. Verified: 99.9 % against a declared 60 s, **14.1 %**
when decimated.

> A qualifier that cannot fail is decoration. `stamp()` omits `coverage` rather
> than printing `nan`, because a field that is always present is a field nobody
> reads.

### The skip marker was decorative

`<!-- check: skip -->` skipped the marker *line* and then processed the following
fence normally. The block it was meant to suppress — `df.dropna()`, the first thing
notebook 02 argues against — ran, and failed on `NameError: df is not defined`.

A marker that does not set state is a comment. It has to be state, or "skip" means
"delete the comment".

### Seven wrong numbers in my own prose, all caught by the gate

Once the gate worked, it found seven claims I had written and that were false:

| claimed | actual | why it was wrong |
|---|---|---|
| `1 of 168` hourly buckets | **1 of 1** | `resample` spans the data it is given, not the week |
| `118` invented hours | **115** | the underflow signal's own span is 165 h, not 168 |
| `9` buckets with >4 readings | **49** | I wrote what the pattern suggested, not the run |
| a `0`-indexed table row | no index | `to_string(index=False)` |
| `3000.00 mg/L` | `2999.99999769` | conflating the formatted print with the value |
| `underflow_m`, 1 Hz | `underflow_solids_pct`, 5 s | I had the signal from the pre-injection seeder |
| `168 hours` | **165** | the signal does not span the dataset |

**Every one of them would have survived review.** Not one was a typo I would catch
reading — they were plausible numbers I had written from a model of the data rather
than from the data. That is the whole argument for making claims machine-checkable,
and it took a working gate to prove it rather than assert it.

### The lesson's central claim was also wrong, and the data was better

I opened notebook 02 asserting that `resample().mean()` forward-fills a quiet
signal, turning thirteen one-row signals into a fabricated flat line. **It does
not.** `resample` leaves a single-row signal with one bucket and `ffill` cannot
fill forward from a reading at the very start of the dataset.

The real fabrication case is `PRIMARY:PRI-CL-1:UNDERFLOW`: **30,718 readings, 4
distinct values**, 50 hourly buckets with data, and `ffill` producing **165** — one
of the four values held for **23 consecutive hours**. And the contrast signal is
better than the fake one: `INFLUENT:LIFT:FLOW` has 169 of 169 buckets before and
after, longest flat run 8 hours, so `ffill` is harmless on a signal that reports.

> A lesson built on a mechanism that does not fire is worse than no lesson: it
> teaches a true-sounding rule for a false reason, and the reader cannot tell the
> difference. Two of the three findings here came from the data refusing the story.

### `uv sync --extra X` replaces the environment

The first `uv sync --extra analysis` installed pandas and matplotlib and **removed**
`asyncua`, `pymodbus` and `fastapi`. Extras are additive per-invocation, not
cumulative, and the plant's own protocol stack vanished without a message about it.

`make sync` should list them all. **Not done** — a loose end, recorded here so it is
not mistaken for a decision.

### Two guards that outlived what they were written for

`test_03_advanced_is_not_described_as_unwritten` asserted that the word
**"Unwritten"** appeared in the README, on the reasoning that a table has to be
right in each direction. `04-expert/` then got written, the row was fixed, and the
guard failed.

An unwritten stage is transient; a test that *requires* the word outlives the stage
it was written for. The requirement that survives is narrower and is the one that
matters: **the table must never claim a delivered stage is missing.** It now asserts
the absence — scoped to table rows, because the README discusses this exact failure
in prose, and a guard matching the bare word would fail on its own explanation.

> How a guard gets deleted instead of fixed: make it match something it was never
> about.

Also: `make notebooks     # a comment` in a fenced bash block failed
`test_no_command_line_carries_a_trailing_comment`, correctly. In some contexts that
`#` reaches the shell as an argument.

### Notebook 01: two claims I was about to ship, both wrong

Building the orientation notebook surfaced the sharpest version of the same
failure: **I wrote a finding, then checked it, and the check refuted it in a way I
would not have noticed by reading.**

**"Four signals correlate with the air temperature at exactly 1.0000."** They do
not. `INFLUENT:FLOW:TEMP` and `SITE:WEATHER:AIR_TEMP` share only **28** readings,
and the correlation on those 28 is +0.999958 — a number that is true and that I
had rounded into a falsehood. On hourly means with a `min_periods=48` floor it
still came out because the *resampled* frame has more non-null cells than the raw
overlap suggests. A near-perfect correlation computed on 28 points is not a
finding.

**"32 of 849 pairs correlate above 0.99, driven by the daily cycle."** Also wrong,
in the way that matters: the top six pairs were the *duplicate tags from section
one*, so I had written the same finding twice and counted the duplicates as
evidence of independence. Dropping the six redundant tags gives **18 of 491**, and
the strongest surviving pair is +0.999958.

The fix is better than the correction, and it became the notebook's third
technique:

    +0.999958  n=163  INFLUENT:FLOW:TEMP         SITE:WEATHER:AIR_TEMP

**Print six decimals and the sample size.** The duplicate pair prints
`+1.000000`; the real relationship prints `+0.999958`. At the two decimals this
project's own dashboard habits reach for, both print `1.00` and nothing
distinguishes a tag-list artefact from a physical fact.

### The duplicate sweep found nine, not two

I opened the notebook naming two duplicate pairs and left "find the rest" as
exercise 1. Then I ran the sweep. **Nine pairs, and only six redundant tags** —
the four water temperatures (`INFLUENT:FLOW:TEMP`, `AERATION:AHU-1:WTEMP`,
`SECONDARY:SEC-CL-1:TEMP`, `EFFLUENT:FLOW:TEMP`) are **1,943 identical readings**
under four names, every pair among them equal, which is six of the nine pairs from
four tags.

The exercise became the notebook, because the sweep is nine lines and the lesson is
much better as "here is how you find them" than as "here are two, trust me."

One detail the sweep exposed: `EFFLUENT:FLOW:TSS` has 128,030 rows against
`SECONDARY:SEC-CL-1:OVERFLOW`'s 128,222, identical on everything they share.
**"Identical on the overlap" is not "the same series"** — check the row counts.

### A +1.0000 that is not +1

`AERATION:AHU-1:WASTE_RATE` and `INFLUENT:LIFT:CURRENT` correlate at
**+1.000000** on hourly means, and are **not** the same series: max absolute
difference 20.83 over 15,359 shared readings.

So `r == 1.0` is not available as a duplicate test even in principle — the
correlation is computed on aggregated values, two deterministic functions of a
shared driver can produce it, and pandas prints it rounded. The duplicate test
has to be on raw values, and the correlation needs its digits.

### The averaging case I chose was the wrong signal, and the right one was 21 %

My section on "two averages, ten per cent apart" used influent ammonia and got
**2 %** — a boring result I had written up as a dramatic one, because the number I
had in my head came from a scratch query that had silently `dropna()`-ed a pivot
and measured a different subset.

Ranking every signal with more than 1,000 raw readings by relative disagreement
found the real case immediately:

    AERATION:AHU-1:BLOWER_VALVE  29.36 -> 23.09  -21.4%
    INFLUENT:LIFT:WETWELL_LEVEL   3.09 ->  3.45  +11.9%
    EFFLUENT:FLOW:TURBIDITY     10.17 ->  9.22   -9.4%

**The two most active signals disagree the most**, which is the mechanism stated
as a pattern: the more a signal moves, the more its raw mean over-states its
hourly mean, because a change-triggered historian samples activity. That is
notebook 02's finding arriving from a third direction.

### A pandas detail that ate an hour of guesswork

The disagreements table printed as

    AERATION:AHU-1:BLOWER_VALVE 29.36 -> 23.09 -21.4% (n=  268,086 raw, 169 hours)

and the gate rejected it, because `f"{268086:>7,}"` is exactly seven characters —
**the thousands separator counts toward the field width**. So `>7` adds no padding
at all for six-digit numbers and one space for five-digit ones. Reconstructed from
the printed run rather than reasoned about, which is the only reliable method.

### Notebook 04: a claim that was the opposite of the truth

The notebook argues that bucketing computes a different estimator from `AVG`. The
part I was most confident about was that the continuous aggregate and pandas
would *disagree*, because their first buckets have different edges — the rollup
starts on the hour and pandas starts at `01:43:25`.

Restricted to the same window, they give **23.087** and **23.087**. Identical.

> What I had actually written was a plausible mechanism for a disagreement that did
> not exist, and I only found it because the gate compares pasted output rather
> than because the reasoning was wrong. **A wrong reason and a right answer look
> identical until someone checks the number.**

The finding that replaced it is better: the rollup spans **two extra days** and
**38,689 readings** the raw table does not have, so the *unrestricted*
`avg(mean)` is **22.774** — within **1.3 %** of the correct answer. A wrong window
returns a plausible number, which is the most dangerous shape a bug can take, and
it is worth stating that the implementation question (rollup vs pandas) is
genuinely *not* the problem here. The estimator question is.

### The identity, proved rather than asserted

`AVG(value)` equals the count-weighted mean of bucket means to **3.55e-15** —
floating-point noise, identical for any data. I had asserted this as arithmetic in
the notebook text and then made the notebook print both numbers, which is the only
reason I noticed I had written `2.75e-14` from a mental estimate.

The proof is one line of algebra and one print, and it is the kind of claim that
should never be made in prose without a run next to it.

### `f"{268086:>7,}"` is exactly seven characters

The bucket-count table came back with `(n=  268,086 raw, 169 hours)` in the run and
the gate rejected my pasted version for the whole block. The cause: **the thousands
separator counts toward the field width**, so a six-digit number gets no padding
at all under `>7` and a five-digit one gets a single space.

Reconstructed from the printed run rather than reasoned about, which is the only
reliable method and the reason the gate compares text rather than parsing it.

### Two habits worth keeping from building three notebooks

**Reconstruct claims from the run, never from memory.** Every wrong number in
these three notebooks came from writing what I expected. Every one was fixed by
copying what printed. None was found by re-reading the reasoning.

**A punchline is not a measurement.** The notebook-01 averaging section claimed
"10 %" and delivered 2 %, because the number came from a scratch query that had
silently `dropna()`-ed a pivot. The honest version — rank every signal by relative
disagreement, take the worst — gave **21.4 %** and a *mechanism* ("the more a signal
moves, the further apart the two estimators get") that the single example could
not have supported.

Ranking to find the worst case is both more honest and more useful than picking one.

### The port was the fault, and the error named the password

Notebook 01's first cell died with:

    connection to server at "127.0.0.1", port 5432 failed:
    FATAL:  password authentication failed for user "wwtp"

The password was correct. **The port was not** — this project's database is on
**55433**, and `storage.postgres.schema.dsn()` defaults to 5432. The message named
the wrong thing, and I had written a `make notebooks-open` target that handed the
kernel no `POSTGRES_PORT` at all.

**`docker compose` loads `.env` itself, which is exactly why nobody had noticed.**
`make up` and `make seed` always worked. Compose was the only thing in the
repository loading that file, and anything reaching Postgres directly —
`make psql`, `make query`, `make test`, `make notebooks-open` — was relying on its
caller to export the environment by hand. **I had been doing that by hand in my
own shell**, which is precisely the condition under which a missing line stays
missing.

Fixed at the Makefile level rather than in the notebook:

    ifneq (,$(wildcard .env))
    include .env
    export
    endif

`include` reads `.env` as make variables; a bare `export` puts all of them into
every recipe's environment. Safe here because every line is a plain `KEY=value` —
and now tested, because **make does not strip quotes**, so `FOO="bar"` exports the
literal `"bar"`, the password on screen is correct, and every connection fails on
it.

### A guard I wrote that could not catch its own target

`test_every_env_line_is_a_plain_key_equals_value` used `^[A-Za-z_][A-Za-z0-9_]*=`,
which **matches `QUOTED_SECRET="with quotes"`** — the regex never looked at the
value. Found by mutation: appended a quoted line, the test passed, and the case it
exists for walked straight through.

The regex now has to match the whole line with a value free of quotes, spaces and
backslashes, and it names what make does to each. Second time in this work that a
guard's regex was narrower than the prose it guarded.

### Jupyter rewrites notebooks that have no cell `id`

The bigger find, and it was waiting behind the port.

`nbformat` warns that cells lack an `id` field and offers `normalize()`. JupyterLab
calls it on load, marks the document dirty, and writes it back on save — so
**merely opening `01-meet-the-plant.ipynb` and pressing Ctrl-S** added an `id` to
every cell and split every `source` from a single string into a list of lines. A
585-line diff, all of it generated, none of it requested.

> A gate that fails because someone followed the instructions to use the tool is
> worse than no gate. The instruction and the gate were contradicting each other
> and I had only just written both.

Fixed by generating the file **in the form Jupyter itself writes**: `source` as a
list of lines, and `id` derived from `blake2b(cell text)` rather than randomly, so
an unchanged source still produces an unchanged file. Verified by launching
JupyterLab through `make`, opening notebook 01, saving it, and confirming
`git status` was clean and `--check` exited 0.

The `MissingIDFieldWarning` that had been in every gate run since the notebooks
existed is now gone, which is the other half of the point: it was not noise, it was
the tool telling me the file was not in the shape it wanted to own.

### A Jupyter checkpoint got committed

`.ipynb_checkpoints/` appeared the moment anyone opened a notebook, and my first
attempt at this commit included a mid-session copy of a generated file. Now
git-ignored, with the reason: it is drift with a timestamp on it.

### `@NB_PORT ?= 8899` inside a recipe is a shell command

Writing make variables as recipe lines fails with `NB_PORT: command not found`,
because a tab-prefixed line is a recipe and `?=` means something else there. Both
variables now live beside `TEST_PORT` at make level.

JupyterLab 4 also prints its own URL with the token **masked as `token=...`**, so
the target mints the token and prints the URL itself. And the port is pinned at
8899 rather than left to Jupyter, which had silently moved to 8889 because 8888 was
in use — a failure that would have shown up as a connection refused against the
documented URL.

### The port trap, three more times, and the one that was mine to expect

I fixed the Makefile so every recipe gets `.env`, and then immediately reproduced
the original failure myself by running `python -m jupyter` under `env -i` to test
something unrelated. Port 5432, "password authentication failed", the whole thing.

Which is the real lesson, and it is not the one in the commit message:

> **Fixing an environment bug at one layer does not stop it at another.** The
> Makefile now exports `.env`, which makes every `make` target safe. It does
> nothing for `pytest` run bare, for an editor's kernel, or for a script. In the
> same session `tests/test_grafana_dashboards.py` **skipped itself** on the identical
> error while every `make` target passed.

So the fix is layered deliberately, not universally:
* `Makefile` exports `.env`, so every documented target is safe from a fresh shell;
* the error message names the **port**, because the port is the tell and the
  password is not;
* the README says *use `make`, or source `.env` yourself*, rather than pretending
  the trap has been removed.

`env -i` is how I test that the Makefile fix works at all, so I hit the
complementary failure every time I used it. A test for the fix is also a test for
the thing the fix does not cover.

### `%matplotlib inline` was missing, so `make notebooks-read` showed no figures

The HTML export produced 72 output areas and **zero images**. `save()` writes PNGs
to `notebooks/figures/` correctly — they were on disk the whole time — but nothing
told the kernel to render figures *inline*, so the HTML had text and no plots.

The fix is one line at the top of each notebook's first cell, and it is a line a
notebook should have had from the start:

    %matplotlib inline

It has to be the first statement, above the imports, because it selects the
backend the whole session renders with. Now 3, 2 and 2 images respectively, and
`make notebooks-read` is genuinely readable rather than a wall of `print` output.

Worth noting what the gate said: **green throughout**. `make notebooks` executes
every notebook, verifies every claim, and never once noticed that no figure was
being rendered. Every check in this repository verifies something it was written to
verify, and "does the figure appear" had never been one of them.

### CI had never been green, and I made it worse before I made it better

Checking CI because a run was red revealed **0 successes in 22 runs**. Every
failure was older than the notebook work. Nothing about the notebooks caused any
of it; all of it had simply never been read.

Four distinct causes, in the order CI surfaced them:

1. **`.gitignore` line 36 was a bare `spool/`**, which matches a directory of that
   name at *any* depth — and `gateway/spool/` is the source package. Untracked
   since it was written. CI: `ModuleNotFoundError: No module named 'gateway.spool'`
   in five test modules, on every run.

   Nothing local could see it, because **an ignored file is still on disk**. Every
   test passed, every lesson ran, and `make lint` — which lints `gateway` — had
   nothing to say about a package it could not see.

2. **`POSTGRES_PASSWORD` was set on seven of eight jobs.** The one that missed it,
   `generated files vs the contract`, has `docker compose config` as its last step
   and failed on interpolating a variable the file requires.

3. **compose requires four such variables, not one.** `GATEWAY_DB_PASSWORD`,
   `POSTGRES_PASSWORD`, `WEB_DB_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`. I fixed one,
   pushed, and CI reported the same error naming a different variable on a
   different job that had been failing just as long.

   > Finding them one per CI run, from the error message, is the slow way to find a
   > set. `grep -oE '.${VAR:?...}' compose.yaml` lists them all and should have
   > been the first command.

4. **The OPC UA lesson gate had never worked in CI.** `check_lessons.py` runs 87
   snippets against a live server, and the `unit` job — "no database, no
   containers" — started no server. 17 snippets got `ConnectionRefusedError` and
   the gate exited 1, every run.

   > **A gate that has only ever failed is not a gate.** It is a different failure
   > from one that has never been run: it reports, every time, and is
   > indistinguishable from a real failure. That is a stronger argument for
   > watching CI than "CI catches regressions" — this caught nothing and cost four
   > CI round trips anyway.

## The mistake in the middle

Consolidating `POSTGRES_PASSWORD` to workflow level, I ran
`re.sub(r"\n *POSTGRES_PASSWORD: itpass(?=\n)", "", t)` to remove the job-level
copies. It removed **eight** of them and the workflow-level one I had just added
was in the same match set. The commit removed eight lines and added none, and its
message claimed otherwise.

The third removal was in a **`services:` block**, and service containers do not
inherit workflow-level `env` — only their own `env` block reaches them. So the
timescale container stopped starting, with:

    Database is uninitialized and superuser password is not specified.

which blames the image for configuration that was deleted.

> Consolidating a variable and deleting it are one keystroke apart, and only one of
> them is what the commit message says. The grep was right; the claim about it was
> not, and the claim is what a reviewer reads.

## Two tests that needed a `.env` to exist

Both passed locally for a week and failed in CI immediately, because CI has no
`.env`:

* one asserted that a git-ignored credentials file is present in a fresh clone;
* one asserted a child process sees `POSTGRES_PORT`, which is true on a
  developer's machine and meaningless on CI.

Both now skip without `.env`, verified by moving the file aside and watching the
reasons print.

> **CI is the only place a fresh clone exists, so CI is the only place those two
> tests could have been caught.** Both had been written to pass on the machine
> that wrote them.

## `.SHELLFLAGS` has never worked on this machine

GNU Make **3.81**; `.SHELLFLAGS` arrived in **3.82**. So `SHELL := /bin/bash` is
honoured and `.SHELLFLAGS := -euo pipefail -c` has been silently ignored since the
Makefile was written. Every recipe in this repository has run without `errexit`,
without `nounset` and without `pipefail` — for the life of the file.

Nothing has been *fixed* about it here, because the portable fix is a judgement
call: `SHELL := /bin/bash -euo pipefail` works on 3.81 by smuggling flags into
`SHELL`, and breaks on 3.82+ where make appends its own. Recorded rather than
patched, because guessing at this is how a Makefile stops working on somebody
else's machine.

## The gate that reported a password problem and the cause was a missing `;`

`make notebooks-data` failed with:

    psycopg.OperationalError: connection failed: connection to server at
    "127.0.0.1", port 5432 failed: FATAL:  password authentication failed
    for user "wwtp"

and the password was right, the user existed, and the database was up.

**The port is the tell.** This project's Postgres is on **55433** and nothing here
has ever run on 5432. A tool asking for 5432 is not authenticating badly; it is
asking a different server, and the server it reached had a different password
configured. A connection error whose *endpoint* is wrong is a configuration
error, and reading the authentication clause instead of the port is how this took
an hour.

Then the actual cause. GNU make has a **fast path**: a recipe line containing no
shell metacharacters is forked and exec'd **directly**, without ever starting
`$(SHELL)`. `$(SHELL)` is the only thing that reads `BASH_ENV`, so on that line
`tools/env.sh` never ran, `.env` was never sourced, and the command inherited
make's own environment — in which `POSTGRES_PORT` does not exist and the default
is 5432.

    make query SQL="select 1"      works   — the line has a quote
    make notebooks-data            fails   — the line has no metacharacter

Same Makefile, same moment, same database. Confirmed by bisection rather than by
reading, because reading the Makefile explains nothing here — the two lines are
identical apart from a character:

    $(PY) -m tools.notebook_data --status      not available (OperationalError)
    $(PY) -m tools.notebook_data --status ;    4,239,284 readings, the pinned seed

`tests/test_makefile_env.py` pins the mechanism, and two mutations were tried: `PY
:= .venv/bin/python` fails three tests, and `env.sh` losing its `set -a` fails
three others. A comment is not a test.

### Three gates were in this class, and the worst one passed

`make test`, `make sql` and `make lessons` all ran without `.env`. **Only
`make test` was affected in a way that could not be noticed**, because unit tests
need no database and therefore succeeded in the wrong environment. That is the
shape of the failure worth remembering: the gate that is *supposed* to be
environment-sensitive is the one that hides it.

The fix is `tools/py.sh` — sources `env.sh`, then `exec`s the venv interpreter —
and it works on the fast path too, because the kernel honours the shebang.
Sourcing `env.sh` rather than repeating `set -a; . .env` keeps exactly one place
that knows how `.env` is loaded, which is the same reason `BASH_ENV` exists at
all.

> There is no switch to turn the fast path off. The only two ways out are a
> metacharacter in every recipe, or making the *command* a shell. The second is
> one file.

## `make sql` was failing on the clock, and `check_sql.py` was right to

`check_sql.py` reported a query as `non-deterministic: run 3 differs from run 1`
and exited 1. Not a bug in the checker: the gateway was running, **writing**, and
`sql/01-beginner/01-01_ask_a_question.md` counts rows. A server still ingesting
answers a different number every few seconds, so the gate failed on the clock
rather than on the query.

This is the *same* reason the notebooks read a database of their own, arrived at
from the other direction. There, the clock was pinned so the prose could be
checked; here, the writer was stopped. `make sql` now depends on `db-still`,
which stops `gateway` and **says so on stdout**, because silently stopping a
service the user started is its own surprise.

> A non-determinism report is a measurement about the *target*, not about the
> measurement. The first instinct — trust the checker less — is backwards.

## A guard that counted the thing it was guarding

`test_no_document_says_the_ci_workflow_has_five_jobs` existed to catch a stale
job count in prose. It hardcoded 5, the workflow had 6, and it still passed: the
guard's own number was the second stale number, and the failure mode was a check
that could only be made green by **editing the test rather than the workflow**.

It now reads the count out of `gates.yml` with `yaml` and asserts that no
document's number disagrees. Widening the pattern to *any* number before "jobs"
then flagged four real claims and four false ones — `Signal.equipment` "doing two
jobs at once", and `POSTGRES_PASSWORD` "set on seven of eight jobs". Both true,
neither about CI. So the line must be *about* CI, matched against the raw line
because two of the real claims say "CI" only inside a backticked `docs/CI.md`.

And `docs/LEARNING-LOG.md` quotes two stale "five CI jobs" lines **inside a code
fence**, because finding them was the point of the entry. A check that fails on
its own evidence can only be silenced by deleting the record, so fenced lines are
excluded — explicitly, with the reason, rather than by the accident that the
document-level quote stripper happened to swallow the fence markers.

> A guard that hardcodes its own expected value is a second value to keep
> current, and it is wrong at exactly the moment the thing it watches changes.

## Forty-eight tests that had been skipping, and a password that was not the one

`make integration` reported `2 passed, 46 skipped` — on a machine where the
scratch Postgres at 55432 was **running, correct and reachable**. The reason given
in every skip was:

    FATAL:  password authentication failed for user "wwtp"

which is a true statement about a password, and not about the password being
used. The scratch instance's password is `itpass`; the test was sending the
compose stack's.

The cause is that the fixture guarded the port and not the credential, which is
backwards — a port is a decision and a password is a *default*, and only a
decision can be wrong:

    os.environ["POSTGRES_PORT"] = os.environ.get("POSTGRES_TEST_PORT", "55432")
    os.environ.setdefault("POSTGRES_PASSWORD", "itpass")   # never reached

`setdefault` never fired because **`.env` sets `POSTGRES_PASSWORD=replace-me`**,
`make` sources `.env` into every recipe, and so the variable is always present
before pytest starts. The default was unreachable on this project, and had been
since the day the fixture was written.

It is also the *third* instance of one shape in this file. The make fast path
dropped the environment on some recipes; the CI job needed a service container's
own `env`; the roles test reached past its fixture to 5432, the compose stack, and
failed on a developer's real database while the tests beside it skipped politely.
**In all three, the mistake was reaching past the thing that sets the environment
and the thing that allows the test not to run.** `POSTGRES_TEST_PASSWORD` is
passed by `make integration` and documented in `.env.example`; the roles test
takes the `db` fixture and never uses it, because the dependency *is* the point.

> A skip is a claim. A test that cannot say "not here" by design will say it
> anyway, in a message about the wrong thing, and be believed.

Also in this commit, found by reading rather than by a failure:
`test_the_lookback_accepts_fractional_hours` caught its own connection error and
skipped in every CI run, was not marked `integration`, and **had never been
checked once**. Same shape, third face: a `try`/`skip` around a connection is a
claim that the test needs one, and the marker is where that claim has to be
recorded.

## CI had been failing on every push, on two gates, for three runs

Found by reading `gh run list` rather than by being told. Both failures were
invisible locally, which is the part worth writing down.

### `--check` where the compose file has an apply

The `integration` job's setup step was

    uv run python -m storage.postgres.roles --check

and it reported `all 2 roles are as declared` on a **fresh container with no
roles in it**. True, and empty: `--check` reports and changes nothing, so the
step created nothing and then observed the nothing it had not created. Two
minutes later the SQL course failed 583 times with

    ERROR:  role "wwtp_gateway" does not exist
    STATEMENT:  SET LOCAL ROLE wwtp_gateway
    FATAL:  password authentication failed for user "wwtp_gateway"

A check where an apply belongs, reading as a check. `init-db` on a laptop does
schema → group roles → **login roles**, and the job reproduced only the first two
and then *verified* instead of applying. Both roles now applied, then verified —
separately, so the verification is an assertion rather than the same command a
second time.

### `free_port()` was green on macOS and red on Linux

`82/87 snippets ran` on every CI run, five failures, all
`address already in use ('127.0.0.1', 48500)`. The allocator asked the OS for port
0 and, if the port came back **below** its own floor of 48500, returned the
constant 48500 anyway:

    chosen = <port from the OS>
    if chosen < FIRST_SNIPPET_PORT:
        return FIRST_SNIPPET_PORT        # every snippet gets the same port

macOS's ephemeral range starts at 49152, so the branch **cannot fire on a Mac**.
Linux's starts at 32768, so it fires for roughly half of all calls. The
replacement for a constant had kept the constant, behind a branch that only the
machine I do not use takes.

It is now: bind port 0, and if the answer is below the floor or already handed
out, **walk up from the floor until a bind succeeds** — so the port returned is
known free rather than believed free — and remember every port handed out in this
process, so a snippet that leaks its server cannot have its port reused and the
failure land on the wrong lesson.

The test file is the interesting part. It substitutes `socket.socket` so that
`bind(("127.0.0.1", 0))` lands below the floor, which reproduces the CI failure
on a Mac; a mock of `free_port` would have been asserting against the shape of
the fix rather than its behaviour. And it asserts the premise too — that macOS
*cannot* take the branch — so the next reader of a green local run learns why
that is not evidence.

> A gate that passes on the machine you are standing on and fails on the runner
> has told you the gate is portable and the *assertions* are not. The bug was
> never a port; it was an operating-system default, and I tested one of them.

## Two of my own gates were about one machine, and a gitignored file was fatal

Pushed the fix above; the very next run failed again, in the two jobs that had
just been given something to do. Neither failure was a mistake of logic. Both
were **a number or a file that is true here and not there**, which is the theme
of this whole commit arriving from a third direction.

### A rollup's size on disk is a fact about a disk

Notebook 03's opening table is "what each tier costs", and the cost column
beside `rows` was `hypertable_size()` — 1 043 MB, 57 MB, 2 MB. Every other
number in the series is derived from a pinned seed, and this one was not, because
a hypertable is measured in *compressed chunks* and the compression is a
per-chunk, write-order-dependent result. The row counts matched to the digit on
the runner and the byte counts did not:

    03-choosing-a-tier.ipynb: the notebook does not print
      reading_1h 5882 2 721 24
      claimed in 03-choosing-a-tier.md. Re-run it and update the block.

Which is the sentence the gate exists to make impossible to ignore, and which
would have been *fixed* by pasting the runner's number in — a number that is then
wrong on the laptop it was written on.

So the column is gone, and the cell says why in the place a reader reaches it. The
prose needed no change, which is the test of the change: it had been arguing from
rows all along (23×, 721×, 34 978 / 1 038 / 24), and the bytes were decoration
next to an argument that did not use them.

> "Re-run it and update the block" is the right instruction for a stale number and
> the wrong one for a number that was never portable. A gate cannot tell them
> apart, so the discipline has to be at the point of writing: **if it is a
> property of the disk, the machine, or the clock, it is not a number this series
> states.**

### `.env` is gitignored, so CI had never had one

`tools/env.sh` is sourced by bash at the top of *every* recipe, via `BASH_ENV`.
It ended in a bare `. .env`, and CI — a checkout, with no credentials and no
local configuration — therefore failed **every target** with

    tools/env.sh: line 43: /…/.env: No such file or directory
    make: *** [Makefile:NNN: some-target] Error 1

including `make lint`, which has nothing to do with the environment. It cost a CI
run as well: `tests/test_makefile_env.py` invokes the real `$(PY)` wrapper, so
four of its tests failed on a runner that was doing nothing wrong.

The fix is to warn and continue, with the message naming the fix. And the second
half of it is the interesting part: the guard test
`test_the_makefile_loads_env_for_every_recipe` asserted `^set -a$`, and failed on
**correct code** the moment `set -a` was indented inside an `if [ -f .env ]`. The
assertion had been written to match the file's *shape* rather than its meaning, so
the correct repair — widen it to `^\s*set -a\s*$` — and the tempting one —
unindent the file so the old regex matches — look identical from a distance.

> A guard stricter than the thing it guards is a guard that will be deleted, and
> it is always deleted in the moment somebody does the right thing.

This is the third appearance in this commit of the same asymmetry. `free_port()`
was a Linux-only constant. `hypertable_size()` is a disk-only number. `.env` is a
file only the developer has. **All three were checked by running them on the
machine they were written on**, which is the one machine where all three are
correct.

## `0.00e+00` was a fact about my CPU

The next run failed on the same job, on a different notebook, with the same shape.
Notebook 04 proves that a count-weighted mean of hourly means is `AVG(value)`, and
proved it by printing the difference:

    weighted vs AVG difference        : 0.00e+00

**Exactly zero**, and the prose called it "the rounding of the last digit". On the
runner it was `1.33e-15`.

Both sides sum the same 168 terms in a **different order**, floating-point addition
is not associative, and the last bit therefore depends on the platform's vector
width and on which pairwise summation numpy was built with. The number is a
property of the machine, and I printed it as a property of the arithmetic.

    assert abs(weighted - raw) < 1e-9

is the version of the claim that can be true on two machines: a yes/no about the
arithmetic, rather than a rendering of the noise. The tolerance is nine decimal
places against a difference of order 10<sup>-15</sup>, so it is not a judgement
call about how close is close — the two numbers are the *same sum divided by the
same count*, and anything above floating-point noise would mean a bug.

Worth stating plainly: **the identical bug, three notebooks apart.** A hardcoded
megabyte, a printed epsilon, a port constant. In each case the number was
generated by something whose value is not a function of the pinned seed alone, and
in each case the only evidence was that it came out right here. The pinned-seed
fingerprint I was so pleased with protects against the *data* drifting; it says
nothing about the *arithmetic* drifting, because the arithmetic runs on whatever
machine asks.

## A skip that could never be reached

`_env_value()` in `tests/test_makefile_env.py` had this shape:

    for line in (ROOT / ".env").read_text().splitlines():
        ...
    pytest.skip(f"{name} is not in .env")

The skip was for "the name I want is not in the file". The failure was for "there
is no file", and it arrives **before** the skip — a guard that sits after the thing
it guards. So three tests raised `FileNotFoundError` on a runner, and the reason
the skip existed at all was recorded in a docstring describing a failure that had
already happened.

> Put the skip first. A check that fires after the thing it checks has already
> raised is a comment, and the difference is only visible on the machine where the
> thing is absent.

I found this one by *running the CI command locally with `.env` moved aside*,
which is not something the gate suggested doing and costs nothing. Two commits of
CI failures had been found by reading logs; this one took a minute, and the whole
unit suite then runs clean in both environments: 713 passed here with `.env`, and
713 passed in a stripped `env -i` without one.

That simulation also caught a fourth instance of the class, this time in
`test_scada_contract.py::test_the_scada_service_itself_is_valid`. It supplied a
fallback for `POSTGRES_PASSWORD` and then failed on the *next* variable compose
wants:

    error while interpolating services.gateway.environment.… is missing

Nothing to do with the volume arrangement the test exists for. CI sets all four
variables at workflow level, so it passed there by luck of ordering rather than by
design; the same situation is a clean skip three files away in `test_readme_claims.py`.
It now checks all four and skips with their names, which is the difference between
a test that declines to run and one that fails about a subject it was not asked
about.

## The first thing anyone did on a clean machine, and what it cost to get right

Someone cloned this repository onto a machine that had never seen it, typed
`make`, and got:

    warning: no /home/someone/developer/wwtp-iiot/.env
    ── starting the database
    error while interpolating services.db.environment.POSTGRES_PASSWORD:
    required variable POSTGRES_PASSWORD is missing a value
    make: *** [Makefile:414: db-up] Error 1

The `warning` line is this repository's own. `tools/env.sh` knew there was no
`.env`, said so, and then handed the problem to `docker compose`, which reported
it as a **password** problem two steps from its cause — the same "the port is the
tell" shape as everything else in this file, except here the wrong thing is the
*file*.

And `README.md` documents `cp .env.example .env`, in Quick start, which is a
section a reader reaches **after** trying `make`, because `make` is what
everybody tries first. A correct instruction placed after the failure it prevents
is not an instruction.

### The obvious fix was wrong, and one afternoon is what it cost

`env.sh` is the single place that knows how `.env` is loaded, and it is sourced
at the top of every recipe. So the natural repair was to have **it** write
`.env` from `.env.example` when the file is absent. That fixed the reported
error, immediately, and broke the test suite in a way that took a simulated
no-`.env` run to notice:

    FAILED test_the_makefile_loads_env_for_every_recipe

Because `tools/py.sh` sources `env.sh` too — and so does every test here that
shells out to `make` or to `$(PY)` — **running the suite created a `.env`
part-way through a run.** One test then skipped because there was no `.env`, and
a later one failed because a test before it had made one.

> An order-dependent suite passes on Tuesday. That is the sentence this
> repository keeps rediscovering, and this time I introduced it myself while
> fixing an unrelated thing.

So the writing moved to a `setup` target and `env.sh` only reads. **A file whose
job is to load the environment should not also be changing the filesystem**, and
"every recipe and every `$(PY)` call" is the wrong blast radius for a side
effect. The warning now names the command that fixes it.

### The cost of a target is a forgotten prerequisite, so a test covers it

A target has to be depended upon, and the next person can forget. That is the
whole trade, and it is the same trade as a `# noqa`: leave a hole, then put a
check in it.

`tests/test_clean_checkout.py` computes the prerequisite graph and asserts every
target that reaches the environment can **reach** `setup` — transitively, because
make finishes all prerequisites before a recipe runs, so `sql → db-still → db-up
→ setup` is sufficient and requiring a direct `setup:` on eleven targets would be
asserting a shape nobody writes. Two more tests guard the bug's *reachable* form,
which is the one that matters from now on: every `${VAR:?…}` that `compose.yaml`
requires must be defined in `.env.example`, and defined **non-empty**, because
compose's own test is a non-empty value rather than a present name.

Three mutations tried, all caught: dropping `setup` from `db-up`, making `setup`
copy unconditionally over a real `.env`, and adding a required variable to
`compose.yaml` that the template lacks.

### A bootstrap that overwrites is worse than no bootstrap

`setup` never replaces an existing `.env`. That is one behaviour from the
operator's point of view and two from the code's, and the second one is the
dangerous one: a bootstrap that clobbers a real `.env` swaps working credentials
for `replace-me` and the failure looks like a database problem, several minutes
later, in a different tool.

Verified on a real clone: `make setup` creates it, `docker compose … config -q`
exits 0, and the reported `POSTGRES_PASSWORD` error is gone.

> I also destroyed the local `.env` while testing this, by deleting a file in a
> repository I do not own, and recovered it from a stale copy in `/tmp` made
> hours earlier. The lesson is not about backups. It is that **`rm` on a path
> outside a scratch directory is not a thing to do while investigating**, and the
> same hour took the database container down for the same reason — a second
> checkout with a fixed `container_name` grabbing the one already running. Both
> were recovered, and neither was a bug in the project.
