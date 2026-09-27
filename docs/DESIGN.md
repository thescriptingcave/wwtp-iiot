# Design

Why the architecture is shaped this way, and — in more detail than is usual —
**what was rejected, and how that was established.**

The short version: this project used to store its history in InfluxDB 3 and its
metadata in Couchbase, and the stated reason was that they are "two genuinely
different jobs". That justification turned out to run backwards. The split was
*caused by* the choice of time-series database, and the argument for keeping it
was written afterwards. This document records the measurements, because a design
decision you cannot audit is a preference.

---

## The decision

**One PostgreSQL 16 database with TimescaleDB 2.30.** `reading` is a hypertable.
`site`, `equipment`, `signal` and `event` are ordinary relational tables. Events
carry a `JSONB` detail column for the part that genuinely varies.

```
site ──1──n──► equipment ──1──n──► signal ──1──n──► reading  (hypertable)
                                              event  (JSONB detail)
```

---

## What was rejected, and the evidence

### 1. The document store was holding a relational dataset

Couchbase was justified as the right home for metadata: it is flexible, it
scales, it does not impose a schema. So the first thing to do was count the
shapes:

| Key-set | Documents | Distinct shapes | Heterogeneity |
|---|---|---|---|
| `site` | 1 | 1 | none |
| `equipment#` | 22 | 1 | none |
| `tag#` | 57 | 1 | none |

**80 documents, three key-sets, exactly one shape each, zero heterogeneity.**

A document store's value proposition is schema flexibility *across heterogeneous
documents*. There were none to be flexible about. Every document in the store had
the same keys, and adding a key meant rewriting 80 documents. It was a relational
dataset being stored as documents because a document store was available.

The second measurement is sharper. The only genuinely document-shaped thing in the
system is the event log — and it does not need a document store either:

```sql
CREATE TABLE event (
    id           BIGSERIAL   PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind         TEXT        NOT NULL,
    severity     TEXT        NOT NULL CHECK (severity IN ('info','warning','critical')),
    message      TEXT        NOT NULL,
    signal_id    TEXT             REFERENCES signal (id),
    equipment_id TEXT             REFERENCES equipment (id),
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb
);
```

`kind` and `severity` are real, constrained, indexable columns, so *"everything
critical this week"* is a fast typed query. `detail` is `JSONB`, so the varying
payload stays flexible and is still indexable with GIN.

**A document store gives you the flexibility and takes away the constraints —
the wrong way round for the fields you actually query on.** In the Couchbase
version, `severity` was a key inside a document and that query was a full scan
with a filter.

### 2. The two stores could not be joined, which was the only reason there were two

Reading a number and knowing what instrument it came from — a value with a unit,
a range, a normal band, an owner — required two connections and the data in
between. In SQL that is one `JOIN`:

```sql
SELECT r.ts, r.value, s.unit, s.normal_low, s.normal_high, e.id AS equipment
FROM reading r
JOIN signal s  ON s.id = r.signal_id
LEFT JOIN equipment e ON e.id = s.equipment_id;
```

There is no interesting technical reason a document store and a time-series store
cannot be joined. The reason they were not was that joining them was awkward, and
the design adopted the awkwardness. That is a storage decision masquerading as an
architectural one.

### 3. InfluxQL lacks six things the course needs

Measured by running each query and reading the error, not from documentation:

| Missing | Consequence |
|---|---|
| `CASE` | No conditional expressions. The pivot idiom every metrics tutorial uses is a parse error. |
| `IN (…)` | A regex on a tag was the only option — slower, and wrong for a value containing metacharacters. |
| `HAVING` | Filtering an aggregate required a subquery. |
| `INTERVAL` | Durations are bare literals: `now() - 24h`, not `INTERVAL '24 hours'`. And `INTERVAL` as a *type* did not exist, so the retention policy lived in a YAML file. |
| scalar subqueries | `WHERE ts > (SELECT max(ts) …)` is a parse error. The "last 24 hours of the data" anchor had to be computed client-side. |
| `ORDER BY` on anything but `time` | *"Show me the three noisiest signals"* — the most common dashboard query — was not expressible. |

