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
| Containers run as non-root | **Enforced** | UID 10001, verified in the build |
| `no-new-privileges` | **Enforced** | Every service |
| All Linux capabilities dropped | **Enforced** | Every Python service |
| Secrets from environment, never baked | **Enforced** | `.env` is gitignored; see below |
| Named volumes, not host bind mounts | **Enforced** | Portable, correctly owned |
| Log rotation | **Enforced** | 10 MB × 3 per service |
| OPC UA encryption | **Not enforced** | Anonymous, `None` security policy |
| OPC UA authentication | **Not enforced** | Anyone who can reach the port can read everything |
| Modbus authentication | **Impossible** | The protocol has none |
| Modbus write protection | **By contract only** | See gap 2 |
| Network segmentation | **Partial** | One flat bridge network |
| TLS termination | **None** | Local development only |

The two gaps in bold at the bottom of the first block are the ones worth
understanding, and they get their own sections.

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

## The two databases hold different things, and that matters for security

| | InfluxDB 3 | Couchbase |
|---|---|---|
| Holds | Numeric time series | Documents, metadata, events |
| Access pattern | Range scans and time buckets | Key lookup and N1QL joins |
| A compromise yields | A trend line | The plant's description and its weak points |

Couchbase is the more valuable target. A bucket of equipment metadata tells an
attacker which pumps matter, which sensors are trusted, and what the alarm
thresholds are — which is a map of where to push. The raw trend data is largely
reconstructible from public documentation about how treatment works; the
metadata is not.

That asymmetry is a reason to keep credentials separate per service, which this
project does: the gateway has one `COUCHBASE_PASSWORD` scoped to its bucket,
and nothing in the stack has the admin credential.

## Secrets

- `.env` holds real values. It is gitignored, and the gitignore puts secrets
  first, before build artefacts, so it stays first when the file is edited.
- No real credential appears anywhere in the repository, including in comments
  and in `docs/`.
- The compose file fails loudly on a missing required variable
  (`${INFLUX_TOKEN:?...}`) rather than starting a container that cannot work.
- Nothing is baked into an image layer. The licence key and the tokens are
  passed as environment variables at run time, so they live in the container's
  configuration, not in `docker history`.

**What is not done, and would be in production:**

- No Docker secrets or Vault. Compose environment variables are visible in
  `docker inspect` to anyone with socket access — which on a dev machine is
  usually you, and on a shared host is not.
- No rotation story. Tokens in `.env` are rotated by editing the file and
  restarting. Fine for a laptop; not a process.
- No secret scanning in CI. Worth adding, and deliberately not added here rather
  than added badly.

## The licence question, stated plainly

InfluxDB 3 Enterprise requires a licence key. This project uses the **home-use**
licence: free, non-commercial, at home, single node. It is appropriate for this
repository and inappropriate for a plant, a pilot, or a company. That is not a
technicality — using a home-use key commercially is a licence violation, and it
is called out here because the alternative is that someone finds out later.

Couchbase runs Community Edition, which is genuinely free and genuinely
single-node. It is fine for a demonstration and has no path to a cluster without
a commercial licence, which matters if this architecture is ever more than a
teaching project.

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

## What this project would need before facing a real network

In rough order of value per effort:

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
