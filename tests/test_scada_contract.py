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
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from scada import build_flows, generate_tags
from scada.build_flows import build_all_names
from softplc.contract import Contract
from softplc.contract import contract as get_contract
from softplc.servers.modbus import HOLDING_BASE as MODBUS_HOLDING_BASE
from softplc.servers.modbus_server import ModbusTcpServer

FLOWS = build_flows.FLOWS_DIR
TAGS = generate_tags.TAGS_PATH

#: Node types that are legitimately not wired to anything. A `debug` node
#: terminates a branch, a `comment` has no ports at all, and a `tab` holds the
#: nodes rather than sitting in a path.
TERMINALS = {"debug", "comment", "tab"}

#: Fields whose value the runtime **compares against a boolean with `==`**, so a
#: JSON string there is silently falsy and a JSON `1` is silently not `true`.
#:
#: Read off the installed node packages rather than remembered, for the reason
#: `test_the_node_flag_types_match_the_runtime_comparisons` explains: the failure
#: mode is a node that runs, receives, and publishes nothing.
DEBUG_FLAG_TYPES = {"tosidebar": bool, "active": bool, "console": bool,
                    "tostatus": bool}

#: Every table the flows are allowed to touch. Not derived from a schema dump —
#: stated, so that adding a query against a table nobody reviewed is a visible
#: edit here rather than a string in a flow file.
ALLOWED_TABLES = {"reading", "event", "signal", "equipment", "site"}

#: Words that follow `FROM`/`JOIN` in SQL and are not tables. The regex cannot
#: tell them apart, and the first version of this test reported
#: `query touches 'LATERAL'` — which is a keyword, and a test that cries wolf
#: about `LATERAL` is a test people learn to ignore.
SQL_KEYWORDS_AFTER_FROM = {
    "lateral",     # LEFT JOIN LATERAL (...) — a row subquery
    "select",      # FROM (SELECT ...) AS t
    "unnest",      # FROM unnest(...)
    "generate_series",
    "values",
}


def _tables_in(sql: str) -> set[str]:
    import re as _re

    return {
        t for t in _re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", sql, _re.I)
        if t.lower() not in SQL_KEYWORDS_AFTER_FROM
    }


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
    # `Contract.writable` was a *superset* of the writable signals: it also held
    # plant-level registers like `FAULT_CODE`, which are not signals and so have
    # no tag. The test asserted that asymmetry existed with `assert register_only`,
    # on the reasoning that the difference was a modelling fact rather than a bug.
    #
    # **There is no longer a bare writable register.** `FAULT_CODE` and
    # `STORM_FLAG` were `writable: true` while nothing applied a write to either,
    # and both are read-only now. So `writable` and the writable signals are the
    # same set, and asserting they must differ would be asserting the fault back
    # into existence.
    #
    # The relationship is still checked in both directions -- a tag claiming
    # writability the contract does not grant is the dangerous direction, and the
    # one `generate_tags` could plausibly get wrong. But the *absence* of a
    # difference is now the documented fact rather than a mismatch to be
    # tolerated, so the assertion is inverted to say so out loud.
    writable = {t["id"] for t in tags["tags"] if t["writable"]}
    assert writable <= set(c.writable), (
        f"tags claim {sorted(writable - set(c.writable))} is writable, which the "
        "contract does not say"
    )
    register_only = sorted(set(c.writable) - set(c.signals))
    assert register_only == [], (
        f"{register_only} is a bare register with no signal and therefore no tag. "
        "That is a legitimate shape for a writable value -- a plant-level injection "
        "point has no measurement behind it -- so if one is intended, say so here "
        "and give it a setter in SoftPlc._apply_pending_writes. What is never "
        "acceptable is a writable entry that nothing applies a write to."
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


def test_a_multi_register_write_uses_the_multi_register_function_code(
        flows: dict[str, list[dict]]) -> None:
    """`quantity > 1` and FC6 cannot both be true, and only one of them errors.

    `node-red-contrib-modbus` derives the function code from `dataType` and
    nothing else:

        Coil               -> 5    write single coil
        HoldingRegister    -> 6    write single register
        MCoils             -> 15   write multiple coils
        MHoldingRegisters  -> 16   write multiple registers

    So `dataType: "HoldingRegister"` is FC6, and **FC6 writes exactly one register
    and ignores `quantity` completely** -- there is no exception, no warning, and
    the node reports success. A flow configured that way sends the first of a
    float32's two words and drops the second, leaving a fresh high word on a
    stale low word in the PLC.

    That is the failure this test exists to prevent, and it is worth being precise
    about why it survived so long. Nothing is *broken*: the node reports success,
    the audit-trail node downstream writes its `event` row, the historian records
    a new setpoint, the mimic display shows the operator's number, and the
    operator is told it worked. What does not happen is the only thing that
    mattered -- the plant's control loop integrates against the value it was
    handed at startup. Every layer agreed and the setpoint did not move.

    It was found when the soft PLC started refusing short writes with
    `ILLEGAL_DATA_VALUE` (see `ModbusTcpServer._accept_write`), which turned a
    silent corruption into a legible refusal. That is the only reason it was
    found at all, which is the argument for the server checking width rather
    than accepting whatever arrives.

    The failure is also invisible to review, because `HoldingRegister` and
    `MHoldingRegisters` differ by one letter and the wrong one is the more
    natural thing to type: the contract calls the thing a "holding register", so
    `dataType: "HoldingRegister"` reads as correct and `MHoldingRegisters` reads
    like a typo. The docstring in `build_flows.py` had it wrong too, describing
    the FC16 behaviour the flow was not getting.
    """
    single_register = {
        "HoldingRegister": 6,
        "Coil": 5,
    }
    multiple_register = {
        "MHoldingRegisters": 16,
        "MCoils": 15,
    }

    seen: list[str] = []
    for flow_name, nodes in flows.items():
        for node in nodes:
            if node.get("type") != "modbus-write":
                continue
            seen.append(f"{flow_name}/{node.get('name')}")
            data_type = node.get("dataType")
            quantity = node.get("quantity", 0)
            if quantity > 1:
                assert data_type in multiple_register, (
                    f"{flow_name}/{node.get('name')} writes {quantity} registers as "
                    f"dataType={data_type!r}, which is function code "
                    f"{single_register.get(data_type, '?')} -- a single-register "
                    f"write. FC6 ignores quantity, so only the first word of the "
                    f"value reaches the PLC and the rest is dropped without an "
                    f"error. Use {sorted(multiple_register)} for a multi-register "
                    f"write."
                )
            else:
                assert data_type not in multiple_register, (
                    f"{flow_name}/{node.get('name')} writes {quantity} register(s) "
                    f"with dataType={data_type!r}, a multi-register function code. "
                    f"FC16 with quantity=1 is accepted by the soft PLC but is not "
                    f"what a single-register write should say."
                )
            assert data_type in single_register or data_type in multiple_register, (
                f"{flow_name}/{node.get('name')} has dataType={data_type!r}, which "
                f"is not a function code node-red-contrib-modbus knows"
            )

    assert seen, (
        "no modbus-write nodes were found; if the flows moved, point this test at "
        "the new location rather than deleting it -- this is the only check that "
        "the project's one control write is shaped correctly"
    )


def test_a_write_range_is_inside_the_engineering_range(
        tags: dict, c: Contract) -> None:
    """The permit range must be reachable by the plant's own instrument.

    The write range is a subset of the engineering range, checked here as well as
    in `test_writable_tags_carry_their_permit_range`. Split out because the
    assertion's failure message names the tag and its two ranges, and burying it
    in the loop over a dozen tags meant it was only ever read when something
    *other* in that test had already failed.
    """
    for tag in tags["tags"]:
        if not tag["writable"]:
            continue
        low, high = tag["write_range"]
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
    """`z` is the node's tab. A node pointing at nothing is not in any flow.

    **Except a config node, which is not on a tab.** `postgreSQLConfig` and
    `modbus-client` are runtime-global: Node-RED draws them outside the canvas and
    they have no `z` key at all. Asserting `z` on them would be asserting that a
    config node is a normal node, and it would make the one correct way to add a
    database connection look wrong.

    They are also exactly once across the whole set, which the id-uniqueness check
    does not cover on its own -- that one looks within a file.
    """
    seen_config: dict[str, str] = {}
    for name, nodes in flows.items():
        tab_ids = {n["id"] for n in nodes if n["type"] == "tab"}
        for node in nodes:
            if node["type"] == "tab":
                continue
            if node["type"] in build_flows.CONFIG_NODE_TYPES:
                assert "z" not in node, (
                    f"{name}: {node['type']!r} is a config node and must not carry "
                    "a `z`. Node-RED draws config nodes outside the canvas and "
                    "looks them up by id, not by tab."
                )
                assert node["id"] not in seen_config, (
                    f"{name}: config node {node['name']!r} also appears in "
                    f"{seen_config[node['id']]}. Config nodes are runtime-global, "
                    "so the assembled flows.json has to contain exactly one."
                )
                seen_config[node["id"]] = name
                continue
            assert node.get("z") in tab_ids, (
                f"{name}: {node['name']!r} has z={node.get('z')!r}, which is not "
                f"this flow's tab {tab_ids}"
            )


def test_every_config_node_is_referenced_and_referrers_resolve(flows: dict) -> None:
    """A config node nobody uses, and a node pointing at one that is absent.

    Both directions matter, and the second is the one that produced the loudest
    failure this repository has had: `node-red-contrib-postgresql` resolves its
    `postgreSQLConfig` at construction, and when it cannot, it substitutes a stub
    rather than raising, so every query fails at run time with

        TypeError: node.config.pgPool.connect is not a function

    which names no database and is identical for "no such node" and "the node is
    broken". Nothing else in the runtime complains.

    So: exactly one `postgreSQLConfig` across the assembled set, exactly one
    `modbus-client`, every reference resolves, and neither is orphaned. Because
    the config node is defined in one file and referenced from the other two, the
    uniqueness check has to run over all files together -- which is also the
    boundary the entrypoint works at, and the boundary `nid()` used to get wrong.
    """
    assembled: dict[str, dict] = {}
    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] in build_flows.CONFIG_NODE_TYPES:
                assert node["id"] not in assembled, (
                    f"{name}: config node {node['name']!r} also defined in "
                    f"{assembled[node['id']]}"
                )
                assembled[node["id"]] = node

    assert {n["type"] for n in assembled.values()} == {
        "postgreSQLConfig", "modbus-client"
    }, f"expected both config node types, got {sorted(assembled.values())}"

    # Which node type is referenced by which field, so a contrib rename is caught.
    referring_field = {
        "postgresql": "postgreSQLConfig",
        "modbus-write": "server",
    }
    referenced: set[str] = set()
    for name, nodes in flows.items():
        for node in nodes:
            field = referring_field.get(node["type"])
            if field is None:
                continue
            assert node.get(field), (
                f"{name}: {node['name']!r} is a {node['type']} with no {field!r}. "
                f"{node['type']} requires one and fails at run time, not at deploy."
            )
            assert node[field] in assembled, (
                f"{name}: {node['name']!r} points {field} at {node[field]!r}, which "
                f"is not a config node in the assembled set. Known: "
                f"{sorted(assembled)}"
            )
            referenced.add(node[field])

    for node_id, node in assembled.items():
        assert node_id in referenced, (
            f"config node {node['name']!r} is defined and nothing uses it, so it "
            "is a connection nobody opens"
        )


