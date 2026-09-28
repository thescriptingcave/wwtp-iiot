# 07 — Security, and the warning on every start

**Next:** [08 — The address space as generated code](08-generated.md) · [Back to the course](../README.md) · [Previous: 06](06-async.md)

Every single time the OPC UA server starts, it prints two lines to stderr:

```
No encrypting policy available, password may get transferred in plaintext
Endpoints other than open requested but private key and certificate are not set.
```

Every one of those 600+ test runs, every `docker compose up`, every
`opcua_browser.py browse` — and nobody has read them, including me, until writing
this course. They are the server telling the truth about itself in the only
channel available to it, and they are not an error condition, so nothing treats
them as one.

`docs/SECURITY.md` is honest about the underlying facts, to its credit:

| Control | Status | Note |
|---|---|---|
| OPC UA encryption | **Not enforced** | Anonymous, `None` security policy |
| OPC UA authentication | **Not enforced** | Anyone who can reach the port can read everything |

So the lesson is not "this project is insecure and doesn't know it". It is
narrower and more interesting: **the threat model ranks the wrong asset first,
and the top remediation is already in the codebase, unwired.**

## The question

> What does someone get, today, by connecting — and does the document that
> describes the risk agree with it?

## One line of code, two warnings

Here is the entirety of this server's security configuration:

```bash
grep -n "set_security\|allow_anonymous\|certificate\|user_manager" softplc/servers/opcua.py
```

```
(no output)
```

Not a weak configuration — an absent one. `build_address_space` calls
`server.set_endpoint(endpoint)` and nothing else. No `set_security_policy`, no
`allow_anonymous(False)`, no user manager, no `ApplicationInstance` certificate.
The same grep across the whole repository, tests included, returns nothing.

Which is what a client negotiates:

```python
from asyncua import Client
c = Client(url="opc.tcp://127.0.0.1:4840/x/")
await c.connect()
print("security policy :", type(c.security_policy).__name__)
print("user identity   :", c.user_certificate, " (no certificate)")
await c.disconnect()
```

```
security policy : SecurityPolicyNone
user identity   : None  (no certificate)
```

`SecurityPolicyNone` is the specification's name for *no signing and no
encryption*. It is not a weak cipher — it is the absence of one. So:

- **nothing is signed**, so a client cannot prove who published a value, and a
  network attacker can alter readings in flight without detection
- **nothing is encrypted**, so every value crosses the wire in the clear,
  including the permit limits below
- **the session is anonymous**, so there is no identity to check a permission
  against

The two warnings are `asyncua` being honest about the first two, and the
`LOGIN_PASSWORDS` table I once assumed configured OPC UA users is in fact
Postgres roles (`storage/postgres/login_role.py`) — a different protocol
entirely. `tools/opcua_browser.py` says what it does in a docstring: *"The client
endpoint, with a well-known anonymous identity."*

## What an anonymous walk gets you

So here is the reconnaissance, in one unauthenticated connection. No username, no
password, no certificate:

```python
from asyncua import Client

c = Client(url="opc.tcp://127.0.0.1:4840/x/")
await c.connect()

plant = None
for ch in await c.nodes.objects.get_children():
    if (await ch.read_browse_name()).Name.startswith("PLANT-A"):
        plant = ch
print("connected with no username, no password, no certificate\n")
print("plant:", (await plant.read_browse_name()).Name)

print("\nthe compliance limits, readable by anyone:")
for x in await plant.get_children():
    n = (await x.read_browse_name()).Name
    if n.startswith(("Design", "Permit")):
        print(f"  {n:34} = {await x.read_value()}")

areas = [x for x in await plant.get_children()
         if not (await x.read_browse_name()).Name.startswith(("Design", "Permit"))]
signals = 0
for a in areas:
    for holder in await a.get_children():
        for var in await holder.get_children():
            names = {(await p.read_browse_name()).Name
                     for p in await var.get_children()}
            if "SignalId" in names:
                signals += 1
print(f"\nareas enumerable        : {len(areas)}")
print(f"measured signals walked : {signals}")
print("and each carries its unit, range, normal band and deadband.")
await c.disconnect()
```

