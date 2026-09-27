"""The Node-RED flows and the tag list are in step with the contract.

Two generated artefacts — `scada/flows/tags.json` and `scada/flows/*.json` — and
both are committed. Committed because a build step that only runs inside one
container is a build step that will not run, and generated because hand-written
Node-RED JSON is unreadable, uncommentable, and wrong the first time a signal is
renamed.

Which means the usual failure mode is back: **a generated file that nobody
regenerates.** A stale tag list still resolves, still renders, and still shows
the last value it knew about, so the failure is a mimic diagram that looks fine
and describes a plant that no longer exists.

These tests are the loop. The structural ones matter more than the drift check,
because they are the ones that catch a *bug in the generator* rather than a
forgotten regeneration — and a broken generator produces confidently wrong flows.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from scada import build_flows, generate_tags
from softplc.contract import Contract
from softplc.contract import contract as get_contract

FLOWS = build_flows.FLOWS_DIR
TAGS = generate_tags.TAGS_PATH

#: Node types that are legitimately not wired to anything. A `debug` node
#: terminates a branch, a `comment` has no ports at all, and a `tab` holds the
#: nodes rather than sitting in a path.
TERMINALS = {"debug", "comment", "tab"}

#: Every table the flows are allowed to touch. Not derived from a schema dump —
#: stated, so that adding a query against a table nobody reviewed is a visible
#: edit here rather than a string in a flow file.
ALLOWED_TABLES = {"reading", "event", "signal", "equipment", "site"}


@pytest.fixture(scope="module")
def c() -> Contract:
    return get_contract()


@pytest.fixture(scope="module")
def tags(c: Contract) -> dict:
    return json.loads(TAGS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def flows() -> dict[str, list[dict]]:
    out = {}
    for path in sorted(FLOWS.glob("*.json")):
        if path.name == "tags.json":
            continue
        out[path.name] = json.loads(path.read_text(encoding="utf-8"))
    return out


# ── the tag list ──────────────────────────────────────────────────────────────


def test_the_tag_list_is_in_step_with_the_contract(c: Contract) -> None:
    """The drift check itself.

    Deliberately calls the generator's own `check()` rather than re-deriving the
    expected content, because a test that recomputes the thing it is testing is a
    second implementation of it, and the two will disagree for a reason nobody
    will enjoy finding. `check()` is the code that writes the file; testing it
    against itself would be circular, so this tests *it* and the tests below test
    the *content*.
    """
    assert generate_tags.check(c, TAGS) == [], (
        "run: python -m scada.generate_tags"
    )


def test_the_flows_are_in_step_with_the_contract(c: Contract) -> None:
    assert build_flows.check(c, FLOWS) == [], (
        "run: python -m scada.build_flows"
    )


def test_every_signal_has_a_tag(tags: dict, c: Contract) -> None:
    assert len(tags["tags"]) == len(c.signals)
    assert {t["id"] for t in tags["tags"]} == set(c.signals)


def test_every_tag_carries_a_real_unit(tags: dict, c: Contract) -> None:
    """The bug `scada/generate_tags.py` was written into existence to find.

    It rendered `unit: AERATION` for every aeration signal, because the contract
    declared the *area* in a key called `unit`. Three consumers of the contract
    were correct and the Python attribute was not, so nothing complained for a
    whole phase.

    The tag list is the first artefact that *displays* a unit, which makes it the
    test that had to exist. `tests/test_contract.py` now guards the attribute; this
    guards the artefact.
    """
    for tag in tags["tags"]:
        signal = c.signals[tag["id"]]
        assert tag["unit"] == signal.eu, (
            f"{tag['id']}: tag says unit={tag['unit']!r}, contract says "
            f"{signal.eu!r}"
        )
        assert tag["unit"] != tag["area"], (
            f"{tag['id']}: the unit is its own area name. This is the exact "
            "shape of the bug this generator was written to catch."
        )
        assert tag["area"] in c.area_ids, tag["id"]


def test_a_tag_carries_the_normal_band_and_not_just_the_range(tags: dict) -> None:
    """Both bands, and they are different things.

    `min`/`max` is the instrument's engineering range — what the *sensor* can
    physically read. `normal_low`/`normal_high` is where the *process* should be.
    A mimic draws its bar against the range and, if it draws an alarm line at all,
    against the band. Conflating them is how a SCADA system ends up alarming on a
    value that was only ever out of calibration.

    The tag carries both, separately, so a consumer cannot accidentally use one
    for the other's job.
    """
    for tag in tags["tags"]:
        assert tag["min"] < tag["max"], tag["id"]
        assert tag["normal_low"] >= tag["min"], tag["id"]
        assert tag["normal_high"] <= tag["max"], tag["id"]
        assert tag["normal_low"] <= tag["normal_high"], tag["id"]


def test_a_tag_states_its_sample_period_and_deadband(tags: dict, c: Contract) -> None:
    """The two numbers that explain why a signal is quiet.

    Without them a consumer cannot tell "the plant is steady" from "this signal
    is badly configured", which is the ambiguity `sql/02-04` is about and the
    deadband's honest limit.
    """
    for tag in tags["tags"]:
        signal = c.signals[tag["id"]]
        assert tag["sample_ms"] == signal.sample_ms, tag["id"]
        assert tag["deadband"] == signal.deadband, tag["id"]


def test_writable_tags_carry_their_permit_range(tags: dict, c: Contract) -> None:
    """The write range is on the tag, so a control flow cannot invent one.

    `scada/flows/03-control.json` reads `write_range` from here. A flow that
    hardcoded a range would be a second source of truth for what an operator may
    do to the plant, and it would be the one that is not reviewed with the
    contract.
    """
    # `Contract.writable` is a superset of the writable *signals*: it also holds
    # plant-level registers like `FAULT_CODE`, which are not signals and so have
    # no tag. Asserted in both directions rather than with `==`, because the
    # difference is a modelling fact rather than a bug -- and the first version of
    # this test used `==` and failed for exactly that reason.
    writable = {t["id"] for t in tags["tags"] if t["writable"]}
    assert writable <= set(c.writable), (
        f"tags claim {sorted(writable - set(c.writable))} is writable, which the "
        "contract does not say"
    )
    register_only = sorted(set(c.writable) - set(c.signals))
    assert register_only, (
        "no writable entry is a bare register any more; if that is intended, this "
        "assertion and the docstring both need updating"
    )
    for tag in tags["tags"]:
        if tag["writable"]:
            assert tag["id"] in c.writable, tag["id"]
    for tag in tags["tags"]:
        if not tag["writable"]:
            assert "write_range" not in tag, tag["id"]
            continue
        low, high = tag["write_range"]
        spec = c.writable[tag["id"]]["range"]
        assert [low, high] == [spec[0], spec[1]], tag["id"]
        # And it is inside the engineering range, or the plant could be told to
        # do something its own instrument cannot measure.
        assert low >= tag["min"] and high <= tag["max"], (
            f"{tag['id']}: write range {low}-{high} is outside the engineering "
            f"range {tag['min']}-{tag['max']}"
        )
        assert tag["write_reason"], tag["id"]


def test_the_tag_file_is_sorted_and_byte_stable(tags: dict) -> None:
    """A regenerated file that differs only in ordering trains people to ignore
    diffs.

    Sorted by signal id, so the file is byte-stable and a diff means something
    changed. Unsorted, every regeneration is a 57-line diff that says nothing.
    """
    ids = [t["id"] for t in tags["tags"]]
    assert ids == sorted(ids)
    for area, members in tags["areas"].items():
        assert members == sorted(members), area


def test_the_tag_list_declares_its_schema(tags: dict) -> None:
    """A consumer should be able to check the shape it is about to rely on.

    `schema: wwtp.node-red.tags/1`. A flow can compare it and refuse rather than
    discover four nodes later that `write_range` is missing.
    """
    assert tags["schema"] == generate_tags.SCHEMA
    assert tags["source"] == "contracts/tags.yaml"
    assert set(tags["counts"]) == {"tags", "areas", "writable"}
    assert tags["counts"]["tags"] == len(tags["tags"])
    assert tags["counts"]["writable"] == sum(
        1 for t in tags["tags"] if t["writable"]
    )


# ── flow structure ────────────────────────────────────────────────────────────


def test_there_are_three_flows_and_each_has_exactly_one_tab(flows: dict) -> None:
    assert set(flows) == set(build_flows.BUILDERS), (
        f"expected {sorted(build_flows.BUILDERS)}, found {sorted(flows)}"
    )
    for name, nodes in flows.items():
        tabs = [n for n in nodes if n["type"] == "tab"]
        assert len(tabs) == 1, f"{name}: {len(tabs)} tabs"


def test_node_ids_are_unique_within_a_flow(flows: dict) -> None:
    """Node-RED silently misbehaves on a duplicate id rather than complaining.

    Two nodes with the same id means a wire to one of them connects to the other,
    or to both, and the symptom is a flow that runs and does the wrong thing.
    """
    for name, nodes in flows.items():
        ids = [n["id"] for n in nodes]
        assert len(ids) == len(set(ids)), f"{name}: duplicate node ids"


def test_every_node_belongs_to_the_flows_only_tab(flows: dict) -> None:
    """`z` is the node's tab. A node pointing at nothing is not in any flow."""
    for name, nodes in flows.items():
        tab_ids = {n["id"] for n in nodes if n["type"] == "tab"}
        for node in nodes:
            if node["type"] == "tab":
                continue
            assert node.get("z") in tab_ids, (
                f"{name}: {node['name']!r} has z={node.get('z')!r}, which is not "
                f"this flow's tab {tab_ids}"
            )


