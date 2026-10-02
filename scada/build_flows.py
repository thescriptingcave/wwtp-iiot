"""Generate the Node-RED flows from the contract.

    python -m scada.build_flows            # write scada/flows/*.json
    python -m scada.build_flows --check    # report drift, change nothing

## Why the flows are generated rather than hand-written

Node-RED flows are JSON, and hand-written JSON is the worst possible authoring
format: no comments, no names, a 32-character hex id on every node, and a
dangling-wire typo that Node-RED reports as a silently dead node rather than an
error. Every real Node-RED project I have worked on has a `flows.json` that
nobody can edit by hand after the first month.

So the flows are built here, in Python, with the same discipline as
`scada/generate_tags.py`:

* **tags come from the contract.** A flow that references a signal nobody
  declared is impossible to write, because the reference is a lookup.
* **SQL is written against the schema, not remembered.** The `reading` and
  `event` column names appear in the query templates in this file, next to a
  comment, rather than in a JSON string in a flow.
* **the output is still JSON**, and it is still a flow you can import with the
  Node-RED editor. Generated is not a lesser thing here; it is the only way to
  keep the flows in step with a contract that is itself the source of truth.
* **it is committed**, so `docker compose` needs no build step and a reader can
  read the flows without running anything.

## What the three flows are for

Node-RED earns its place here by doing the two things it is genuinely better at
than a service: presenting plant state to a person, and being a place where an
operator's intent enters the system.

**`01-mimic.json`** — the operator's overview. Reads the newest reading for a
watched set of tags on an interval and builds one status object. It is read-only
and it is the flow that shows the deadband's honest semantics: a signal that has
not moved has *no new row*, so "last known value" and "how long ago" are two
different facts and the flow carries both.

**`02-annunciator.json`** — alarm acknowledgement. This closes a gap
`docs/ALARMS.md` lists as not built: `AlarmEngine.acknowledge()` exists and is
tested, and **nothing calls it**, because there is no operator. The flow reads
unacknowledged critical alarms, shows them, and records the acknowledgement as an
`alarm_acknowledged` event. The engine keeps its state in memory, so a flow
restart does not lose the alarms — they are rows.

**`03-control.json`** — setpoint writes. This is the third thing in the project
that has been declared and never consumed: the contract's `writable` surface and
its `permit_limits`. The flow checks the requested value against the signal's
`write_range` **before** writing, and refuses outside it. That check is the point:
the permit range is what stops an operator mistyping 20 mg/L into a setpoint
field, and it belongs in a place a human can see rather than in the plant.

## On the two non-core nodes

`node-red-contrib-postgresql` and `node-red-contrib-modbus`, pinned in
`scada/nodered/package.json`. Both are the de facto standard for their job in
Node-RED and both are in the Dockerfile, so the flows import with every node
already present — a flow that imports and then shows red boxes is the Node-RED
equivalent of a stack that starts and then logs errors.

**The control flow writes to Modbus, not to the database.** That is not an
implementation detail, it is the safety property. An operator's command has to
reach the plant through the same path a remote setpoint would, and it has to
be subject to the same validation. Writing a setpoint into Postgres would create
a number that looks like a command and is not one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from softplc.contract import Contract
from softplc.contract import contract as get_contract
from softplc.servers.modbus_server import ModbusTcpServer

log = logging.getLogger("scada.build_flows")

FLOWS_DIR = Path("scada/flows")

#: Stable, short node ids derived from a name, so regenerating produces a
#: byte-identical file and the diff is only ever a real change.
#:
#: Node-RED does not care what the ids are, only that they are unique within a
#: flow. Deriving them from names means a regenerated flow keeps its wires, and
#: — more usefully — a diff shows *which node changed* rather than a page of
#: reassigned hex.
def nid(*parts: str) -> str:
    """A stable 13-character hex id, derived from a name **and its scope**.

    **Not `hash()`.** Python salts string hashing per process, so `hash(name)`
    returns a different value on every run, which made every regeneration of every
    flow a complete diff of reassigned ids. That defeats the entire reason for
    generating the flows: a diff that is always everything tells you nothing, and
    a reader learns that a changed file is not a changed flow.

    Found by `tests/test_scada_contract.py::test_the_flows_are_in_step_with_the_
    contract`, which failed on the very run that generated the file it was
    checking.

    **And not the name alone.** Deriving from the name was fine while ids only had
    to be unique *within* one flow, which is what the docstring used to claim. They
    do not: `scada/nodered/entrypoint.sh` concatenates all three generated files
    into the single `flows.json` a runtime reads, and Node-RED requires ids to be
    unique across that whole document. Four nodes are deliberately emitted once per
    flow -- the tag loader, because Node-RED's `global` context is per flow file --
    and name-only derivation gave all three copies of each the same id. Node-RED
    did not complain; it kept one of the three and wired the other nine endpoints
    to whichever it happened to keep, so two of the three flows silently ran with
    an empty tag list.

    So every id is now `nid(scope, name)` with the tab id as the scope for nodes on
    a tab, which is unique by construction. The join character is ASCII unit
    separator, so no pair of ordinary names can produce the same digest as a
    different pair.
    """
    return "a" + hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


#: The scope for a node that is not on any tab.
#:
#: Node-RED's config nodes -- `postgreSQLConfig`, `modbus-client` -- belong to the
#: *runtime*, not to a flow. They carry no `z` and are shared by every tab that
#: references them, which is the entire reason the type exists. So they need an id
#: that is the same in all three generated files and is not derived from any one
#: tab, and this is that scope.
CONFIG_SCOPE = "config"

#: The config node types these flows use.
#:
#: Exposed rather than re-spelled in the tests, because the properties that make
#: them different from every other node are three separate ones -- no tab, no
#: ports, and referenced by an id field rather than by a wire -- and three separate
#: lists of exceptions in three separate tests is three chances to miss the fourth
#: config node someone adds next year.
CONFIG_NODE_TYPES: frozenset[str] = frozenset({"postgreSQLConfig", "modbus-client"})


# ─── the watched set ──────────────────────────────────────────────────────────

#: Tags on the mimic. Chosen as the ones an operator glances at, and *stated* as
#: a list rather than derived, because "which six signals are on the overview" is
#: a decision and not a calculation. `tests/test_scada_contract.py` checks every
#: id resolves, so a renamed signal fails the build rather than the mimic.
MIMIC_TAGS: tuple[str, ...] = (
    "INFLUENT:FLOW:FLOW",
    "AERATION:AHU-1:DO",
    "AERATION:AHU-1:AIR_FLOW",
    "AERATION:AHU-1:BLOWER_RPM",
    "PRIMARY:PRI-CL-1:BLANKET",
    "SECONDARY:SEC-CL-1:BLANKET",
    "SLUDGE:DIG-1:PH",
    "EFFLUENT:FLOW:NH4",
    "EFFLUENT:FLOW:TSS",
)

#: The control flow's one writable tag, and why this one.
#:
#: `AERATION:AHU-1:SETPOINT_DO` because it is the only process setpoint on the
#: writable surface, and because an operator changing dissolved oxygen is the
#: most consequential and most obviously bounded action in the plant. The other
#: two writable signals are fault-injection flags and belong in a test harness.
CONTROL_TAG = "AERATION:AHU-1:SETPOINT_DO"

#: The name of the single PostgreSQL config node, and the single Modbus client.
#:
#: Config nodes are runtime-global in Node-RED, so there is exactly one of each and
#: every tab that needs it refers to the same one by id. They used to be called
#: "credential ids", which was the shape the *previous* major version of
#: `node-red-contrib-postgresql` wanted: a credential id hung directly off each
#: query node. That version is gone -- see `postgres_config()` -- and the name
#: outlived the thing it named.
PG_CONFIG = "wwtp-postgres"
MODBUS_CONFIG = "wwtp-softplc"


def pg_config_id() -> str:
    """The id every PostgreSQL query node points at, in every flow."""
    return nid(CONFIG_SCOPE, PG_CONFIG)


def modbus_config_id() -> str:
    """The id the control flow's write node points at."""
    return nid(CONFIG_SCOPE, MODBUS_CONFIG)