```
connected with no username, no password, no certificate

plant: PLANT-A: Northgate Water Reclamation Facility

the compliance limits, readable by anyone:
  DesignFlow_m3h                     = 1800.0
  Permit_eff_nh4_mg_l_30d_mean       = 10.0
  Permit_eff_tss_mg_l                = 30.0
  Permit_eff_ph_min                  = 6.0
  Permit_eff_ph_max                  = 9.0
  Permit_dis_bacti_geomean           = 200.0

areas enumerable        : 8
measured signals walked : 57
and each carries its unit, range, normal band and deadband.
```

**The permit limits.** Ammonia 10, TSS 30, pH 6–9, coliforms 200, design flow
1800 m³/h. That is the complete set of numbers somebody would need in order to
discharge into this plant's permit without tripping it — and it is the first
thing any client reads.

**Then the plant model**, self-describing: 8 areas, 57 signals, each with its
engineering unit, range, normal band and deadband. Lesson 02's complaints about
conformity now read as a security property in reverse — the reason a
discovery tool can find nothing is the same reason an attacker has to walk the
tree, and the tree is a gift.

## The honest two-sided lesson

This is the part worth taking to a real plant, and it is not a defect in OPC UA:

| | Modbus | OPC UA |
|---|---|---|
| authentication | impossible — the protocol has none | possible, and unused here |
| an attacker gets | 4 314 integers | the complete, labelled plant model |
| engineering units | in a document, not on the wire | on every variable |
| permit limits | not addressable at all | properties on the root object |
| a compliant client can | read what the map says | read anything the server offers |

**The protocol's single greatest feature is also its single greatest
reconnaissance surface.** A Modbus attacker gets numbers and has to guess what
they mean; an OPC UA attacker gets the map, the labels and the limits, and never
has to guess. Self-description is what makes OPC UA worth choosing, and it is
also what makes an unauthenticated OPC UA server a complete disclosure.

The correct conclusion is not "do not expose OPC UA". It is that **an OPC UA
server is only as safe as its authentication, and authentication is the one
thing that cannot be retrofitted** — a client that expects `None` will happily
talk to a `Sign` endpoint, and a client that requires `Sign` will not talk to
this server at all, so tightening the policy later breaks every client that
connected while it was loose. That is the real argument for doing it at the
start, and it is not in `SECURITY.md`.

## The bind address, which is in no document

```
$ git grep -n "0\.0\.0\.0" -- '*.md' | grep -vE '^courses/|LEARNING-LOG'
(no output)
```

The server binds `opc.tcp://0.0.0.0:4840` and `compose.yaml:79` publishes
`"${OPCUA_PORT:-4840}:4840"` — which is **every interface on the host**, not
loopback. The loopback-only form is `127.0.0.1:${OPCUA_PORT:-4840}:4840`, and it
is a two-token difference.

`0.0.0.0` appears in `compose.yaml`, `contracts/tags.yaml`, and **no reference
documentation** — not `SECURITY.md`, not `ARCHITECTURE.md`, not
`GETTING-STARTED.md`, not `README.md`. The only markdown naming it is this lesson
and the learning-log entry about it, which is the usual fate of a gap: the only
places that know about it are the documents complaining that it exists. So
`SECURITY.md`'s "Anyone who can reach the port" is unresolvable by reading the
project: it does not say *who* can reach it. On a laptop that is the local
network. On a server in a rack that is the internet, depending entirely on the
firewall nobody in this repository has written.

And the compose file sets up the expectation and then does not meet it:

<!-- compose.yaml:71-73 — a comment in the file, quoted not run -->
<!-- check: skip -->
```yaml
      # Bind to all interfaces *inside the container*. The service name is what
      # the rest of the network uses; publishing to the host is a separate,
      # deliberate decision made per port below.
```