Every one of the new `sql/02-intermediate` lessons needs at least one of these.
That stage was **unwritable**, not merely awkward.

### 4. A table's schema was fixed by its first write and never released

A tag *key* that appeared later was rejected outright: *"Detected a new tag in
write."* So the rule *"never put a value in a tag"* was not a style preference —
it was the only way the write succeeded.

**This is where the project's first design rule came from, and it is worth
seeing how.** The rule was stated as *"tag by identity, field by value"*, as
though it were a principle about data modelling. It was not. It was
reverse-engineered from a database that refused writes, and then written down as
though it were a principle. That is a common and subtle way for an implementation
detail to become an unquestioned law.

The current schema has no such rule. It has foreign keys and `CHECK` constraints,
which are a different kind of thing: they do not describe a preference, they
refuse a write.

### 5. Fifteen bugs, all found by running against the live databases

None of these would have been found by unit tests, and none of them would have
been found by reading the code.

**Six in the InfluxDB schema and transport:**

1. A table's column set is fixed by its first write, so a schema generated from
   the contract could not be *changed* by a contract change.
2. A third field per point is rejected, and the error does not mention it.
3. The default write API batches asynchronously and **drops the last batch on
   interpreter shutdown, after returning success**. Every write had to be
   `SYNCHRONOUS`.
4. `field` is a reserved word in InfluxQL, so a tag had to be called `signal`.
5. `NaN`, `Inf` and quoted strings are all refused for a float column. This turned
   out to be the *feature* — see §6 — but it arrived as three separate errors.
6. Line protocol has no escaping. A comma in a tag value silently changes the
   structure of the line, and a space in a tag value does the same.

**Four in the Couchbase SDK and configuration:**

7. `--services="query,data,index"` is not optional. Without the Query service
   the cluster starts, `/nodes/self` answers, and **only reads** fail — after the
   SDK's entire retry budget, with `Streaming operation failed`, a message that
   mentions neither the network nor ports. A write-only test suite would never
   have found it.
8. Port 8093 is the query port. Omitting it from the port map means every `get`
   and `upsert` works and every query fails.
9. The application user has to be created. The cluster and the bucket existed and
   nothing had ever created the user the gateway authenticates as, so it failed
   with `AuthenticationException`.
10. The health check must be **authenticated**. Unauthenticated,
    `/nodes/self` answers 401 on a perfectly healthy node, and because everything
    waits on `service_healthy` a cosmetic bug becomes a stack that never starts.

**Five in `compose.yaml`, three of which would each have stopped the stack on a
fresh machine:**

11. The Couchbase init container could not reach the cluster: `cluster-init`
    defaults to `http://127.0.0.1:8091`, which inside a *separate* container is
    that container's own loopback.
12. Overriding `command` on the couchbase container replaces its entrypoint, which
    *is* the server, so it never started. A separate one-shot service is the only
    way.
13. `bucket-list` prints YAML-ish blocks rather than one name per line, so the
    "does it already exist" check needed a word match.
14. The role for the scoped application user is `bucket_full_access[wwtp]` with
    **brackets**. Three other spellings are plausible and all three fail.
15. InfluxDB 3 Enterprise refuses to start without a licence key, so a fresh
    `docker compose up` began by failing for a reason that had nothing to do with
    the project.

---

## The decisions in the current schema

These are the choices worth defending, and each is a place where the previous
design was either working around something or had no way to express something.

### `value` is nullable, `quality` is not, and the database enforces the relationship

```sql
CONSTRAINT reading_null_is_not_good CHECK (value IS NOT NULL OR quality <> 0)
```

A failed instrument is a row with `value = NULL` and `quality = 2`. It is not
dropped, and it is not stored as zero — because zero is a real dissolved-oxygen
concentration, a real flow rate, and a real alarm state.