#: Where the database connection details come from, and why it is not in git.
#:
#: `node-red-contrib-postgresql` has a `*FieldType` of `env` on every connection
#: field, which reads `process.env[<the field's value>]` at startup. So the flow
#: names the *environment variable* and the address stays in compose, which means
#: moving a container does not change a file in git.
#:
#: `node-red-contrib-modbus` has no such thing -- its client reads `tcpHost`
#: literally -- so the Modbus address is a literal in the flow. That asymmetry is
#: the contrib node's, not a preference, and
#: `tests/test_scada_contract.py::test_the_modbus_address_matches_compose` fails
#: the build if the literal and compose.yaml ever disagree.
PG_HOST_ENV = "POSTGRES_HOST"
PG_PORT_ENV = "POSTGRES_PORT"
PG_DATABASE_ENV = "POSTGRES_DB"

#: The soft PLC's address, as a literal, because the Modbus client has no
#: environment indirection. Mirrors compose.yaml's `MODBUS_HOST` / `MODBUS_PORT`,
#: and `tests/test_scada_contract.py::test_the_modbus_address_matches_compose`
#: fails the build if the two ever disagree.
MODBUS_HOST = "softplc"
MODBUS_PORT = 5020


def _sql_literal(value: str) -> str:
    """A single-quoted SQL string literal.

    Interpolated, not bound, because the query is a *template* that Node-RED's
    PostgreSQL node sends as text. Every value goes through this, and it is the
    only place in the file where a value becomes part of a statement — so a tag id
    containing a quote cannot become SQL, which matters because the ids come from
    a file a human edits.
    """
    if "'" not in value and "\\" not in value:
        return f"'{value}'"
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def mimic_query(tags: tuple[str, ...]) -> str:
    """One query for the whole watched set, not one per tag.

    Thirteen rules over nine signals was already 1.4:1 in the alarm engine, and a
    mimic that issues one round trip per tag would spend its whole budget on
    latency. This is a single statement with an `IN` list, so the cost is one
    query however many tags are watched.

    The `max(ts)` per signal is not a convenience: it is the only correct way to
    read a deadbanded signal. There is no row for a value that did not move, so
    "the current value" is "the newest row", and a query that filtered on recency
    instead would return nothing at all for a steady signal.
    """
    ids = ", ".join(_sql_literal(t) for t in tags)
    return f"""
-- One row per watched tag: the newest reading, and how long ago it was.
-- `reading.value` is NULL for a Bad reading, which is a *reported* failure and
-- not a missing row; `quality` carries it. Do not coalesce these away.
SELECT DISTINCT ON (signal_id)
       signal_id,
       value,
       quality,
       source,
       ts,
       now() - ts AS age
FROM reading
WHERE signal_id IN ({ids})
ORDER BY signal_id, ts DESC
""".strip()


def annunciator_query() -> str:
    """Alarms still on the operator's list, and whether a human has seen them.

    The predicate is the one `alarms/replay.py` uses, and it is not "not
    cleared". A `critical` alarm **latches** — the engine never writes an
    `alarm_cleared` for one, because acknowledging it is what takes it off the
    list — so an acknowledged critical has no clear event *ever* and a naive
    "still active" test would show it forever.

    Two independent reasons an alarm leaves the panel, and the query has to know
    about both:

    * somebody acknowledged it, or
    * the condition cleared (a `warning` auto-clears, so this only happens for
      non-latching severities — kept anyway, because the day a rule is
      reclassified this query should already be right).

    The `severity = 'critical'` filter is because a warning auto-clears and so is
    never something an operator can acknowledge. The window is bounded so the
    panel does not grow without limit, and the bound is in the query rather than
    in a node so it is visible to whoever reads the flow next.
    """
    return """
SELECT r.id,
       r.ts                                            AS raised_at,
       r.severity,
       r.message,
       r.signal_id,
       r.equipment_id,
       r.detail->>'rule'                                AS rule,
       (a.id IS NOT NULL)                              AS acknowledged,
       a.ts                                            AS acknowledged_at,
       now() - r.ts                                    AS age_s
FROM event r
LEFT JOIN LATERAL (
    SELECT ack.id, ack.ts
    FROM event ack
    WHERE ack.kind = 'alarm_acknowledged'
      AND ack.detail->>'rule' = r.detail->>'rule'
      AND ack.ts > r.ts
    ORDER BY ack.ts
    LIMIT 1
) a ON TRUE
WHERE r.kind = 'alarm_raised'
  AND r.severity = 'critical'
  AND r.ts >= now() - interval '24 hours'
  AND a.id IS NULL                       -- nobody has seen it since it raised
  AND NOT EXISTS (
      SELECT 1 FROM event c
      WHERE c.kind = 'alarm_cleared'
        AND c.detail->>'rule' = r.detail->>'rule'
        AND c.ts > r.ts
  )
ORDER BY r.ts DESC
LIMIT 200
""".strip()


def acknowledge_query() -> str:
    """Record the acknowledgement.

    Matched on `detail->>'rule'` rather than on the alarm's own id, because the
    engine identifies a rule by id and an operator acknowledges a *rule*, not an
    occurrence. The consequence is that acknowledging one occurrence of a
    flapping rule acknowledges the rule, which is the behaviour an operator
    expects and the opposite of what an occurrence-based system does.

    The `::text` cast on `$4` is not decoration. A bind parameter inside
    `jsonb_build_object` has **no inferable type**, and Postgres says so:

        could not determine data type of parameter $4

    Which is only visible by running it, and which was found by running it. The
    parameters in the `VALUES` list infer fine from the column types; the one
    inside a function call has nothing to infer from.

    **Positional, not named.** This used to use `$msg`-style named placeholders,
    which the installed contrib node does not bind: it reads `msg.params` or
    `msg.queryParameters` off the *message* and nothing off the node. So `$msg`
    reached `pg` as an unbound parameter literally named `msg`. Positional `$1`
    works with `msg.params`, and unlike the `named` rewrite it is unambiguous
    before a `::` cast.

    This statement was also **dead**: the annunciator carried its own copy inline,
    differing from this one by omitting `equipment_id`, and
    `test_the_acknowledge_query_is_bound_not_interpolated` asserted against this
    copy while its own docstring said "it is not in a flow yet". The flow now calls
    this function, so the thing the test checks is the thing that runs.
    """
    return """
INSERT INTO event (ts, kind, severity, message, signal_id, equipment_id, detail)
VALUES (now(), 'alarm_acknowledged', 'critical', $1, $2, $3,
        jsonb_build_object('rule', $4::text, 'acknowledged_at', now()))
RETURNING id
""".strip()


# ─── node constructors ────────────────────────────────────────────────────────


def tab(name: str, label: str, disabled: bool = False) -> dict[str, Any]:
    return {
        "id": nid("tab", name),
        "type": "tab",
        # A `name` on the tab too. Node-RED's own tab template has one, and its
        # absence is the reason four assertions in the test file had to special-
        # case `n.get("name")` — which is a smell, and a smell in a test is a
        # smell in the thing it describes.
        "name": label,
        "label": label,
        "disabled": disabled,
        "info": _TAB_INFO[name],
        "env": [],
    }


_TAB_INFO: dict[str, str] = {
    "mimic": (
        "Read-only plant overview. Reads the newest reading for a watched set of "
        "tags and builds one status object per refresh. It cannot write anything, "
        "which is a property of the flow and not a promise."
    ),
    "annunciator": (
        "Alarm panel and acknowledgement. Closes the gap in docs/ALARMS.md: "
        "AlarmEngine.acknowledge() exists and nothing called it, because there "
        "was no operator. Acknowledgement is recorded as an event, so it survives "
        "a flow restart even though the engine's own state does not."
    ),
    "control": (
        "Operator setpoint. Checks the requested value against the contract's "
        "write_range before sending it over Modbus. Outside the range the flow "
        "refuses and says why; it does not clamp, because a clamped setpoint is "
        "a setpoint the operator did not ask for."
    ),
}


