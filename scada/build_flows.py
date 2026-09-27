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

log = logging.getLogger("scada.build_flows")

FLOWS_DIR = Path("scada/flows")

#: Stable, short node ids derived from a name, so regenerating produces a
#: byte-identical file and the diff is only ever a real change.
#:
#: Node-RED does not care what the ids are, only that they are unique within a
#: flow. Deriving them from names means a regenerated flow keeps its wires, and
#: — more usefully — a diff shows *which node changed* rather than a page of
#: reassigned hex.
def nid(name: str) -> str:
    """A stable 13-character hex id, derived from the node's name.

    **Not `hash()`.** Python salts string hashing per process, so `hash(name)`
    returns a different value on every run, which made every regeneration of every
    flow a complete diff of reassigned ids. That defeats the entire reason for
    generating the flows: a diff that is always everything tells you nothing, and
    a reader learns that a changed file is not a changed flow.

    Found by `tests/test_scada_contract.py::test_the_flows_are_in_step_with_the_
    contract`, which failed on the very run that generated the file it was
    checking.
    """
    return "a" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


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

#: The credential ids the flows refer to. These must match the keys
#: `scada/nodered/entrypoint.sh` writes into `flows_cred.json`, and
#: `tests/test_scada_contract.py` asserts they do — because a mismatch is the
#: sharpest Node-RED failure there is:
#:
#:     [error] [postgresql:read newest values] TypeError:
#:             node.config.pgPool.connect is not a function
#:
#: which is an internal, names no database, and appears for a *misconfigured* node
#: and a *missing* node identically.
PG_CREDENTIAL = "wwtp-db"
MODBUS_CREDENTIAL = "wwtp-softplc-modbus"

#: The Modbus config entry the control flow writes through, by *name*. The user
#: creates it in the editor pointing at the soft PLC, so the flow file contains no
#: address and nothing in git changes when a container moves.
MODBUS_SERVER = "wwtp-softplc-modbus"


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

    The `::text` cast on `$rule` is not decoration. A bind parameter inside
    `jsonb_build_object` has **no inferable type**, and Postgres says so:

        could not determine data type of parameter $3

    Which is only visible by running it, and which was found by running it. The
    parameters in the `VALUES` list infer fine from the column types; the ones
    inside a function call have nothing to infer from.
    """
    return """
INSERT INTO event (ts, kind, severity, message, signal_id, equipment_id, detail)
VALUES (now(), 'alarm_acknowledged', 'critical', $msg, $sig, $eq,
        jsonb_build_object('rule', $rule::text, 'acknowledged_at', now()))
RETURNING id
""".strip()


# ─── node constructors ────────────────────────────────────────────────────────


def tab(name: str, label: str, disabled: bool = False) -> dict[str, Any]:
    return {
        "id": nid(f"tab:{name}"),
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
) -> dict[str, Any]:
    """A timer.

    `once` is what the control and acknowledgement flows use to start: they are
    event-driven and have nothing to poll for.
    """
    node: dict[str, Any] = {
        "id": nid(name),
        "type": "inject",
        "z": tab_id,
        "name": name,
        "props": [{"p": "payload"}, {"p": "topic", "vt": "str"}],
        "repeat": "" if once else str(every),
        "crontab": "",
        "once": once,
        "onceDelay": "0.5",
        "topic": "",
        "payload": "",
        "payloadType": "date",
    }
    if once:
        node["repeat"] = ""
        node["crontab"] = ""
    return node


def function(name: str, tab_id: str, body: str, *, outputs: int = 1) -> dict[str, Any]:
    return {
        "id": nid(name),
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


def debug(
    name: str, tab_id: str, *, active: bool = False, to: str = "console",
) -> dict[str, Any]:
    return {
        "id": nid(name),
        "type": "debug",
        "z": tab_id,
        "name": name,
        "active": active,
        "tosidebar": to,
        "console": False,
        "tostatus": False,
        "complete": "payload",
        "targetType": "msg",
        "statusVal": "",
        "statusType": "auto",
        "x": 400,
        "y": 100,
        # A `debug` node is a terminal: it has one output and it is never wired.
        # Present as an empty list rather than absent so the output-count check
        # has something to compare.
        "wires": [[]],
    }


def comment(name: str, tab_id: str, info: str) -> dict[str, Any]:
    return {
        "id": nid(name),
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
    something. `filenameType: msg` with an injected payload is the smallest way
    to say "read this when the flow starts".
    """
    return {
        "id": nid(name),
        "type": "file in",
        "z": tab_id,
        "name": name,
        "filename": path,
        "filenameType": "msg",
        "format": "utf8",
        "chunk": False,
        "sendError": False,
        "encoding": "none",
        "allProps": False,
        "x": 200,
        "y": 100,
        # Two outputs: 0 is "file not found", 1 is the contents. The default in
        # Node-RED's own template is `[["1"]]`, which is a *filename*, not a wire
        # -- and it survives into the flow file and makes the first assertion in
        # `test_every_wire_points_at_a_node_that_exists` fail on the string "1".
        "wires": [[], []],
    }


