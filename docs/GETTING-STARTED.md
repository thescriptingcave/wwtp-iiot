# Getting started

From nothing to a running plant with a week of history and a dashboard you can
query.

**Time: about five minutes.** Nothing here needs a licence key, an account, or
anything from outside this repository.

---

## Requirements

* Docker with Compose v2
* [`uv`](https://docs.astral.sh/uv/) for Python — or any Python 3.13
* About 2 GB of disk for a seeded week

```bash
docker --version      # v2 or later
docker compose version
uv --version
```

---

## 1. Configure

```bash
cp .env.example .env
```

There is **nothing marked REQUIRED**, and that is the single biggest change from
the previous version of this stack. It used to begin with

```
INFLUX_LICENSE_KEY=replace-me-with-your-home-use-licence-key
```

and a paragraph explaining that InfluxDB 3 Enterprise refuses to start without
one, that the licence is non-commercial and home-use only, and that the key had to
be obtained from InfluxData by a human. Every fresh `docker compose up` on a new
machine started by failing, for a reason that had nothing to do with the project.

At minimum, change the two passwords:

```bash
POSTGRES_PASSWORD=…
GRAFANA_ADMIN_PASSWORD=…      # only if you use the observability profile
```

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
uv run python tools/check_sql.py sql/    # 64 queries, all against a live server
```

---

## 7. Optional: Grafana and the custom dashboard

```bash
docker compose --profile observability up -d
```

Grafana on <http://localhost:3000>. It reads the hypertable through TimescaleDB's
Postgres datasource.

**There is no Next.js dashboard yet.** `ui/web` has a Dockerfile and no
application, because Phase 5 has not been written, so the service sits behind a
`ui` profile and is not started. The port is reserved (`WEB_PORT=3001`) and the
Dockerfile is correct; what is missing is `package.json` and the app.

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
| `POSTGRES_USER` | `wwtp` | database user |
| `POSTGRES_PASSWORD` | — | **set this** |
| `POSTGRES_DB` | `wwtp` | database name |
| `POSTGRES_PORT` | `5432` | host port mapping |
| `RETENTION_RAW_DAYS` | `7` | retention on `reading`; `0` disables |
| `RETENTION_MINUTE_DAYS` | `90` | retention on `reading_1m`; `0` disables |
| `SOFTPLC_SCAN_HZ` | `50` | PLC scan rate |
| `SEED_DAYS` | `7` | days of history the seeder replays |
| `DEMO_STORM_AT_HOURS` | `2` | hours before the *end* of the run to arm the storm; negative to skip |
| `GATEWAY_DEADBAND_DEFAULT` | `0.0` | deadband for signals the contract does not specify; `0` disables filtering |
| `GATEWAY_SPOOL_MAX_MB` | `512` | spool size cap before the oldest data is dropped |
| `GATEWAY_LOG_LEVEL` | `INFO` | |

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