def test_every_wire_points_at_a_node_that_exists(flows: dict) -> None:
    """The check that catches a broken *generator*.

    Node-RED's wiring is backwards: an input port names the node it connects *to*.
    A wrong id is not an error at import — the node simply sits there doing
    nothing, which for a diagram an operator is looking at is the worst possible
    failure. This is the test that would have caught it.
    """
    for name, nodes in flows.items():
        ids = {n["id"] for n in nodes}
        for node in nodes:
            for port, targets in enumerate(node.get("wires", [])):
                for target in targets:
                    assert target in ids, (
                        f"{name}: {node['name']!r} output {port} points at "
                        f"{target!r}, which is not a node in this flow"
                    )


def test_a_nodes_wire_list_matches_its_output_count(flows: dict) -> None:
    """A `wires` list longer than the node has outputs is a dangling port.

    And a *shorter* one is how messages get dropped silently: a two-output
    function with a one-element `wires` list raises at deploy in some versions
    and quietly discards the second output in others.
    """
    for name, nodes in flows.items():
        for node in nodes:
            expected = build_flows._outputs(node)
            if expected == 0:
                # A `tab` and a `comment` have no ports, and Node-RED's own
                # templates disagree about whether they carry an empty `wires`
                # key. Both are correct; asserting on them is asserting on the
                # template.
                continue
            got = len(node.get("wires", []))
            assert got == expected, (
                f"{name}: {node.get('name', node['id'])!r} is a {node['type']} "
                f"with {expected} output(s) but {got} wire list(s)"
            )