def _outputs(node: dict[str, Any]) -> int:
    """How many output ports a node has, by type.

    Guessed rather than configured, and the guess is checked by
    `tests/test_scada_contract.py` — a `wires` list whose length does not match
    the node's output count is how a Node-RED flow imports cleanly and then
    silently drops every message on an output nobody wired.
    """
    if node["type"] in ("tab", "comment"):
        # Neither has ports. A `tab` has no `wires` key at all in Node-RED's own
        # template, and a `comment` has an empty one, so returning 1 for both
        # made the output-count check fail on two nodes that are correct.
        return 0
    if node["type"] == "function":
        return int(node.get("outputs", 1))
    if node["type"] in ("postgresql", "file in"):
        return 2
    return 1


def modbus_write(name: str, tab_id: str, server: str) -> dict[str, Any]:
    """`node-red-contrib-modbus`'s write node.

    The server name is not the host: it is a config entry the user creates in the
    Node-RED editor, pointing at the soft PLC. Kept as a name rather than a host
    so the flow file contains no address, which means it is portable and there is
    nothing in git to change when a container moves.

    `datatype: float` because the register is `float32` in the contract — see the
    word-order note below, which is the single most dangerous thing about writing
    to this plant.
    """
    return {
        "id": nid(name),
        "type": "modbus-write",
        "z": tab_id,
        "name": name,
        "server": server,
        "datatype": "float",
        "scale": 1,
        "offset": 0,
        "polling": "",
        "x": 400,
        "y": 100,
        "wires": [[]],
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
    """
    by_name = {n["name"]: n for n in nodes if n["type"] != "tab"}
    for source_name, output, target_name in pairs:
        source = by_name[source_name]
        for label in (source_name, target_name):
            if label not in by_name:
                raise KeyError(
                    f"wire references {label!r}, which is not a node in this flow. "
                    f"Known: {sorted(by_name)}"
                )
        source["wires"] = source.get("wires") or [
            [] for _ in range(_outputs(source))
        ]
        while len(source["wires"]) <= output:
            source["wires"].append([])
        source["wires"][output].append(nid(target_name))


def postgres(
    name: str, tab_id: str, query: str,
    *, params: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A PostgreSQL query node.

    The `params` list is what makes the acknowledgement flow safe: a value from
    `msg` goes in as a bind parameter, never as text spliced into the statement.
    The mock flow's tag list is interpolated because it is a fixed set of ids
    known at generation time; the acknowledgement's message and rule are not, and
    are bound.
    """
    return {
        "id": nid(name),
        "type": "postgresql",
        "z": tab_id,
        "name": name,
        "query": query,
        # The *credential id*, not a connection string and not a name.
        #
        # A credential id in a committed flow file is not a secret: it is a
        # pointer, and the thing it points at lives in `flows_cred.json`, which is
        # gitignored and generated from the environment by
        # `scada/nodered/entrypoint.sh`. So the flow can name a database and
        # cannot contain a password, which is the whole shape of the problem.
        "mydb": PG_CREDENTIAL,
        "maxsize": "0",
        "params": params or [],
        "x": 360,
        "y": 100,
        # Two outputs: 0 is an error row, 1 is the result. Left unwired here and
        # wired in the builder, because the mimic wants the error and the
        # annunciator wants to ignore it.
        "wires": [[], []],
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
TAGS_PATH_IN_FLOW = "/data/tags.json"

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
    """The three nodes that get a tag list into `global`.

    A `trigger` fires once on deploy, which is the only way to do work at startup
    in Node-RED: nothing runs until a message arrives, and an `inject` with
    `once` is the way to make one arrive.
    """
    trigger = {
        "id": nid("load tags: on deploy"),
        "type": "trigger",
        "z": tab_id,
        "name": "load tags: on deploy",
        "op1": "1",
        "op2": "0",
        "op1type": "str",
        "op2type": "str",
        "duration": "0.1",
        "extend": False,
        "overrideDelay": False,
        "units": "ms",
        "reset": "",
        "bytopic": "all",
        "topic": "topic",
        "outputs": 1,
        "x": 200,
        "y": 40,
        "wires": [[]],
    }
    reader = file_in("read tags.json", tab_id, TAGS_PATH_IN_FLOW)
    loader = function("put tags in global", tab_id, _LOAD_TAGS_BODY)
    report = debug("tag list loaded", tab_id, active=True)

    nodes = [trigger, reader, loader, report]
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
    tab_id = nid("tab:mimic")
    units = {t: c.signals[t].unit for t in MIMIC_TAGS}

    nodes = [*_loader_nodes(tab_id),
        tab("mimic", "01 — Plant mimic (read-only)"),
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
        debug("mimic status", tab_id, active=False),
    ]
    wire(nodes,
         ("every 5 s", 0, "read newest values"),
         ("read newest values", 1, "shape the status object"),
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
    tab_id = nid("tab:annunciator")
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
// `detail->>'rule'` is the join key because the engine puts the rule there and
// there is no column for it. See alarms/replay.py for why that is a cost being
// paid knowingly.
const rule = msg.payload.rule;
const alarm = msg.payload.orig || msg.payload;

msg.payload = {
    msg: `acknowledged: ${alarm.message || rule}`,
    rule: rule,
    signal: alarm.signal_id || null,
};
msg.topic = 'acknowledge';
return msg;
""",
        ),
        postgres(
            "record the acknowledgement", tab_id,
            """
INSERT INTO event (ts, kind, severity, message, signal_id, detail)
VALUES (now(), 'alarm_acknowledged', 'critical', $msg, $signal,
        jsonb_build_object('rule', $rule::text, 'acknowledged_at', now()))
RETURNING id
""",
            params=[
                {"type": "msg", "name": "msg"},
                {"type": "msg", "name": "signal"},
                {"type": "msg", "name": "rule"},
            ],
        ),
        debug("annunciator panel", tab_id, active=True),
        debug("acknowledged", tab_id, active=True),
    ]
    wire(nodes,
         ("refresh the panel", 0, "read unacknowledged criticals"),
         ("read unacknowledged criticals", 1, "mark staleness"),
         ("mark staleness", 0, "annunciator panel"),
         # The operator path. Separate from the refresh path, deliberately: a
         # refresh is a timer and must never be able to acknowledge anything.
         ("annunciator panel", 0, "acknowledge what the operator pressed"),
         ("acknowledge what the operator pressed", 0,
          "record the acknowledgement"),
         ("record the acknowledgement", 1, "acknowledged"))
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
    tab_id = nid("tab:control")

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
        error: `{{requested}} {{spec.unit}} is outside the writable range `
             + `{{lo}}-{{hi}} {{spec.unit}}`,
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
return [msg, null];
"""

    nodes = [*_loader_nodes(tab_id),
        tab("control", "03 — Operator control (setpoint)"),
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
        inject("enter a setpoint", tab_id, once=True),
        function("check against the permit range", tab_id, check, outputs=2),
        function(
            "build the Modbus write", tab_id,
            """
// `node-red-contrib-modbus`: one write register, function code 6 or 16.
// The address is the contract's, resolved at generation time -- this flow never
// contains a hand-typed register number, which is the whole reason the contract
// links signals to Modbus registers.
const req = msg.payload;
msg.payload = {
    unitid: global.get('wwtpModbusUnitId') || 1,
    fc: 6,
    address: REGISTER_ADDRESS,
    data: [req.value],
};
msg.topic = 'setpoint';
return msg;
""".replace("REGISTER_ADDRESS", str(_modbus_address(c, CONTROL_TAG))),
        ),
        debug("control: refused", tab_id, active=True),
        function(
            "record what was written", tab_id,
            """
// A control action with no record is indistinguishable from a control action
// that did not happen, and in an incident those are very different stories.
// The event row is the audit trail; the historian is not.
// Bind parameters, named for the PostgreSQL node's `params` list above. The
// operator's number goes in as a *parameter*, never spliced into the statement --
// the alternative is a SQL injection one mistyped number away.
const req = msg.req || {};
msg.payload = {
    msg: `setpoint ${req.tag} set to ${req.value} ${req.unit} `
       + `(range ${req.write_range.join('-')} ${req.unit})`,
    tag: req.tag,
    value: req.value,
    range: req.write_range,
};
// Keep the human-readable form for the sidebar, and carry the bound fields
// alongside so both consumers see the same fact.
msg.req = req;
return msg;
""",
        ),
        modbus_write("write the setpoint", tab_id, MODBUS_SERVER),
        postgres(
            "audit trail", tab_id,
            """
INSERT INTO event (ts, kind, severity, message, signal_id, detail)
VALUES (now(), 'setpoint_written', 'info', $msg, $tag,
        jsonb_build_object('value', $value::float8,
                           'write_range', $range::jsonb))
RETURNING id
""",
            params=[
                {"type": "msg", "name": "msg"},
                {"type": "msg", "name": "tag"},
                {"type": "msg", "name": "value"},
                {"type": "msg", "name": "range"},
            ],
        ),
        debug("control: written", tab_id, active=True),
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
         ("audit trail", 1, "control: written"))
    return nodes


def _unit_list(signal_id: str, units: dict[str, str]) -> str:
    """``do_mg_l=mg/L`` for the mimic's comment node.

    Named because the f-string it replaces was a nested comprehension inside an
    f-string inside a list, which is three levels of quoting to read a comment.
    """
    return f"{signal_id.rsplit(':', 1)[-1]}={units[signal_id]}"


def _modbus_address(c: Contract, signal_id: str) -> int:
    """The contract's register address for a signal.

    Raises rather than defaulting, because a control flow with a wrong register
    address writes to the wrong piece of plant, and a default of zero would be
    worse than a refusal.
    """
    for reg in c.registers:
        # The attribute is `signal`, not `signal_id` -- the register *is* the
        # mapping, so the name is the thing it is.
        if getattr(reg, "signal", None) == signal_id:
            return int(reg.address)
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
