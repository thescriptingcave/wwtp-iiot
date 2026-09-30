# Getting started

From nothing to a running plant with a week of history and a dashboard you can
query.

**Time: about five minutes.** Nothing here needs a licence key, an account, or
anything from outside this repository.

---

## The short version

If you want the plant and nothing else, this is the whole thing:

```bash
make up
```

It writes a `.env` if you do not have one, starts the simulation, gives it a week
of history, and blocks until the first readings actually land. Two and a half
minutes, most of it the seed. You end up with **4.3 M readings** in TimescaleDB.

```bash
make query SQL="SELECT count(*) FROM reading"
```

To watch the plant's live values, and to read its tags over OPC UA:

```bash
uv run python tools/opcua_browser.py browse
```

The rest of this document is the same four steps with the explanations, plus
Grafana, the courses and the troubleshooting. **These three commands are the same
ones in the README's Quick start, and a test fails if they diverge** — two
documents describing the setup differently is how somebody ends up on a clean
machine running a command nobody tested there.

---

## Requirements

* Docker with Compose v2
* [`uv`](https://docs.astral.sh/uv/) for Python — or any Python 3.13
* About 2 GB of disk for a seeded week

```bash
# v2 or later
docker --version
docker compose version
uv --version
```

---

## 1. Configure

```bash
cp .env.example .env
```

The big improvement on the previous version of this stack is that there is **no
licence key**. It used to begin with

```
INFLUX_LICENSE_KEY=replace-me-with-your-home-use-licence-key
```

and a paragraph explaining that InfluxDB 3 Enterprise refuses to start without
one, that the licence is non-commercial and home-use only, and that the key had to
be obtained from InfluxData by a human. Every fresh `docker compose up` on a new
machine started by failing, for a reason that had nothing to do with the project.

**But three variables are now genuinely required, and this section was wrong
about that for a phase.** It used to say "nothing marked REQUIRED" and then list
two passwords. It now says three, because making the per-service credentials
mandatory is the whole point of having them — and it is worth being honest that
the paragraph above is now arguing partly against its own author:

| variable | who uses it | must differ? |
|---|---|---|
| `POSTGRES_PASSWORD` | `db`, `init-db`, `seed` — the **owner** | the root of the two below |
| `GATEWAY_DB_PASSWORD` | the gateway, as `wwtp_gateway` | **yes** from the owner |
| `WEB_DB_PASSWORD` | the dashboard, as `wwtp_ui` | **yes** from both |
| `GRAFANA_ADMIN_PASSWORD` | Grafana's own login | only with the `observability` profile |

So:

```bash
POSTGRES_PASSWORD=…
# not the same as the owner's
GATEWAY_DB_PASSWORD=…
# not the same as either
WEB_DB_PASSWORD=…
# only if you use the observability profile
GRAFANA_ADMIN_PASSWORD=…
```

`.env.example` already contains all four, so `cp .env.example .env` starts the
stack as-is. What it does **not** do is make them different — and three services
sharing one password is a working configuration that defeats the point of a
scoped credential while looking like you set one up. Two lines of `.env` is the
price.

**Changing a password later needs `--force-recreate`, not `restart`** — see
[section 7](#the-custom-dashboard).

---

## 2. Start the plant

```bash
docker compose up -d
```

Four services come up: `db`, `softplc`, `init-db` (which runs once and exits) and
`gateway`. Watch it work:

```bash
docker compose logs -f softplc gateway
```

You should see the soft PLC announce 57 signals and 22 assets, and the gateway
reporting readings written per poll.

**If `init-db` failed**, the gateway will not have started — it waits for it. That
ordering is deliberate: `reading.signal_id` is a foreign key onto `signal`, so
history cannot be written before the contract is in place. Check:

```bash
docker compose logs init-db
```

---

## 3. Check that data is arriving

```bash
docker compose exec db psql -U wwtp -d wwtp -c "SELECT count(*) FROM reading;"
```

Zero is a legitimate answer for the first few seconds and **not** a legitimate
answer a minute later. If it stays zero:

```bash
docker compose logs gateway | grep -i 'postgres\|spool'
```

`no POSTGRES_DSN; spooling only` means the environment block did not reach the
container — and the spool is absorbing everything, so nothing has been lost, it
just is not landing in the database yet.

---

## 4. Seed a week of history

This is the step people skip, and skipping it teaches the wrong lesson. An empty
database returns no rows for everything, and the natural conclusion — "my SQL is
wrong" — is usually right, but for the wrong reason.

```bash
docker compose --profile demo run --rm seed
```

About 4.3 million readings in about two minutes, ~29 000/second. The
seeder replays the *actual* process model through the *actual* fault scenarios, at
high speed, so the history contains a diurnal load pattern, a wet-weather storm
near the end, a blower trip and recovery, and the deadband gaps that make
`WHERE ts` interesting.

Then:

```bash
docker compose exec db psql -U wwtp -d wwtp -c \
  "SELECT count(*), min(ts), max(ts) FROM reading;"
```

```
   count    |          min          |          max
------------+------------------------+------------------------
  4289810   | 2026-09-20 02:56:23.89 | 2026-09-27 02:56:22.89
```

**The seeder refuses to run twice into the same window.** It uses `COPY`, which
cannot upsert, so a duplicate timestamp is an error rather than a silent doubling.
A doubled history would be worse than an error message. If you want to start
again:

```bash
docker compose exec db psql -U wwtp -d wwtp -c "TRUNCATE reading;"
```

---

## 5. Look at the plant

Two clients, both speaking to the soft PLC directly.

```bash
uv run python tools/opcua_browser.py browse
uv run python tools/opcua_browser.py read AERATION:AHU-1:DO
uv run python tools/opcua_browser.py watch AERATION:AHU-1:DO
```

Or with any OPC UA client — the address space is generated from
`contracts/tags.yaml`, so it is the contract, not the server, that tells you what
exists.

Modbus is a binary protocol and has no browser, which is the point. The register
map is in the contract:

```bash
uv run python -c "
from softplc.contract import contract
for r in contract().registers:
    mark = ' <- ' + r.signal if r.signal else ''
    print(f'{r.address}  {r.name:24} {r.word_order or \"\":6}{mark}')
"
```

The five registers with no signal are not oversights: a heartbeat, a fault code, a
state bitfield, and the two halves of a 32-bit counter. Not everything on a Modbus
map is a measurement, and a system that assumes it is will produce a dashboard
full of nonsense.

---

## 6. Query it

```bash
docker compose exec db psql -U wwtp -d wwtp
```

or, if you would rather not install `psql` locally:

```bash
uv run python tools/sqlrun.py "SELECT count(*) FROM reading"
```

Then work through [`sql/`](../sql/README.md). Start with
[`sql/00-foundations`](../sql/00-foundations/) — it is three lessons and it is the
foundation for everything else.

```bash
# 64 queries, all against a live server
uv run python tools/check_sql.py sql/
```

---

## 7. Optional: Grafana and the custom dashboard

```bash
docker compose --profile observability up -d
```

Grafana is on **`GRAFANA_PORT`**, which defaults to 3000. **Do not assume 3000** —
find it:

```bash
docker compose port grafana 3000
```

```bash
docker compose --profile observability up -d
```

That prints the host port it actually got. This is worth doing rather than
trusting the default, because a busy machine very often already has 3000 taken —
and **the thing that grabs it will look like it worked.** This project was
verified by hand for a phase against `http://localhost:3000`, which turned out to
be an unrelated Next.js dev server belonging to another repository. It returned
HTTP 200, it had a login page, and it had no datasource and no dashboards — which
is exactly what "Grafana is broken" looks like when you are looking at the wrong
application.

The same applies to the dashboard below, on `WEB_PORT` (3001 by default), and to
Node-RED on `SCADA_PORT` (18880 by default).

Grafana reads the hypertable through its **built-in** PostgreSQL datasource — no
plugin, because a hypertable is a table with extra storage attached and the
datasource cannot tell the difference.

Two dashboards appear, in a folder called `WWTP`, both **generated from
`contracts/tags.yaml`** by `python -m ui.grafana.generate_dashboards` rather than
clicked into existence:

| dashboard | what it is for |
|---|---|
| **WWTP — overview** | trends per area, plus a table of how long each signal last reported |
| **WWTP — discharge permit** | the parameters a permit is written against |

`allowUiUpdates: false`, so an edit in the UI is not silently reverted by the next
regeneration — and an edit *is* reverted, loudly, by `make dashboards-check`.

### If you cannot log in

`GRAFANA_ADMIN_PASSWORD` only applies when Grafana **creates** its admin user.
After that the password lives in Grafana's own database and the environment
variable is ignored, so changing it in `.env` has no effect on an existing
volume — and `docker compose up` will cheerfully start a Grafana you cannot log
into. The symptom is a `401` on every API call, which looks like a wrong password
rather than a stale one.

Reset it from inside the container:

```bash
docker compose exec grafana grafana cli admin reset-admin-password admin
```

Or start from nothing, which is the honest way to check that provisioning works,
because **every bit of Grafana's state here is provisioned from files in git**:

```bash
docker compose stop grafana
docker volume rm "$(docker volume ls -q | grep grafana)"
docker compose --profile observability up -d grafana
```

> Worth knowing when reading its logs: Grafana prints `starting to provision
> dashboards` and `finished to provision dashboards` at boot **whether or not it
> inserts anything**, and the per-file insert lines are `level=debug`, which is
> off by default. So a log with no "inserted dashboard from file" in it is
> *expected*, and I spent a while treating it as a fault. To check provisioning
> actually happened, ask Grafana:

```bash
docker compose exec grafana curl -s -u admin:replace-me http://localhost:3000/api/search?type=dash-db
```

(the password is whatever you put in `GRAFANA_ADMIN_PASSWORD`)

### The custom dashboard

```bash
docker compose --profile ui up -d web
```

or just `make web`, which is the same thing.

On <http://127.0.0.1:3001> (`WEB_PORT`). Three pages — overview, permit, alarms —
server-rendered, reading the database through a server-side pool as the
read-only role `wwtp_ui`.

**It is behind a `ui` profile**, so plain `docker compose up` does not start it.
That is deliberate: it is the one service here that needs `node_modules` and a
Next.js build, and a reader arriving for the plant should not wait for it.

The health check is `SELECT 1`, so **healthy means the page can reach the
database** — not merely that a port is open:

```bash
curl -s "http://127.0.0.1:${WEB_PORT:-3001}/api/health"
# {"ok":true,"database":"reachable","ms":1}
```

If it says `password authentication failed`, your `WEB_DB_PASSWORD` and the
database's `wwtp_ui` password disagree. **`init-db` is a one-shot service, so
changing it in `.env` does nothing until you re-run it:**

```bash
docker compose up -d --force-recreate init-db
docker compose up -d --force-recreate gateway web
```

**`--force-recreate`, not `restart`.** A restart reuses the container's existing
environment, so a changed password never reaches the service and the symptom is
still an authentication failure. This cost an hour of "the fix did not work".

`init-db` logs which variable each role's password came from, which is the
fastest way to see this:

```
password for wwtp_gateway taken from GATEWAY_DB_PASSWORD
password for wwtp_ui      taken from WEB_DB_PASSWORD
```

`[ui/web/README.md](../ui/web/README.md)` has the design decisions, and what is
*not* tested.

> This section used to say **"There is no Next.js dashboard yet. `ui/web` has a
> Dockerfile and no application, because Phase 5 has not been written."** That was
> true when written and false for four phases, which is worse than never having
> said it: a reader following this document was told a working page did not
> exist. Found by asking whether it was still true.

---

## Running the tests

```bash
uv sync --all-extras
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

`-p no:cacheprovider` is not optional decoration — it keeps pytest from writing
into `.pytest_cache` in the repo root, which the Modbus socket test objects to.

The unit tests need nothing. The integration tests need a database and **refuse to
run against one holding real data**, because they truncate `reading`:

```bash
docker compose exec db psql -U wwtp -d wwtp -c "CREATE DATABASE wwtp_test;"
POSTGRES_TEST_DB=wwtp_test .venv/bin/python -m pytest tests/integration -q
```

If you point it at the seeded database instead, it will skip with an explanation
rather than quietly destroy a week of history. That guard exists because it
already destroyed one.

---

## Configuration reference

| Variable | Default | What it does |
|---|---|---|
| `POSTGRES_USER` | `wwtp` | database user, and the **owner** |
| `POSTGRES_PASSWORD` | — | **required.** the owner's; used by `db`, `init-db`, `seed` |
| `GATEWAY_DB_PASSWORD` | — | **required.** the gateway's, as `wwtp_gateway` in `wwtp_writer` |
| `WEB_DB_PASSWORD` | — | **required.** the dashboard's, as `wwtp_ui` in `wwtp_reader` |
| `POSTGRES_DB` | `wwtp` | database name |
| `POSTGRES_PORT` | `5432` | host port mapping |
| `RETENTION_RAW_DAYS` | `7` | retention on `reading`; `0` disables |
| `RETENTION_MINUTE_DAYS` | `90` | retention on `reading_1m`; `0` disables |
| `SOFTPLC_SCAN_HZ` | `50` | PLC scan rate |
| `SEED_DAYS` | `7` | days of history the seeder replays |
| `DEMO_STORM_AT_HOURS` | `2` | hours before the *end* of the run to arm the storm; negative to skip |
| `GATEWAY_DEADBAND_DEFAULT` | `0.0` | deadband for signals the contract does not specify; `0` disables filtering |
| `GATEWAY_SPOOL_MAX_MB` | `512` | spool size cap before the oldest data is dropped |
| `GRAFANA_ADMIN_PASSWORD` | — | **required** with the `observability` profile |
| `GATEWAY_LOG_LEVEL` | `INFO` | |

**The three password rows are the ones to get right.** `init-db` creates both
login roles from them and each service authenticates with its own, so a mismatch
is an authentication failure at startup:

```bash
docker compose logs init-db | grep 'password for'
```

```text
password for wwtp_gateway taken from GATEWAY_DB_PASSWORD
password for wwtp_ui      taken from WEB_DB_PASSWORD
```

(The second block is what the log **prints**, not something to run — hence
`text` rather than `bash`. An automated pass over this file once rewrote those two
lines as shell comments, which is perfectly pasteable and completely meaningless.
It is the reason the fence languages here are deliberate.)

That log line is the fastest way to see which variable a role actually used, and
it exists because the alternative — a silent fallback to the owner's password —
defeated the point invisibly for a phase. `tests/test_readme_claims.py` asserts
that every variable compose marks required appears in the table above, which is
how the two that were missing got found.

---

## Troubleshooting

**`init-db` exits non-zero and the gateway never starts.** The gateway waits for
it, on purpose. `docker compose logs init-db` will say why — usually the password.

**`no POSTGRES_DSN; spooling only` in the gateway log.** `POSTGRES_HOST` is not in
the container's environment. Inside a container, `localhost` is that container —
a mistake that produces a connection-refused loop that looks like a networking
problem and is really a naming one. The gateway should say `db`, not `localhost`.

**`pg_isready` says healthy but connections are refused.** The Postgres image
starts a *temporary* server on a unix socket to run its init scripts. A health
check that does not pass `-h 127.0.0.1` reports ready during that window. This is
already handled in `compose.yaml`; if you have copied the health check, copy that
part too.

**The seeder says `duplicate key value violates unique constraint`.** It has
already seeded that window. `TRUNCATE reading` first — see step 4.

**A query returns no rows.** Almost always the time window. The seeded data is in
the past, so `now() - interval '1 day'` is empty. Anchor to the data:

```sql
WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 day'
```

**Thirteen signals have one reading all week.** That is the deadband, not a bug, and
it is a real limitation rather than a misconfiguration.
[`sql/02-04`](../sql/02-intermediate/02-04_gaps.md) explains it.

---

## Next

* [`DESIGN.md`](DESIGN.md) — why it is shaped this way, and what was rejected
* [`LEARNING-LOG.md`](LEARNING-LOG.md) — every wrong assumption, including the
  occasions when the tests were wrong before the code was
* [`SECURITY.md`](SECURITY.md) — the threat model and the two known gaps
