# Security

This project is a simulation and says so. It also runs on a laptop on a home
network, which is where the risk actually lives. This document is honest about
which is which, and it names the two places where the code is weaker than the
documentation would like.

## The threat model, stated first

**What this is.** A plant simulator. There is no real plant. Nothing here
controls anything that matters.

**What a real deployment of this architecture would face.** Wastewater
treatment is a regulated process under environmental permit. Getting ammonia or
solids into a river is a legal event, not a bug report. So the properties that
matter in a real plant are, in order:

1. **Integrity of the control path.** Someone able to write a setpoint can
   change what the plant does. This is the asset worth defending.
2. **Integrity and availability of the historian.** An operator who cannot trust
   the trend cannot make a decision, and a historian that can be silently
   altered is worse than no historian, because it is trusted.
3. **Confidentiality of metadata.** Equipment lists, alarm setpoints and process
   values are reconnaissance for anyone planning physical interference.
4. **Availability.** A stopped gateway loses data permanently unless it was
   built to spool. A stopped PLC in a real plant stops treating sewage.

**What is explicitly out of scope here.** Multi-tenancy, internet exposure,
certificate lifecycle management, and authenticated non-repudiation of
operator actions. None are simulated and none are claimed.

## What this project actually does

| Control | Status | Note |
|---|---|---|
| Containers run as non-root | **Enforced** | 10001 (Python), 10002 (web), upstream `node-red` (1000). Not verified by a test — see below. |
| `no-new-privileges` | **Enforced** | Every service |
| All Linux capabilities dropped | **Enforced** | Every Python service |
| Secrets from environment, never baked | **Enforced** | `.env` is gitignored; see below |
| Named volumes, not host bind mounts | **Enforced** | Portable, correctly owned |
| Log rotation | **Enforced** | 10 MB × 3 per service |
| The non-root claim is *tested* | **No** | Reading the table above, "verified in the build" was the old wording and there was no verification. `tests/test_web_page.py` asserts the web image's `USER`; nothing asserts the Python one. |
| OPC UA encryption | **Not enforced** | Anonymous, `None` security policy |
| OPC UA authentication | **Not enforced** | Anyone who can reach the port can read everything |
| Modbus authentication | **Impossible** | The protocol has none |
| Modbus write protection | **By contract only** | See gap 2 |
| Network segmentation | **Partial** | One flat bridge network |
| TLS termination | **None** | Local development only |
| Database credentials scoped per service | **Partial** | Two of the five long-running services have their own role. Three do not need one and still have it — Gap 3. |

The three gaps — OPC UA encryption, Modbus write protection, and database
credential scoping — are the ones worth understanding, and each gets its own
section.

**A correction, because this table was wrong in a way that mattered.** It said
`Database credentials scoped per service` was **Not enforced**, "one password,
shared by `db`, `init-db`, `gateway` and `seed`", with a "See below" that pointed
at nothing. Both halves were false by the time anyone read it. The gateway has had
its own `wwtp_gateway` login role since Phase 3g, and the web dashboard has had
`wwtp_ui` since Phase 5c — so the "See below" pointed at a section that did not
exist, in a document whose subject is gaps.

That is a stale count again, wearing a table row instead of a sentence, and it is
recorded here rather than quietly corrected because **the specific failure is
worth noticing: a security document that understates its own gaps reads as
reassuring.** The gap is real; the list of who has the owner password is longer
than the table said. Gap 3 has the current list.

## Gap 1 — OPC UA engineering ranges are advisory, not enforced

Every variable in the address space carries its engineering unit and its
permit limits from `contracts/tags.yaml`, and a client can read them. That is
the useful half.

The other half is not true: a client that **writes** a value outside the range
is not stopped. The range lives in `EUInformation`, which OPC UA defines as
metadata for display. It is not a constraint the server is required to enforce,
and `asyncua` does not enforce it.

Consequences, precisely:

- A write of `DO = -40` is accepted and published.
- The historian records `-40`, tagged with a valid tag name, and nothing about
  the record says it was impossible.
- The permit limit is therefore a *display* property, not a *control* property.

**What this project does about it.** The in-process writer,
`OpcUaServer.write_value`, *does* enforce the range — see its docstring. That
covers writes originating inside the process. It does not cover a remote client
writing directly, because by then the server is outside our code.

**What a real deployment would do.** Either:

