# Node-RED — the operator flows

```bash
make scada                      # start the runtime
open http://127.0.0.1:18880/scada   # the editor (off by default; see below)
python -m scada.build_flows     # regenerate the flows after a contract change
python -m scada.build_flows --check   # report drift, change nothing
```

## What Node-RED is for here, and what it is not

It is the operator's side of the plant: a mimic to look at, an annunciator to
clear, and a setpoint to change. Three flows, and each one closes a gap that
`docs/ALARMS.md` or `contracts/tags.yaml` had been carrying as declared-but-unused.

| Flow | What it is | What it closes |
|---|---|---|
| `01-mimic.json` | newest reading per watched tag, every 5 s | — |
| `02-annunciator.json` | unacknowledged criticals, with staleness, **and acknowledgement** | `AlarmEngine.acknowledge()` existed and **nothing called it** |
| `03-control.json` | a setpoint, range-checked, written over Modbus | the contract's `writable` surface and `permit_limits` |

It is **not** a historian, **not** an alarm system, and **not** a place to put
business logic. The historian is `reading`; the alarm engine is `alarms/`, and it
decides what is wrong from a measured healthy distribution rather than from a
band on a diagram. The mimic deliberately does *not* decide anything — it says
"the last value happened to be inside the band the contract declares", which is
true most of the time, is not a fault detector, and says so in
`docs/ALARM-TUNING.md`.

## The flows are generated, and that is the whole point

`contracts/tags.yaml` lists **"Node-RED tag list"** among its consumers. A
hand-written tag list is 57 entries that are correct on the day they are written
and *plausibly* wrong after that: a stale one still resolves, still renders, and
still shows the last value it knew about. The failure is a mimic diagram that
looks fine and describes a plant that no longer exists.

So:

* `python -m scada.generate_tags` → `scada/flows/tags.json`, 57 tags over 8 areas.
* `python -m scada.build_flows` → `scada/flows/0*.json`, three flows.
* Both are **committed**, because a build step that only runs inside one
  container is a build step that will not run.
* `tests/test_scada_contract.py` has 37 tests, and the ones that matter are not
  the drift check — they are the structural ones, which catch a *bug in the
  generator*, which produces confidently wrong flows.

Node-RED's own `flows.json` is the worst authoring format there is: no comments,
no names, a hex id on every node, and a dangling wire that imports cleanly and
does nothing. Writing the flows in Python and generating the JSON is not a
preference.

## The three things that went wrong building this

All three are documented at the point they bit, because each cost more time than
it should have.

### The base image never installed the nodes

`nodered/node-red` looks like it installs your `package.json` at build time. It
does not — **the base image is prebuilt**, so its `npm install` already ran
against *its* package.json. The symptom is the sharpest Node-RED has:

```
[info] Waiting for missing types to be registered:
[info]  - postgresql
[info]  - modbus-write
```

and then it sits there. No error, no exit, no timeout. The runtime is up, the
editor would load, and every node is a red box waiting for a type that will never
arrive. `scada/nodered/Dockerfile` installs them explicitly, and says why.

### The image is `nodered/node-red`, not `node-red`

`node-red` on Docker Hub is a different, unmaintained project, and pulling it
fails with `pull access denied` — which reads like a network problem and a
registry login, and is neither.

### `settings.js` went to the wrong path and every setting in it was ignored

The runtime's data directory is `/data`. The image's *build* directory is
`/usr/src/node-red`. Copying the file to `/opt/node-red/data/settings.js` puts it
somewhere the runtime never reads, and the symptom is that nothing happens: the
admin root stays `/`, the external-modules restriction is not applied, and no
log line says so. The container *does* log the path it read — `Settings file :
/data/settings.js` — and the file is not there.

## The credential, and why it is not in git

The PostgreSQL node needs a credential and a credential is a secret. The two
obvious options are both wrong for this project:

* **commit `flows_cred.json`** — puts the database password in git history. This
  project has already had a conversation about a shared password owning the whole
  database; undoing it silently, in a file nobody reads, would be worse.
