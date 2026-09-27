# What InfluxQL would not have taught you

**This is a historical note, not a constraint.** Nothing in the course works
around InfluxQL any more, and if you are working through the lessons in order you
can skip this file entirely.

It is kept for one reason: the six things the course used to have to work around
are all things that appear in the first hour of any SQL-for-metrics tutorial, and
knowing that a query is *impossible* rather than *hard* is a different kind of
knowledge from knowing how to write it. Several of these were only discovered by
running the query and reading the error, which means the mistakes were available
to make.

Everything below was established against a live InfluxDB 3 Core instance, not from
documentation. The reason that distinction matters is in the last section.

## The six

**1. No `CASE`.** The pivot idiom every SQL-for-metrics tutorial is built on —
`max(CASE WHEN source = 'opcua' THEN value END)` — is a parse error. There is no
way to write a conditional expression, so every "one row per signal, one column
per source" query became a long-format result and a `pivot()` function that
worked differently from `PIVOT()`.

**2. No `IN`.** `WHERE signal IN ('a', 'b')` is a parse error. Regex on a tag was
the only option, which is both slower and wrong for a value containing regex
metacharacters.

**3. No `HAVING`.** Filtering an aggregate needs a subquery:

<!-- check: skip -->
```sql
-- required, in InfluxQL
SELECT mean(value) FROM aeration
WHERE time >= now() - 24h
GROUP BY signal
  HAVING mean(value) > 2      -- parse error
```

**4. No `INTERVAL`.** Durations are bare literals: `time >= now() - 24h`, not
`INTERVAL '24 hours'`. And `INTERVAL` as a *type* does not exist, so you cannot
store one, which is why the retention policy lived in a YAML file.

**5. No scalar subqueries.** `WHERE ts > (SELECT max(ts) FROM reading)` is a parse
error. The anchor had to be computed by the client and interpolated, which meant
"the last 24 hours of the data" and "the last 24 hours of the wall clock" were two
different code paths in every consumer.

**6. `ORDER BY` accepts `time` and nothing else.** `ORDER BY signal` fails with
*"invalid ORDER BY, expected TIME column"*. You could not order by an aggregate, so
*"show me the three noisiest signals"* — the single most common dashboard query —
was not expressible.

Two smaller ones worth knowing, because they will bite if you ever read older
notes in this repository:

- **`field` and `key` are reserved words.** That is why a tag was called `signal`
  and why `AS key` is a parse error.
- **Timestamp filters need RFC 3339, not epoch numbers.** Writes carried integer
  milliseconds; filters wanted a date string, and the two were not
  interchangeable. The error was *"'1758844800000' is not a valid timestamp"*.

## The two that were not about SQL

**A table's schema is fixed by its first write and never released.** A tag *key*
that appeared later was rejected outright with *"Detected a new tag in write."* A
tag whose *values* varied was merely expensive — it created a new series per
distinct value. So the rule "never put a value in a tag" was not a style
preference; it was the only way the write would succeed.

This is where the project's first design rule came from, and it is worth seeing
where the rule actually came from. It was not derived from first principles about
data modelling. It was reverse-engineered from a database that refused writes, and
it was then written down as though it were a principle. That is a common and
subtle way for an implementation detail to become an unquestioned law, and the
current schema has no such rule — it has a foreign key, which is a different kind
of thing entirely.

**The planner is non-deterministic, and the edition that fixes it needs a licence
key.** InfluxDB 3 Core picks among query plans in a way that is not stable for a
fixed data set. Nine of twenty-three course queries failed *intermittently*,
depending on which plan was chosen. The tools in this directory used to run each
query five times and classify the failure as "parse" or "planning" specifically to
work around this.

The stable planner is in the Enterprise edition, which needs a home-use licence
key obtained from InfluxData by a human. So a fresh `docker compose up` began by
failing, for a reason that had nothing to do with the project, and nine of the
lessons were unteachable on a free installation.

## The part that is still worth reading

The lesson here is not "InfluxQL is bad". It is about how a data model's shape
gets decided by the storage engine rather than by the data.

The tag-column design was not chosen because tag columns are a good fit for
industrial telemetry. It was chosen because the time-series database wanted
columns that rarely change and one that changes constantly, and then every
downstream decision followed: the metadata went to a second database because it
did not fit the first; the two databases could not be joined, which was the only
reason there were two; a new contract field could not be added to an existing
measurement; and a "relational" claim in the design document was really a claim
about a key-value store with timestamps in it.

Read [`docs/DESIGN.md`](../../docs/DESIGN.md) for the measurements. The short
version is that the two-database design was *caused by* the time-series choice,
and the justification for it ran backwards: the argument for the document store
was "these are genuinely different jobs", and the reason they were in different
jobs was the first decision.

None of that was visible while the queries worked. It became visible the moment
there were fifteen bugs to explain, and the explanation was the storage model.