1. Use a node type with a `MinimumSamplingInterval`-style constraint plus a
   write *handler* that rejects out-of-range values. asyncua supports write
   handlers, and rejecting the write is a two-line change that this project
   deliberately leaves visible as a gap rather than quietly closing, because a
   silently-closed gap in a teaching project is worse than a documented one.
2. Better: do not let remote clients write measurements at all. A measurement
   node should be read-only for everyone; setpoints are separate nodes with
   their own ranges, their own permissions and their own audit trail.

**This is the single most important thing to take from this document.** In
industrial systems, "the value is out of range" and "the value is wrong" are
different failures, and only one of them is loud. Range checking that only
*displays* is a comment, not a control.

## Gap 2 — Modbus is read-only by contract, not by the wire

`contracts/tags.yaml` marks most registers read-only, and the register model
(`softplc/servers/modbus.py`) is tested against that. The server does not
prevent a client writing to a register the contract calls read-only.

This is not a bug that could be fixed cheaply; it is a property of Modbus. The
protocol has no concept of a read-only register. Function code 6 (write single
register) is answered by any conforming server for any address in range. There
is no authentication, no authorisation, and no integrity protection anywhere in
the protocol. Modbus TCP over a network is a convenience wrapper around serial
Modbus; the security model is "the cable".

**What this project does about it.**

- Binds to a container-internal port, published to the host only for
  convenience.
- Publishes only what the contract says is publishable.
- Documents the registers that are writable, and why.

**What a real deployment would do.** Put the Modbus server behind a protocol
gateway that enforces the register map, on a dedicated network segment, with
the PLC's own logic never reachable over Modbus. That is a real architecture
with real cost, and it exists because Modbus cannot be secured by the thing
speaking Modbus.

**The teaching point.** Modbus is a 1979 serial protocol that kept its shape
when it was given an IP transport. Nothing about it was designed for a network
that strangers share. Choosing it is choosing that, and the choice should be
visible in the architecture rather than buried in a config file.

## Gap 3 — Three services authenticate as the database owner

The honest version of the table row, with the current list rather than a summary.

**What is scoped.** Two of the long-running services authenticate as a `LOGIN`
role that belongs to exactly one group role, and each grant is proved by
attempting the write and reading the refusal rather than by reading a catalog:

| service | role | group | can | cannot |
|---|---|---|---|---|
| `gateway` | `wwtp_gateway` | `wwtp_writer` | insert and upsert readings, insert events | `DELETE`, `TRUNCATE`, `DROP`, or change the contract |
| `web` | `wwtp_ui` | `wwtp_reader` | read the historian, the rollups, the event log | insert, update, delete, truncate, `CREATE TABLE` |

**What is not.** Three services authenticate as `wwtp`, the owner, and two of them
have no reason to:

| service | needs owner because | needs it? |
|---|---|---|
| `db` | it is the database | yes |
| `init-db` | it applies the schema and creates roles | yes |
| `seed` | it bulk-loads 4.3 M rows | arguably — `COPY` is dramatically faster, and a seeder is a build step rather than a service |
| **`scada`** | nothing — every Node-RED flow is a read except two `INSERT`s | **no** |
| **`grafana`** | nothing — both dashboards are `SELECT` | **no** |

So the scoped set is the two services that run continuously and the unscoped set
is the two that do not need to be there. That is the gap, stated precisely: **it
is not that the project has one password, and it is not that the project has
scoped them.** It has scoped the hard cases and left the easy ones, which is the
shape of almost every real privilege-escalation finding — the interesting service
is fine and the boring one is not.

**Cost of closing it.** Small, and it is on the list:
`python -m storage.postgres.login_role --name wwtp_scada` already works, because
the mechanism exists and the gateway uses it. Grafana needs the same, plus a
datasource `secureJsonData` change in `ui/grafana/provisioning/`. Neither is done,
and both are in `docs/LEARNING-LOG.md` as open threads.

**The teaching point, and it is the same one as thread 9's.** When the Couchbase
migration introduced one shared owner password and recorded it as a deliberate
regression, the reasoning was right for the two *protocol* gaps — OPC UA's
advisory `EUInformation` and Modbus's total absence of authentication are
properties of the technology — and wrong for the database one, which was a grant
that had not been made yet.

> **A documented omission is a teaching point. A documented regression is debt
> somebody agreed to pay.**

A credential that is scoped for two services and not the other three is neither,
exactly: it reads as deliberate, and half of it is an omission. Naming which is
which is the whole point of the section.

## One database, and how the credential is scoped