def test_the_output_counts_match_the_installed_node_packages(flows: dict) -> None:
    """Re-derive the output counts from the node packages in the running container.

    `_outputs()` is a table in `scada/build_flows.py`, and a table is a memory.
    Three of its entries were wrong -- `postgresql` and `file in` said two outputs
    when they have one, `modbus-write` said one when it has two -- and nothing
    complained: a `wires` list with more entries than the node has ports is
    accepted, imported and then never delivered on.

    A contrib upgrade that changes a port count is the same failure with a newer
    package, so this reads the numbers out of the packages themselves. It needs the
    container, and skips without it rather than pretending to have checked: a
    skipped check that reads as a pass is how the table got to be wrong in the first
    place.
    """
    probe = _node_package_probe()
    if probe is None:
        pytest.skip("the scada container is not running, so the node packages "
                    "cannot be read")

    used = {
        node["type"]
        for nodes in flows.values()
        for node in nodes
        if node["type"] not in build_flows.CONFIG_NODE_TYPES
    }
    uncheckable = used - set(probe) - STRUCTURAL_TYPES
    assert not uncheckable, (
        f"these node types appear in the flows but not in the probe, so their "
        f"output counts have never been checked against a package: "
        f"{sorted(uncheckable)}. Add them to the probe's search, or confirm "
        f"deliberately that no installed package registers them."
    )

    for name, nodes in flows.items():
        for node in nodes:
            kind = node["type"]
            if kind in build_flows.CONFIG_NODE_TYPES or kind in PER_NODE_OUTPUTS:
                continue
            declared = build_flows._outputs(node)
            if kind not in probe:
                continue
            assert declared == probe[kind], (
                f"{name}: build_flows._outputs says {kind} has {declared} output(s), "
                f"the installed package registers {probe[kind]}. A wrong count is "
                f"silent: the flow imports and nothing arrives on the extra port."
            )


#: Node types whose output count is a property of the *instance*, read from the
#: node's own fields rather than from its type. The probe can only see what
#: `registerType` declares, which for these is the default — and comparing a
#: two-output `function` node against the registered default of 1 is not a
#: disagreement about the package, it is a disagreement about what is being
#: measured. Listed, so adding a third such type is a visible edit.
PER_NODE_OUTPUTS = {"function"}


#: Node-RED structural elements, which appear in `flows.json` but are not
#: registered node types and so have no package and no `registerType` call.
#: Verified by grepping the installed `@node-red/nodes` for them: a `tab` is a
#: flow container the editor creates, not something a package declares.
#:
#: Listed because the alternative is a probe that cannot find them, which is
#: indistinguishable from a probe that stopped working — the exact confusion that
#: hid the two bugs above.
STRUCTURAL_TYPES = {"tab", "subflow", "group"}


