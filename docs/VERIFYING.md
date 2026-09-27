# End-to-end verification, one step at a time

Every step is a command you can paste, plus **what to look for** and **what would
mean a bug**. Nothing here needs a UI to be open except where it says so.

Work through it in order — each step assumes the previous ones passed.

Run it from the repository root. If you use a virtualenv rather than `uv`, the
`.venv/bin/python` commands work as-is; `uv run …` also works.

---

## Before you start

```bash
cd /Users/dev/Developer/wwtp-iiot
git log --oneline -1          # expect: 767fcfc
git status --short            # expect: nothing
```

**This destroys the seeded week if you run it.** The seeder takes about two
minutes to rebuild, so it is cheap, but it is not free:

```bash
make clean                   # stops everything and deletes the volumes
```

`make clean` deletes the database volume, which is where the 4.29 M seeded
readings live. `make down` keeps them.

---

## Part 1 — the contract

The claim: one file defines the plant, and everything else derives from it.

### 1.1 The contract loads, and here is what is in it

```bash
.venv/bin/python -c "
from softplc.contract import contract
c = contract()
print('version   ', c.version)
print('signals   ', len(c.signals))
print('equipment ', len(c.equipment))
print('areas     ', len(c.areas))
print('registers ', len(c.registers))
"
```

**Expect:** 57 signals, 22 equipment, 8 areas, 19 registers.
**A bug would be:** an exception, or a different count — every downstream number
in every document is asserted against this.

### 1.2 The engineering units are real, and one of them is not an area

```bash
.venv/bin/python -c "
from softplc.contract import contract
c = contract()
for s in list(c.signals.values())[:5]:
    print(f'{s.id:34} {s.field:16} {s.eu}')
"
```

**Expect:** units like `m3/h`, `mg/L`, `NTU` — never an area name. This is the
thread-20 bug: `Signal.unit` used to hold `"AERATION"` for every aeration signal.
The area is in `s.area`, the unit is in `s.eu`, and they are different fields.
**A bug would be:** a unit that looks like `AERATION` or `SECONDARY`.

### 1.3 The contract is the only place meaning is declared

```bash
git grep -l "tags.yaml" -- '*.py' | grep -v '^tests/' | wc -l
.venv/bin/python -c "
from softplc.contract import contract
c = contract()
# every register that names a signal must name a real one
for r in c.registers:
    if getattr(r, 'signal', None):
        assert r.signal in c.signals, r.signal
print('every linked register names a real signal')
"
```

**Expect:** a number, and no exception.
**A bug would be:** a register pointing at a signal the contract does not declare.
That is the whole reason the register model is derived rather than hand-written.

---

## Part 2 — the process model

The claim: the chemistry is hand-written and dimensionally right.

### 2.1 The plant builds

```bash
.venv/bin/python -c "
from softplc.process.plant import Plant
p = Plant()
print('built:', p)
"
```

**Expect:** no exception.

### 2.2 A week of simulation produces sane numbers, not just numbers

```bash
.venv/bin/python -c "
import time
from softplc.process.plant import Plant
p = Plant()
for _ in range(4000):
    p.step(1.0)
s = p.state
for k in sorted(vars(s))[:14]:
    v = getattr(s, k)
    print(f'{k:22} {v:12.4f}' if isinstance(v, float) else f'{k:22} {v}')
" 2>&1 | head -20
```

**Expect:** values in physically plausible ranges. DO in single-digit mg/L, SRT
in tens of days, concentrations in hundreds of mg/L. **A unit error in this
project produced a realised SRT of 5.6 d against a commanded 15 d while every
gauge read healthy** — so if a number is wildly out of range, the failure is in
the mass balance, not the display.
**A bug would be:** a negative concentration, a concentration of 0.002, or a
doubling every step.

### 2.3 The dimensional-analysis tests pass

```bash
.venv/bin/python -m pytest tests/test_process.py tests/test_control.py -q -p no:cacheprovider
```

**Expect:** 66 tests, all passing.

---

## Part 3 — protocols

### 3.1 Start the plant

```bash
make up
```