def inject(
    name: str, tab_id: str, *, every: float = 5.0, once: bool = False,
    manual: bool = False, payload: str = "", payload_type: str = "date",
) -> dict[str, Any]:
    """A timer, or — with `manual` and a payload — the operator's button.

    Three shapes, because they mean three different things and Node-RED will not
    tell them apart:

    * default — fires every `every` seconds.
    * `once` — fires once at deploy. Used to start the event-driven flows.
    * `manual` — **never fires on its own**, and can only be triggered by a
      person from the editor sidebar.

    **`manual` is what the two operator entry points use, and it is load-bearing.**
    Both used to be `once` with `payloadType: "date"`, and that combination has
    two separate faults:

    1. The control flow's entry point could not be given a number. It fired a
       timestamp, `Number()` turned that into epoch milliseconds, and the permit
       check refused a setpoint of `1790925871153`. The flow could only ever
       refuse — indistinguishable, from the outside, from an interlock working.
    2. Giving it a real number to fix that would have written that number to the
       plant on **every container restart**. `once` fires at deploy. So the fix
       for (1) has to come with a node that does not fire at all on its own.

    There is no dashboard in this project, so the sidebar *is* the operator
    interface: edit the payload, then trigger the node. The placeholder payload
    is a shape rather than a value — it names the fields the next node reads, so
    what to edit is visible in the flow and not only in this docstring.
    """
    node: dict[str, Any] = {
        "id": nid(tab_id, name),
        "type": "inject",
        "z": tab_id,
        "name": name,
        "props": [{"p": "payload"}, {"p": "topic", "vt": "str"}],
        "repeat": "" if (once or manual) else str(every),
        "crontab": "",
        "once": False if manual else once,
        "onceDelay": "0.5",
        "topic": "",
        "payload": payload,
        "payloadType": payload_type,
    }
    return node


def function(name: str, tab_id: str, body: str, *, outputs: int = 1) -> dict[str, Any]:
    return {
        "id": nid(tab_id, name),
        "type": "function",
        "z": tab_id,
        "name": name,
        "func": body,
        "outputs": outputs,
        "timeout": 0,
        "noerr": 0,
        "initialize": "",
        "finalize": "",
        "libs": [],
        "x": 400,
        "y": 100,
        "wires": [[] for _ in range(outputs)],
    }


def debug(name: str, tab_id: str) -> dict[str, Any]:
    """A diagnostic tap. Every one of these is active.

    **`tosidebar` is the boolean `True`, and it used to be the string
    `"console"`.** Node-RED's own default is `tosidebar: {value: true}` -- a
    boolean, from a checkbox. The node then guards its publish with

        if (node.active && node.tosidebar) { ... }      // `complete: "true"`
        if (node.tosidebar == true) { sendDebug(...) } // `complete: "payload"`

    and `"console" == true` is **false** in JavaScript: the string coerces to
    `NaN`, which compares equal to `0`, and `true` is `1`. So every debug node in
    all three flows had been receiving messages, discarding them, and publishing
    nothing -- no sidebar entry, no message count on the node in the editor, no
    log line. Eight diagnostic taps, all silently dead, on the flows whose whole
    purpose is to be readable.

    `mimic status` made it worse by also carrying `active: false`, which is a
    defensible thing to want for a node that fires every five seconds, and which
    had never been visible because the other half was broken anyway.

    **`console` stays false deliberately.** `console: true` writes to the
    container log, which is the one output an operator with `docker compose logs`
    actually sees. It is off because `mimic status` fires every five seconds and
    would bury everything else -- the log is a place for the once-per-event
    taps, not for the poll. So the three operator taps (`acknowledged`,
    `control: refused`, `control: written`) and `tag list loaded` are worth
    logging and `mimic status` is not; if that split is wanted, it is one flag
    per node, and the flag is `console`.
    """
    return {
        "id": nid(tab_id, name),
        "type": "debug",
        "z": tab_id,
        "name": name,
        "active": True,
        # A boolean. See the docstring for what the string "console" does here.
        "tosidebar": True,
        "console": False,
        "tostatus": False,
        "complete": "payload",
        "targetType": "msg",
        "statusVal": "",
        "statusType": "auto",
        "x": 400,
        "y": 100,
        # **Empty, not `[[]]`.** Node-RED registers `debug` with `outputs: 0`
        # (`21-debug.html:104`) — a debug node is a sink with no output port at
        # all. This emitted `[[]]`, a single port wired to nothing, and the
        # comment above it claimed the node "has one output", which is what kept
        # the wrong number in place across every rewrite of this function. See
        # `_outputs()`.
        "wires": [],
    }


def comment(name: str, tab_id: str, info: str) -> dict[str, Any]:
    return {
        "id": nid(tab_id, name),
        "type": "comment",
        "z": tab_id,
        "name": name,
        "info": info,
        "x": 180,
        "y": 100,
        "wires": [],
    }


def file_in(name: str, tab_id: str, path: str) -> dict[str, Any]:
    """Read a file at startup.

    A core node, and the reason the tag list is a *file*: Node-RED cannot read
    one from inside a function, so the generated JSON has to be loaded by
    something.

    **`filenameType: 'str'`, not `'msg'`.** The docstring here used to describe the
    `msg` form as deliberate — "with an injected payload, the smallest way to say
    read this when the flow starts" — but nothing injected a payload. The node is
    driven by a `trigger` whose `op1` is `"1"`, so `msg.payload` was the string
    `1` and the node dutifully tried to read a file *named* `1` out of the working
    directory. Not found, sent nowhere, and the tag list stayed empty.

    The docstring also said the `msg` form was chosen because Node-RED's own
    template defaults `filename` to `["1"]`, "which is a filename, not a wire". That
    observation is right and the conclusion drawn from it was inverted: the template
    puts a placeholder filename there precisely so the node is inert until someone
    fills it in, and `'str'` with a real path is the way to fill it in.
    """
    return {
        "id": nid(tab_id, name),
        "type": "file in",
        "z": tab_id,
        "name": name,
        "filename": path,
        # A literal, and `filename` is the path. See the docstring.
        "filenameType": "str",
        "format": "utf8",
        "chunk": False,
        "sendError": False,
        "encoding": "none",
        "allProps": False,
        "x": 200,
        "y": 100,
        # One output, which used to be documented here as two -- "0 is file not
        # found, 1 is the contents" -- on the strength of Node-RED 0.x. Node-RED
        # 4's core `file in` registers `outputs: 1` and sends the contents there;
        # a read error goes to `node.error`, not to a second port. So the wire
        # from this node was already on the right port and the declared port count
        # was the thing that was wrong.
        #
        # **`sendError` is false on purpose**, and it is what makes a wrong path
        # silent: with it true the node sends a message with `msg.error` on output
        # 0, which would land on the loader and fail there. With it false a
        # missing file produces `node.error` on the node itself -- visible in the
        # log -- and nothing downstream. Which is why the tag list being empty is
        # reported by the *consumer*, not by the reader: `test_a_consumer_says_so_
        # when_the_tag_list_is_missing` exists for exactly that gap.
        "wires": [[]],
    }


def _outputs(node: dict[str, Any]) -> int:
    """How many output ports a node has, by type.

    Guessed rather than configured, and the guess is checked by
    `tests/test_scada_contract.py` — a `wires` list whose length does not match
    the node's output count is how a Node-RED flow imports cleanly and then
    silently drops every message on an output nobody wired.

    **Three of these were wrong, in the same direction, and none of them
    complained.** `node-red-contrib-postgresql` 0.16 *and* Node-RED 4's core `file
    in` each register with **one** output; this said two. `node-red-contrib-modbus`
    registers with **two**; this said one. A `postgresql` node wired from output 1
    therefore never fired at all, and every flow in this project ended on a
    result nobody saw.

    The counts are now read off the node packages rather than remembered, and
    `tests/test_scada_contract.py::test_the_output_counts_match_the_installed_
    node_packages` re-derives them from the running container, so a contrib
    upgrade that changes an output count fails the build instead of the runtime.
    """
    if node["type"] in ("tab", "comment"):
        # Neither has ports. A `tab` has no `wires` key at all in Node-RED's own
        # template, and a `comment` has an empty one, so returning 1 for both
        # made the output-count check fail on two nodes that are correct.
        return 0
    if node["type"] in CONFIG_NODE_TYPES:
        # No ports at all, and no `wires` key in Node-RED's own export of one.
        # They are referenced by an id field on the node that uses them.
        return 0
    if node["type"] == "function":
        return int(node.get("outputs", 1))
    if node["type"] == "modbus-write":
        # `node-red-contrib-modbus` registers it with `outputs: 2`. Output 0 is
        # the completed write; output 1 is the error response.
        return 2
    if node["type"] == "debug":
        # **Zero, and this said one.** `21-debug.html:104` registers `outputs: 0`:
        # a debug node is a sink. So `debug()` emitted `"wires": [[]]`, one output
        # port pointing at nothing, which Node-RED imports without complaint and
        # which the output-count check could not see because both numbers came
        # from the same wrong place.
        #
        # Found by the probe once the probe worked, which is the argument for
        # spending the effort on it: three hand-written corrections and the first
        # probe to actually run immediately found a fourth.
        return 0
    return 1


