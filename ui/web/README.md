# `ui/web` — the custom operator dashboard

Next.js 15, server-rendered, reading the database through a server-side pool.
Three pages and a health endpoint. No charting library, no CSS framework, no ORM.

```bash
make page          # regenerate lib/contract.json from contracts/tags.yaml
make web           # npm ci, build, and serve against the stack's database
make web-check     # the drift gate: is the read model in step with the contract?
```

## What is here

| Path | What it is |
|---|---|
| `generate_page.py` | **the generator.** Writes `lib/contract.json` from `contracts/tags.yaml` |
| `lib/contract.json` | **generated.** Areas, signals, units, bands, ranges, precision |
| `lib/types.ts` | the hand-written shape of that JSON, so a change is a type error |
| `lib/db.ts` | the pool, marked `server-only`, refuses to start without a credential |
| `lib/queries.ts` | the four SQL statements, and the reasoning behind each |
| `lib/format.ts` | number formatting, and the rule that `null` renders as a dash |
| `components/Sparkline.tsx` | 90 lines of SVG, which breaks its line across gaps |
| `components/Panel.tsx` | one signal: value, age, trend |
| `app/page.tsx` | overview — every signal, grouped by area |
| `app/permit/page.tsx` | the five permit parameters, and what it is not |
| `app/alarms/page.tsx` | what is waiting for a human |
| `app/api/health/route.ts` | `SELECT 1`, and a 503 when it fails |
| `tests` | `tests/test_web_page.py`, 22 tests, in the Python suite |

## The four decisions, and what each one cost

### 1. The read model is generated; the JSX is not

`contracts/tags.yaml` is the source of truth for six consumers, and this is the
seventh. A hand-written page holds signal ids somebody typed, and **a wrong
signal id is a wrong number, not an error** — thread 22 of the learning log, where
a Grafana permit dashboard read pH from the TSS signal and rendered a plausible
number.

Which is not hypothetical here: the first version of `app/permit/page.tsx` named
`EFFLUENT:FLOW:BOD` and `EFFLUENT:FLOW:NH4_IN`, **neither of which exists**. The
page built, ran, and showed "no data" for both — correctly, because a query for a
signal that is not there returns nothing rather than failing. The test caught both
on its first run.

So the *data* is derived and the *markup* is not. A generator that emits JSX
tends to emit JSX nobody wants to read, and the value here is entirely in the
numbers being right rather than the layout being derivable.

### 2. There is no credential in the browser, and there is a test for it

`lib/db.ts` imports `server-only`, which throws at build time if a client
component imports it. The connection details are runtime environment variables.
`tests/test_web_page.py` asserts no `NEXT_PUBLIC_` database variable exists in any
source file, in the compose service, or in any `ARG`/`ENV` line of the Dockerfile.

The Dockerfile used to take `NEXT_PUBLIC_POSTGRES_URL` as a build argument while
its own comment said "the browser cannot keep a secret". That is true and
irrelevant, because the argument existed. It was a host and a port, so nothing
leaked — and a reviewer reading `NEXT_PUBLIC_POSTGRES_URL` has no way to know
which it is. The pattern is one argument away from the password.

The page authenticates to Postgres as `wwtp_ui`, a `LOGIN` role in `wwtp_reader`
and nothing else: it cannot insert, update, delete or truncate. The page has no
write path at all, so a read-only role is free, and `read_only: true` on the
container makes "this service cannot be talked into writing" a property rather
than a claim.

### 3. The line breaks across gaps, and the empty state is loud

`reading_1m` is `materialized_only`, so a bucket that has not been refreshed is
**absent** rather than silently recomputed. Thirteen of the 57 signals report
once a week, because a deadband makes a healthy steady signal and a failed
instrument the same observation (thread 8).

So `trend()` generates the empty buckets with `generate_series` and a `LEFT JOIN`,
and a missing minute is a `null` that **breaks the SVG path** rather than a point
that gets skipped. Skipping would draw a straight line across an interval the page
knows nothing about, and on those thirteen signals that line would be a lie about
a week of data.

Verified by running it: 40 readings inserted with 6 removed produced **two**
`<path>` elements in the response. The empty state says `no data in this window`
rather than drawing a flat line, and `Panel` shows how long ago each signal last
reported — because a value without an age is a claim about currency the page
cannot verify.

### 4. No charting library, no CSS framework, no ORM

A dashboard that fails to render during an incident is worse than an ugly one.
The sparkline is 90 lines with no transitive dependencies; the stylesheet is one
file with custom properties; the data access is `pg` and four strings of SQL.
`test_there_is_no_charting_library` asserts the dependency list is exactly
`{next, pg, react, react-dom, server-only}`, so adding one has to be a decision
rather than an `npm install`.

Same argument as the Grafana dashboards using the built-in PostgreSQL datasource
instead of a plugin.

## How it was verified

* `npm run build` — compiles, typechecks, and reports all three pages as
  `ƒ (Dynamic)`. `force-dynamic` matters: a static build would render once at
  image-build time, when there is no database, and serve that frozen render
  forever.
* `next start` against a seeded database, then a `curl` per route: all four
  returned 200, zero errors in the log, and the permit table rendered five real
  signals with one "over limit".
* The sparkline's gap handling, by inserting and removing readings as above.
* `tsc --noEmit` — clean. It found two real errors on the first run: a wrong
  relative import in the health route, and an unreachable `'none'` branch in the
  permit page that `as const` had proved impossible by narrowing every candidate
  to `never`.

## What is *not* verified, and it is the weakest part of the project

* **The JSX renders correctly.** There is no TypeScript test runner. The
  verification above was manual — build, serve, curl — and
  `tests/test_web_page.py` does not make it automatic.
* **The sparkline's geometry.** Asserted as a *source* property (the code splits
  runs) rather than as a test of the output.
* **The TypeScript compiles**, in the sense that `npm run build` and
  `tsc --noEmit` do it — and neither is in `make check`, because both need
  `node_modules`.

The data path is tested from Python. The rendering is verified by running it and
written down here. That is a worse position than the rest of the project is in,
and it is the open thread in [`docs/LEARNING-LOG.md`](../../docs/LEARNING-LOG.md).

## A note on the `pg` dialect

`lib/queries.ts` uses `$1` placeholders, which `pg` supports and **psycopg3 does
not**. So there is no way to run these four statements from Python without a
translation, and `tests/test_web_page.py` has one — the same situation as the
Node-RED flows' `$name` dialect, for the second time in this project. The
translation has to use *named* pyformat (`%(p1)s`), because in `trend()` the
placeholders are not in numeric order: the numbers follow the function signature
and `make_interval(hours => $2::int)` appears above `a.signal_id = $1`.