This starts `db`, `softplc`, `init-db` (runs once and exits) and `gateway`,
then seeds a week and waits for the first readings. **About two minutes.**

```bash
docker compose ps
```

**Expect:** `db`, `softplc`, `gateway` all `healthy` or `running`; `init-db`
`Exited (0)`.

### 3.2 Modbus: the register map, including the word-order trap

```bash
.venv/bin/python -m tools.opcua_browser --help >/dev/null 2>&1; echo "---"
.venv/bin/python -c "
from softplc.contract import contract
c = contract()
lo = [r for r in c.registers if str(getattr(r,'word_order','')).lower().startswith('low')]
hi = [r for r in c.registers if not str(getattr(r,'word_order','')).lower().startswith('low')]
print(f'{len(lo)} low-word-first, {len(hi)} high-word-first, {len(c.registers)} total')
for r in lo[:3]:
    print('  LOW ', r.name, '@', r.address)
"
```

**Expect:** both kinds present, deliberately mixed.
**A bug would be:** all one kind. The whole point is that reading a low-word-first
register as high-word-first gives a finite, in-range, wrong number — nothing in
Modbus will ever tell you.

### 3.3 Read a real register over the wire

```bash
.venv/bin/python -c "
import asyncio
from pymodbus.client import ModbusTcpClient
async def main():
    c = ModbusTcpClient('127.0.0.1', port=5020)
    await c.connect()
    r = await c.read_holding_registers(0, count=4, device_id=1)
    print('registers 0-3:', r.registers)
    c.close()
asyncio.run(main())
"
```

**Expect:** a list of four integers. Non-zero and varying.
**A bug would be:** an exception, or all zeros (the server is not publishing).

### 3.4 Browse the OPC UA address space

```bash
make browse
```

**Expect:** a tree, and a final line like

```
173 nodes visited: 35 1, 138 2
```

`1` is a folder node and `2` is a variable. Look for the eight area folders
(`INFLUENT`, `PRIMARY`, `AERATION`, `SECONDARY`, `EFFLUENT`, `SLUDGE`, `UTILITY`,
`SITE`), the equipment folders inside them, the measurement variables, and the
six `Permit_*` aggregate nodes at the top.

**Note what `browse` does *not* show: engineering units.** It prints browse names
and node classes only. This step used to claim it showed units; step 3.5 does.
**A bug would be:** an empty tree, or `0 nodes visited`.

### 3.4b Read one variable, with its metadata

This is the step that checks the address space is more than names:

```bash
uv run python tools/opcua_browser.py read AERATION:AHU-1:DO
```

**Expect:**

```
AERATION:AHU-1:DO
  value   2.0253777989827593
  status  Good
  EngineeringUnits         6152
  UnitSymbol               mg/L
  NormalBandLow            1.5
  NormalBandHigh           3.0
  SignalId                 AERATION:AHU-1:DO
```

Every line is a claim: the value is held at its setpoint, the status is `Good`,
the unit is `mg/L` and not an area name, the band is the contract's, and
`SignalId` round-trips the contract id onto the wire.

**A bug would be:** a missing line, a wrong unit, or `status` not `Good`.

**Both identifier forms work**, and both are worth typing once:

```bash
uv run python tools/opcua_browser.py read AERATION.AHU-1.do_mg_l   # dotted path
uv run python tools/opcua_browser.py read AHU-1.do_mg_l             # short form
```

The dotted path is `Area.Equipment.field` — the *field* name, not the id's third
segment. The contract id `AERATION:AHU-1:DO` also works, and **for four phases it
did not**: `make watch` and every document that showed it printed
`Not found: AERATION:AHU-1:DO`. The diagnostic tool was the one place in the
project that did not speak the project's own signal id — which is the tool you
reach for when something is not working. Fixed, and
`tests/test_opcua.py::test_the_documents_only_use_a_signal_id_the_browser_can_resolve`
now checks the documentation side.

### 3.5 Watch one signal move

```bash
make watch SIGNAL=AERATION:AHU-1:DO
```

**Expect:**