def modbus_client(name: str, host: str, port: int, unit_id: int) -> dict[str, Any]:
    """`node-red-contrib-modbus`'s TCP client, as a runtime-global config node.

    **This node used not to exist.** `modbus_write()` pointed its `server` field at
    the *string* `"wwtp-softplc-modbus"`, on the stated reasoning that "the user
    creates it in the editor pointing at the soft PLC". The field is a config-node
    id, so the write node did `t.nodes.getNode("wwtp-softplc-modbus")`, got
    `undefined`, and guarded every registration with `if (client)`. It registered
    with nothing, received nothing, wrote nothing, and reported no error. A control
    flow that silently cannot act on the plant, on a tab whose comment says it is
    the way a setpoint reaches the PLC.

    The address is a literal because this contrib node has no environment
    indirection -- there is no `tcpHostFieldType`, unlike the PostgreSQL config
    node. So the drift between this and compose.yaml is guarded by a test rather
    than by cleverness.
    """
    return {
        "id": nid(CONFIG_SCOPE, name),
        "type": "modbus-client",
        "name": name,
        "clienttype": "tcp",
        "bufferCommands": True,
        "stateLogEnabled": False,
        "queueLogEnabled": False,
        "failureLogEnabled": True,
        "tcpHost": host,
        "tcpPort": port,
        "tcpType": "DEFAULT",
        "serialPort": "/dev/ttyUSB",
        "serialType": "RTU-BUFFERD",
        "serialBaudrate": 9600,
        "serialDatabits": 8,
        "serialStopbits": 1,
        "serialParity": "none",
        "serialConnectionDelay": 100,
        "serialAsciiResponseStartDelimiter": "0x3A",
        "unit_id": unit_id,
        "commandDelay": 1,
        "clientTimeout": 1000,
        "reconnectOnTimeout": True,
        "reconnectTimeout": 2000,
        "parallelUnitIdsAllowed": True,
        "showErrors": True,
        "showWarnings": True,
        "showLogs": False,
    }


def modbus_write(
    name: str, tab_id: str, config_id: str, *, address: int, quantity: int,
    data_type: str, unit_id: int,
) -> dict[str, Any]:
    """`node-red-contrib-modbus`'s write node.

    **Almost every field name here was wrong, and none of them was required.**

    The node was emitted with `datatype`, `scale`, `offset` and `polling`. The node
    reads `dataType`, `adr` and `quantity`; it ignores the other three entirely,
    and `dataType`, `adr` and `quantity` are all `required: true` in its editor
    registration. So it loaded with three missing required fields, and
    `Number(undefined)` for the address is `NaN` — a write to address NaN, which
    the soft PLC rejects, so the failure would have been on the far side of the
    wire where nobody is looking.

    `datatype: "float"` was not a value the node has ever accepted either. Its
    valid values are `Coil`, `HoldingRegister`, `MCoils` and `MHoldingRegisters`,
    and it sends numbers as they are -- it has no float encoding at all. A float32
    occupies two registers, so the node writes `quantity: 2` holding registers and
    the two 16-bit words are built by the function node upstream, in the word order
    the contract states. `docs/ALARMS.md` and the contract both call the
    swapped-order registers a trap; the encoding is generated from
    `word_order`, so the trap cannot be entered by hand here.

    Two outputs: 0 is the completed write, 1 the error response.

    **`address` is a contract 4xxxx address and is translated here**, because
    `adr` is a PDU offset and the two are not the same number. The contract says
    `40102`; the wire wants `102`.

    That gap produced the most honest-looking failure in this project: every
    layer agreed with the contract, the flow read a clean address out of the
    contract, the write was attempted, and the soft PLC answered

        Modbus exception 2: Illegal data address (register not supported by device)

    Naming the register as unsupported rather than as "40000 too high". So the
    write never landed, and the one safety-critical path in the project — the one
    whose entire job is to move a number into the plant — did nothing, while
    reporting that it had tried.

    The translation belongs in the generator, in one place, using the server's own
    constant rather than a literal: `softplc.servers.modbus_server.ModbusTcpServer
    .wire_offset` is the authority on this arithmetic, and it derives from three
    shifts (the model's `address - 40000`, a one-slot block lead-in, and pymodbus
    resolving `PDU = index - 1`) that cancel to a subtraction. Importing it means
    a change to the block layout cannot leave this writing to the wrong offset.
    """
    return {
        "id": nid(tab_id, name),
        "type": "modbus-write",
        "z": tab_id,
        "name": name,
        # A config-node **id**, not a name. See `modbus_client()`.
        "server": config_id,
        "showStatusActivities": False,
        "showErrors": True,
        "showWarnings": True,
        "unitid": unit_id,
        "dataType": data_type,
        # The wire offset, not the contract's 4xxxx address. See the docstring.
        "adr": ModbusTcpServer.wire_offset(address),
        "quantity": quantity,
        "emptyMsgOnFail": False,
        # **True, and this was False.** `modbus-write` builds its output with
        #   keepMsgProperties ? Object.assign(msg, built) : built
        # so `false` means the output is *only* the Modbus payload
        # `{value, unitid, fc, address, quantity, messageId}` and every other
        # message property is discarded. `record what was written` reads `msg.req`
        # -- the tag, the setpoint the operator asked for, the range it was checked
        # against -- so with `false` that node got `undefined`, read
        # `req.write_range.join('-')`, and threw
        #
        #     TypeError: Cannot read properties of undefined (reading 'join')
        #
        # on every write. The write itself had already succeeded: the setpoint was
        # in the PLC, and the audit trail -- whose entire purpose is to record that
        # -- crashed trying to say so. A crash in the audit node is not a safe
        # failure, it is an unlogged control action.
        "keepMsgProperties": True,
        "delayOnStart": False,
        "startDelayTime": "",
        "x": 400,
        "y": 100,
        "wires": [[], []],
    }


def wire(nodes: list[dict[str, Any]], *pairs: tuple[str, int, str]) -> None:
    """Attach outputs to inputs, given node *names*.

    Node-RED wiring is **backwards**: an input port names the id of the node it
    connects *to*, not the node it connects from. Every hand-written `flows.json`
    gets this wrong first, and the symptom is a node that imports cleanly and does
    nothing — the worst possible failure for a diagram an operator is looking at.

    So it is written once, here, forwards, and the backwards direction never
    appears in a flow file. Names rather than node dicts because a builder has the
    names and the ids are derived from them, so a rename cannot leave a wire
    pointing at nothing: an unknown name is a `KeyError` at generation time.

    **Config nodes are not wireable and are excluded by type, not by name.** They
    have no ports; a config node is attached to a node that uses it by an id field,
    which is the whole of Node-RED's mechanism. So a config node appearing in
    `by_name` could only be reached by a mistake, and the failure would be an
    `AttributeError` on `source["z"]` or a wire to a node with no ports. Both are
    worse than naming the problem.
    """
    by_name = {
        n["name"]: n
        for n in nodes
        if n["type"] != "tab" and n["type"] not in CONFIG_NODE_TYPES
    }
    for source_name, output, target_name in pairs:
        for label in (source_name, target_name):
            if label not in by_name:
                raise KeyError(
                    f"wire references {label!r}, which is not a wireable node in "
                    f"this flow. Wireable nodes: {sorted(by_name)}. A config node "
                    f"({', '.join(sorted(CONFIG_NODE_TYPES))}) has no ports and is "
                    f"attached by an id field instead."
                )
        source = by_name[source_name]
        source["wires"] = source.get("wires") or [
            [] for _ in range(_outputs(source))
        ]
        while len(source["wires"]) <= output:
            source["wires"].append([])
        if output >= _outputs(source):
            raise ValueError(
                f"wire reads output {output} of {source_name!r}, a "
                f"{source['type']} with {_outputs(source)} output(s). Node-RED "
                f"accepts the wire and nothing arrives on that port."
            )
        # Scoped by the *source's* tab, which is the only tab both nodes are on --
        # `wire` refuses a cross-tab pair, so this is unambiguous.
        source["wires"][output].append(nid(source["z"], target_name))