InfluxDB 3 arrived at this representation, but only because it refused every
alternative: `NaN` is not a float it would accept, `Inf` is not, and a quoted
string is a different type. **The refusal was the feature.** Here it is a design
choice with a `CHECK` constraint behind it, and the difference is that a
convention is only as strong as the writer's memory.

### `reading.signal_id` is a foreign key — the single largest correctness gain

In the tag-column model a reading could name a signal that never existed, or a
typo'd one, and **nothing would say so**. The write would succeed and the data
would be unfindable forever. There was no mechanism to express the constraint,
because there was no relation to constrain.

It immediately paid for itself. On first run it refused:

```
ERROR:  insert or update on table "signal" violates foreign key constraint
DETAIL:  Key (equipment_id)=(FLOW) is not present in table "equipment".
```

Fifteen signals were naming a holder — `FLOW`, `LIFT`, `SITE`, `WEATHER` — that is
not a piece of equipment, because `Signal.equipment` was doing two jobs at once:
*"the asset id"* and *"whatever the middle of the signal id happens to be"*. Those
are now two fields, `equipment` (nullable) and `holder` (always the middle of the
id), and 24 signals correctly have `equipment = NULL`.

**A constraint found a modelling error that a year of querying had not.** That is
the argument for a relational time-series database, in one incident.

### `source` is in the primary key

```sql
PRIMARY KEY (ts, signal_id, source)
```

Both protocol faces can record the same signal at the same instant. That is what
makes *"do Modbus and OPC UA agree?"* a query rather than a hunch — and it is the
only defence against this project's signature bug class.

A 32-bit float whose words arrive **low-word-first**, decoded as though they
arrived high-word-first, produces about `2.3e-41`. Put that through every check
the system could have:

* finite? **yes**
* inside the signal's 0–20 mg/L range? **yes**
* a plausible dissolved-oxygen concentration? **no**
* would a deadband notice? **yes** — and that is the only reason it was ever
  caught, in a plant that was not actually running

Nothing about the *number* is wrong. **No validation you can write against it will
ever catch it.** The defence is to compare two independent observations of the
same physical quantity, which means storing both, which means `source` has to be
part of the row's identity.

### Both continuous aggregate tiers roll up from `reading`, not from each other

```sql
CREATE MATERIALIZED VIEW reading_1m WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 minute', ts) AS bucket, signal_id, source,
       avg(value) AS mean, min(value) AS min, max(value) AS max,
       count(value) AS n
FROM reading GROUP BY bucket, signal_id, source;
```

…and `reading_1h` with the same body and a one-hour bucket, also from `reading`.

**This is the most important single decision in the schema, and it removes a bug
the previous rollup worker documented at length.** Rolling 1 minute up into 1 hour
by averaging the 1-minute averages is wrong as soon as two minutes hold different
numbers of points — and they always do, because the deadband filtered some of them
and the last window of a run is short. The error is largest exactly when the data
is most interesting, which is during an event.

Aggregating both tiers from raw costs a second pass over the data and eliminates
the problem: `avg` is always a true average over raw points and `count` is always
exact. `tests/integration/test_postgres.py` asserts the hourly mean against the
raw mean over a deliberately uneven hour, and the assertion fails on a tiered
design.

`count(value)` rather than `count(*)`: the first counts readings you can use, the
second counts readings that happened. They are different numbers, and conflating
them is how a "fraction of readings that were bad" quietly measures the wrong
thing.

`materialized_only = true` means the aggregate does not answer for the last
incomplete window. A rollup that answers for a window that is still changing
teaches a reader to trust a number that is about to move. The price is that
"right now" is a query on `reading`, which is the correct place to ask it.

### Metadata is not repeated on every reading

Six tag columns per point, every point, forever. A point was about 1.4 kB; a row
here is 71 bytes. The metadata is stored 57 times instead of four million.