```
Watching AERATION:AHU-1:DO — Ctrl-C to stop

  #1     2.025397336285624
  #2     2.0254025248337353
  #3     2.025407704349692
```

Values arriving roughly once a second, and **the last digits changing while the
first three do not** — that is the DO control loop holding 2.0 mg/L against a
setpoint of 2.0, and the small variation is the controller working. A value
climbing monotonically, or frozen, is a bug.

Press `Ctrl-C` to stop. It runs until you stop it.

---

## Part 4 — the gateway and the historian

### 4.1 The gateway is writing, and it is the only thing writing

```bash
docker compose logs gateway --tail 20
```

**Expect:** no errors. The gateway logs its scan rate and its spool state.

### 4.2 Readings are landing, from both protocols

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT source, count(*), min(ts) AS from, max(ts) AS to
FROM reading GROUP BY source ORDER BY 2 DESC;"
```

**Expect:** two rows — `seed` (the backfilled week, ~4.29 M) and `opcua` (live
from the gateway) — with the live `max(ts)` seconds old, not hours.

**You will probably NOT see a third row, and that is a known bug, not your
machine.** See step 4.7.

### 4.3 The seeded week is really there

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT count(*) AS readings,
       min(ts)::date AS from,
       max(ts)::date AS to
FROM reading;"
```

**Expect:** ~4.29 M rows spanning seven days.
**A bug would be:** a much smaller number — the seeder would have failed.

### 4.4 The continuous aggregates are populated

This is the thread-17 bug: `reading_1m` and `reading_1h` were created and never
refreshed, so a 4.29 M-row week produced a **2-row** `reading_1h`.

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT 'reading_1m' AS view, count(*) FROM reading_1m
UNION ALL SELECT 'reading_1h', count(*) FROM reading_1h;"
```

**Expect, on this stack:** `reading_1m` ≈ 198 000, `reading_1h` ≈ 6 400.

`reading_1h` is much larger than the 168 buckets you might expect for 7 × 24 h,
because it is **buckets × signals**: 168 hours × 57 signals = 9 576, and you get
6 436 of those because the deadband means not every signal reports in every
hour. `reading_1m` at 198 000 against 4.45 M raw rows is the aggregation doing
its job.

**A bug would be:** a tiny number in `reading_1h` — the thread-17 bug produced
**2 rows** for a 4.29 M-row week, because the continuous aggregates were created
and never refreshed, and a policy cannot do the seeder's job.

### 4.5 The deadband is doing its job

Run it against the **seeded** rows only, or the live data will mask it:

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT count(*) AS signals_with_one_row FROM
  (SELECT signal_id FROM reading WHERE source = 'seed'
   GROUP BY signal_id HAVING count(*) = 1) t;"
```

**Expect: 13.** Without the `source = 'seed'` filter you get **0**, because the
live readings give every signal more than one row.

**This is not a bug.** A row is written when the value *moves*, so a signal that
never moves produces one row, forever. Thirteen of the 57 signals do that across
a seeded week. It is a real limitation with a stated cost — periodic key-value
reporting would roughly triple the row count — and it is why every UI in this
project shows "how long since this last reported" rather than drawing a line
that stops.

**This is the number behind the "empty trends are honest" design**, so it is
worth seeing once: pick one of those 13 signals and watch its panel show
"no data in this window" rather than a flat healthy-looking line.

### 4.6 The gateway's credential really is limited

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SET ROLE wwtp_gateway;
DELETE FROM reading WHERE false;" 2>&1 | head -3
```

**Expect:** `ERROR: permission denied for table reading`.
**A bug would be:** the DELETE succeeding. `wwtp_gateway` is a `LOGIN` role in
`wwtp_writer` and nothing else. It can insert and it cannot delete, because a
historian that can be made to forget is worse than one that stops.

### 4.7 Known bug: `source` is always `opcua`, so the cross-protocol check cannot work

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT DISTINCT source FROM reading ORDER BY 1;"
```

**Expect: only `opcua` and `seed`. There is no `modbus`, and there never will be
from the live path.**

This is a real bug, found by this walkthrough, and it is the most consequential
one in the project. The chain:

1. `gateway/main.py::poll_once` polls both protocols and merges them into **one
   `readings` dict**. Where they disagree, the comment says *"OPC UA wins on
   conflict"*, because it carries a StatusCode and Modbus does not.
2. So by the time `_publish` is called, **the protocol a reading came from no
   longer exists** as information.
3. `SpoolRecord` has four fields — `ts`, `signal`, `value`, `quality`. **There is
   no source field.** A real file from the volume:

   ```json
   {"ts":1790534563,"s":"SECONDARY:SEC-SCR-1:TORQUE","v":44.84732813,"q":0}
   ```

4. `drain()` therefore writes `source="opcua"` unconditionally.

**Why that matters more than a mislabelled column.** `source` is part of the
primary key of `reading`, and the README's answer to Modbus's silent-corruption
problem is:

> The only defence is to compare two independent observations of the same
> physical quantity, which is why `source` is part of the primary key.

A wrong word order yields `2.3e-41` — finite, in-range, and undetectable. The
*designed* defence is to read the same physical quantity over both protocols and
disagree. **That defence is not implemented**, because there is only ever one
source in practice. Modbus is currently a *fallback* for signals OPC UA has gone
silent on, not a second observation.

Every unit test passes, because the writer's tests and the spool's tests each
test their own half and neither knows the provenance does not survive the trip.
That is why it survived to this point, and it is the strongest argument in the
project for doing exactly what you are doing now.

**Open thread.** Not fixed, because the fix is a schema change (a field on
`SpoolRecord`, which changes the on-disk format) plus a decision about the
primary key, and both deserve their own commit.

---

## Part 5 — the SQL course

The claim: 64 queries, all run against a real seeded database.

```bash
make sql
```

**Expect:**

```
64 queries in 21 files: 64 ok, 0 failed, 0 empty, 53 illustrative (skipped)
```

- `0 failed` — every query runs.
- `0 empty` — every query that *should* return rows does. **An empty result in a
  seeded database usually means a wrong signal id.**
- `53 illustrative` are skipped by design: they are snippets in prose, marked
  `<!-- check: skip -->`, and several are meant to come back empty.

**A bug would be:** any `failed`, any `empty`, or a query count below 64.

Now run one by hand and look at the answer:

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT signal_id, count(*) AS n, avg(value) AS mean
FROM reading_1h
WHERE bucket > now() - interval '24 hours'
  AND signal_id LIKE 'AERATION%'
GROUP BY signal_id ORDER BY mean;"
```

**Expect:** a handful of signals with means in single-digit mg/L.

---

## Part 6 — the alarm engine

The claim: 10 detectors, 15 rules, every threshold measured rather than guessed.

### 6.1 The false-positive ratchet

```bash
.venv/bin/python -m pytest tests/test_alarm_rules.py -q -p no:cacheprovider \
  -k "healthy_plant"
```

**Expect:** pass. It asserts the *set* of rules that fire on a healthy plant and
the *count*, so a newly-tuned rule has to be removed deliberately and a
regression is an obvious failure.

### 6.2 The coverage matrix — fault against rule

This is the tool that told the project its own thresholds were wrong.

```bash
make coverage
```

**Expect, after about eight minutes:** a matrix of 11 faults × 15 rules, and a
report naming the faults that no rule catches. Read the summary at the end — it
is the most useful output in the project.

**A bug would be:** a rule that fires on almost every fault (that was
`aeration_do_sagging` at one point, firing on ten of eleven), or a fault with an
empty row and no explanation.

### 6.3 The engine against the live historian

```bash
make alarms
```

**Expect:** a tick line every second or so:

```
tick=N rules=15 active=… N waiting for a human (log) events=…
```

Two different numbers, deliberately: `active` is what the engine has *evaluated*
this tick; `waiting for a human` is what the **event log** says is outstanding.
They differ after a restart, and the difference is the reason `alarms/replay.py`
exists.

Press `Ctrl-C`.

### 6.4 Acknowledgement is durable, and this is the one worth checking

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT kind, count(*), max(ts) FROM event
WHERE kind LIKE 'alarm_%' GROUP BY kind ORDER BY 1;"
```