def postgres_config(name: str) -> dict[str, Any]:
    """`node-red-contrib-postgresql`'s config node: the database connection.

    **This node used not to exist**, and its absence was the loudest failure in the
    project. Each query node was emitted with `"mydb": "wwtp-db"` — a credential id
    hung directly off the query node, which is what version 0.8 of the contrib node
    wanted. Version 0.16.2 replaced it with a separate `postgreSQLConfig` node and
    removed `mydb` from the editor entirely, so the field survived in the flow file
    as a key nothing reads.

    The consequence is worth recording, because the error names neither the node
    that is wrong nor the thing that is missing:

        TypeError: node.config.pgPool.connect is not a function

    That is `postgresql.js`'s fallback for "I could not find the config node you
    referenced", substituting a `{pgPool: {totalCount: 0}}` stub so the editor shows
    the right shape. At runtime it throws on every poll. It looks like a driver
    version problem, and it is not.

    Every `*FieldType` of `env` makes the node read `process.env[<value>]`, so the
    host, port and database name are environment variables named here rather than
    addresses written into git. The user and password are the exception: they are
    `cred`, and `scada/nodered/entrypoint.sh` writes them into `flows_cred.json`
    keyed by **this node's id** — which is what `credentials.get(id)` reads, and
    why the id has to be stable and shared.
    """
    return {
        "id": nid(CONFIG_SCOPE, name),
        "type": "postgreSQLConfig",
        "name": name,
        # No `z`: a config node is not on a tab. See CONFIG_SCOPE.
        "host": PG_HOST_ENV,
        "hostFieldType": "env",
        "port": PG_PORT_ENV,
        "portFieldType": "env",
        "database": PG_DATABASE_ENV,
        "databaseFieldType": "env",
        "ssl": False,
        "sslFieldType": "bool",
        "applicationName": "wwtp-scada",
        "applicationNameType": "str",
        "max": 10,
        "maxFieldType": "num",
        "idle": 1000,
        "idleFieldType": "num",
        "connectionTimeout": 10000,
        "connectionTimeoutFieldType": "num",
        # `cred` on both, so neither value is ever in a flow file.
        "userFieldType": "cred",
        "userEnv": "",
        "passwordFieldType": "cred",
        "passwordEnv": "",
    }


def postgres(
    name: str, tab_id: str, query: str,
) -> dict[str, Any]:
    """A PostgreSQL query node.

    **Bind parameters moved from the node to the message.** This used to carry a
    `params` list of `{"type": "msg", "name": ...}` entries and the SQL used named
    placeholders like `$msg`. Version 0.16.2 removed the node-level `params`
    entirely: binds are now read from `msg.params` (positional) or
    `msg.queryParameters` (named, rewritten by the `named` package). So the
    `params` lists were dead configuration, and the `$msg` in the statement was
    never bound by anything — it reached `pg` as a literal parameter named `msg`,
    which is exactly the injection the placeholder existed to prevent.

    The queries now use positional `$1`/`$2` and the function node upstream sets
    `msg.params`. Positional rather than `msg.queryParameters` because a `:name`
    placeholder cannot be followed by a `::type` cast unambiguously, and the
    `jsonb_build_object` arguments need one.
    """
    return {
        "id": nid(tab_id, name),
        "type": "postgresql",
        "z": tab_id,
        "name": name,
        "query": query,
        # The config-node **id**, not a credential id. See `postgres_config()`.
        "postgreSQLConfig": nid(CONFIG_SCOPE, PG_CONFIG),
        "split": False,
        "rowsPerMsg": 1,
        "maxsize": "0",
        "x": 360,
        "y": 100,
        # **One output.** It used to be two, wired from output 1, on the
        # assumption -- carried over from the 0.x contrib node -- that output 0 was
        # an error row. There is no error output. A query that fails calls
        # `done(err)`, which raises a catch node or logs, and sends nothing at all;
        # so the error path is already "nothing arrives downstream", and the output
        # the results were wired from was one that does not exist. Every flow in
        # this project ended on that output.
        "wires": [[]],
    }


#: Emitted at the head of every flow.
#:
#: Node-RED's `global` context is **per flow file**, not per runtime, so two flows
#: cannot share a loaded tag list. That is a sharp edge and it produces a
#: `global.get('wwtpTags')` returning `undefined` in the second flow with no error
#: anywhere -- the function's default parameter silently kicks in and the
#: annotation node produces rows with no units on them. So the loader is emitted
#: per flow. It is twelve lines and it is a node, not a require, so it costs
#: nothing at all.
#:
#: **The path is where compose mounts the flows, not where the runtime writes
#: them.** compose.yaml mounts `./scada/flows:/flows:ro`, so `tags.json` is at
#: `/flows/tags.json` and nothing ever put it at `/data/tags.json` -- the tag
#: loader pointed at a file that did not exist in any container. Every consumer
#: has a `?? []` default, so that produced a warning on a `debug` node and a mimic
#: showing six signals with no units and no bands, every five seconds, and no
#: error anywhere: a plant that looks alive and is showing nothing.
TAGS_PATH_IN_FLOW = "/flows/tags.json"

_LOAD_TAGS_BODY = """
// The tag list, generated from contracts/tags.yaml. Committed and regenerable:
// `python -m scada.generate_tags`.
//
// Stored in `global`, which is per *flow file* in Node-RED, so every flow loads
// its own. The `?? []` defaults in the other nodes are the symptom of this not
// having run: they make a missing tag list look like a flow that works.
const parsed = JSON.parse(msg.payload);
global.set('wwtpTags', parsed.tags);
global.set('wwtpAreas', parsed.areas);
global.set('wwtpPermit', parsed.permit);
global.set('wwtpTagSchema', parsed.schema);
node.status({
    fill: 'green',
    shape: 'dot',
    text: `${parsed.tags.length} tags, ${parsed.counts.writable} writable`,
});
msg.payload = {
    loaded: parsed.tags.length,
    areas: Object.keys(parsed.areas).length,
    schema: parsed.schema,
};
return msg;
"""


def _loader_nodes(tab_id: str) -> list[dict[str, Any]]:
    """The four nodes that get a tag list into `global`.

    **`inject` with `once`, not a `trigger`.** The loader used to be driven by a
    `trigger` node with `op1`/`op2`, which reads like the documented way to fire
    once on startup. It is not: in Node-RED 4 the `trigger` node is purely
    reactive. Its implementation registers `this.on("input", ...)` and a `close`
    handler and has no startup path at all, so with `inputs: 1` and nothing wired
    into it, the `op1` branch never executes. It did not fire on deploy, in any
    version, ever.

    `inject` with `once: true` does: `20-inject.js:88` sets a timeout of
    `onceDelay` (default 0.1 s) on construction and sends. That is the whole
    mechanism, and it is the only one.

    The symptom was downstream of this, and pointed everywhere except here. No tag
    list, so every consumer's `?? []` default produced rows with no units and no
    bands; the `file in` node never ran, so nothing said the file was missing; and
    the five-second injects ran fine and reported an empty plant every time. A flow
    that imports cleanly, starts cleanly, polls cleanly and shows nothing.
    """
    starter = inject("load tags: on deploy", tab_id, once=True)
    reader = file_in("read tags.json", tab_id, TAGS_PATH_IN_FLOW)
    loader = function("put tags in global", tab_id, _LOAD_TAGS_BODY)
    report = debug("tag list loaded", tab_id)

    nodes = [starter, reader, loader, report]
    wire(nodes,
         ("load tags: on deploy", 0, "read tags.json"),
         ("read tags.json", 0, "put tags in global"),
         ("put tags in global", 0, "tag list loaded"))
    return nodes



# ─── the three flows ──────────────────────────────────────────────────────────