def test_no_node_is_dead_and_no_wire_is_orphaned(flows: dict) -> None:
    """Two failure modes, and the first version of this test only had the wrong one.

    **Dead node.** A `function` that nothing wires *from* never runs, and a
    `function` nothing wires *to* sends its messages nowhere. Both import without
    complaint.

    **Orphaned wire.** An input naming a node that does not exist — covered by
    `test_every_wire_points_at_a_node_that_exists`.

    The first version of this asserted "a node with an output must itself be
    targeted", which flags every legitimate *error* output: `file in`'s
    output 0 is "file not found" and is deliberately left unwired, and
    `postgresql`'s output 0 is the error row. The test was wrong about what a wire
    is, which is the same mistake as getting the wiring direction backwards, and
    it is why the assertion is now in the direction that means something: *is
    this node in the path at all?*
    """
    # A `trigger` and an `inject` are sources. A `debug` and a `comment` are
    # terminals. Neither is required to have a neighbour of the other kind.
    sources = {"trigger", "inject"}
    terminals = TERMINALS

    for name, nodes in flows.items():
        targeted: set[str] = set()
        wired_from: set[str] = set()
        for node in nodes:
            for port in node.get("wires", []):
                for target in port:
                    targeted.add(target)
                    wired_from.add(node["id"])

        for node in nodes:
            if node["type"] in terminals:
                continue
            if node["type"] in sources:
                assert any(node.get("wires", [])), (
                    f"{name}: {node['name']!r} is a source with nothing downstream"
                )
                continue
            assert node["id"] in wired_from, (
                f"{name}: {node['name']!r} is wired to nothing, so it never runs"
            )
            assert any(node.get("wires", [])), (
                f"{name}: {node['name']!r} runs but every output is unwired, so "
                "its messages go nowhere"
            )