**Expect:** rows for `alarm_raised`, and `alarm_cleared` for warnings.
**Note:** a `critical` alarm **latches** — the engine never writes
`alarm_cleared` for one. It leaves the panel when a human acknowledges it. That is
why "not cleared" is not a test for "still on the panel", and it is the bug the
replay module was written after.

```bash
.venv/bin/python -m alarms.replay
```

**Expect:** a summary — how many alarms, how many still standing, and either a
list waiting for a human or "nothing is waiting for an acknowledgement".
**A bug would be:** an exception, or a count that disagrees with the query above.

---

## Part 7 — Node-RED

```bash
make scada
sleep 20
docker compose logs scada --tail 20
```

**Expect:**
- `nodered: assembled /data/flows.json from 3 file(s), 45 nodes`
- `nodered: writing /data/flows_cred.json for wwtp@db:5432/wwtp`
- **zero lines containing "error"**

The three things to check, because all three of these have shipped broken:

```bash
docker compose logs scada 2>&1 | grep -ci error     # expect: 0
docker exec wwtp-scada sh -c 'grep -c "\"type\"" /data/flows.json'   # expect: 45
docker compose ps scada --format '{{.Status}}'       # expect: healthy
```

**A bug would be:** any non-zero error count, a node count other than 45, or a
status that is not healthy. A Node-RED container that starts cleanly, deploys
nothing, and logs nothing is the single most common failure shape in this
project's history — three separate incidents.

The flows are read from the live database, so the mimic panels are showing real
data. There is no editor (`NODE_RED_EDITOR=false`) and the flows are generated
from the contract, which is what `make scada-check` verifies.

---

## Part 8 — Grafana

```bash
make grafana
```

Then open the URL it prints. If port 3000 is taken on your machine it will be
3002 — check with `docker compose ps grafana`.

**Expect:** a datasource called `wwtp-postgres` that says **OK**, and two
dashboards: **Plant overview** and **Discharge permit**.

Check three things, because they have each been wrong:

1. **The permit page shows pH from `EFFLUENT:FLOW:PH`** — not from the TSS
   signal. A wrong signal id is a wrong *number*, never an error, so nothing in
   the database objects.
2. **A panel with no data says so** rather than drawing a flat line.
3. **The trend lines are smooth.** If a value that should be ~2.0 is reading
   2.3e-41, that is the Modbus word-order trap reaching the dashboard.

---

## Part 9 — the custom web dashboard

```bash
make web
sleep 25
docker compose ps web --format '{{.Status}}'
```

**Expect:** `healthy`. The health check runs `SELECT 1`, so "healthy" means the
page can reach the database — not merely that a port is open.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001/            # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001/permit      # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001/alarms      # 200
curl -s http://127.0.0.1:3001/api/health
```

**Expect:** three 200s and

```json
{"ok":true,"database":"reachable","ms":1}
```

Now the credential check, which is the one that matters:

```bash
docker exec wwtp-web sh -c \
  "grep -rl 'POSTGRES_PASSWORD\|POSTGRES_HOST\|NEXT_PUBLIC' /app/.next/static | wc -l"