**This section was wrong for two phases and is the reason the review found
anything.** It was titled "One database, so one credential — and that is a
regression", it said the gateway "can `DROP TABLE`", and it said the three-role
arrangement was "not implemented here" while printing the exact SQL that exists.

None of that was true by the time anyone read it. `wwtp_owner`, `wwtp_writer` and
`wwtp_reader` are created by `storage/postgres/roles.py`; `wwtp_gateway` and
`wwtp_ui` are `LOGIN` roles created at run time from the environment by
`storage/postgres/login_role.py`; and each refusal is proved by attempting the
operation and reading the error, not by reading a catalog.

**A security document that understates its own protections is as dangerous as one
that overstates its own risks.** This one said the database was wide open in a
document whose purpose is to be honest about gaps — and a reader assessing this
project would have reached the opposite conclusion about the part of it that is
in fact the most carefully built.

### What exists now

```sql
-- group roles, created by storage/postgres/roles.py
wwtp_owner     -- owns the schema; used by init-db and nothing that runs
wwtp_writer    -- SELECT, INSERT, UPDATE on reading; INSERT on event; USAGE on event_id_seq
wwtp_reader    -- SELECT on reading, reading_1m, reading_1h, signal, equipment, site, event

-- login roles, created at run time from the environment
wwtp_gateway   -- member of wwtp_writer and nothing else
wwtp_ui        -- member of wwtp_reader and nothing else
```

Three grants in `wwtp_writer` are not padding, and each was found by running the
stack rather than by reading the file:

- **`SELECT`** — `INSERT … ON CONFLICT DO UPDATE` reads the conflicting row to
  decide whether to update it.
- **`UPDATE`** — `DO UPDATE` then writes it. This is the one that looks like a
  mistake; UPDATE on a historian feels wrong, and it is required by the upsert the
  gateway's re-send path depends on. It is `UPDATE` on `reading` and nothing else,
  so it cannot change history — only overwrite a row at a timestamp the gateway
  already believes it wrote.
- **`USAGE` on `event_id_seq`** — `event.id` is `BIGSERIAL`, so an `INSERT` needs
  it, and it is not a table privilege, which is why `check()` verifies it by
  attempting a write rather than by reading `role_table_grants`.

`DELETE` and `TRUNCATE` are withheld from both, and those are the ones that
matter: **a historian that can be made to forget is worse than one that stops.**
An operator who sees a gap investigates it; an operator who sees a
plausible-but-truncated trend does not.

### Why the password is composed as a quoted literal

`CREATE ROLE … PASSWORD %s` is a syntax error — DDL cannot be parameterised, and
the first version failed with exactly that. The password is therefore built with
`psycopg.sql.Literal`, which is the only way to put a value into DDL without
concatenating it.

### The part that is still a gap

Gap 3, above: `scada` and `grafana` authenticate as the owner and do not need to.
That is a real privilege-escalation path and it is open.

### The compensating control

The gateway's spool means a compromise of the database does not lose the
*incoming* data, only the *outgoing*. An attacker with write access can corrupt
history, but the plant keeps producing and the spool keeps filling, so the
tampering is bounded and detectable rather than total.

## Secrets

- `.env` holds real values. It is gitignored, and the gitignore puts secrets
  first, before build artefacts, so it stays first when the file is edited.
- No real credential appears anywhere in the repository, including in comments
  and in `docs/`.
- The compose file fails loudly on a missing required variable
  (`${POSTGRES_PASSWORD:?...}`) rather than starting a container that cannot
  work. This was found the hard way and is deliberate: the `${VAR:?}` form fails
  at `docker compose up`, where you are looking, rather than producing four
  containers that exit 1 with a message nobody scrolls to.
- Nothing is baked into an image layer. Passwords are passed as environment
  variables at run time, so they live in the container's configuration, not in
  `docker history`.

**What is not done, and would be in production:**

- No Docker secrets or Vault. Compose environment variables are visible in
  `docker inspect` to anyone with socket access — which on a dev machine is
  usually you, and on a shared host is not.
- No rotation story. Tokens in `.env` are rotated by editing the file and
  restarting. Fine for a laptop; not a process.
- No secret scanning in CI. Worth adding, and deliberately not added here rather
  than added badly.

## The licence question, and why there is not one any more

The previous stack had one, and it is worth recording what it cost.

InfluxDB 3 Enterprise requires a licence key. This project used the **home-use**
licence: free, non-commercial, at home, single node. That is appropriate for this
repository and inappropriate for a plant, a pilot, or a company — and the
practical cost was that **a fresh `docker compose up` on a new machine could not
succeed**, because a human had to obtain a key from InfluxData first. A
demonstration that will not start is a demonstration nobody runs.