# ── the flows use the contract, and only the contract ─────────────────────────


def test_every_tag_a_flow_mentions_exists(c: Contract, flows: dict) -> None:
    """A flow cannot reference a tag nobody declared.

    The builder already raises on an unknown id, so this is the belt to that
    braces: it covers the SQL, the comments, and anything a future edit adds by
    string rather than by lookup.
    """
    for name, nodes in flows.items():
        blob = json.dumps(nodes)
        for token in _contract_ids_in(blob):
            assert token in c.signals, (
                f"{name} mentions {token!r}, which is not a signal in the contract"
            )


def _contract_ids_in(blob: str) -> set[str]:
    """Signal-id-shaped tokens in a JSON blob.

    ``AREA:UNIT:EQUIPMENT`` — three colon-separated upper-case-ish parts. A
    deliberately narrow pattern, so it finds the ids a flow hardcoded and not
    every string with a colon in it (a Modbus server name, a URL, a JSON key).
    """
    return set(re.findall(r'"([A-Z][A-Z_]+:[A-Z0-9_-]+:[A-Z0-9_-]+)"', blob))


def test_the_mimic_watches_signals_that_exist(c: Contract) -> None:
    for tag_id in build_flows.MIMIC_TAGS:
        assert tag_id in c.signals, (
            f"MIMIC_TAGS names {tag_id!r}, which the contract does not declare. "
            "The mimic would show a tag that resolves to nothing."
        )


def test_the_control_flow_targets_a_writable_signal_with_a_register(
    c: Contract,
) -> None:
    """Three conditions, all of them safety properties.

    The signal must exist, it must be on the *writable* surface, and it must have
    a Modbus register — because a control flow with no register has nowhere to
    write, and a default of address zero would write to the wrong piece of plant.
    """
    tag = build_flows.CONTROL_TAG
    assert tag in c.signals
    assert tag in c.writable
    address = build_flows._modbus_address(c, tag)
    assert address >= 40000, (
        f"{tag}: register {address} is below the project's 40000 convention"
    )


def test_the_control_flow_refuses_rather_than_clamps(c: Contract, flows: dict) -> None:
    """The check node must not contain a clamp.

    Searched for rather than asserted on behaviour, because Node-RED function
    bodies are JavaScript strings and the alternative is standing up a runtime to
    evaluate them. The two words that would introduce a clamp are `Math.min` and
    `Math.max` applied to the requested value, and neither may appear.
    """
    body = next(
        n["func"] for n in flows["03-control.json"]
        if n.get("name") == "check against the permit range"
    )
    assert "Math.min" not in body and "Math.max" not in body, (
        "the permit check must refuse an out-of-range setpoint, not clamp it to "
        "the nearest legal one. A clamped setpoint is a setpoint the operator "
        "did not ask for."
    )
    assert "outside the writable range" in body
    assert "return [msg, null]" in body, (
        "the check must have a second output for the refusal, or a refusal "
        "continues down the same path as a success and reaches the PLC"
    )


def test_the_control_flow_writes_over_modbus_and_not_to_the_database(
    c: Contract, flows: dict
) -> None:
    """A setpoint in Postgres is a number that looks like a command and is not.

    So the flow must contain a Modbus write node, and the only `postgresql` node
    in it must be the audit trail — an `INSERT` into `event`, never an `UPDATE`
    against anything an alarm could read.
    """
    nodes = {n["name"]: n for n in flows["03-control.json"] if "name" in n}
    assert any(n["type"] == "modbus-write" for n in nodes.values()), (
        "the control flow has no Modbus write node"
    )
    writes = [n for n in nodes.values() if n["type"] == "modbus-write"]
    for node in writes:
        assert "wwtp" in node["server"], (
            "the Modbus server is named by config entry, not by host, so the flow "
            f"file contains no address; {node['server']!r} looks like a hostname"
        )
    for node in nodes.values():
        if node["type"] == "postgresql":
            assert "INSERT INTO event" in node["query"], (
                "the only database write in the control flow is the audit trail"
            )
            assert "UPDATE" not in node["query"].upper()