The part that is not merely about size: in the old design, changing a signal's
range was not a query you could run, it was a data migration — and it would have
been **wrong**, because the reading taken in March was compliant with the range
that applied in March. Permit limits now live on `site` for the same reason.

### The seeder uses `COPY`, the gateway uses `INSERT … ON CONFLICT`

Not a shortcut — a different tool for a different job. `COPY` is a different
protocol path inside Postgres, roughly an order of magnitude faster for bulk load,
and it **cannot upsert**. A re-run into a window already written therefore fails
loudly, which is right for a seeder and wrong for a gateway that re-sends from its
spool. The integration suite pins both halves.

### The writer's failed batch is kept, and automatic flushes back off

A failed flush keeps its rows. So the buffer is still over the row bound, so the
very next `add` triggers another flush, which also fails. At one poll per second
over 57 signals that is **57 doomed round trips per second**, against a database
that is already unwell — a self-inflicted denial of service aimed at the thing you
most need working. Found by a test; the fix is a retry deadline.

---

## What did not change, and why that matters

Roughly 5 900 lines are database-agnostic. The process model
(`softplc/process/`, 1 600 lines of hand-written Monod kinetics, mass balances and
control loops), the contract loader, both protocol servers, the deadband, the
spool, and the fault engine are untouched.

That is the argument for keeping storage behind a narrow interface, and it is why
the migration was bounded rather than open-ended. `PostgresWriter` takes an
`execute` callable rather than a connection, which is why its buffering policy is
testable with a list and no database.

**The cost of that discipline is visible in this document.** The reason there is a
long list of measured limitations here is that the previous storage was chosen
before the workload was understood, and every one of those limitations was
discovered by running something rather than by reading about it.

---

## The honest weaknesses of the current design

Stated here rather than left to be found.

**A row exists when the value moved, so "no data" is ambiguous.** The 02-04
lesson found thirteen signals that produced exactly one reading in a week. Nothing
in the schema objects: the signal is present, the foreign key is satisfied, the
row is valid. The industry answer is periodic key-value reporting — force a
reading every N minutes regardless of movement — and this project does not do it
because it would roughly triple the row count. That is a trade with a cost, not a
free improvement, and whether it is right depends on what the data is for.

**`avg()` is not bit-reproducible, and neither is the *order* of a result sorted
by it.** Floating-point addition is not associative, Postgres aggregates in
parallel by default, and it does not pin the plan. The same query on the same
data returns `6391.155254170624` and then `6391.155254170615`. Worse, two signals
whose means differ by 10⁻¹² produce two different result orderings across fifteen
runs. Round in the `ORDER BY` as well as the `SELECT`, and tie-break.

**The OPC UA write permission is enforced on the wire; the range is not.** A
client can write a value outside the signal's engineering range and the server
accepts it. It is logged. It is not rejected. See the open threads in
[`LEARNING-LOG.md`](LEARNING-LOG.md).

**The aeration DO loop has about 19 % headroom.** `kla_per_h = 5.5` holds DO 2.0
with that margin, and the basin holds about 40 kg of oxygen, so a blower trip
sags DO over one to two hours and ammonia breaks through hours later. That
*ordering* is asserted by a test and is the basis for alarm design — but 19 % is
thin, and a larger influent load would find it.

**Solids balance closes to about 15 %.** Bounded by its own test, and stated
rather than tuned away, because a mass balance that closes suspiciously well is
usually one that has been fitted.

---

## Further reading

* [`ARCHITECTURE.md`](ARCHITECTURE.md) — components and boundaries
* [`DATA-FLOW.md`](DATA-FLOW.md) — one scan, end to end
* [`LEARNING-LOG.md`](LEARNING-LOG.md) — every wrong assumption, including the
  occasions when the tests were wrong before the code was
* [`adr/0001-single-postgres-database.md`](adr/0001-single-postgres-database.md) —
  the decision record
* [`TESTING.md`](TESTING.md) — what is verified and what is deliberately not
