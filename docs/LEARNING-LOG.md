# Learning log

Append-only. One entry per phase, in the learner's own words where possible.
The point is to record *what surprised me*, because that is what needs revisiting
before a design decision is made again under different assumptions.

Format: what was built, what was expected, what actually happened, what it cost.

---

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

## Open threads

1. **Modbus wire addressing — resolved, and it took three attempts.** The net
   translation is `PDU = contract address - 40000`, but it is the *composition*
   of three shifts (model index, a one-slot block lead-in, and pymodbus's
   `PDU = index - 1`), any of which can be changed independently. Two earlier
   probes disagreed because they were run against blocks populated differently.
   The one that settled it probed a live server and located returned values in
   the block. Ten xfail'd tests are now passing.
2. **OPC UA engineering range is not enforced on the wire.** `EUInformation` is
   advisory in the base specification and `asyncua` does not enforce it, so a
   client can write 99 mg/L to a 0.5–6.0 setpoint. Write *permission* **is**
   enforced by the protocol. The gateway must reject out-of-range writes.
   Recorded in `docs/SECURITY.md` as a known gap.
3. **Aeration is near its limit.** `kla_per_h = 5.5` holds 2.0 mg/L with ~19 %
   transfer headroom. Below ~4.5 the basin is aeration-limited. That is a real
   design fact, but the margin is thin for storm scenarios.
4. **Solids balance closes to ~15 %** and that is honest: the waste rate is
   commanded from the SRT target rather than emergent, so the residual measures
   commanded-vs-realised discharge. What must not happen is growth without
   bound, and that has its own test.
5. **The contract now links signals to Modbus registers — resolved.** Each
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
6. **Storage runs against live databases — resolved, by removing the problem.**
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

7. **Lint debt in the older test files.** `ruff check tests/` reports ~60
   findings, nearly all in the Phase 1–2 test files: import ordering, function-
   local imports, unused unpacked variables. The files are correct and the
   findings are cosmetic. Left visible rather than swept in a commit that
   claims to be about something else.

8. **The deadband makes "no data" ambiguous, and thirteen signals show it.** A row
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

9. **Database credentials — resolved.** The old gateway authenticated to Couchbase
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

10. **False-positive alarm rules — six resolved, five impossible.** Every
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

11. **The contract's `NOT_detectable_by` — resolved, and it was an absolute.**
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

12. **The SQL course's shown outputs are not tested.** `tools/check_sql.py`
    verifies that every query runs and returns rows. Whether the output printed in
    a lesson is still what the query produces is unchecked, because the answers
    change as the seeder's random seed changes. Every shown output in `sql/` was
    generated from a real run and is correct as of this commit, and nothing will
    tell me when that stops being true. A test that fails when a lesson's
    illustrative numbers age out is a test that gets deleted rather than fixed, so
    the right fix is pinning the seeder's seed and checking the outputs — which is
    a piece of work, not a patch.

13. **`web` has no source.** `ui/web/Dockerfile` is correct and
    `ui/web/package.json` does not exist, because Phase 5 has not been written.
    The service is behind a `ui` profile so the rest of the stack starts. Every
    document's service count is now accurate; anything that implies a dashboard is
    available is not, and `ui/grafana/dashboards/` is an empty directory for the
    same reason.

14. **The soft PLC's Modbus link is single-threaded against its own scan loop.**
    Even correctly paced at 50 Hz, one scan per 20 ms and a blocking Modbus
    server thread share a container. It is comfortable now, but it is the same
    shape as the bug in thread 5 and it would surface first on a smaller machine.
    The fix is a separate concern — a scan budget check, or a lower default rate
    — and is not done.

15. **A parameter declared in three places and used in none.** `min_span_s` — the
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

16. **The two harnesses disagree by a factor of seven.** `alarms.tune` and
    `alarms.scenarios.run_fault` measure the same rules over the same window and
    report healthy DO slopes of −0.005 and −0.028 mg/L per hour. A threshold from
    the first fires on a healthy plant in the second. It is set from the wider
    measurement, with 3× the margin on either side, and the disagreement is
    **unexplained** — which is the honest place for it.

17. **The continuous aggregates were never refreshed.** `schema.sql` created
    `reading_1m` and `reading_1h`; nothing populated them, ever. A seeded week of
    4 290 000 readings produced a *2-row* `reading_1h` and every query in a stage
    of the course returned nothing. Nothing failed and no test caught it, because
    a continuous aggregate is a table *and a definition* and the definition does
    not fill the table.

18. **Two harness bugs that made every threshold wrong.** The plant's *startup*
    was in the "healthy" sample, and the fault was armed at t = 0 — so with a
    9h15m settle a two-hour storm was over before the first measurement, and the
    tool reported influent flow as identical under a storm and under a healthy
    sky. The tell was that every distribution equalled the baseline's: **an
    identical distribution is a finding about the harness, not about the fault.**

19. **The slow tests now take 18 minutes.** The settle window is set by the
    longest rule lookback, so one long-horizon rule makes every scenario slow.
    Making it depend on the rules under test is exercise 5 of `03-04`.

20. **A field called `unit` that held an area.** Every measurement in the contract
    declared its area in a key called `unit`, and the loader passed it into
    `Signal.unit`, so `signal.unit` was `"AERATION"` for every aeration signal.
    The database, the OPC UA engineering units and the Modbus scaling were all
    *correct*, because they all read `Signal.eu` — three consumers right, one
    wrong, and no test, because the wrong one was the one nothing read.

    Found by `scada/generate_tags.py`, the first consumer to *render* a unit. The
    lesson is about what a test suite covers: **a bug in an attribute nobody
    reads is not a bug that has not happened, it is a bug waiting for the first
    consumer that does.**

21. **Three Node-RED failures that all present as "it started".** The base image
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

## What I would do next, in order

1. **Find the factor of seven** (thread 16). Two harnesses, same rules, same
   window, healthy DO slopes an order of magnitude apart. Until that is explained
   no threshold here is trustworthy to better than a factor of two, and the fix is
   in the harness rather than in the rules.
2. **Per-pump signals in the contract** (thread 10). `lift_pump_failure` is
   structurally undetectable because both lift signals are station totals and the
   controller compensates. A `contracts/tags.yaml` change, and the only way to
   close that fault.
3. **Make the settle window depend on the rules under test** (thread 19), which
   takes the slow suite from 18 minutes back under two.
4. **Pin the seeder's seed and check the course's shown outputs** (thread 12).
5. **A CI workflow.** `make check` runs the four gates in the order that fails
   fastest, so the YAML is thin — but it is missing, which means the gates run when
   I remember.
6. **`ui/web` has no tests at all.** The least verified part of the project and
   the part a portfolio reviewer will click first. Phase 5.
7. **Phase 5** — Grafana dashboards and the custom Next.js page. `ui/web` has a
   Dockerfile and no `package.json`, and `ui/grafana/dashboards/` is empty.
8. **A read-only Postgres role for the SCADA service.** It authenticates as the
   owner while the gateway authenticates as `wwtp_gateway` and cannot delete a
   reading. The mimic is read-only by construction, but "the flows are read-only"
   is a claim about the flows and not about the credential.

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