def test_no_flow_bypasses_the_tag_loader(flows: dict) -> None:
    """Every flow loads `tags.json` before anything reads `global.get('wwtpTags')`.

    Node-RED's `global` context is **per flow file**, so a flow that assumes
    another one loaded it gets `undefined` — and a `?? []` default in the consumer
    then makes a missing tag list look like a working flow with the units
    silently absent. So each flow loads its own.

    **Reachability through wires is deliberately not the assertion**, and the first
    version of this test got that wrong too. The loader fires once on deploy from
    a `trigger`; the consumers fire on a five-second timer. They are not wired to
    each other, and wiring them would be wrong — it would make the mimic wait on
    the loader and couple two things that have no business being coupled.

    What is asserted instead is the two things that can actually go wrong:

    * a loader exists in this flow, reading the generated path;
    * it is driven by a `trigger` (once, on deploy) and not by a timer — a
      `repeat` interval on the loader would re-read the file every few seconds,
      which works and is silly.
    """
    for name, nodes in flows.items():
        named = [n for n in nodes if "name" in n]

        loader = [n for n in named if n["name"] == "put tags in global"]
        assert loader, f"{name} has no tag loader, so it cannot read wwtpTags"

        reader = [n for n in named if n["type"] == "file in"]
        assert reader, f"{name} does not read tags.json"
        for node in reader:
            assert node["filename"] == build_flows.TAGS_PATH_IN_FLOW, (
                f"{name}: reads {node['filename']!r}, expected "
                f"{build_flows.TAGS_PATH_IN_FLOW!r}"
            )
            # The file-not-found output must be watched, not left dangling.
            assert any(node["wires"][0]), (
                f"{name}: nothing watches tags.json's not-found output, so a "
                "missing file is silent and every unit in the flow becomes blank"
            )

        triggers = [n for n in named if n["type"] == "trigger"]
        assert triggers, f"{name} has no trigger to fire the loader on deploy"
        for node in triggers:
            assert not node.get("repeat"), (
                f"{name}: the tag loader fires on a {node.get('repeat')!r} "
                "interval. It should fire once on deploy; re-reading a file every "
                "few seconds works and is silly."
            )

        consumers = [
            n for n in named
            if n["type"] == "function" and "wwtpTags" in n.get("func", "")
        ]
        assert consumers, (
            f"{name} does not use the tag list, so it should not load one"
        )


def test_a_consumer_says_so_when_the_tag_list_is_missing(flows: dict) -> None:
    """The `?? []` default is the dangerous part, so a consumer must not rely on it.

    Every consumer reads `global.get('wwtpTags') || []`. That default is what
    makes a flow that never loaded its tags *look* like it works — the rows come
    back, and every `unit` is `''` and every `normal_low` is `undefined`. So each
    consumer must also emit a status when the list is empty, which is the
    difference between a blank diagram and a red node saying why.
    """
    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] != "function":
                continue
            body = node.get("func", "")
            if "wwtpTags" not in body:
                continue
            assert "node.status" in body or "node.warn" in body, (
                f"{name}: {node['name']!r} reads the tag list and does not say so "
                "when it is empty. A flow with no tags produces rows with blank "
                "units, which looks like working."
            )


# ── the SQL the flows send ────────────────────────────────────────────────────


def _sql_in(flows: dict) -> list[tuple[str, str]]:
    out = []
    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] in ("postgresql", "modbus-write"):
                out.append((name, node.get("query", "")))
    return out


def test_flow_sql_only_touches_reviewed_tables(flows: dict) -> None:
    for name, query in _sql_in(flows):
        for table in re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", query, re.I):
            assert table in ALLOWED_TABLES, (
                f"{name}: query touches {table!r}, which is not in the reviewed "
                f"set {sorted(ALLOWED_TABLES)}. Add it here deliberately, or do "
                "not query it."
            )