"a separate, deliberate decision made per port below" — and the port line below
publishes to all host interfaces, which is a *default* rather than a decision. A
reader would have to know the Docker publishing syntax to notice. Either the port
line binds loopback for a development stack, or the comment says why all
interfaces is the choice. Right now it claims a deliberation that the syntax
does not show.

## What the threat model gets wrong

`SECURITY.md` states its scope honestly, which is more than most:

> **What this is.** A plant simulator. There is no real plant. Nothing here
> controls anything that matters.

And then it ranks the properties that matter in a real plant, first:

> 1. **Integrity of the control path.** Someone able to write a setpoint can
>    change what the plant does. This is the asset worth defending.

**Nobody can.** Lesson 05 established it: `OpcUaServer.write_value()` has no
caller, the scan loop overwrites every write within a cycle, and the DO
controller reads its own dataclass field. The number-one asset in this project's
threat model is the one asset that is *not* exposed, because nothing is connected
to it.

Meanwhile the thing that **is** exposed — the plant model and all five permit
limits, unauthenticated, on all host interfaces — does not appear in the ranked
list at all. Confidentiality of the compliance envelope is nowhere in a threat
model whose top-ranked risk is control-path integrity.

This is not a criticism of the document's reasoning. It is a document reasoning
correctly about a *real plant* and being applied to a simulator whose control
path happens to be inert. The ranked list is right for the architecture and
wrong for the code, and nothing in the file says which one you are reading.

## And the top remediation is already in the codebase

The list of "what this would need before facing a real network" opens with:

> 1. **Write handlers on OPC UA** that reject out-of-range values, and a real
>    certificate with a real trust list. Closes gap 1.

A write handler that rejects out-of-range values is `OpcUaServer.write_value()`.
It exists, it is correct, it is covered by tests, and **it has no caller.** The
number-one security improvement on the list is already written and already
unreachable, and the document does not know it.

And completing it would not achieve the stated outcome either, because closing
gap 1 only stops out-of-range writes reaching a control loop that is not
listening. The real state is stronger than "gap 1 open": it is "the control path
is not connected", which is a different finding with a different fix.

## What is actually true

| claim | verdict |
|---|---|
| The server is unauthenticated and unencrypted | **yes** — `SecurityPolicyNone`, stated in `SECURITY.md` |
| Anything that reaches the port can read the plant | **yes** — and the permit limits with it |
| The port is reachable from outside the host | **yes, and undocumented** — `0.0.0.0`, published on all interfaces |
| The top ranked threat is exploitable | **no** — the control path is inert |
| The top remediation is missing | **no** — it exists and is unwired |
| Self-description is a security liability | **only without authentication** — and the fix cannot be retrofitted |

Two of those rows are the reason this lesson is worth more than the last six
combined. Everything in lessons 02–06 was a *documentation* defect: the code was
either right or harmlessly broken, and the prose overstated it. This one runs the
other way. The prose is careful and the ranking is defensible, and the
*code* leaves an entire asset class — the compliance envelope — outside the model
entirely, because the model was written for a different plant.

The three things worth doing, in order:

1. ~~**Bind loopback in `compose.yaml`**~~ **DONE.** All five published ports
   bind `127.0.0.1` by default now, through `${HOST_BIND}`, and the opt-out is
   documented in `.env.example` beside a warning about what it exposes. It is a
   default rather than a policy — `HOST_BIND=0.0.0.0` restores the old behaviour
   for anyone demoing on a network they trust — and a test asserts all five,
   because the failure mode is a compose file that parses perfectly and publishes
   an unauthenticated plant to a network.
2. **Move confidentiality into the threat model**, where the permit limits
   belong, and demote control-path integrity with a note saying the path is
   currently inert.
3. **Reword remediation 1** to say the write handler exists and needs a caller,
   and that the control path needs connecting before the range check means
   anything.

None of them is a lesson about OPC UA, which is the point. The protocol taught
correctly; the *application* of it to this repository is where the security
question actually lives, and that is never something a protocol course can
check for you.

**Next:** [08 — The address space as generated code](08-generated.md)