* **"open the editor and add it"** — a manual step, so `docker compose up` on a
  clean checkout produces a runtime full of red boxes and this error:

  ```
  [error] [postgresql:read newest values] TypeError:
          node.config.pgPool.connect is not a function
  ```

  which is a Node-RED internal, names no database, and is *identical* for "no such
  credential" and "the credential is broken".

So `scada/nodered/entrypoint.sh` derives the credential from the environment and
writes it once, mode 600, into a named volume. An existing file is never
overwritten, so a credential entered in the editor survives a restart. The
`credentialSecret` is generated alongside it and read by `settings.js`, which
removes Node-RED's "system-generated key, unrecoverable" warning.

The flow files name the *credential id* — `wwtp-db` — which is a pointer, not a
secret. A test asserts the ids the flows use are the ids the entrypoint writes,
because a mismatch produces that same opaque error.

### One thing this does not fix

The SCADA runtime authenticates as the **owner**, while the gateway
authenticates as `wwtp_gateway` and cannot delete a reading. The mimic is
read-only by construction and the credential is generated per environment, so
nothing here can do damage the gateway could not — but *"the flows are
read-only"* is a claim about the flows and not about the credential, and those
are not the same claim. A read-only Postgres role for this service is the right
next step and is not done.

## The editor is on loopback only

The editor is **running** at `http://127.0.0.1:18880/scada/`. It is not switched
off, because Node-RED has no switch for it — the setting does not exist, and a
README that tells you to set it is telling you to set an environment variable
that does nothing.

What protects the flows is the **port bind**: `compose.yaml` publishes
`127.0.0.1:${SCADA_PORT:-18880}:1880`, so the editor is not reachable from the
network at all. See the comment above that bind and the note in
`scada/nodered/settings.js`.

The flows are generated, so a hand edit in the editor is a change
`python -m scada.build_flows` silently reverts. That is why the bind matters and
why it is checked: `tests/test_scada_contract.py` fails if the scada port stops
being loopback-bound.

## What the deadband does to a mimic

Two facts the mimic keeps apart, because collapsing them is the mistake:

* **`value` is the last value the plant reported.** `age_s` is how long ago that
  was. A signal whose value has not moved is *not* a signal that has stopped
  reporting.
* **A `NULL` value with `quality != 0` is the instrument saying it does not
  know.** It is not a missing row and it must not be rendered as zero.

The query is `DISTINCT ON (signal_id) ... ORDER BY signal_id, ts DESC` and that
is not a shortcut: there is no row for a value that did not move, so the current
value of a signal *is* its newest row. Thirteen of the 57 signals produce exactly
one reading in a seeded week, which is what makes this the difference between a
mimic that works and one that is empty.

## Not built

* **No dashboard.** Node-RED's `dashboard` group is not installed and not wired;
  the flows emit to the sidebar. The Grafana dashboards are Phase 5.
* **No alarm suppression or shelving.** Two blowers tripping at once produce two
  alarms, and an operator facing four unacknowledged criticals cannot rank them.
  A real system annunciates with a deadband on the *panel* as well as on the
  signal.
* **The control flow does not check the Modbus result.** It writes and records an
  audit row; it does not read the value back to confirm the plant accepted it. A
  write to a soft PLC that silently failed leaves an audit trail saying it
  succeeded, which is the same class of problem as the missing aggregates.
* **The engine does not read its own acknowledgements back.** The flow writes an
  `alarm_acknowledged` row and the *panel* query excludes acknowledged alarms, so
  the operator path works. But `AlarmEngine.acknowledge()` is still in-memory: a
  restarted engine re-raises a critical it was already acknowledged on, and
  writes a second `alarm_raised` row. The panel then shows it as outstanding
  again, because the replay's newest raise is unacknowledged.
  `alarms/replay.py` exists and is wired into `alarms serve`, which restores the
  acknowledgements — **but only for rules the engine has itself raised since it
  started**, because `acknowledge()` refuses anything it has not seen. The
  durable fix is for the engine to *raise* from replayed history rather than to be
  told about it afterwards.