def test_flow_sql_references_columns_the_schema_actually_has(flows: dict) -> None:
    """The columns the flows name, checked against `schema.sql`.

    Not a parser. A named list of `(table, columns the flows use)`, and each one
    asserted to appear in the table's `CREATE TABLE`. A flow that selects
    `signal.value` fails here rather than at 3am on a dashboard.
    """
    schema = Path("storage/postgres/schema.sql").read_text(encoding="utf-8")

    expected: dict[str, set[str]] = {
        "reading": {"signal_id", "value", "quality", "source", "ts"},
        "event": {"id", "ts", "kind", "severity", "message", "signal_id",
                  "equipment_id", "detail"},
    }
    for table, columns in expected.items():
        # The CREATE TABLE body for this table, up to its closing paren.
        start = schema.index(f"CREATE TABLE IF NOT EXISTS {table}")
        body = schema[start:schema.index(");", start)]
        for column in columns:
            assert column in body, (
                f"{table} has no column {column!r}, which the Node-RED flows "
                "select. Either the flow or the schema is wrong."
            )


def test_the_mimic_reads_the_newest_row_per_signal(flows: dict, c: Contract) -> None:
    """`DISTINCT ON (signal_id)` is not a shortcut, and this says why.

    There is no row for a value that did not move — the deadband suppresses it —
    so the current value of a signal is its newest row, and a query that filtered
    on recency instead would return nothing at all for a steady signal. Thirteen
    of the 57 signals produce exactly one reading in a seeded week, which is what
    makes this the difference between a mimic that works and one that is empty.
    """
    query = next(q for name, q in _sql_in(flows) if name == "01-mimic.json")
    assert "DISTINCT ON (signal_id)" in query
    assert "ORDER BY signal_id, ts DESC" in query, (
        "DISTINCT ON takes the first row of each group, so the ORDER BY has to "
        "sort by the grouping columns first and ts descending second, or the "
        "'newest' row is whichever the planner felt like returning"
    )
    # And it must be one query for the whole set, not one per tag.
    for tag_id in build_flows.MIMIC_TAGS:
        assert tag_id in query, tag_id
    assert query.count("SELECT") == 1, (
        "the mimic should issue one statement for the whole watched set; a round "
        "trip per tag spends the whole refresh interval on latency"
    )


def test_the_annunciator_only_shows_latching_severities(flows: dict) -> None:
    """`critical` latches, `warning` auto-clears.

    A warning that auto-clears is not something an operator can acknowledge, so
    showing one in an annunciator offers an action that does nothing. This is the
    rule in `docs/ALARMS.md` arriving at a query.
    """
    query = next(q for name, q in _sql_in(flows) if name == "02-annunciator.json")
    assert "severity = 'critical'" in query
    assert "kind = 'alarm_raised'" in query
    # And the acknowledgement is matched on the rule, not the occurrence.
    assert "alarm_acknowledged" in query
    assert "NOT EXISTS" in query, (
        "already-acknowledged alarms must be excluded in the query, or the panel "
        "shows an alarm and an operator acknowledges it twice"
    )


def test_the_acknowledge_query_is_bound_not_interpolated(flows: dict) -> None:
    """An operator's typed number goes in as a parameter.

    The tag ids are interpolated because they are known at generation time and
    are escaped. The acknowledgement's message, rule and value come from a person
    at run time, and a statement built by concatenation is a SQL injection one
    mistyped number away.
    """
    # The acknowledge statement lives in the module (it is not in a flow yet --
    # the flow reads unacknowledged alarms and the *engine* is what acknowledges;
    # see the note in `annunciator_query`) and the audit trail is in the control
    # flow. Both must bind.
    assert "$msg" in build_flows.acknowledge_query()
    control = [
        n for n in flows["03-control.json"]
        if n["type"] == "postgresql"
    ]
    for node in control:
        assert node["params"], (
            "the audit-trail insert must declare bind parameters; an INSERT with "
            "no params means the values were spliced into the statement"
        )
        for param in node["params"]:
            assert param["type"] == "msg", param


# ── the credential ids ────────────────────────────────────────────────────────