def build_mimic(c: Contract) -> list[dict[str, Any]]:
    missing = [t for t in MIMIC_TAGS if t not in c.signals]
    if missing:
        raise KeyError(
            f"mimic watches signals the contract does not declare: {missing}. "
            f"Fix MIMIC_TAGS; a flow cannot reference a tag that does not exist."
        )
    tab_id = nid("tab", "mimic")
    units = {t: c.signals[t].unit for t in MIMIC_TAGS}

    nodes = [*_loader_nodes(tab_id),
        tab("mimic", "01 — Plant mimic (read-only)"),
        # The one PostgreSQL config node, emitted here and **only** here.
        #
        # It is a runtime-global config node, so it must appear exactly once across
        # the concatenated `flows.json` — emitting it per flow would give three
        # nodes one id, which is the same duplicate-id failure `nid()` had. The
        # other two flows reference it by the same derived id and do not define it.
        #
        # The cost is that importing `02-annunciator.json` or `03-control.json`
        # *alone* into an editor gives a flow whose query nodes have nothing to
        # point at. The deployment path is the entrypoint concatenating all three,
        # and that is what the drift gate checks.
        postgres_config(PG_CONFIG),
        comment(
            "c1", tab_id,
            "Generated by `python -m scada.build_flows`. Do not edit by hand: "
            "edit `scada/build_flows.py` and regenerate, or the next run will "
            "silently revert it.\n\n"
            "The `DISTINCT ON (signal_id)` is not a shortcut. There is no row for "
            "a value that did not move — the deadband suppresses it — so the "
            "current value of a signal is the newest row, and a query that "
            "filtered on recency would return nothing for a steady signal.\n\n"
            f"Watched: {', '.join(MIMIC_TAGS)}\n"
            f"Units:  {', '.join(_unit_list(t, units) for t in MIMIC_TAGS)}",
        ),
        inject("every 5 s", tab_id, every=5.0),
        postgres("read newest values", tab_id, mimic_query(MIMIC_TAGS)),
        function(
            "shape the status object", tab_id,
            """
// Attach the tag metadata the tag list carries, so the payload is renderable
// without a second lookup. The units come from `tags.json`, which is generated
// from the same contract as the query above -- so a unit cannot be right in one
// and wrong in the other.
const tagList = global.get('wwtpTags') || [];
if (tagList.length === 0) {
    // **Say so.** The `|| []` above is what makes a flow that never loaded its
    // tags look like it works: the rows still come back, every `unit` is `''` and
    // every `normal_low` is `undefined`. A blank diagram is worse than a red
    // node, because nobody reports a blank diagram.
    node.status({
        fill: 'red', shape: 'ring',
        text: 'tag list not loaded — run the loader node',
    });
    node.warn('wwtpTags is empty: the tag loader has not run');
} else {
    node.status({ fill: 'green', shape: 'dot', text: `${tagList.length} tags` });
}
const byId = Object.fromEntries(tagList.map((t) => [t.id, t]));

const tags = [];
for (const row of msg.payload) {
    const t = byId[row.signal_id] || {};
    tags.push({
        id: row.signal_id,
        label: t.label || row.signal_id,
        area: t.area || null,
        equipment: t.equipment || null,
        value: row.value,
        unit: t.unit || '',
        quality: row.quality,
        source: row.source,
        // Two different facts, kept apart. `value` is the last value the plant
        // reported; `age` is how long ago that was. A signal whose value has not
        // moved is NOT a signal that has stopped reporting, and collapsing the
        // two is the bug this flow exists to avoid.
        ts: row.ts,
        age_s: row.age,
        // Bad quality is a *reported* failure. A NULL value with quality != 0 is
        // the instrument saying it does not know, which is not the same as no
        // row -- and it must not be rendered as zero.
        bad: row.value === null || row.quality !== 0,
    });
}

tags.sort((a, b) => a.id.localeCompare(b.id));
msg.payload = {
    generated_at: new Date().toISOString(),
    count: tags.length,
    bad: tags.filter((t) => t.bad).length,
    tags,
};
return msg;
""",
        ),
        function(
            "annotate against the normal band", tab_id,
            """
// In-band or not, decided here and not in the database.
//
// This is NOT the alarm engine. `alarms/rules.py` decides whether something is
// wrong, from a measured healthy distribution, and it writes an event when it
// decides. This only says "the last value happened to be inside the band the
// contract declares" -- which is true of most values most of the time, is not a
// fault detector, and must not be read as one. See docs/ALARM-TUNING.md for why
// a band drawn from a specification is a poor alarm threshold.
const tagList = global.get('wwtpTags') || [];
const byId = Object.fromEntries(tagList.map((t) => [t.id, t]));

if (tagList.length === 0) {
    node.status({ fill: 'red', shape: 'ring', text: 'no tag list — no bands' });
}

for (const t of msg.payload.tags) {
    const spec = byId[t.id];
    if (!spec || t.value === null) {
        t.in_band = null;
        continue;
    }
    t.in_band = t.value >= spec.normal_low && t.value <= spec.normal_high;
    t.normal_low = spec.normal_low;
    t.normal_high = spec.normal_high;
}
return msg;
""",
        ),
        debug("mimic status", tab_id),
    ]
    wire(nodes,
         ("every 5 s", 0, "read newest values"),
         # Output 0, not 1: the node has one output. See `postgres()`.
         ("read newest values", 0, "shape the status object"),
         ("shape the status object", 0, "annotate against the normal band"),
         ("annotate against the normal band", 0, "mimic status"))
    return nodes


def build_annunciator(c: Contract) -> list[dict[str, Any]]:
    """The annunciator needs no contract data, and that is worth saying.

    It reads the `event` table, which the alarm engine writes, and the events
    carry the rule id and the equipment the engine already resolved. So the flow
    has no tag lookups to do and no contract input to go stale — a property worth
    having and one the mimic does not have.

    `c` is accepted and unused, because every builder takes the contract and a
    signature that is uniform for two of three is a signature that gets special-
    cased the next time the third needs it.
    """
    del c
    tab_id = nid("tab", "annunciator")
    nodes = [*_loader_nodes(tab_id),
        tab("annunciator", "02 — Alarm annunciator"),
        comment(
            "a1", tab_id,
            "Generated by `python -m scada.build_flows`.\n\n"
            "This is the operator side of the alarm engine. `AlarmEngine."
            "acknowledge()` exists and is tested; before this flow, nothing called "
            "it, because there was no operator to call it.\n\n"
            "Why the panel is event-driven and the mimic is not: the panel has "
            "nothing to poll for. Alarms change when the engine writes them, and "
            "polling every five seconds would be a five-second-old panel and a "
            "query nobody needed. The mimic is the opposite -- it shows the "
            "plant's current state, which changes whether or not anyone says so.",
        ),
        inject("refresh the panel", tab_id, once=True),
        # The operator's own input. See the note on `wire()` below for why this
        # exists at all.
        inject("operator acknowledges an alarm", tab_id, manual=True,
               payload='{"rule": "…", "orig": {"message": "…", "signal_id": "…", '
                       '"equipment_id": "…"}}',
               payload_type="json"),
        postgres("read unacknowledged criticals", tab_id, annunciator_query()),
        function(
            "mark staleness", tab_id,
            """
// A panel that cannot tell you how old it is is a panel you trust wrongly. The
// query is bounded at 24 hours; this says what the bound means.
const now = Date.now();
msg.payload = (msg.payload || []).map((a) => ({
    ...a,
    age_s: Math.round((now - new Date(a.ts).getTime()) / 1000),
    rule: (a.detail && a.detail.rule) || null,
    equipment: a.equipment_id || null,
    signal: a.signal_id || null,
}));
msg.payload = {
    generated_at: new Date().toISOString(),
    window_h: 24,
    unacknowledged: msg.payload.length,
    alarms: msg.payload,
};
return msg;
""",
        ),
        function(
            "acknowledge what the operator pressed", tab_id,
            """
// The operator pressed a row. This is the path that `AlarmEngine.acknowledge()`
// never had for three phases: a method that existed, was tested, and was called
// by nothing because there was no operator.
//
// The write is bound, not interpolated -- the rule id comes from a row the
// operator clicked, and a rule id is machine-generated today but a message is
// not. An INSERT built by concatenation is a SQL injection one mistyped row away.
//
// **`msg.params`, not a list on the node.** The installed contrib node takes its
// binds from the message; the `params` list these nodes used to carry is a 0.x
// field it no longer reads, so the statement was running with nothing bound. The
// order here is the `$1..$4` order in `acknowledge_query()`, and the `::text` cast
// on the rule is because a parameter inside `jsonb_build_object` has no inferable
// type.
//
// `detail->>'rule'` is the join key because the engine puts the rule there and
// there is no column for it. See alarms/replay.py for why that is a cost being
// paid knowingly.
const rule = msg.payload.rule;
const alarm = msg.payload.orig || msg.payload;

msg.params = [
    `acknowledged: ${alarm.message || rule}`,
    alarm.signal_id || null,
    alarm.equipment_id || null,
    rule,
];
msg.topic = 'acknowledge';
return msg;
""",
        ),
        postgres("record the acknowledgement", tab_id, acknowledge_query()),
        debug("annunciator panel", tab_id),
        debug("acknowledged", tab_id),
    ]
    wire(nodes,
         ("refresh the panel", 0, "read unacknowledged criticals"),
         # Output 0, not 1: the node has one output. See `postgres()`.
         ("read unacknowledged criticals", 0, "mark staleness"),
         ("mark staleness", 0, "annunciator panel"),
         # The operator path. Separate from the refresh path, deliberately: a
         # refresh is a timer and must never be able to acknowledge anything.
         #
         # **It used to run off `annunciator panel` -- the `debug` node.** That
         # is the shape you draw when you want a debug tap to double as the
         # operator's button, and it is not a shape Node-RED has: `debug`
         # registers `outputs: 0` (`21-debug.html:104`), so that wire pointed at
         # a port that does not exist and the acknowledge path was dead for the
         # same reason it was built.
         #
         # Which is the second time in this project a *debug node* has been asked
         # to carry messages (`tosidebar` in `debug()` is the first). A tap that
         # displays is not a control surface, and using it as one is invisible in
         # the flow file: the wire is drawn, the id resolves, and the diagram
         # looks complete.
         ("operator acknowledges an alarm", 0,
          "acknowledge what the operator pressed"),
         ("acknowledge what the operator pressed", 0,
          "record the acknowledgement"),
         ("record the acknowledgement", 0, "acknowledged"))
    return nodes


