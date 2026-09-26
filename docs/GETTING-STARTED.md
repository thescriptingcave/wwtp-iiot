# Getting started

From nothing to a running plant you can query, in about ten minutes.

## 0. What you need

| Tool | Why | Check |
|---|---|---|
| Docker + Compose v2 | Everything except the front end | `docker compose version` |
| `uv` | Python environment and the SQL track's tooling | `uv --version` |
| Python 3.13 | Only if you want to run the PLC outside Docker | `python3 --version` |
| An InfluxDB 3 home-use licence key | InfluxDB 3 Enterprise will not start without one | [influxdata.com](https://www.influxdata.com/) |

> **On the licence.** InfluxDB 3 splits into Community (free, no key) and
> Enterprise (needs a key). The Community edition has no native SQL ingest path,
> and SQL is the entire point of this project — the `sql/` track would be
> teaching a dialect you cannot query the real data with. So: Enterprise, with a
> **home-use** licence, which is free for non-commercial use at home. That is
> this project. It is not free for a company. See `docs/SECURITY.md`.

## 1. Configure

```bash
cp .env.example .env
```

`.env` is gitignored and must stay that way. Fill in at minimum:

| Variable | Where to get it |
|---|---|
| `INFLUX_LICENSE_KEY` | InfluxData account → Licenses |
| `INFLUX_ADMIN_PASSWORD` | Yours. Created on first boot. |
| `INFLUX_TOKEN` | Created by you at first boot — see step 4 |
| `COUCHBASE_ADMIN_PASSWORD` | Yours |
| `COUCHBASE_PASSWORD` | Yours, for the app user |
| `GRAFANA_ADMIN_PASSWORD` | Only if you start the `observability` profile |

The compose file uses `${VAR:?message}` for each of these, so a missing value
fails at `docker compose up` with a message naming the variable — rather than
producing a container that exits with code 1 and a log line nobody scrolls to.

> `INFLUX_LICENSE_KEY` is read by the image at build/run time from the
> environment. It is never baked into a layer, and never appears in `git log`.

## 2. Build

```bash
docker compose build
```

One Python image serves the PLC, the gateway and the rollup worker. That is
deliberate: they share a dependency surface, and three near-identical images mean
three times the build time and three times the CVEs to track. The build
argument in `docker/Dockerfile.python` is what lets one definition serve all
three.

Watch for this line, which is the whole reason the file is shaped as it is:

```
CACHED
```

Dependencies are installed before any application source is copied, so editing
`softplc/` does not reinstall `pymodbus`. If you ever see a full dependency
reinstall after a one-line change, that layer order has been broken.

## 3. Start the plant on its own

Start the PLC alone first. It is the only service with no dependencies, and
getting it working alone means the next failure is unambiguously not the
database's fault.

```bash
docker compose up -d softplc
docker compose logs -f softplc
```

You should see, within a few seconds:

```
Modbus TCP listening on 0.0.0.0:5020 (device 1)
OPC UA server listening on opc.tcp://0.0.0.0:4840/wwtp/server/
soft PLC up — 57 signals, 22 equipment
```

**If it does not, in order of likelihood:**

1. Port already in use. `lsof -i :5020`. Change `MODBUS_PORT` in `.env`.
2. The endpoint name `softplc` does not resolve. That happens if the service is
   not on the `plant` network. It is, by default.
3. The healthcheck is failing but the container is up. `docker inspect
   --format '{{json .State.Health}}' wwtp-softplc | jq`.

### Prove it speaks both protocols

The PLC exposes 22 equipment items and 57 signals across two protocols. Check
both, from your host, with the tool that replaces UaExpert:

```bash
uv run python tools/opcua_browser.py browse
```

```
Endpoint      opc.tcp://127.0.0.1:4840/wwtp/server/
  Objects
    PLANT-A
      AERATION
        AHU-1
          do_mg_l          2.03 mg/m3   [2.0 .. 8.0]
          air_flow_m3h     7120    m3/h
```

The units and ranges come from `contracts/tags.yaml`, not from the code. A
client that can read the engineering unit and the permit limit without a lookup
table is a client that cannot be misconfigured into reading millimetres as
metres.

Watch one value move:

```bash
uv run python tools/opcua_browser.py watch AERATION:AHU-1:DO
```

Then break something on purpose:

```bash
docker compose stop softplc
docker compose up -d softplc
# in another terminal
uv run python tools/opcua_browser.py watch AERATION:AHU-1:DO
```

The value resumes from where it stopped, not from zero. The process model is
continuous, so an interruption is a gap in the record, not a reset. That is the
behaviour you want, and it is why the plant holds its state rather than
recomputing it.

## 4. Arm a fault scenario

The scenarios in `contracts/fault-scenarios.yaml` are the interesting part.
`wet_weather` is the best first one, because it exercises the whole plant:

```bash
docker compose up -d softplc \
  --force-recreate
# with the scenario:
docker compose run --rm --entrypoint python softplc \
  -m softplc.main --scenario wet_weather
```

Or more simply, list them and pick one:

```bash
uv run python -m softplc.main --list-scenarios
```

```
baseline                   Healthy plant, one week
wet_weather                Storm over a healthy plant
aeration_loss              Blower trip and recovery
night_shift_compliance     Ammonia shock at night
bad_instrument             DO probe fouling behind a healthy process
everything_at_once         Compound event
```

`bad_instrument` is the one to spend time on. The plant is **healthy** and the
DO probe is **wrong**. Every flow-based alarm stays quiet, because the flows are
correct. The only way to catch it is to compare the probe against something the
probe does not measure — which is the whole argument for the alarm engine in
Phase 4.

## 5. Start the databases

```bash
docker compose up -d influxdb couchbase
docker compose ps          # wait for both to be healthy
```

> **InfluxDB 3 does not work the way InfluxDB 1.x and 2.x do**, and this cost a
> long afternoon. There is no `DOCKER_INFLUXDB_INIT_*` setup mode, no port 8086,
> and no Docker Hub image. The compose service starts it with an explicit
> `influxdb3 serve --host-id … --object-store … --bearer-token …`, and the
> database and first token are created with the bundled CLI:
>
> ```bash
> docker compose exec influxdb influxdb3 create database wwtp --token "$INFLUX_TOKEN"
> docker compose exec influxdb influxdb3 create token
> ```
>
> `create token` prints both a plaintext token (for clients) and a hash (which
> must be passed to `serve --bearer-token`). **The server must be restarted with
> that hash**, or the API answers 404 to everything — including `/api/v3/*`,
> which does not make it obvious that the server is misconfigured rather than
> absent. The full sequence, and what each failure looks like, is in
> `docs/LEARNING-LOG.md`.
>
> This build is **not stable enough to rely on**: query results were observed to
> be non-deterministic. See the recommendation at the end of that section before
> building anything on it.

Couchbase takes 30–60 s on first boot to initialise itself. It will report
unhealthy while it does, and then become healthy without intervention. That is
normal and is why its `start_period` is 60 s rather than 10 s.

Create the InfluxDB token the gateway will use. The `DOCKER_INFLUXDB_INIT_*`
variables create an org, a bucket and an initial admin token on first boot, so
the simplest path is to take the token you put in `INFLUX_TOKEN` and confirm it:

```bash
curl -s -H "Authorization: Token $INFLUX_TOKEN" \
  http://localhost:8086/api/v3/buckets | jq '.buckets[] | {name, retentionRules}'
```

## 6. Start the rest

```bash
docker compose up -d gateway rollup web
```

`gateway` waits for `softplc`, `influxdb` **and** `couchbase` to be *healthy*,
not merely started. That distinction is the difference between a stack that comes
up cleanly and one that comes up eventually, after a burst of connection errors
in the logs that everyone learns to ignore.

Verify data is landing:

```bash
docker compose logs gateway | tail -20
```

```
scan 8412  published 57 signals (3 changed)  influx ok  spool 0
```

- **3 changed**, not 57: the deadband is working. A gateway that writes 57 points
  per scan regardless is generating 300× the data and telling you nothing extra.
- **spool 0**: nothing queued, so nothing lost.

## 7. Seed history

An empty database teaches nothing. The seeder generates a week of plant
operation — including a storm — so the dashboards and the SQL track have
something to bite on:

```bash
docker compose --profile demo run --rm seed
```

## 8. The SQL track

With data in place, the course in `sql/` becomes the main event. Start here:

```bash
uv run sql/00-foundations/01_read_one_signal.sql
```

Each lesson states a question, gives a query, and has its expected output in
`sql/_answers/`. Work through them in order — the later stages assume the shapes
the earlier ones introduced.

The single most useful lesson is `sql/02-intermediate/` on **time bucketing**,
because it is the point where time-series stops being "a table with a timestamp
column" and starts being its own thing.

## 9. The front end

```bash
docker compose up -d web                       # http://localhost:3001
docker compose --profile observability up -d grafana   # http://localhost:3000
```

Grafana is behind a profile because it is genuinely optional: this project ships
a custom dashboard, and Grafana is there so you can see how much work a
bespoke one saves.

## 10. Tear down

```bash
docker compose down          # stops containers, KEEPS data
docker compose down -v       # stops containers and DELETES all volumes
```

`down` keeping your data is deliberate. If you have ever lost a week of history
to a `down -v` while cleaning up an unrelated service, you already know why this
is called out.

## Troubleshooting

**`address already in use` on startup.** Something else holds the port.
`lsof -i :5020`. The PLC is not special here; a container is not a reservation
system.

**`connection refused` from the gateway to `influxdb`.** Either the service name
is wrong or you are on the default bridge network. On a user-defined network
Docker's DNS resolves service names; on the default bridge it does not, and
`influxdb` resolves to nothing useful.

**The gateway logs connection errors every few seconds and recovers.** The
databases were not healthy when it started. Check `docker compose ps` for
`starting` rather than `healthy`.

**Everything works but the dashboards are empty.** You have not seeded. Step 7.

**A test hangs on a Modbus socket.** Run pytest with `-p no:cacheprovider`.
pytest's cache provider can hold a session-scoped fixture's socket open between
the write and the read.

**The PLC exits after one scan from the CLI.** A signal handler fired. In a
container, check for a healthcheck that restarts it; locally, run without
`--duration` and watch for `Ctrl-C`.

## Where to go next

- `docs/DESIGN.md` — why it is built this way
- `docs/DATA-FLOW.md` — one scan, all the way through
- `docs/LEARNING-LOG.md` — what surprised the author, which is usually the
  interesting part
- `sql/` — the course