There is also a subtler cost. The stable query planner — without which nine of
twenty-three course queries failed *intermittently* — is in the Enterprise
edition. So the licence was not only a key: it was the difference between a
teaching project that teaches and one that intermittently 500s.

PostgreSQL and TimescaleDB are open source. The database section of this document
is now about scoping and segmentation rather than about which edition you are
permitted to run, which is where a security document should be spending its
attention.

**What did not improve.** Neither TimescaleDB's Community edition nor Postgres has
a "home use" boundary that you can accidentally cross, but both are now *in* the
project's supply chain in a way the previous stack was not. See below.

## Base image pinning

`docker/Dockerfile.python` pins `python:3.13.2-slim-bookworm` by tag. A digest
(`python@sha256:...`) is stronger: a tag can be repointed, a digest cannot.

The trade-off, stated so it can be argued with rather than inherited:

- **Tag:** readable, easy to update when a base is rebuilt for a CVE, easy to
  get wrong by accident.
- **Digest:** reproducible, tamper-evident, and a maintenance burden that
  invites people to stop updating the base — which is a worse outcome than an
  occasionally stale tag.

For a teaching project on a laptop, the tag is the right call. For anything
that runs unattended, pin the digest and automate the bump. The one thing not to
do is pick a tag and then not check whether the base has a known vulnerability
in it.

### The argument above is wrong in general, and the previous stack proved it

The section above argued for tags over digests, and reached the right conclusion
for the wrong reason. It is kept because being wrong in an instructive way is
more useful than being right quietly.

The counter-example is real. InfluxDB 3 is not on Docker Hub. It is on Quay, and
its complete tag list is 17 entries: `latest`, `latest-arm64`, `latest-amd64`,
`arm64`, `intel`, and one commit SHA per build. **There is no version tag.**
`influxdb:3.2` does not exist — which is the tag this project originally wrote
before trying to run it.

So the question is not "tag or digest" in the abstract. It is **what does this
vendor publish**. For a vendor that publishes only SHAs, the SHA tag *is* the
pinnable version and `latest` is not pinnable at all. `compose.yaml` pinned the
bare commit SHA, which is the multi-architecture manifest list.

TimescaleDB publishes `2.30.1-pg16` — version *and* Postgres major in the tag —
so `timescale/timescaledb:2.30.1-pg16` is a genuinely informative pin, and
nothing about the decision required an argument.

**The general lesson is worth more than the rule I started with:** a pinning
policy should be written per dependency, from what that dependency actually
publishes, rather than applied uniformly from a principle. A uniform policy is
either unenforceable (no version tags exist) or ignored (a digest nobody can
update).

## What this project would need before facing a real network

In rough order of value per effort:

0. ~~**Stop publishing the ports on every interface.**~~ **DONE.** All five
   published ports now bind `127.0.0.1` by default via `${HOST_BIND}`, and the
   opt-out is documented in `.env.example` next to a warning about what it
   exposes. It was the cheapest item on this list and it was not on it: the
   compose file published `4840:4840` — which Docker reads as *every* interface —
   while the comment above it claimed that host publishing was "a separate,
   deliberate decision made per port below", and `0.0.0.0` appeared in no markdown
   file in the repository. A test now asserts all five.
1. **Write handlers on OPC UA** that reject out-of-range values, and a real
   certificate with a real trust list. Closes gap 1.
2. **A protocol gateway** in front of Modbus, enforcing the register map, on its
   own network segment. Closes gap 2's practical half.
3. **Segmentation.** Split `plant` into a control network (soft PLC alone), a
   historian network (gateway, databases) and a DMZ (web, Grafana). The compose
   file has one network and that is the weakest part of its topology.
4. **Authentication on OPC UA**, with users and certificates per client role.
5. **Audit logging** of every write, to somewhere the operator cannot edit. The
   most valuable thing a historian does that a database does not.
6. **Secrets from a real secret store**, and rotation.
7. **Read-only root filesystems** for the services that do not need to write.
   `read_only: true` is in the compose file but disabled, because the gateway's
   spool needs a writable path and the simplest honest answer was one volume
   rather than three tmpfs mounts.

## Reporting a problem

This is a portfolio project with no security contact. If you find something
genuinely broken — especially in the OPC UA or Modbus servers — the useful thing
to do is open an issue describing it, or write it in
`docs/LEARNING-LOG.md` if you are working through the project yourself.