def build_control(c: Contract) -> list[dict[str, Any]]:
    if CONTROL_TAG not in c.signals:
        raise KeyError(f"{CONTROL_TAG} is not a signal in the contract")
    if CONTROL_TAG not in c.writable:
        raise KeyError(
            f"{CONTROL_TAG} is not on the writable surface, so a control flow for "
            "it would be asking the plant to accept something the contract says "
            "is read-only"
        )
    spec = c.writable[CONTROL_TAG]
    low, high = spec["range"]
    signal = c.signals[CONTROL_TAG]
    tab_id = nid("tab", "control")

    # The register's address, word order and unit id all come from the contract,
    # never from a number typed here. The address and the word order are the two
    # that matter: a wrong word order writes a perfectly plausible-looking float to
    # a register the plant will read as something else entirely, and `contracts/
    # tags.yaml` marks two registers as traps for exactly this reason.
    address, word_order, unit_id = _modbus_register(c, CONTROL_TAG)
    modbus_host, modbus_port = MODBUS_HOST, MODBUS_PORT
    modbus_unit = unit_id

    if word_order not in ("big", "little"):
        raise KeyError(
            f"register for {CONTROL_TAG} has word_order {word_order!r}; the flow "
            f"encoder knows 'big' and 'little' and refuses to guess"
        )

    check = f"""
// The permit check, and the most important {{}} lines in this project.
//
// `write_range` is the contract's `writable:` range:
// {low} to {high} {signal.unit}.
// The contract also carries the *permit limits* -- the discharge consent
// conditions -- and this range sits inside them, because a setpoint that put DO
// below what nitrification needs would breach the permit on the effluent side
// hours later.
//
// **Refuse, do not clamp.** A clamped setpoint is a setpoint the operator did not
// ask for, and an operator who types 20 and gets 6 will not try again; an
// operator who types 20 and is told "20 is outside 0.5 to 6" will. The refusal
// carries the range and the reason so the message is actionable.
const tagList = global.get('wwtpTags') || [];
const spec = tagList.find((t) => t.id === '{CONTROL_TAG}');

if (!spec) {{
    // Without this the next line throws a TypeError inside the function, Node-RED
    // logs it, and the flow does nothing at all — which an operator reads as "the
    // setpoint was not accepted" rather than "the tag list never loaded". A
    // control flow that fails by doing nothing is the worst of the three
    // behaviours available here.
    node.status({{ fill: 'red', shape: 'ring', text: 'no tag list' }});
    msg.payload = {{
        ok: false, tag: '{CONTROL_TAG}',
        error: 'the tag list has not loaded, so the writable range is unknown. '
             + 'Nothing was written to the plant.',
    }};
    return [msg, null];
}}
node.status({{ fill: 'green', shape: 'dot', text: spec.write_range.join('-') }});

const requested = Number(msg.payload);
if (!Number.isFinite(requested)) {{
    msg.payload = {{
        ok: false, tag: '{CONTROL_TAG}',
        error: 'not a number', requested: msg.payload,
    }};
    return [msg, null];
}}

const [lo, hi] = spec.write_range;
if (requested < lo || requested > hi) {{
    msg.payload = {{
        ok: false, tag: '{CONTROL_TAG}', requested, write_range: [lo, hi],
        unit: spec.unit,
        // A **template literal** -- backticks *and* `$`. It used to be
        // A template literal with no `$` in it. It was valid JavaScript that
        // interpolated nothing, so the operator was shown the literal
        // placeholder text -- "requested and spec.unit, outside the writable range
        // lo-hi spec.unit". The refusal -- the one message in this project whose
        // entire job is to tell an operator what they typed and what the range is
        // -- said neither.
        error: `${{requested}} ${{spec.unit}} is outside the writable range `
             + `${{lo}}-${{hi}} ${{spec.unit}}`,
        // The reason is from the contract. It is the sentence an operator needs,
        // and it is stored rather than rendered so the refusal and the write
        // carry the same justification.
        reason: spec.write_reason,
    }};
    return [msg, null];
}}

msg.payload = {{
    ok: true, tag: '{CONTROL_TAG}', value: requested,
    write_range: [lo, hi], unit: spec.unit, reason: spec.write_reason,
}};
// **Output 1, not output 0.** This was `return [msg, null]`, the same as all
// three refusals above, so a setpoint that passed the check left on the
// *refusal* port and output 1 -- the Modbus write -- got null.
//
// Which means the control flow had never once written to the plant. Every
// refusal worked, the refusals were the only thing that ever happened, and every
// refusal looks like correct behaviour. Found by injecting 3.5 into the running
// flow and watching the `control: refused` tap receive `ok: true`.
//
// The three refusals are correct as written: refused goes to output 0, which is
// what the `control: refused` tap is wired to.
return [null, msg];
"""

    nodes = [*_loader_nodes(tab_id),
        tab("control", "03 — Operator control (setpoint)"),
        modbus_client(MODBUS_CONFIG, modbus_host, modbus_port, modbus_unit),
        comment(
            "k1", tab_id,
            "Generated by `python -m scada.build_flows`.\n\n"
            f"Writes `{CONTROL_TAG}` over **Modbus**, to the soft PLC, through the "
            "same path a remote setpoint would take.\n\n"
            "It does not write to Postgres, and that is a safety property rather "
            "than an implementation detail. A setpoint in the database is a number "
            "that looks like a command and is not one: nothing acts on it, and a "
            "reader would reasonably believe the plant had been told.\n\n"
            "The check node refuses out-of-range values with the range and the "
            "reason. It does not clamp.\n\n"
            f"Contract range: {low} to {high} {signal.unit}. "
            f"Reason: {spec['reason']}",
        ),
        inject("enter a setpoint", tab_id, manual=True, payload="3.0",
               payload_type="num"),
        function("check against the permit range", tab_id, check, outputs=2),
        function(
            "build the Modbus write", tab_id,
            """
// Encode one float32 into the two 16-bit holding registers the contract says it
// lives in, in the word order the contract says.
//
// **This is the single most dangerous thing about writing to this plant.** A
// float32 is four bytes; a Modbus register is two. Which half goes first is a
// convention, both conventions are common, and getting it wrong does not error --
// it writes a plausible float to the register and the plant reads a different
// plausible float. `contracts/tags.yaml` marks two registers "⚠ TRAP: low word
// first" for exactly this. So the word order here is read out of the contract at
// generation time and written into this node, and there is nowhere in the flow to
// type one by hand.
//
// Byte-for-byte the same as `softplc/servers/modbus.py::encode_float32`, which is
// the other end of this wire: `>f` here is `struct.pack(">f")` there, and `>H` is
// `struct.unpack(">HH")`. `Buffer` is one of the globals the function node's
// sandbox provides, so this needs no external module.
const value = msg.payload.value;
const raw = Buffer.alloc(4);
raw.writeFloatBE(value, 0);
const hi = raw.readUInt16BE(0);
const lo = raw.readUInt16BE(2);
const words = WORD_ORDER === 'little' ? [lo, hi] : [hi, lo];

// `modbus-write` reads `msg.payload.value` and falls back to `msg.payload` itself,
// and takes the unit, function code, address and quantity from **its own fields**.
// This node used to build a whole message -- `{unitid, fc, address, data}` -- which
// is the shape `modbus-flex-write` wants, and the write node then read
// `payload.value` as `undefined` and sent nothing. The message is just the words.
//
// **Stash the request before overwriting the payload.** This node replaces
// `msg.payload` with the two 16-bit words, which are all the write node wants and
// nothing else can use -- so the request the operator made (tag, value, unit, the
// range it was checked against) has to be kept somewhere, and this is the only
// node on the path that still has it.
//
// `record what was written` reads `msg.req`, and no node has ever set it. So it
// got `undefined`, read `req.write_range.join('-')`, and threw
//
//     TypeError: Cannot read properties of undefined (reading 'join')
//
// on every single write -- *after* the write had already succeeded. The setpoint
// was in the PLC and the audit trail, whose entire purpose is to record that, was
// crashing instead of writing. An unlogged control action is not a safe failure;
// it is a control action nobody can account for.
//
// `keepMsgProperties: True` on the write node is what carries `msg.req` the rest
// of the way; see `modbus_write()`.
msg.req = msg.payload;
msg.payload = words;
msg.topic = 'setpoint';
return msg;
""".replace("WORD_ORDER", f"'{word_order}'"),
        ),
        debug("control: refused", tab_id),
        function(
            "record what was written", tab_id,
            """
// A control action with no record is indistinguishable from a control action
// that did not happen, and in an incident those are very different stories.
// The event row is the audit trail; the historian is not.
//
// **Bound, via `msg.params`.** The operator's number goes in as a *parameter*,
// never spliced into the statement -- the alternative is a SQL injection one
// mistyped number away. The installed contrib node reads binds off the message;
// the `params` list this node used to carry was a 0.x field it no longer reads, so
// the statement was running unbound.
//
// The order is the `$1..$4` order of the statement in `postgres()` below, and the
// `::jsonb` cast on the range is why the array is stringified here: `pg` would
// otherwise send a JS array as a Postgres array literal, not as JSON.
const req = msg.req || {};
msg.params = [
    `setpoint ${req.tag} set to ${req.value} ${req.unit} `
       + `(range ${req.write_range.join('-')} ${req.unit})`,
    req.tag,
    req.value,
    JSON.stringify(req.write_range),
];
// Keep the bound fields for anything downstream that wants to read them, and so
// the sidebar can show what was actually sent rather than what was asked for.
msg.payload = msg.params[0];
msg.req = req;
return msg;
""",
        ),
        modbus_write(
            "write the setpoint", tab_id, modbus_config_id(),
            address=address, quantity=2, data_type="HoldingRegister",
            unit_id=unit_id,
        ),
        postgres(
            "audit trail", tab_id,
            """
INSERT INTO event (ts, kind, severity, message, signal_id, detail)
VALUES (now(), 'setpoint_written', 'info', $1, $2,
        jsonb_build_object('value', $3::float8,
                           'write_range', $4::jsonb))
RETURNING id
""",
        ),
        debug("control: written", tab_id),
    ]
    # Two outputs from the check, and they do opposite things: one writes to the
    # plant, one writes to the sidebar and goes no further. A refusal that
    # continued down the same path as a success would be a refusal that reaches
    # the PLC.
    wire(nodes,
         ("enter a setpoint", 0, "check against the permit range"),
         ("check against the permit range", 0, "control: refused"),
         ("check against the permit range", 1, "build the Modbus write"),
         ("build the Modbus write", 0, "write the setpoint"),
         ("write the setpoint", 0, "record what was written"),
         ("record what was written", 0, "audit trail"),
         # Output 0, not 1: the node has one output. See `postgres()`.
         ("audit trail", 0, "control: written"))
    return nodes