```

**Expect: `0`.** Nothing about the database reaches the browser. The connection
details are runtime environment variables, `lib/db.ts` is marked `server-only`
and throws at build time if a client component imports it, and the page
authenticates as `wwtp_ui` in `wwtp_reader`.

And the role really is read-only:

```bash
docker exec wwtp-web sh -c "echo \\"SELECT 1;\\" | true"  # (no-op, for shell habit)
```

Better, through the health endpoint's own connection — or just trust step 4.6's
pattern applied to `wwtp_ui`, which the test suite does on every CI run.

Open <http://127.0.0.1:3001> and look at the **Plant overview** page. You should
see eight areas, 57 panels, each with a value, a unit, a "how long ago", and a
trend. If the trends are empty, that is because the seeded data has aged past the
6-hour window — and the page **says so** in a banner rather than sliding its
window back to hide it. That banner is the honest behaviour, not a failure.

---

## Part 10 — the gates

Everything CI will run, locally:

```bash
make check
```

**Expect, in order:**

```
── ruff ──            All checks passed!
── lint debt ratchet  159 findings, baseline 159
── mypy ──             Success: no issues found in 55 source files
── unit tests ──       606 passed
── the SQL course ──   64 queries in 21 files: 64 ok, 0 failed, 0 empty
── all gates green ──
```

Then the two that are not in `make check`:

```bash
.venv/bin/python -m pytest tests/integration -q -p no:cacheprovider   # 46 passed
make integration                                                            # or this
```

**Expect:** 46 passed.

### 10.1 The drift gates — generated files vs the contract

```bash
for m in scada.generate_tags scada.build_flows ui.grafana.generate_dashboards ui.web.generate_page; do
  printf '%-34s ' "$m"; .venv/bin/python -m "$m" --check >/dev/null 2>&1 && echo ok || echo DRIFT
done
```

**Expect:** `ok` four times.
**A bug would be:** `DRIFT` — which would mean a signal was renamed in the
contract and a consumer was not regenerated. That is the classic industrial
integration failure: plausible-looking wrong data rather than an error.

### 10.2 The images build

```bash
docker compose build db
docker compose --profile scada build scada
docker compose --profile ui build web
```

**Expect:** all three succeed. The scada one is worth watching — its base image is
*prebuilt*, so it never installs a `package.json` added afterwards, and the
runtime then sits at "Waiting for missing types" forever. The Dockerfile works
around that; this is the step that proves it.

### 10.3 The Node-RED node types resolve

```bash
docker compose --profile scada run --rm --no-deps --entrypoint sh scada -c \
  "ls /data/node_modules | grep -qx node-red-contrib-postgresql &&
   ls /data/node_modules | grep -qx node-red-contrib-modbus &&
   echo 'both node types present'"
```

**Expect:** `both node types present`. "Builds successfully" and "the flows load"
are different claims; only the second one matters.

---

## Part 11 — the claims in the documents

Because "the docs say so" is the claim most likely to be wrong, and five of them
were.

```bash
.venv/bin/python -m pytest tests/test_readme_claims.py -q -p no:cacheprovider
```

**Expect:** 31 passed. This file counts what `README.md`, `docs/TESTING.md` and
`docs/CI.md` state and compares against reality. It has already caught five false
claims and three documents quoting a stale "57 queries".

Two of its tests are worth reading, because they are the kind of test most
projects do not have:

- `test_the_documented_test_counts_match_the_suite` asserts a table **about test
  counts, including a row for itself**. Add a test to it and it fails. That is the
  intended behaviour.
- `_claims_only` exists because a sweep test kept failing on the *correction
  notes* that quoted the wrong numbers in order to say they were wrong.

---

## Part 12 — the security posture

```bash
docker compose exec -T db psql -U wwtp -d wwtp -c "
SELECT rolname, rolsuper, rolcreatedb
FROM pg_roles WHERE rolname LIKE 'wwtp%' ORDER BY 1;"
```

**Expect:** three `rolsuper = false`, and the two login roles.

```bash
for role in wwtp_gateway wwtp_ui; do
  echo "--- $role ---"
  docker compose exec -T db psql -U wwtp -d wwtp -c "SET ROLE $role; DELETE FROM reading WHERE false;" 2>&1 | head -1