def _node_package_probe() -> dict[str, int] | None:
    """Output counts straight from the installed node packages, or None.

    Reads each node's `.html`, because that is the only file where `registerType`
    carries the `outputs` field and it is not minified. **The node type is read
    from the `registerType` call inside the file, not from the filename** — a
    filename is a packaging detail (`file in` is `10-file.html`, `postgresql` is
    `postgresql.html`) and the first version matched on filenames.

    Two failures got this wrong in a row, both invisible:

    1. `wanted.exec(p)` called on a `Set`. `Set` has no `exec`, so the script
       threw a `TypeError` on the first file and wrote nothing.
    2. Then it matched filenames, and found `modbus-write` and nothing else.

    Either way the caller could not tell a broken probe from a missing container,
    so the test **skipped** — with a skip reason naming Docker, while the
    container was up and healthy. A probe that cannot fail loudly is worse than no
    probe, because the skip reads as a pass. So a broken probe now raises, and
    the caller asserts that every node type in the flows was actually found.
    """
    script = r"""
const fs = require("fs"), path = require("path");
// **Derived from package.json, not a hardcoded list.** An earlier version walked
// all of /data/node_modules, which also holds npm's own bundled dependency tree
// — tens of thousands of .html files — and blew the 60 s timeout on every run.
// That timeout was caught and reported as "no container", the same skip that hid
// the first two bugs in this function, so a slow probe is indistinguishable from
// an absent one for exactly as long as nobody looks.
//
// So: the three declared packages, plus the core nodes package. A fourth
// dependency means adding a root here, which is the right amount of ceremony.
const declared = Object.keys(
  JSON.parse(fs.readFileSync("/data/package.json", "utf8")).dependencies || {}
);
const roots = [];
for (const name of declared) {
  for (const base of ["/data/node_modules", "/usr/src/node-red/node_modules"]) {
    const p = path.join(base, name);
    if (fs.existsSync(p)) roots.push(p);
  }
}
// Core nodes live inside @node-red/nodes and are not a declared dependency of
// this service -- they are node-red's own.
const core = [
  "/usr/src/node-red/node_modules/@node-red/nodes",
  "/data/node_modules/@node-red/nodes",
];
for (const p of core) if (fs.existsSync(p)) roots.push(p);

const out = {};
const files = {};

// Find `outputs: N` at the *top level* of the registerType options block.
// Depth-aware, because a node's `defaults` block can itself contain an
// `outputs` key and reading that one would be a different number entirely.
function outputsIn(block) {
  let depth = 0;
  const re = /([{}])|(?:^|[\s,])outputs\s*:\s*(\d+)/g;
  let m;
  while ((m = re.exec(block)) !== null) {
    if (m[1] === "{") { depth++; continue; }
    if (m[1] === "}") { depth--; continue; }
    if (depth === 1) return Number(m[2]);
  }
  // No `outputs` in the registration block means one output, which is Node-RED's
  // own documented default -- and is exactly how `file in` and `postgresql`
  // declare theirs.
  return 1;
}

function walk(dir, depth) {
  if (depth > 5 || !fs.existsSync(dir)) return;
  let entries;
  try { entries = fs.readdirSync(dir, {withFileTypes: true}); } catch (e) { return; }
  for (const e of entries) {
    // Locale bundles repeat the same .html many times over.
    if (e.name === "locales" || e.name === "docs" || e.name === ".bin") continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) { walk(p, depth + 1); continue; }
    if (!e.name.endsWith(".html")) continue;
    const src = fs.readFileSync(p, "utf8");
    // Only the type name here. The options block is located by brace matching
    // rather than by regex, because editor files come in two shapes --
    //     registerType('postgresql', { ...options... })
    //     registerType("debug", DebugNode, { ...options... })
    // -- and a single pattern that fits one misses the other silently. That is
    // what this function got wrong twice.
    const re = /registerType\(\s*['"]([^'"]+)['"]/g;
    let m;
    while ((m = re.exec(src)) !== null) {
      const type = m[1];
      if (type in out) continue;
      let i = src.indexOf("{", m.index + m[0].length);
      // If the constructor was written inline, that first brace opens the
      // function body rather than the options.
      if (i > 0 && /function\b/.test(src.slice(m.index + m[0].length, i))) {
        i = src.indexOf("{", i + 1);
      }
      if (i < 0) continue;
      let depth2 = 0, end = -1;
      for (let j = i; j < src.length; j++) {
        if (src[j] === "{") depth2++;
        else if (src[j] === "}" && --depth2 === 0) { end = j; break; }
      }
      if (end < 0) continue;
      out[type] = outputsIn(src.slice(i, end + 1));
      files[type] = p;
    }
  }
}
for (const r of roots) walk(r, 0);
process.stdout.write(JSON.stringify({counts: out, files: files, roots: roots}));
"""
    try:
        raw = subprocess.run(
            ["docker", "compose", "--profile", "scada", "exec", "-T", "scada",
             "node", "-e", script],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        if not shutil.which("docker"):
            # Docker is not installed. That is a genuine "cannot check".
            return None
        raise AssertionError(
            f"could not run the node-package probe: {exc}\n\nIf Docker is absent "
            f"this should skip rather than fail, but a docker that exists and does "
            f"not work is a different problem and should be visible."
        ) from exc

    if raw.returncode != 0 or not raw.stdout.strip():
        raise AssertionError(
            f"the node-package probe failed inside the running scada container "
            f"(exit {raw.returncode}):\n{raw.stderr.strip()}\n\nThe container is "
            f"reachable, so this is a bug in the probe script, not a missing "
            f"container. Fix the script; do not skip."
        )
    try:
        parsed = json.loads(raw.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"the node-package probe wrote something that is not JSON: "
            f"{raw.stdout[:400]!r}"
        ) from exc
    return parsed["counts"] or None


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


def test_a_debug_tap_is_a_tap_and_not_a_bin(flows: dict) -> None:
    """`tosidebar` is a boolean, and every debug node in these flows was not one.

    Node-RED's own default is `tosidebar: {value: true}` (`21-debug.html:79`) —
    a boolean, from a checkbox. The node then publishes under

        if (node.active && node.tosidebar)  sendDebug(...)   // complete:"true"
        if (node.tosidebar == true)       sendDebug(...)   // complete:"payload"

    These flows set `"tosidebar": "console"`, and `"console" == true` is
    **false**: the string coerces to `NaN`, `NaN == 1` is false. So every debug
    node in all three flows received its messages and published nothing — no
    sidebar entry, no message count on the node in the editor, no log line.

    That is the worst failure mode in this repository and it is not rare: eight
    diagnostic taps, dead, on the flows whose entire purpose is being readable.
    And it is invisible from outside, because nothing errors. A generator bug that
    produces *fewer* messages than expected is indistinguishable from a plant
    that is quiet.

    So the flag types are asserted rather than their values being eyeballed: the
    type is the part that is wrong in a way no editor will show you, and
    `active: false` on a tap is a policy choice rather than a defect, so it is
    not asserted.
    """
    taps = [n for f in flows.values() for n in f if n["type"] == "debug"]
    assert taps, "no debug nodes at all, so nothing in these flows is observable"
    for node in taps:
        for field, want in DEBUG_FLAG_TYPES.items():
            got = node.get(field)
            assert isinstance(got, want), (
                f"{node['name']!r}: {field} is {got!r} ({type(got).__name__}), not a "
                f"{want.__name__}. The runtime tests these with `==` against a "
                "boolean, so a string is silently falsy and the node publishes "
                "nothing while reporting no error."
            )


def test_every_debug_tap_is_reachable_and_enabled(flows: dict) -> None:
    """A tap with nothing upstream, or switched off, is not a tap.

    Reachability is asserted on **reachability from the flow's sources**, not on
    having a wire in each: `mimic status` sits behind a poll and three function
    nodes, and a rule that each tap had to be wired to something would be
    satisfied by a tap wired to a dead end. So the check is that every node
    upstream of the tap is itself reachable from a source.
    """
    sources = {"trigger", "inject"}
    for name, nodes in flows.items():
        # id -> the ids its output ports point at, flattened.
        wires_from: dict[str, list[str]] = {
            node["id"]: [t for port in (node.get("wires") or []) for t in port]
            for node in nodes
        }
        targets: set[str] = {
            t for outs in wires_from.values() for t in outs
        }

        # Everything reachable by walking forward from a source node.
        reachable: set[str] = set()
        frontier = [n["id"] for n in nodes if n["type"] in sources]
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(wires_from.get(current, []))

        for node in nodes:
            if node["type"] != "debug":
                continue
            assert node.get("active") is True, (
                f"{name}: the tap {node['name']!r} is inactive. Nothing this "
                "project generates should be observable only in principle."
            )
            assert node["id"] in reachable, (
                f"{name}: {node['name']!r} is not reachable from any "
                f"{sorted(sources)}. "
                "A debug node nothing reaches receives nothing."
            )
            assert node["id"] in targets, (
                f"{name}: nothing points at {node['name']!r}"
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

    **A config node is neither source nor terminal.** It has no ports, so it is not
    wired to anything; and it is not wired *to*, so by the rule below it would read
    as dead and unused. It is attached by an id field on the node that uses it,
    which `test_every_config_node_is_referenced_and_referrers_resolve` checks
    instead — including that it is not merely present but used.
    """
    # A `trigger` and an `inject` are sources. A `debug` and a `comment` are
    # terminals. Neither is required to have a neighbour of the other kind.
    sources = {"trigger", "inject"}
    terminals = TERMINALS | set(build_flows.CONFIG_NODE_TYPES)

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


def test_every_node_has_its_own_place_on_the_canvas(flows: dict) -> None:
    """No two nodes share a position, and wires flow left to right.

    **Every node factory in `build_flows.py` hardcoded `"x": 400, "y": 100`**
    because a coordinate is not a behaviour and nobody reading the generator
    thought about it. The Node-RED editor therefore opened on a pile: **nineteen
    nodes at the identical coordinate** and nine more with no position at all.
    The flows are a deliverable meant to be *read* in that editor, and they were
    unreadable — and zoom cannot help, because zooming cannot separate nodes
    that occupy the same point.

    Three assertions, each catching a different way the layout can be wrong:

    * **no collisions** — two nodes at one coordinate stack, which looks like a
      rendering fault and is the failure that actually happened;
    * **a position exists** — Node-RED auto-places a node that has none, and
      where it chooses is not the generator's to rely on;
    * **x increases along a wire** — the layout is read off the graph, so this
      holds unless depth is computed wrong. A flow that steps *backwards*
      renders as a right-to-left tangle and is much harder to follow than a
      pile.
    """
    staged = {"tab", "comment", "postgreSQLConfig", "modbus-client"}
    for flow_name, nodes in flows.items():
        body = [n for n in nodes if n.get("type") not in staged]
        assert body, f"{flow_name}: no nodes to lay out"

        occupied: dict[tuple[int, int], str] = {}
        for node in body:
            assert isinstance(node.get("x"), int) and isinstance(node.get("y"), int), (
                f"{flow_name}: {node.get('name', node['id'])!r} has no position "
                f"({node.get('x')!r}, {node.get('y')!r}). Node-RED invents one "
                f"and the generator stops being the thing that decides layout."
            )
            here = (node["x"], node["y"])
            assert here not in occupied, (
                f"{flow_name}: {node.get('name', node['id'])!r} and "
                f"{occupied[here]!r} are both at {here}, so they stack on the "
                f"canvas and the flow is unreadable."
            )
            occupied[here] = node.get("name", node["id"])

        by_id = {n["id"]: n for n in body}
        for node in body:
            for output in node.get("wires", []):
                for target_id in output:
                    target = by_id.get(target_id)
                    if target is None:
                        continue
                    assert target["x"] > node["x"], (
                        f"{flow_name}: {node.get('name', node['id'])!r} at "
                        f"x={node['x']} wires to "
                        f"{target.get('name', target['id'])!r} at "
                        f"x={target['x']}. The layout is graph depth, so data "
                        f"flows left to right; this renders as a tangle."
                    )


def test_the_control_flow_targets_a_writable_signal_with_a_register(
    c: Contract, flows: dict,
) -> None:
    """Four conditions, all of them safety properties.

    The signal must exist, it must be on the *writable* surface, it must have a
    Modbus register — because a control flow with no register has nowhere to write,
    and a default of address zero would write to the wrong piece of plant — and the
    register must be a `float32`, because that is the only encoding the flow has.

    **And the flow's own node must carry the contract's numbers, not its own.** The
    last part used not be checked at all. The write node read `adr`, which was not a
    field the node has, and `datatype: float`, which is not a value it accepts; so
    it imported with three `required: true` fields missing and `Number(undefined)`
    for the address. Nothing in a flow file rejects that. Comparing the emitted node
    against the contract is the only place the two can disagree visibly.
    """
    tag = build_flows.CONTROL_TAG
    assert tag in c.signals
    assert tag in c.writable
    address, word_order, unit_id = build_flows._modbus_register(c, tag)
    assert address >= 40000, (
        f"{tag}: register {address} is below the project's 40000 convention"
    )
    assert word_order in ("big", "little")

    writes = [
        n for n in flows["03-control.json"]
        if n["type"] == "modbus-write"
    ]
    assert writes, "the control flow has no Modbus write node"
    for node in writes:
        # **The wire offset, not the contract address.** These are different
        # numbers and conflating them is the bug this assertion was written to
        # prevent — it asserted the *contract* address, so it agreed with the node
        # that was wrong, and passed while the write went nowhere.
        #
        # `adr` is a PDU offset. The contract says 40102; the wire wants 102.
        # The soft PLC's answer to the contract address was
        #
        #     Modbus exception 2: Illegal data address (register not supported)
        #
        # which names the register as unsupported rather than as 40000 too high,
        # so nothing anywhere said "off by the block base".
        assert node["adr"] == address - MODBUS_HOLDING_BASE, (
            f"the write node targets PDU offset {node['adr']}, but {tag} is at "
            f"contract address {address}, which is wire offset "
            f"{address - MODBUS_HOLDING_BASE}. A wrong offset writes to the wrong "
            "piece of plant, and the soft PLC will not object."
        )
        # And the translation is the server's own, not a subtraction repeated here.
        assert node["adr"] == ModbusTcpServer.wire_offset(address)
        # Regression guard on the specific number, so the failure names the plant.
        assert node["adr"] == 102, (
            f"adr is {node['adr']}; AERATION:AHU-1:SETPOINT_DO is at contract "
            "40102, which is wire offset 102. If this changed, check "
            "ModbusTcpServer.wire_offset before assuming the contract moved."
        )
        assert node["unitid"] == unit_id, (
            f"the write node uses unit {node['unitid']}, the contract says {unit_id}"
        )
        # **This assertion was `== "HoldingRegister"`, and it was wrong in the exact
        # way that mattered.** It passed for as long as the flow was broken,
        # because it agreed with the bug.
        #
        # `node-red-contrib-modbus` maps `dataType` to a function code and nothing
        # else: `HoldingRegister` is **FC6**, write *single* register, which
        # ignores `quantity` completely. So the node was configured to write one
        # register, `quantity: 2` was decorative, and only the high word of the
        # float32 ever reached the PLC -- with no error, no warning, and a
        # success reported all the way back to the operator. The one control
        # write in the project had never once worked.
        #
        # What made this survive so long is that the assertion's own message
        # stated the false belief as fact: *"a float32 occupies two holding
        # registers, so dataType must be HoldingRegister"*. Both the test and the
        # generator's docstring said it, so it read as settled. Nobody was
        # checking the function code, because everyone agreed on the wrong one.
        #
        # `MHoldingRegisters` is FC16 and sends both words in one request.
        assert node["dataType"] == "MHoldingRegisters", (
            f"a float32 occupies two holding registers, so the write must use "
            f"dataType=MHoldingRegisters (function code 16, write multiple "
            f"registers). {node['dataType']!r} is "
            + (
                "function code 6, write SINGLE register, which ignores quantity "
                "and sends only the first word of the float"
                if node["dataType"] == "HoldingRegister" else
                "not a multi-register write"
            )
        )
        assert node["quantity"] == 2, (
            f"a float32 is four bytes and a Modbus register is two, so quantity "
            f"must be 2; {node['quantity']!r} would write one register and leave "
            "the other holding whatever was there"
        )

    # The word order is not on the write node -- contrib has nowhere to put it --
    # so it is generated into the function node that builds the payload. Checked
    # by name because the word appears inside a JavaScript string.
    builder = next(
        n for n in flows["03-control.json"]
        if n.get("name") == "build the Modbus write"
    )
    body = builder["func"]
    assert f"'{word_order}'" in body, (
        f"the contract says {tag}'s register is {word_order}-word-order, and the "
        f"payload builder does not say so. Wrong word order does not error -- it "
        f"writes a plausible float that the plant reads as a different one."
    )
    assert "writeFloatBE" in body and "readUInt16BE" in body, (
        "the payload builder must encode a float32 into two 16-bit words explicitly"
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


def test_the_permit_check_sends_a_valid_setpoint_to_the_write_and_not_the_refusal(
    flows: dict,
) -> None:
    """The check node's two outputs, and which way each decision leaves.

    `return [a, b]` is output 0 then output 1, so the array position *is* the
    safety decision: output 0 goes to the `control: refused` tap and output 1
    goes to the Modbus write.

    **Every branch of this node used to `return [msg, null]` — including the
    success.** So a setpoint that passed the permit check left on the refusal
    port, output 1 got `null`, and the write node received nothing.

    The consequence is the reason this test exists rather than a code review
    comment: the control flow had never written to the plant, and **every
    observable behaviour of it was correct**. All three refusals fired, with the
    right messages. The refusals were simply the only thing that ever happened,
    and a flow whose every reachable path is the safe one looks like a working
    safety interlock. Found by injecting `3.5` into the running flow and watching
    the `control: refused` tap receive `{"ok": true, ...}`.

    Counted structurally: exactly one `return [null, msg]` (the success), and
    every `return [msg, null]` is preceded by `ok: false` — because a count alone
    would pass on three successes and one refusal.
    """
    raw = next(
        n["func"] for n in flows["03-control.json"]
        if n.get("name") == "check against the permit range"
    )
    # Comments are prose about this very bug and quote the old source verbatim, so
    # counting them would count the documentation of the defect as the defect.
    body = "\n".join(re.split(r"//", line)[0] for line in raw.splitlines())

    assert body.count("return [null, msg]") == 1, (
        "expected exactly one success exit on output 1 (the Modbus write port); "
        f"found {body.count('return [null, msg]')}"
    )
    assert body.count("return [msg, null]") == 3, (
        "expected exactly three refusal exits on output 0 (no tag list, not a "
        f"number, out of range); found {body.count('return [msg, null]')}"
    )

    # Every remaining exit must be a refusal.
    refusals = body.count("ok: false")
    assert refusals == body.count("return [msg, null]"), (
        f"{refusals} refusal payloads and "
        f"{body.count('return [msg, null]')} exits on output 0. These must be "
        "equal: an exit on the refusal port that is not `ok: false` is a setpoint "
        "that reached the operator as a rejection, and an `ok: false` with no "
        "exit is a refusal that does nothing."
    )

    # And the wiring has to agree with the port order.
    check = next(
        n for n in flows["03-control.json"]
        if n.get("name") == "check against the permit range"
    )
    names = {
        n["id"]: n.get("name") for n in flows["03-control.json"]
    }
    assert check["outputs"] == 2, f"expected two outputs, got {check['outputs']}"
    refused = [names[t] for t in check["wires"][0]]
    written = [names[t] for t in check["wires"][1]]
    assert refused == ["control: refused"], (
        f"output 0 goes to {refused}; it must be the refusal tap, since every "
        "`return [msg, null]` in the body lands there"
    )
    assert written, (
        "output 1 goes nowhere, so the `return [null, msg]` the body needs for a "
        "valid setpoint has nowhere to land"
    )


def test_every_template_literal_in_a_flow_actually_interpolates(flows: dict) -> None:
    """No `{name}` in a template literal without a `$` in front of it.

    Found by driving the control flow live, which is the only way it showed up:
    an operator typing `99` was told

        {requested} {spec.unit} is outside the writable range {lo}-{hi} {spec.unit}

    Backticks, braces, and no `$`. That is a **valid template literal** —
    JavaScript happily evaluates a template with no substitutions — so the flow
    deployed, the refusal fired, the tap received it, the flow refused to write,
    and the safety property held. Nothing errored. The only thing that was wrong
    is the one field whose entire purpose is telling the operator what they typed
    and what the legal range is.

    That is worth a structural check rather than a remembered one: the failure
    mode is a flow that is *more* correct than it looks, and a reader has no way
    to tell without evaluating the string.

    Scope is template literals only. A `{...}` in a comment or in an ordinary
    quoted string is not an interpolation and is not this bug; comments are
    stripped before the check for that reason. An object literal is likewise not
    matched, since only backtick-delimited runs are examined.
    """
    uninterpolated: list[str] = []
    # `{ident}` or `{a.b}`, not preceded by `$`.
    placeholder = re.compile(r"(?<!\$)\{[A-Za-z_$][\w.$]*\}")

    for name, nodes in flows.items():
        for node in nodes:
            if node.get("type") != "function":
                continue
            for lineno, line in enumerate(node.get("func", "").splitlines(), 1):
                # Strip a trailing `//` comment: its braces are prose.
                code = re.split(r"//", line, maxsplit=1)[0]
                for literal in re.findall(r"`[^`]*`", code):
                    for hit in placeholder.findall(literal):
                        uninterpolated.append(
                            f"{name}: {node['name']!r} line {lineno}: {hit} in "
                            f"{literal.strip()[:70]!r}"
                        )

    assert not uninterpolated, (
        "these template literals have a placeholder with no `$`, so JavaScript "
        "prints the placeholder text verbatim:\n  "
        + "\n  ".join(uninterpolated)
    )


def test_the_control_flow_writes_over_modbus_and_not_to_the_database(
    c: Contract, flows: dict
) -> None:
    """A setpoint in Postgres is a number that looks like a command and is not.

    So the flow must contain a Modbus write node, and the only `postgresql` node
    in it must be the audit trail — an `INSERT` into `event`, never an `UPDATE`
    against anything an alarm could read.

    **The address lives in the `modbus-client` config node, not on the write node,
    and this test used to assert the opposite.** It checked that `server` contained
    `wwtp`, on the reasoning that the server was "named by config entry, not by
    host, so the flow file contains no address". But `server` is a config-node
    **id**: the write node did `t.nodes.getNode("wwtp-softplc-modbus")`, got
    `undefined`, and every registration was behind an `if (client)`. The flow
    wrote nothing, forever, with no error — on the tab whose own comment says it is
    how a setpoint reaches the PLC. So the assertion is now that the address is a
    literal on a config node that exists and that the write node resolves to it.
    """
    nodes = {n["name"]: n for n in flows["03-control.json"] if "name" in n}
    assert any(n["type"] == "modbus-write" for n in nodes.values()), (
        "the control flow has no Modbus write node"
    )
    clients = [
        n for n in nodes.values() if n["type"] == "modbus-client"
    ]
    assert len(clients) == 1, (
        f"expected exactly one modbus-client, found {len(clients)}. Without one the "
        "write node resolves nothing and silently writes nothing."
    )
    client = clients[0]
    for node in nodes.values():
        if node["type"] != "modbus-write":
            continue
        assert node["server"] == client["id"], (
            f"the write node points at {node['server']!r}, which is not the "
            f"modbus-client's id {client['id']!r}. contrib resolves this with "
            "getNode(), so a name here resolves to nothing."
        )
    for node in nodes.values():
        if node["type"] == "postgresql":
            assert "INSERT INTO event" in node["query"], (
                "the only database write in the control flow is the audit trail"
            )
            assert "UPDATE" not in node["query"].upper()


def test_no_operator_entry_point_fires_on_its_own(flows: dict) -> None:
    """An inject that leads to a write must not fire at deploy or on a timer.

    Two things about `enter a setpoint` had to change together, and changing
    either alone is a way to write to the plant by accident:

    * It was `payloadType: "date"`, so it fired a timestamp. `Number()` of a
      timestamp is epoch milliseconds, so the permit check refused
      `1790925871153` — the control flow could only ever refuse, which from the
      outside is identical to an interlock that works.
    * Fixing that means giving it a real number, and it was `once: true`, which
      fires **at deploy**. So the fix for the first fault is a way to write that
      number to the plant on every container restart, on every `docker compose
      up`, and on every deploy from the editor.

    So: every inject on a path that reaches a `modbus-write`, a `postgresql`
    write, or an `acknowledge`, must have neither `once` nor `repeat`. Those are
    the two fields that make Node-RED fire a node without a person asking.
    """
    #: Statements that change something. A read is safe to automate; a write is
    #: a decision, and a decision nobody made is not a decision.
    writing = re.compile(
        r"\bINSERT\s+INTO\b|\bUPDATE\b|\bDELETE\s+FROM\b", re.I
    )

    for name, nodes in flows.items():
        by_id = {n["id"]: n for n in nodes}
        children = {
            n["id"]: [t for port in (n.get("wires") or []) for t in port]
            for n in nodes
        }

        #: Does this node lead, directly or through other nodes, to something
        #: that changes state? Computed by walking *forward*, because an inject
        #: is a source and has no parents to inherit the answer from.
        leads_to_write: dict[str, bool] = {}

        def reaches_write(
            node_id: str,
            seen: frozenset[str] = frozenset(),
            _by_id: dict = by_id,
            _children: dict[str, list[str]] = children,
        ) -> bool:
            # `_by_id` and `_children` are bound as defaults rather than closed
            # over: they are rebuilt on every iteration of the `for name` loop
            # below, so a closure that read them late would silently consult the
            # *last* flow's wiring. It is called within the iteration today, which
            # is why this has not been a live bug — and "has not been a bug yet"
            # is the reason to bind it.
            if node_id in seen:
                return False
            seen = seen | {node_id}
            node = _by_id[node_id]
            if node["type"] == "modbus-write":
                return True
            if writing.search(node.get("query", "") or ""):
                return True
            return any(
                reaches_write(child, seen, _by_id, _children)
                for child in _children.get(node_id, [])
            )

        for node in nodes:
            leads_to_write[node["id"]] = reaches_write(node["id"])

        for node in nodes:
            if node["type"] != "inject" or not leads_to_write[node["id"]]:
                continue
            assert not node.get("once") and not node.get("repeat"), (
                f"{name}: the inject {node['name']!r} leads to a write but has "
                f"once={node.get('once')!r} repeat={node.get('repeat')!r}, so "
                f"Node-RED fires it without anyone asking and the write happens "
                f"on every deploy. Operator entry points must be manual."
            )
            assert node.get("payloadType") in {"num", "json", "str"}, (
                f"{name}: the inject {node['name']!r} feeds a write with "
                f"payloadType {node.get('payloadType')!r}. `date` is a timestamp, "
                f"which reaches the write path as epoch milliseconds — refused by "
                f"a range check, which reads as a working interlock."
            )


def test_no_node_reads_a_message_property_nothing_writes(flows: dict) -> None:
    """Every custom `msg.x` a function reads is written by an earlier node.

    A message property is the only wiring a function node has that a flow file
    cannot show you. There is no declaration, no type, and nothing at import time
    that notices a name that was never set — a read of a property nobody writes is
    `undefined` at runtime and a `TypeError` on the first property access, in
    whatever node happened to run first.

    **`msg.req` was read by `record what was written` and written by nobody.**
    The audit node did `const req = msg.req || {}` and then
    `req.write_range.join('-')`, so every single control write threw

        TypeError: Cannot read properties of undefined (reading 'join')

    *after* the setpoint had already reached the PLC. An unlogged control action
    is not a safe failure mode; it is a control action nobody can account for.
    The `|| {}` guard is what made it survivable as a crash rather than as
    something readable — and it is also why the crash happened on `.join` rather
    than on the access.

    Scoped to properties this project invents. `payload`, `topic`, `params`,
    `_msgid` and friends are set by Node-RED or by a node upstream of the
    function, and are excluded by name rather than by pattern so that a genuinely
    new convention has to be listed here to be checked.
    """
    #: Node-RED's own message properties, plus the ones contrib nodes read. Not
    #: "anything that looks standard" — an explicit list, so a new convention
    #: cannot slip past by looking plausible.
    #:
    #: `req`/`res`/`cookie` are here because `http in` sets them and this project
    #: has no `http in` node — they are listed to document that they are *not*
    #: this project's `msg.req`, which is its own convention and must be written
    #: by the flows. Listing `req` as exempt made this test pass on the very bug
    #: it was written for, which is the failure mode an exclusion list always has.
    builtin = {
        "payload", "topic", "params", "queryParameters", "_msgid", "parts",
    }

    #: `msg.x = ...` / `+=` and `delete msg.x` are handled positionally below,
    #: because "this node writes it" is not enough — `record what was written`
    #: both reads and writes `msg.req`, and asserting only that the name appears
    #: somewhere in the body passed on the exact bug this test was written for.

    for name, nodes in flows.items():
        by_id = {n["id"]: n for n in nodes}
        #: What each node writes, accumulated along the wire.
        provided: dict[str, set[str]] = {}

        # Walk forward from the sources so `provided` is path-dependent, which is
        # the point: a property written on one branch is not available on another.
        order: list[str] = []
        seen: set[str] = set()
        frontier = [n["id"] for n in nodes if n["type"] in {"inject", "trigger"}]
        while frontier:
            cur = frontier.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            frontier.extend(t for p in (by_id[cur].get("wires") or []) for t in p)

        # Who points at each node. `wires` on a node are its *outgoing* ports,
        # so parents have to be inverted — reading `node["wires"]` as ancestry
        # gives every node an empty parent set and the whole check becomes a
        # no-op that passes on everything.
        parents: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        for node in nodes:
            for port in (node.get("wires") or []):
                for target in port:
                    parents.setdefault(target, []).append(node["id"])

        for node_id in order:
            node = by_id[node_id]
            inherited: set[str] = set()
            for parent in parents.get(node_id, []):
                inherited |= provided.get(parent, set())

            if node["type"] == "function":
                body = re.sub(r"//.*", "", node["func"])
                #: Every `msg.x` occurrence with its offset, so a write only
                #: counts for reads that come *after* it.
                sites = [
                    (m.start(), m.lastgroup, m.group("prop"))
                    for m in re.finditer(
                        r"\bmsg\.(?P<prop>[A-Za-z_$][\w$]*)"
                        r"(?P<assign>\s*(?:=[^=]|\+=|-=|\*=|/=|\?\?=|\|\|=|&&=))?"
                        r"|\bdelete\s+msg\.(?P<prop2>[A-Za-z_$][\w$]*)",
                        body,
                    )
                ]
                writes_at = [
                    pos for pos, kind, prop in sites
                    if kind in {"assign", "prop2"}
                ]
                missing = {
                    prop for pos, kind, prop in sites
                    if kind == "prop"
                    and prop not in builtin
                    and prop not in inherited
                    and not any(w < pos for w in writes_at)
                }
                assert not missing, (
                    f"{name}: {node['name']!r} reads {sorted(missing)} but no node "
                    f"on its path writes them first. Available from upstream: "
                    f"{sorted(inherited - builtin) or 'nothing'}. A read of a "
                    "message property nothing has written yet is undefined at "
                    "runtime, and nothing in a flow file reports it."
                )
                own = {prop for _, kind, prop in sites if kind in {"assign", "prop2"}}
            else:
                own = set()

            produced = set(inherited)
            # postgresql *reads* msg.params; modbus-write produces a fresh
            # payload but, with keepMsgProperties, keeps everything else.
            if node["type"] == "postgresql":
                produced |= {"params"}
            if node["type"] == "debug":
                produced = set()
            provided[node_id] = produced | own


def test_the_modbus_address_matches_compose(flows: dict) -> None:
    """The Modbus address in the flow, and the one compose gives the runtime.

    `node-red-contrib-modbus` has no environment indirection — there is no
    `tcpHostFieldType`, unlike the PostgreSQL config node — so the address in
    `flows.json` is a literal and it can drift from the one in compose.yaml. There
    is nothing clever to do about that, so this is the check: a compose change that
    moves the soft PLC fails here instead of failing as a control flow that quietly
    stops reaching the plant.

    Parsed rather than grepped. The first version searched for
    `MODBUS_PORT: 5020`, which fails on compose's own `MODBUS_PORT: "5020"` — a
    quoting difference that says nothing about drift — so it would have passed on a
    genuine mismatch and failed on a correct one.
    """
    import yaml

    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    env = compose["services"]["scada"]["environment"]

    assert env["MODBUS_HOST"] == build_flows.MODBUS_HOST, (
        f"the flow writes to {build_flows.MODBUS_HOST!r} but compose gives the "
        f"scada service {env['MODBUS_HOST']!r}. Update MODBUS_HOST in "
        f"scada/build_flows.py and regenerate the flows."
    )
    assert int(env["MODBUS_PORT"]) == build_flows.MODBUS_PORT, (
        f"the flow writes to port {build_flows.MODBUS_PORT} but compose gives the "
        f"scada service {env['MODBUS_PORT']}. Update MODBUS_PORT in "
        "scada/build_flows.py and regenerate the flows."
    )

    clients = [
        n for flow in flows.values() for n in flow
        if n["type"] == "modbus-client"
    ]
    assert clients, "no modbus-client node to check"
    for node in clients:
        assert node["tcpHost"] == build_flows.MODBUS_HOST, node
        assert node["tcpPort"] == build_flows.MODBUS_PORT, node
        assert node["unit_id"] == int(env["MODBUS_UNIT_ID"]), (
            f"the modbus-client uses unit {node['unit_id']}, compose says "
            f"{env['MODBUS_UNIT_ID']}"
        )


def test_the_postgres_address_is_an_environment_variable(flows: dict) -> None:
    """No address, no user and no password in any committed flow file.

    `postgreSQLConfig` reads them by name from the environment
    (`hostFieldType: env` and its four siblings), so compose.yaml is the only place
    that knows where the database is. This asserts the flow says *which* variable
    rather than *what* address — which is the property that makes moving a
    container a compose change and not a commit.

    The credentials are the other half and are not here at all: `userEnv` and
    `passwordEnv` are empty and `userFieldType`/`passwordFieldType` are `cred`, so
    the values live in `flows_cred.json`, keyed by this node's id.
    """
    nodes = [
        n for flow in flows.values() for n in flow
        if n["type"] == "postgreSQLConfig"
    ]
    assert len(nodes) == 1, f"expected exactly one postgreSQLConfig, got {len(nodes)}"
    node = nodes[0]

    for field, env in (
        ("host", build_flows.PG_HOST_ENV),
        ("port", build_flows.PG_PORT_ENV),
        ("database", build_flows.PG_DATABASE_ENV),
    ):
        assert node[field] == env, (
            f"the flow carries {field}={node[field]!r}. It should name the "
            f"environment variable {env!r} so compose.yaml stays the only place "
            f"that knows the address."
        )
        assert node[f"{field}FieldType"] == "env", (
            f"{field}FieldType is {node[f'{field}FieldType']!r}; without 'env' the "
            f"node reads the value literally and {node[field]!r} is a variable name "
            "being used as a hostname."
        )

    assert node["userFieldType"] == "cred", (
        "the user must be a credential, not a field, or it lands in a committed "
        "flow file"
    )
    assert node["passwordFieldType"] == "cred", (
        "the password must be a credential, not a field"
    )
    assert not node["userEnv"] and not node["passwordEnv"], (
        "userEnv/passwordEnv set means the node reads the secret from the "
        "environment and it is in `docker inspect` output instead of encrypted"
    )
    for banned in ("postgresqldb", "mydb", "password", "user"):
        assert banned not in node, (
            f"the config node carries a {banned!r} key. contrib-postgresql 0.16 "
            "reads host/port/database/user/password only through the fields above; "
            "anything else is a key nothing looks at."
        )


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
            # `filenameType: msg` reads `msg.payload` as the path, and the thing
            # driving it is a trigger whose op1 is `"1"` — so the node opens a
            # file literally named `1`, reports nothing downstream, and leaves the
            # tag list empty for the consumer's `?? []` to swallow.
            assert node["filenameType"] == "str", (
                f"{name}: the tag reader is {node['filenameType']!r}, so its "
                f"filename comes from msg.payload. Nothing on this path sets that "
                f"to {build_flows.TAGS_PATH_IN_FLOW!r}, so it reads whatever the "
                f"trigger carries instead. Use 'str'."
            )
            # The not-found case has nowhere to go on a one-output node, so the
            # path being correct is what keeps this silent.
            assert node["sendError"] is False, (
                f"{name}: sendError is true, so a read failure sends a message with "
                "msg.error onto the loader's input. The loader's `?? []` would "
                "still swallow it, but as a different wrong value."
            )
            assert any(node["wires"][0]), (
                f"{name}: nothing watches the tag reader's output, so a missing "
                "file is silent and every unit in the flow becomes blank"
            )

        # **`inject` with `once`, because `trigger` never fires.** The loader was
        # driven by a `trigger` node on the belief that it fires on deploy. It does
        # not: Node-RED 4's `trigger` registers only an `input` handler and a
        # `close` handler, with no startup path, so with `inputs: 1` and nothing
        # wired in, `op1` never executes. It is not a version difference -- there
        # is no version of this node that fires unprompted.
        # So this asserts the mechanism that does work (`20-inject.js:88` arms a
        # `onceDelay` timer on construction), *and* that no `trigger` has crept
        # back, because a `trigger` in this position fails silently and the only
        # symptom is an empty plant.
        triggers = [n for n in named if n["type"] == "trigger"]
        assert not triggers, (
            f"{name}: {triggers[0]['name']!r} is a `trigger` driving the loader. A "
            "trigger does not fire on deploy in any Node-RED version, so the tag "
            "list never loads and every consumer silently reports an empty plant. "
            "Use an inject with once=True."
        )
        starters = [
            n for n in named
            if n["type"] == "inject" and n.get("once")
        ]
        assert starters, (
            f"{name}: nothing fires the tag loader on deploy. Nothing runs in "
            "Node-RED until a message arrives, so without an inject with once=True "
            "the file is never read."
        )
        for node in starters:
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


def test_the_tag_path_is_where_compose_mounts_the_flows(flows: dict) -> None:
    """The path in the flow, resolved against the mount that provides it.

    `TAGS_PATH_IN_FLOW` is an **in-container** path, and nothing in this repository
    checks it against anything. It read `/data/tags.json`, which is where the
    runtime writes `flows.json` — and nothing has ever put `tags.json` there. The
    file is at `/flows/tags.json`, because that is where compose mounts
    `./scada/flows`.

    The failure is the quietest shape this project has: the `file in` node read a
    file that does not exist, sent its error nowhere, and every consumer fell back
    to its `?? []` default. The result is a mimic showing six signals with blank
    units and no normal bands, refreshing every five seconds, with no error
    anywhere — a plant that looks alive and is displaying nothing. It survived
    because a `postgresql` error upstream was louder and hid it.

    So the container path is derived from the mount compose actually declares —
    `FLOWS` is the *host* directory and conflating the two is how `/data/tags.json`
    survived — and the file it names must be the one the generator writes.
    """
    import yaml

    tag_path = Path(build_flows.TAGS_PATH_IN_FLOW)
    assert tag_path.is_absolute(), (
        f"{build_flows.TAGS_PATH_IN_FLOW!r} is not an absolute in-container path"
    )
    assert TAGS.exists(), f"{TAGS} does not exist; run `make scada-flows`"

    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    scada = compose["services"]["scada"]

    # Host directory -> container directory, from the short-form mounts compose uses.
    mounts: dict[str, Path] = {}
    for entry in scada.get("volumes", []):
        parts = str(entry).split(":")
        if len(parts) >= 2 and not parts[0].startswith(("/", "$", "{")):
            mounts[Path(parts[1])] = Path(parts[0])

    host = FLOWS.resolve()
    candidates = [c for c, h in mounts.items() if h.resolve() == host]
    assert candidates, (
        f"compose does not mount {FLOWS} into the scada service, so the flows "
        f"name a file the container cannot see. Mounts: {scada.get('volumes')}"
    )
    target = candidates[0]
    assert target in tag_path.parents, (
        f"the flow reads {build_flows.TAGS_PATH_IN_FLOW!r}, which is not under "
        f"{target}, the container directory compose mounts {FLOWS} at. Nothing "
        f"puts the tag list there."
    )
    assert tag_path.name == TAGS.name, (
        f"the flow reads {tag_path.name!r} but the generator writes {TAGS.name!r}"
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
        for table in _tables_in(query):
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
    mistyped row away.

    **This test was checking that the safety property held in two places where it
    did not hold at all.**

    It asserted `"$msg" in acknowledge_query()` against a function *nothing
    called* — its own comment said "it is not in a flow yet" — while the
    annunciator carried its own copy inline. And it asserted `node["params"]` on
    each query node, a `node-red-contrib-postgresql` **0.x** field that 0.16.2
    removed: binds are read off the *message*, as `msg.params` (positional) or
    `msg.queryParameters` (named). So `$msg` was never bound by anything; it
    reached `pg` as a parameter literally named `msg`, which is exactly the
    injection the placeholder existed to prevent, and the `params` list it claimed
    to declare was read by nobody.

    So it is checked where it is now true, and end to end:

    * no query node carries a `params` list, which 0.16.2 ignores;
    * no statement uses a `$name` placeholder, which 0.16.2 will not bind;
    * a statement that **writes** binds, because a value in it came from a person
      at run time;
    * placeholders are `$1..$n` with no gaps, and the function node upstream
      supplies exactly `n` values — a fourth `$4` fed by three values is the
      unbound-parameter injection this test is named for;
    * a read-only statement may have none, and that is checked rather than
      assumed: the tag ids in the mimic's `WHERE` are generation-time constants
      passed through `_sql_literal`, so binding them would be theatre.
    * the shared `acknowledge_query()` is the statement the annunciator runs, not a
      parallel copy that can drift.
    """
    writes: list[tuple[str, dict]] = []
    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] != "postgresql":
                continue
            assert "params" not in node, (
                f"{name}: {node['name']!r} carries a `params` list. "
                "contrib-postgresql 0.16.2 removed the node-level field; binds "
                "come from msg.params or msg.queryParameters. Keeping it implies "
                "the values are bound when nothing reads the list."
            )
            named = re.search(r"\$[A-Za-z_]\w*", node["query"])
            assert named is None, (
                f"{name}: {node['name']!r} uses a named placeholder "
                f"({named.group(0) if named else ''}). Named binds need "
                "msg.queryParameters; 0.16.2's `named` rewrite cannot "
                "disambiguate a placeholder before a `::` cast, which the "
                "jsonb_build_object arguments require. Use positional $1."
            )
            placeholders = sorted(set(re.findall(r"\$(\d+)", node["query"])),
                                  key=int)
            assert placeholders == [str(i + 1) for i in range(len(placeholders))], (
                f"{name}: {node['name']!r} uses placeholders {placeholders}, which "
                "are not $1..$n with no gaps. A gap means an unbound parameter and "
                "Postgres refuses the statement."
            )

            is_write = bool(re.search(
                r"\b(INSERT|UPDATE|DELETE)\b", node["query"], re.I
            ))
            if not is_write:
                continue

            assert placeholders, (
                f"{name}: {node['name']!r} writes and binds nothing. Every value in "
                "a write reaches it from a person at run time -- an operator's "
                "number, a row they clicked -- so a statement built by "
                "concatenation is a SQL injection one mistyped value away."
            )

            # The node that feeds it must set msg.params, or nothing is bound.
            feeders = [
                n for n in nodes
                if n["type"] == "function"
                and re.search(r"msg\.params\s*=", n.get("func", ""))
            ]
            assert feeders, (
                f"{name}: {node['name']!r} has bind placeholders but no function "
                "node upstream assigns msg.params, so every value is unbound."
            )
            for feeder in feeders:
                literal = re.search(r"msg\.params\s*=\s*\[(.*?)\]\s*;", feeder["func"],
                                    re.S)
                assert literal, (
                    f"{name}: {feeder['name']!r} assigns msg.params but not from a "
                    "literal array; this test can only count a literal one"
                )
                count = len(
                    [p for p in literal.group(1).split(",")
                     if p.strip() and not p.strip().startswith("//")]
                )
                assert count == len(placeholders), (
                    f"{name}: {feeder['name']!r} supplies {count} value(s) for "
                    f"{len(placeholders)} placeholder(s) in {node['name']!r}'s "
                    "statement. Mismatched counts are the injection this test is "
                    "named for."
                )
            writes.append((name, node))

    assert len(writes) == 2, (
        "expected the acknowledgement insert and the control flow's audit trail, "
        f"found {[(n, q['name']) for n, q in writes]}. Every write in these flows "
        "takes a run-time value, so every one of them binds."
    )

    # And the acknowledgement statement is the one the annunciator runs.
    ack = [
        n for n in flows["02-annunciator.json"]
        if n["type"] == "postgresql"
        and re.search(r"\bINSERT\b", n["query"], re.I)
    ]
    assert ack, (
        "the annunciator has no acknowledgement insert. `acknowledge_query()` "
        "must be what the flow runs -- a shared function plus a private inline copy "
        "is how the copy stops being bound."
    )
    assert ack[0]["query"] == build_flows.acknowledge_query()
    assert "$1" in ack[0]["query"]


# ── the credential, and the entrypoint that writes it ─────────────────────────


def test_the_credential_is_keyed_by_the_config_nodes_id(flows: dict) -> None:
    """The flow names a *config node*, and the credential is filed under its id.

    `flows_cred.json` is generated from the environment by
    `scada/nodered/entrypoint.sh` and gitignored. The flow names a database
    connection; the credential holds the user and password; the two are joined at
    run time by the config node's **id**, because that is what
    `credentials.get(id)` is called with
    (`@node-red/runtime/lib/nodes/index.js:98`). A credential filed under any
    other key is present in the file and never read.

    Which means the *ids* have to agree, and a mismatch is the worst failure
    Node-RED has: the node loads, the type resolves, and every query fails with

        TypeError: node.config.pgPool.connect is not a function

    which is an internal, names no database, and is identical for "no such
    credential" and "the credential is broken".

    **This test used to assert `node["mydb"]` appeared in the entrypoint's text.**
    That was the shape `node-red-contrib-postgresql` 0.x wanted — a credential id
    hung directly off each query node — and 0.16.2 removed the field. So the flow
    named a credential, the entrypoint wrote a different shape, the test confirmed
    the two halves of a match that was never used by anything, and the runtime
    logged 77 of those TypeErrors a day.

    What replaced it is not a string search. The entrypoint now *reads the id out
    of the flow it just assembled*, so there is no second place that can invent
    one, and the assertion here is on that mechanism rather than on agreement.
    """
    entrypoint = Path("scada/nodered/entrypoint.sh").read_text(encoding="utf-8")

    assert "postgreSQLConfig" in entrypoint, (
        "the entrypoint no longer mentions the config node type, so it cannot find "
        "the node to file the credential under"
    )
    assert "flows.json" in entrypoint, (
        "the entrypoint must read the assembled flow to learn the config node's id. "
        "Hardcoding the id here would mean two places inventing it."
    )
    # It must not hardcode a derived id back into the shell script.
    for node in (n for f in flows.values() for n in f):
        if node["type"] in build_flows.CONFIG_NODE_TYPES:
            assert node["id"] not in entrypoint, (
                f"{node['id']} appears literally in entrypoint.sh. It is derived by "
                "scada/build_flows.py; a second copy here is how they drift."
            )
    # And no query node may carry a credential id directly: 0.16.2 has no such field.
    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] == "postgresql":
                assert "mydb" not in node, (
                    f"{name}: {node['name']!r} still carries `mydb`, a field "
                    "contrib-postgresql 0.16.2 removed"
                )