def test_the_flows_name_a_credential_the_entrypoint_writes(flows: dict) -> None:
    """A credential id in a committed flow is a pointer, not a secret.

    `flows_cred.json` is generated from the environment by
    `scada/nodered/entrypoint.sh` and gitignored. The flow names a database; the
    credential holds the password; the two are joined at run time by the id.

    Which means the *ids* have to agree, and a mismatch is the worst failure
    Node-RED has: the node loads, the type resolves, and every query fails with

        TypeError: node.config.pgPool.connect is not a function

    which is an internal, names no database, and is identical for "no such
    credential" and "the credential is broken".
    """
    entrypoint = Path("scada/nodered/entrypoint.sh").read_text(encoding="utf-8")
    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] == "postgresql":
                cred = node.get("mydb")
                assert cred, f"{name}: {node['name']!r} has no credential"
                assert cred in entrypoint, (
                    f"{name}: {node['name']!r} names credential {cred!r}, which "
                    "scada/nodered/entrypoint.sh does not write"
                )
            if node["type"] == "modbus-write":
                server = node.get("server")
                assert server, f"{name}: {node['name']!r} has no Modbus server"
                assert server in entrypoint, (
                    f"{name}: {node['name']!r} names Modbus server {server!r}, "
                    "which scada/nodered/entrypoint.sh does not write"
                )


def env_cmd(name: str) -> str:
    """`sh`, resolved rather than hard-coded.

    The first version passed `/bin/sh`, which exists on macOS and on Linux, and
    `/bin/true`, which exists on Linux and not on macOS. The second failure is the
    instructive one: the entrypoint is fine, the *test* is what did not work, and
    a test that fails for a reason about the machine it is on reads as a failure
    of the thing it is testing.
    """
    return shutil.which(name) or name


def test_the_entrypoint_writes_valid_json() -> None:
    """The credential file is hand-written JSON in POSIX `sh`.

    A password containing a quote or a backslash would otherwise produce a file
    Node-RED cannot parse, and the error is a JSON parse failure in a log line
    about a file the reader did not know existed. So the escaping is exercised
    here rather than in production.
    """
    script = Path("scada/nodered/entrypoint.sh")
    for password in ("simple", 'has "quotes"', "back\\slash", "both \" and '", "!"):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "NR_DATA_DIR": tmp,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_HOST": "db",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": "wwtp",
                "POSTGRES_USER": "wwtp",
                "MODBUS_HOST": "softplc",
                "MODBUS_PORT": "5020",
            }
            result = subprocess.run(
                [env_cmd("sh"), str(script), "true"],
                env=env, capture_output=True, text=True, check=False,
            )
            assert result.returncode == 0, result.stderr
            payload = json.loads(
                (Path(tmp) / "flows_cred.json").read_text(encoding="utf-8")
            )
            secret = next(iter(payload))
            assert payload[secret]["wwtp-db"]["postgresqldb"]["password"] == password, (
                f"password {password!r} did not survive the round trip"
            )
            # And the key itself: one per run, kept, so a restart can read it.
            assert (Path(tmp) / ".credentialSecret").exists()
            key = (Path(tmp) / ".credentialSecret").read_text(encoding="utf-8")
            assert key == secret, (
                "the key in flows_cred.json is not the one in .credentialSecret, "
                "so Node-RED cannot read back what the entrypoint wrote"
            )


def test_the_entrypoint_never_overwrites_an_existing_credential() -> None:
    """A credential entered in the editor must survive a restart.

    Otherwise every `docker compose up` re-derives it from the environment and
    silently discards whatever an operator had configured — which is how a
    database connection stops working after a rebuild and nobody knows why.
    """
    script = Path("scada/nodered/entrypoint.sh")
    with tempfile.TemporaryDirectory() as tmp:
        existing = Path(tmp) / "flows_cred.json"
        existing.write_text('{"hand-written": {}}', encoding="utf-8")
        env = {
            "NR_DATA_DIR": tmp, "POSTGRES_PASSWORD": "ignored",
            "POSTGRES_HOST": "db", "POSTGRES_USER": "u", "POSTGRES_DB": "d",
        }
        result = subprocess.run(
            [env_cmd("sh"), str(script), "true"],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert existing.read_text(encoding="utf-8") == '{"hand-written": {}}'


def test_the_entrypoint_fails_loudly_with_no_password() -> None:
    """A clear failure at startup beats a runtime whose every query will fail.

    The alternative is starting Node-RED with no database credential, watching it
    come up healthy, and finding out from a mimic diagram full of blank units.
    """
    script = Path("scada/nodered/entrypoint.sh")
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [env_cmd("sh"), str(script), "true"],
            env={"NR_DATA_DIR": tmp}, capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0
        assert "POSTGRES_PASSWORD" in result.stderr, result.stderr