done
```

**Expect:** `permission denied for table reading`, twice.

Then check the two that are **known gaps**, so you do not mistake them for new
ones:

```bash
grep -n "POSTGRES_USER" compose.yaml
```

**Expect:** `gateway` → `wwtp_gateway`, `web` → `wwtp_ui`, and **`scada` and
`grafana` → `wwtp` (the owner)**. That is **Gap 3** in `docs/SECURITY.md` and an
open thread in the learning log. Neither service needs owner privileges; the fix
is one `login_role` call each and it is not done.

---

## Part 13 — everything the project claims about itself

```bash
.venv/bin/python -c "
from softplc.contract import contract
import yaml, pathlib
c = contract()
f = yaml.safe_load(pathlib.Path('contracts/fault-scenarios.yaml').read_text())
print('signals   ', len(c.signals))
print('equipment ', len(c.equipment))
print('faults    ', len(f['faults']), '   <- fault-scenarios.yaml, NOT tags.yaml')
print('scenarios ', len(f['scenarios']))
print()
print('contract files:', sorted(p.name for p in pathlib.Path('contracts').glob('*.yaml')))
"
```

**Expect:** 57, 22, 11, 6 — and **two** contract files, not one. The README used
to claim one file held all four, which was the single most important false
statement in the repository.

---

## If something fails

The most useful single command:

```bash
make check
```

It runs the four gates in the order that fails fastest, and a failure names the
gate. For anything involving a container:

```bash
docker compose logs --tail 50 <service>
```

And if a generated file is stale:

```bash
make scada-flows dashboards page && git diff --stat
```

`git diff --stat` showing a non-empty diff after regenerating is the drift, made
visible.

---

## Known issues, so you do not report them as new

1. **The factor of seven.** `alarms.tune` and `alarms.scenarios` measure the same
   rules over the same window and report healthy DO slopes an order of magnitude
   apart. Unexplained. Every threshold is set from the wider of the two, so the
   thresholds are trustworthy to better than 2× at best. Open thread, first on the
   list.
2. **Five alarm rules fire on a healthy plant and cannot be tuned away.** They are
   impossible, not untuned — `docs/ALARM-TUNING.md` says which way each one is
   impossible. `secondary_blanket_stuck` because a deadband makes a healthy steady
   signal and a failed instrument the same observation; `lift_pump_flow_lost`
   because both lift signals are station totals.
3. **Four alarm rules have never fired**, so their thresholds are unverified in the
   other direction.
4. **`ui/web` has no test runner.** 22 tests cover the data path; the JSX rendering
   is verified by building and running it. The weakest part of the project.
5. **`dsn()` defaults `POSTGRES_PASSWORD` to `"wwtp"`.** Wrong shape for a project
   whose posture is "credentials from the environment", and deliberately not fixed
   because it would break `make check` on any machine that has not set it.
6. **159 ruff findings**, tracked in `lint-debt-baseline.txt` and ratcheted so they
   can only go down. `make lint-all` shows them.
7. **`test_pace_divides_by_speed_so_a_backfill_is_not_throttled`** fails in a full
   run and passes alone. A wall-clock flake, unfixed, and the only test known to be
   order-dependent.
8. **A restarted alarm engine re-raises an already-acknowledged critical.** The
   operator path works; the restart path is not correct yet. `scada/README.md` says
   so.
9. **`GATEWAY_DB_PASSWORD` and `WEB_DB_PASSWORD` fall back to `POSTGRES_PASSWORD`**
   if left empty. That is a working configuration and it defeats the point of a
   separate credential. Set both in `.env`.
10. **The gateway never reconnects to Postgres.** `storage/postgres/writer.py`
    opens one connection at construction and never re-establishes it —
    `grep -c reconnect storage/postgres/writer.py` is **0**. When that connection
    closed, every write failed from then on, permanently, until the container was
    restarted. This happened for real: the gateway was found with
    `failures: 29702` and `last_error: 'the connection is closed'`.

    The container still reported **healthy**, because its health check verifies
    that the gateway *cannot* `TRUNCATE reading` — a genuinely good privilege
    check — and never tests that a write succeeds.

    **No data was lost**: the spool held 515 files throughout and delivered
    134 078 rows on restart, and the history is continuous across the gap. That
    is the compensating control working exactly as designed. The fix is a
    reconnect on a connection-level error, plus a health signal that reflects
    `failures`.
11. **`pending: 2613776` overstated the data at risk.** That number is
    `len(writer._rows)` — an in-memory buffer fed by live polling, whose contents
    were *also* on disk in the spool. So the status line showed 2.6 M "pending"
    for data that was never at risk, and gave an operator no way to tell the
    difference between "queued in RAM" and "only exists in RAM". The distinction is
    the whole point of the number.