def test_the_entrypoint_refuses_to_start_without_the_config_node() -> None:
    """No `postgreSQLConfig` node means there is nowhere to file the credential.

    The entrypoint exits non-zero with a message that names the problem. It must
    not fall back to writing a credential under some other key: that produces a
    file that exists, parses, and is never read — which is the failure this whole
    section is about, arrived at silently.
    """
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        flows = Path(tmp) / "flows"
        data.mkdir()
        flows.mkdir()
        # A valid flow with a tab and a query node, but no config node.
        (flows / "01-test.json").write_text(
            json.dumps([
                {"id": "a1", "type": "tab", "name": "t", "label": "t", "wires": []},
                {"id": "a2", "type": "postgresql", "z": "a1", "name": "q",
                 "query": "SELECT 1", "postgreSQLConfig": "missing",
                 "wires": [[]]},
            ]),
            encoding="utf-8",
        )
        env = {
            "NR_DATA_DIR": str(data), "NR_FLOWS_DIR": str(flows),
            "POSTGRES_PASSWORD": "x", "POSTGRES_USER": "u",
            "POSTGRES_HOST": "h", "POSTGRES_DB": "d",
        }
        result = subprocess.run(
            [env_cmd("sh"), "scada/nodered/entrypoint.sh", env_cmd("true")],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0, (
            "the entrypoint started with a flow that has no postgreSQLConfig node. "
            "It must refuse, because there is no node id to key the credential by."
        )
        assert "postgreSQLConfig" in result.stderr, (
            f"the refusal must name what is missing; got: {result.stderr!r}"
        )
        assert not (data / "flows_cred.json").exists(), (
            "a credential file was written anyway, keyed by nothing"
        )


def _minimal_flows(dirpath) -> None:
    """A directory with one valid flow, for the tests that only care about
    credentials.

    The entrypoint assembles flows *before* it touches the credential, because a
    runtime with no flows and a runtime with no credential are both broken and the
    flow one is louder. Which means every credential test now has to provide a
    flows directory, and the three that did not were failing on
    `no flows found` before they ever reached the thing they were about.

    **It has to contain a `postgreSQLConfig` node.** The entrypoint files the
    credential under that node's id, so a flow without one is refused at startup —
    correctly, and by `test_the_entrypoint_refuses_to_start_without_the_config_
    node`. Every other test that hands this fixture to the entrypoint needs a
    *valid* flow, not a rejected one, or it fails on the refusal instead of on
    what it is about.
    """
    flows = Path(dirpath)
    flows.mkdir(parents=True, exist_ok=True)
    (flows / "01-test.json").write_text(
        json.dumps([
            {"id": "a1", "type": "tab", "name": "t", "label": "t", "wires": []},
            {
                "id": "a0", "type": "postgreSQLConfig", "name": "wwtp-postgres",
                "host": "POSTGRES_HOST", "hostFieldType": "env",
                "port": "POSTGRES_PORT", "portFieldType": "env",
                "database": "POSTGRES_DB", "databaseFieldType": "env",
                "userFieldType": "cred", "passwordFieldType": "cred",
            },
        ]),
        encoding="utf-8",
    )
    return flows


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
    """The credential file is encrypted in the format Node-RED actually reads.

    A password containing a quote or a backslash would otherwise produce a file
    Node-RED cannot parse, and the error is a JSON parse failure in a log line
    about a file the reader did not know existed. So the escaping is exercised
    here rather than in production.

    **And the round trip is the real one.** This test used to read the password
    back out of a plaintext object keyed by the credential secret, which pinned
    the *broken* format — the file was not in a shape Node-RED reads at all.
    Node-RED derives `sha256(credentialSecret)` as the AES key and stores
    everything under a single literal `$` key whose value is a 16-byte IV in hex
    followed by AES-256-CTR ciphertext in base64. Get the key *name* wrong and it
    does not fail: it logs `Encrypted credentials not found`, adopts the raw file
    as its credential cache, and every lookup misses, which surfaces nodes later as

        TypeError: node.config.pgPool.connect is not a function

    So the assertions below decrypt the file exactly as `credentials.js` does, in
    `node` — which is what writes it. Asserting the shape instead of the round
    trip would only have caught the next way of getting this wrong.
    """
    script = Path("scada/nodered/entrypoint.sh")
    decrypt = (
        'const c=require("crypto"),f=require("fs");'
        'const s=f.readFileSync(process.argv[1],"utf8").trim();'
        'const p=JSON.parse(f.readFileSync(process.argv[2],"utf8"));'
        'const k=c.createHash("sha256").update(s).digest();'
        'const iv=Buffer.from(p["$"].substring(0,32),"hex");'
        'const d=c.createDecipheriv("aes-256-ctr",k,iv);'
        'process.stdout.write('
        'd.update(p["$"].substring(32),"base64","utf8")+d.final("utf8"));'
    )
    for password in ("simple", 'has "quotes"', "back\\slash", "both \" and '", "!"):
        with tempfile.TemporaryDirectory() as tmp:
            _minimal_flows(Path(tmp) / "flows")
            env = {
                "NR_DATA_DIR": tmp,
                "NR_FLOWS_DIR": str(Path(tmp) / "flows"),
                "POSTGRES_PASSWORD": password,
                "POSTGRES_HOST": "db",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": "wwtp",
                "POSTGRES_USER": "wwtp",
                "MODBUS_HOST": "softplc",
                "MODBUS_PORT": "5020",
            }
            result = subprocess.run(
                [env_cmd("sh"), str(script), env_cmd("true")],
                env=env, capture_output=True, text=True, check=False,
            )
            assert result.returncode == 0, result.stderr

            secret_file = Path(tmp) / ".credentialSecret"
            cred_file = Path(tmp) / "flows_cred.json"

            # The shape Node-RED looks for, and nothing else.
            payload = json.loads(cred_file.read_text(encoding="utf-8"))
            assert list(payload) == ["$"], (
                f"expected a single `$` key, got {list(payload)}; Node-RED derives "
                "its own key name from the secret and will not find the credentials "
                "under anything else"
            )

            # And the key itself: one per run, kept, so a restart can read it.
            assert secret_file.exists()
            secret = secret_file.read_text(encoding="utf-8").strip()
            assert len(secret) == 64, secret

            decrypted = subprocess.run(
                [env_cmd("node"), "-e", decrypt, str(secret_file), str(cred_file)],
                capture_output=True, text=True, check=True,
            ).stdout
            creds = json.loads(decrypted)

            # **Keyed by the config node's id, and holding only the two things a
            # flow file cannot.** The host, port and database are not credentials —
            # they are `env`-typed fields on the config node, read from the
            # environment — so putting them here wrote the address into a file
            # whose whole purpose is not to, and the test asserted it as if it
            # were the design.
            assembled = json.loads(
                (Path(tmp) / "flows.json").read_text(encoding="utf-8")
            )
            config_ids = [
                n["id"] for n in assembled if n["type"] == "postgreSQLConfig"
            ]
            assert len(config_ids) == 1, config_ids
            assert list(creds) == config_ids, (
                f"the credential is keyed by {list(creds)} but the config node is "
                f"{config_ids}. `credentials.get(id)` is called with the *node* id, "
                "so a credential under any other key is present and never read."
            )
            assert creds[config_ids[0]]["password"] == password, (
                f"password {password!r} did not survive the round trip"
            )
            assert creds[config_ids[0]]["user"] == "wwtp"
            assert set(creds[config_ids[0]]) == {"user", "password"}, (
                f"the credential holds {sorted(creds[config_ids[0]])}; contrib "
                "declares exactly user and password, and the address belongs on the "
                "config node as an env-typed field"
            )
            # And the password is nowhere in the flow file itself.
            assert password not in (Path(tmp) / "flows.json").read_text(
                encoding="utf-8"
            )

    # A credential is a credential whether or not it happens to be encrypted.
    with tempfile.TemporaryDirectory() as tmp:
        _minimal_flows(Path(tmp) / "flows")
        subprocess.run(
            [env_cmd("sh"), str(script), "true"],
            env={
                "NR_DATA_DIR": tmp,
                "NR_FLOWS_DIR": str(Path(tmp) / "flows"),
                "POSTGRES_PASSWORD": "simple",
                "POSTGRES_USER": "wwtp",
            },
            capture_output=True, text=True, check=True,
        )
        for name in ("flows_cred.json", ".credentialSecret"):
            mode = (Path(tmp) / name).stat().st_mode & 0o777
            assert mode == 0o600, f"{name} is {oct(mode)}, expected 0o600"


def test_the_entrypoint_never_overwrites_an_existing_credential() -> None:
    """A credential entered in the editor must survive a restart.

    Otherwise every `docker compose up` re-derives it from the environment and
    silently discards whatever an operator had configured — which is how a
    database connection stops working after a rebuild and nobody knows why.
    """
    script = Path("scada/nodered/entrypoint.sh")
    with tempfile.TemporaryDirectory() as tmp:
        _minimal_flows(Path(tmp) / "flows")
        existing = Path(tmp) / "flows_cred.json"
        existing.write_text('{"hand-written": {}}', encoding="utf-8")
        env = {
            "NR_DATA_DIR": tmp, "NR_FLOWS_DIR": str(Path(tmp) / "flows"),
            "POSTGRES_PASSWORD": "ignored",
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
        _minimal_flows(Path(tmp) / "flows")
        result = subprocess.run(
            [env_cmd("sh"), str(script), "true"],
            env={"NR_DATA_DIR": tmp, "NR_FLOWS_DIR": str(Path(tmp) / "flows")},
            capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0
        assert "POSTGRES_PASSWORD" in result.stderr, result.stderr


# ── the flow SQL, executed ───────────────────────────────────────────────────


def _to_pyformat(query: str, names: list[str]) -> tuple[str, list[str]]:
    """Rewrite Node-RED's ``$name`` placeholders into psycopg's ``%s``.

    `node-red-contrib-postgresql` takes named bind parameters and rewrites them
    before sending. The query in the flow file is therefore in **Node-RED's own
    dialect**, and that dialect is spoken by nothing else:

    * ``psql`` and libpq understand ``$1``, the server-side prepared-statement
      syntax, not ``$name``;
    * **psycopg3 does not understand ``$1`` at all.** It uses pyformat — ``%s`` or
      ``%(name)s`` — and given ``$1`` with a parameter it says
      ``the query has 0 placeholders but 1 parameters were passed``, which is a
      message about the *driver*, not about the query, and sends you looking in
      the wrong place.

    So there is **exactly one implementation in the world that can run these four
    queries**, it is a node inside a container, and it reports failures as a red
    box. That is a real risk and it is the reason this translation exists at all:
    the queries are now executed on every test run, by a second implementation,
    which is the only thing that makes "it runs" a claim rather than a hope.

    Rewritten to ``%s`` rather than ``%(name)s`` because the order of the values
    is what the node preserves and what the test has to match; the names are
    carried alongside so a mismatch between the two is visible.
    """
    out = query
    order: list[str] = []
    for name in names:
        if f"${name}" in out and name not in order:
            order.append(name)
    for name in order:
        out = out.replace(f"${name}", "%s")
    return out, order


def test_every_named_placeholder_is_declared_in_the_params_list(flows: dict) -> None:
    """A placeholder with no matching entry in `params` is a runtime error.

    Node-RED sends the values in `params` and the placeholders in `query`; if one
    side names something the other does not, the query is rejected at run time
    and the panel shows nothing.
    """
    import re

    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] != "postgresql":
                continue
            declared = {p["name"] for p in node.get("params", [])}
            used = set(re.findall(r"\$([a-z_]+)", node["query"]))
            assert used <= declared, (
                f"{name}: {node['name']!r} uses {sorted(used - declared)}, which "
                f"is not in params {sorted(declared)}"
            )
            assert declared == used, (
                f"{name}: {node['name']!r} declares {sorted(declared - used)}, "
                "which the query never uses"
            )


@pytest.mark.integration
def test_every_flow_query_runs_against_a_live_database(flows: dict) -> None:
    """The check that cannot be done statically, and the one that matters.

    Every `postgresql` node's query is executed with its placeholders bound, and
    **every statement is rolled back** — including the two `INSERT`s, which would
    otherwise leave a probe acknowledgement and a probe setpoint in the event log
    of a running plant.

    The rollback is not defensive. `tools/check_sql.py` and the integration suite
    have both destroyed real data in this project's history by running SQL from a
    directory, and the discipline that came out of it is that a program which runs
    SQL it did not author must not leave anything behind.
    """
    from storage.postgres.schema import connect

    # Skip cleanly when there is no database, rather than fail.
    #
    # It carries `@pytest.mark.integration` and the `unit` make target
    # deselects that marker — but a test that needs a database and reports a
    # connection error as a *failure* is a test that fails on every machine
    # without one, and a test suite with a permanently red test is a test suite
    # people stop reading. Skipping is the honest answer: nothing was verified.
    try:
        with connect() as probe:
            probe.rollback()
    except Exception as exc:
        pytest.skip(f"no database: {exc}")

    ok = total = 0
    for name, nodes in flows.items():
        for node in nodes:
            if node["type"] != "postgresql":
                continue
            names = [p["name"] for p in node.get("params", [])]
            sql, order = _to_pyformat(node["query"], names)
            total += 1
            # Values that satisfy the foreign keys. A probe row with a signal id
            # that does not exist would fail on the constraint and be reported as
            # a query bug, which is the wrong lesson.
            values = [
                {"msg": f"probe from {name}", "signal": "AERATION:AHU-1:DO",
                 "tag": "AERATION:AHU-1:DO", "value": 1.0, "rule": "probe_rule",
                 "range": "[0.5, 6.0]"}
            ]
            params = [values[0][n] for n in order]
            try:
                with connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, params) if order else cur.execute(sql)
                        cur.fetchall()
                    conn.rollback()
                ok += 1
            except Exception as exc:
                pytest.fail(
                    f"{name} / {node['name']!r} does not run: {exc}\n\n{sql}"
                )
    assert total > 0, "no postgresql nodes found; the flow files are not parsing"
    assert ok == total


# ── assembling three files into one runtime ──────────────────────────────────


def test_the_three_flows_assemble_into_one_json_document() -> None:
    """Three generated files, one `flows.json`, and it has to be valid JSON.

    Node-RED loads exactly one flow file. The generator writes three — one per
    flow, because they are built by three independent functions and importing
    one should not import the others — and the runtime needs them as three *tabs*
    in a single file, which is also what makes their `global` context shared.

    It cannot be `cat`. The three files are JSON *arrays*, and three arrays
    concatenated is not a JSON document. `scada/nodered/entrypoint.sh` does it
    with `sed` and the result is asserted here, because a `sed` pipeline that
    quietly produces invalid JSON gives a runtime that starts cleanly and deploys
    nothing — the same shape as the "it came up" failure everywhere else in this
    project.
    """
    import json
    import subprocess
    import tempfile

    assembled: dict[str, list] = {}
    with tempfile.TemporaryDirectory() as tmp:
        flows = Path(tmp) / "flows"
        data = Path(tmp) / "data"
        flows.mkdir()
        data.mkdir()
        for name in build_all_names():
            (flows / name).write_text(
                Path(build_flows.FLOWS_DIR / name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        # `tags.json` must be *excluded*: it is a tag list, not a flow, and
        # concatenating it would produce a document that is valid JSON and means
        # nothing.
        (flows / "tags.json").write_text('{"tags": []}', encoding="utf-8")

        env = {"NR_DATA_DIR": str(data), "NR_FLOWS_DIR": str(flows),
               "POSTGRES_PASSWORD": "x", "POSTGRES_USER": "u",
               "POSTGRES_HOST": "h", "POSTGRES_DB": "d"}
        result = subprocess.run(
            [env_cmd("sh"), "scada/nodered/entrypoint.sh", "true"],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "assembled" in result.stdout, result.stdout
        assembled = json.loads((data / "flows.json").read_text(encoding="utf-8"))

    # Every node from every flow, and the tabs among them.
    expected = sum(
        len(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(build_flows.FLOWS_DIR.glob("0*.json"))
    )
    assert len(assembled) == expected
    assert sum(1 for n in assembled if n["type"] == "tab") == 3
    assert not any(
        n.get("type") == "tags" for n in assembled
    ), "tags.json was concatenated into the flow document"


def test_the_assembly_fails_loudly_with_no_flows() -> None:
    """A runtime with no flows starts cleanly and shows an empty editor.

    Which looks exactly like a working Node-RED with nothing deployed, and is the
    fifth variant of "it came up" in this project. The entrypoint has to say so.
    """
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env = {"NR_DATA_DIR": f"{tmp}/data", "NR_FLOWS_DIR": f"{tmp}/flows",
               "POSTGRES_PASSWORD": "x", "POSTGRES_USER": "u",
               "POSTGRES_HOST": "h", "POSTGRES_DB": "d"}
        Path(f"{tmp}/flows").mkdir()
        Path(f"{tmp}/data").mkdir()
        result = subprocess.run(
            [env_cmd("sh"), "scada/nodered/entrypoint.sh", "true"],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0
        assert "no flows found" in result.stderr, result.stderr


def test_the_scada_service_itself_is_valid() -> None:
    """`docker compose config`, and the two things that broke this service before.

    First arrangement: a named volume for the credentials nested inside a
    read-only bind mount, which Docker refuses with

        mkdirat .../data/credentials: read-only file system

    and the container never starts. Found by running it, from the CI file.
    """
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("no docker")

    # **Every variable compose interpolates, not just this one.** The fallback
    # below covers `POSTGRES_PASSWORD` and nothing else, so on a machine with no
    # `.env` the check failed on the *next* required variable:
    #
    #     error while interpolating services.gateway.environment.… is missing
    #
    # which says nothing about the volume arrangement this test is about. The
    # same situation is a skip in
    # `test_readme_claims.py::test_the_compose_file_is_valid`, and one of the two
    # shapes is wrong.
    #
    # So the check is skipped when the environment cannot answer it, and what it
    # actually asserts — the read-only bind at `/flows` and the credential volume
    # nested inside the writable one — is read from `compose.yaml` below and does
    # not need a container at all. `docker compose config` confirms the file is
    # *loadable*; it was never the thing that catches this.
    env = {**os.environ}
    required = (
        "POSTGRES_PASSWORD",
        "GATEWAY_DB_PASSWORD",
        "WEB_DB_PASSWORD",
        "GRAFANA_ADMIN_PASSWORD",
    )
    missing = [name for name in required if not env.get(name)]
    if missing:
        pytest.skip(
            f"compose cannot be interpolated without {', '.join(missing)}; "
            "these come from .env, which is gitignored"
        )
    result = subprocess.run(
        ["docker", "compose", "--profile", "scada", "config", "-q"],
        capture_output=True, text=True, check=False, env=env,
    )
    assert result.returncode == 0, result.stderr

    compose = Path("compose.yaml").read_text(encoding="utf-8")
    assert "./scada/flows:/flows:ro" in compose, (
        "the flows are mounted read-only at /flows; a volume at /data would "
        "shadow the image's settings.js and every setting in it would be ignored"
    )
    assert "scada-credentials" not in compose.replace(
        "scada-credentials/_data", ""
    ), (
        "a credentials volume nested inside the read-only /data mount is the "
        "arrangement that does not start"
    )


def test_the_assembly_replaces_a_root_owned_flows_file() -> None:
    """The base image ships `/data/flows.json` owned by root, and the container
    runs as `node-red`.

    So `writeFileSync('/data/flows.json', …)` fails with

        Error: EACCES: permission denied, open '/data/flows.json'

    while writing a temporary and renaming over it succeeds — the *directory* is
    owned by `node-red`, so creating and replacing are allowed even though
    opening the shipped file for writing is not.

    The symptom without the rename is the worst one available: the runtime starts
    healthy, serves the base image's own example flow, and logs nothing. A
    generated file that cannot replace the generated file is invisible.
    """
    import subprocess
    import tempfile

    script = Path("scada/nodered/entrypoint.sh")
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        flows = Path(tmp) / "flows"
        data.mkdir()
        _minimal_flows(flows)

        # A pre-existing file the current user cannot write, which is exactly the
        # base image's arrangement.
        existing = data / "flows.json"
        existing.write_text('[{"id": "x", "type": "comment"}]', encoding="utf-8")
        existing.chmod(0o444)

        env = {
            "NR_DATA_DIR": str(data), "NR_FLOWS_DIR": str(flows),
            "POSTGRES_PASSWORD": "x", "POSTGRES_USER": "u",
            "POSTGRES_HOST": "h", "POSTGRES_DB": "d",
        }
        result = subprocess.run(
            [env_cmd("sh"), str(script), "true"],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        nodes = json.loads(existing.read_text(encoding="utf-8"))
        assert nodes and nodes[0].get("name") != "x", (
            "the shipped example flow is still in place; the replace did not "
            "happen and the runtime would serve the wrong flows"
        )