def _unit_list(signal_id: str, units: dict[str, str]) -> str:
    """``do_mg_l=mg/L`` for the mimic's comment node.

    Named because the f-string it replaces was a nested comprehension inside an
    f-string inside a list, which is three levels of quoting to read a comment.
    """
    return f"{signal_id.rsplit(':', 1)[-1]}={units[signal_id]}"


def _modbus_register(c: Contract, signal_id: str) -> tuple[int, str, int]:
    """``(address, word_order, unit_id)`` for a signal's Modbus register.

    Raises rather than defaulting any of the three, because a control flow with a
    wrong register address writes to the wrong piece of plant and a default of zero
    would be worse than a refusal. Word order gets the same treatment for a
    sharper reason: it does not fail, it silently writes the wrong number.
    """
    for reg in c.registers:
        # The attribute is `signal`, not `signal_id` -- the register *is* the
        # mapping, so the name is the thing it is.
        if getattr(reg, "signal", None) == signal_id:
            if getattr(reg, "type", None) != "float32":
                raise KeyError(
                    f"register for {signal_id!r} is "
                    f"{getattr(reg, 'type', None)!r}, and the encoder in 'build "
                    f"the Modbus write' only knows how to split a float32 across "
                    f"two registers. Add that encoding rather than guessing one."
                )
            return int(reg.address), reg.word_order, int(reg.unit_id)
    raise KeyError(
        f"{signal_id} has no Modbus register in the contract, so a control flow "
        "has nowhere to write. The contract links signals to registers precisely "
        "so this cannot be guessed."
    )


# ─── assembly ─────────────────────────────────────────────────────────────────

BUILDERS: dict[str, Any] = {
    "01-mimic.json": build_mimic,
    "02-annunciator.json": build_annunciator,
    "03-control.json": build_control,
}


def build_all_names() -> list[str]:
    """The flow filenames, in deploy order.

    Exposed so a test can name them without duplicating `BUILDERS`, which is the
    kind of duplication that drifts. The *runtime* orders them by glob, so this is
    also the order the entrypoint should produce — the filenames are numbered for
    exactly that reason.
    """
    return list(BUILDERS)


def build_all(c: Contract) -> dict[str, list[dict[str, Any]]]:
    return {name: builder(c) for name, builder in BUILDERS.items()}


def render(flow: list[dict[str, Any]]) -> str:
    return json.dumps(flow, indent=4) + "\n"


def check(c: Contract | None = None, directory: Path = FLOWS_DIR) -> list[str]:
    """Drift between the contract and the committed flows."""
    c = c or get_contract()
    problems: list[str] = []
    for name, builder in BUILDERS.items():
        path = directory / name
        if not path.exists():
            problems.append(f"{path} does not exist")
            continue
        expected = render(builder(c))
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            problems.append(
                f"{path} is out of step with the contract "
                f"(run: python -m scada.build_flows)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scada.build_flows",
        description="Generate the Node-RED flows from contracts/tags.yaml.",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dir", type=Path, default=FLOWS_DIR)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    c = get_contract()

    if args.check:
        problems = check(c, args.dir)
        for line in problems:
            log.error("%s", line)
        if problems:
            return 1
        log.info("all %d flows are in step with the contract", len(BUILDERS))
        return 0

    args.dir.mkdir(parents=True, exist_ok=True)
    for name, flow in build_all(c).items():
        (args.dir / name).write_text(render(flow), encoding="utf-8")
        log.info("wrote %s/%s: %d nodes", args.dir, name, len(flow))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
