"""The address-space serialiser, and the drift gate built on it.

    python -m tools.opcua_address_space            # write
    python -m tools.opcua_address_space --check     # report drift (CI)

`courses/opcua/08-generated.md` counts about fourteen decisions inside
`build_address_space` and `_add_signal` and finds three that are wrong plus a
fourth that is wrong whenever nothing drives the plant. None of them was found by
reading the generator, because a pull request changing it shows a diff of Python
plumbing and all 686 consequences are invisible in that diff.

These tests are the other half of the answer. They assert the serialised form
**records the things a reviewer needs**, so that the next change to one of those
decisions shows up as a changed row — and that the file's own numbers cannot go
stale the way the prose did.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "contracts" / "address-space.json"
sys.path.insert(0, str(ROOT / "tools"))

from opcua_address_space import _first_difference, build  # noqa: E402


def test_the_serialised_address_space_is_committed() -> None:
    """It has to be a file, or there is nothing to diff and nothing to review."""
    assert OUT.exists(), (
        f"{OUT.name} does not exist. This file is the review surface for 686 "
        f"generated nodes; without it the address space is the only generated "
        f"artefact in the project with no artefact."
    )
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    for key in ("nodes", "signals", "equipment", "counts", "namespace_index"):
        assert key in doc, f"the serialised space has no {key!r}"


def test_the_file_is_in_step_with_the_server() -> None:
    """`--check` must pass on a clean tree, or CI is noise."""
    r = subprocess.run(
        [sys.executable, "-m", "tools.opcua_address_space", "--check"],
        capture_output=True, text=True, check=False, cwd=ROOT)
    assert r.returncode == 0, (
        f"{OUT.name} is out of step with the address space the server builds:\n"
        f"{r.stdout[-800:]}\n{r.stderr[-800:]}"
    )


def test_the_counts_match_what_the_course_measured() -> None:
    """686 nodes, 57 signals, 22 equipment — lesson 08's figures.

    Asserted because lesson 08 teaches these numbers, and a lesson that states a
    count is a measurement. The per-area table in `docs/TESTING.md` is guarded the
    same way.
    """
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    assert doc["counts"]["nodes"] == 686, (
        f"the serialised space has {doc['counts']['nodes']} nodes; lesson 08 says "
        f"686. Either the address space changed or the lesson is stale."
    )
    assert doc["counts"]["signals"] == 57
    assert doc["counts"]["equipment"] == 22
    assert len(doc["nodes"]) == doc["counts"]["nodes"]
    assert len(doc["signals"]) == doc["counts"]["signals"]


def test_every_node_records_enough_to_review_a_change() -> None:
    """The fields that make a diff readable.

    `data_type` only on Variables, because a Folder has no such attribute and
    asking for one raises `BadAttributeIdInvalid` — which is the correct answer
    from the server, and aborted the whole walk the first time this ran.
    """
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    for node in doc["nodes"]:
        for key in ("path", "browse_name", "node_class", "reference"):
            assert key in node, f"a node has no {key!r}: {node}"
        if node["node_class"] == "Variable":
            assert "data_type" in node, f"a Variable has no data_type: {node}"
        else:
            assert "data_type" not in node, (
                f"a {node['node_class']} should not carry a data_type: {node}"
            )


def test_every_signal_records_the_value_it_was_constructed_from() -> None:
    """The decision nobody reviewed, written down as a value.

    `normal_low` is already in `contracts/tags.yaml` and already visible in a
    YAML diff. Seeing that it is *used as the node's initial value* is not — and
    that is lesson 03's worst finding, invisible until it was a row in a table.
    """
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    for sig in doc["signals"]:
        made = sig.get("constructed_from")
        assert made, f"{sig['signal_id']} does not record how it was constructed"
        assert made["field"] == "normal_low", (
            f"{sig['signal_id']} is now constructed from {made['field']!r}. If that "
            f"is an improvement, lesson 03 and the course README's finding 5 are "
            f"stale and should say so."
        )
    # And the specific number lesson 03 quotes, so the file cannot drift from it.
    do = next(s for s in doc["signals"] if s["signal_id"] == "AERATION:AHU-1:DO")
    assert do["constructed_from"]["value"] == 1.5, (
        "DO is no longer constructed at 1.5 mg/L; lesson 03's table needs "
        "re-measuring"
    )


def test_the_all_double_finding_is_visible_in_the_file() -> None:
    """Lesson 02's finding, as a row rather than a claim.

    All 57 signals being `Double` is the wart lesson 02 is about. Serialising it
    means a fix — `_variant_type` returning `Int32` for `{Boolean}`, say — shows
    up as one changed row, which is the entire argument for this file.
    """
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    types = {s["data_type"] for s in doc["signals"]}
    assert types == {"Double"}, (
        f"the address space now publishes {sorted(types)}, not just Double. "
        f"If that is lesson 02's fix landing, update the lesson — it currently "
        f"teaches that every value is a Double."
    )
    storm = next(s for s in doc["signals"]
                 if s["signal_id"] == "SITE:WEATHER:STORM")
    assert storm["unit_symbol"] == "{Boolean}"
    assert storm["data_type"] == "Double", (
        "the storm flag is no longer a Double, so lesson 02's 'the type system "
        "lies' example has changed"
    )


def test_the_two_writable_signals_are_the_two_writable_signals() -> None:
    """Lesson 05's write surface, recorded.

    Two of 57. If that changes, lesson 05's "2 of 57 signals are writable" and
    the `SECURITY.md` gap both need revisiting — and now this file says so on
    every change, because the set is a set of rows.
    """
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    writable = sorted(s["signal_id"] for s in doc["signals"] if s["writable"])
    assert writable == ["AERATION:AHU-1:SETPOINT_DO", "SITE:WEATHER:STORM"], (
        f"the write surface is now {writable}; lesson 05 says it is two signals"
    )


def test_equipment_run_state_is_recorded_as_constructed_at_zero() -> None:
    """Lesson 01's closing example, as a row.

    `RunState` is built as `0` and nothing stages it until the process model
    runs, so a bare server shows 22 stopped motors. That is only visible as a
    value if somebody writes it down.
    """
    doc = json.loads(OUT.read_text(encoding="utf-8"))
    assert len(doc["equipment"]) == 22
    for eq in doc["equipment"]:
        assert eq["run_state_constructed_as"] == 0, (
            f"{eq['equipment_id']} is now constructed as "
            f"{eq['run_state_constructed_as']}; lesson 01's RunState example has "
            f"changed"
        )


def test_the_drift_message_names_the_line_and_both_values() -> None:
    """"The files differ" is not a review aid on a 686-node file.

    The whole value of this gate is telling a reviewer which decision moved, so
    the message has to point at the line and quote both sides. A generic "differs"
    would push the work back onto whoever the gate was built to help.
    """
    a = "one\ntwo\nthree\n"
    b = "one\nTWO\nthree\n"
    where = _first_difference(a, b)
    assert "line 2" in where, where
    assert "two" in where and "TWO" in where, where

    # length differences name which file is longer rather than a line that is not
    longer = _first_difference("one\ntwo\n", "one\n")
    assert "longer" in longer and "committed" in longer, longer
    shorter = _first_difference("one\n", "one\ntwo\n")
    assert "longer" in shorter and "built" in shorter, shorter


def test_every_published_port_binds_loopback_by_default() -> None:
    """Lesson 07's first recommendation, asserted on all five ports.

    `4840:4840` means *every* host interface, not loopback — and `0.0.0.0`
    appeared in no markdown file in the repository, so "anyone who can reach the
    port" was unresolvable by reading the project. Every published port now goes
    through `${HOST_BIND:-127.0.0.1}`, which is a default rather than a policy:
    safe unless somebody opts out, and the opt-out is documented in `.env.example`
    next to a warning about what it exposes.

    A test rather than a review note, because the failure mode is a compose file
    that parses perfectly and publishes an unauthenticated plant to a network.
    """
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    published = re.findall(r'^\s+- "(\$\{[^"]+\}:\d+)"', text, re.MULTILINE)
    assert published, "no published ports found — the pattern is stale"
    for mapping in published:
        assert mapping.startswith("${HOST_BIND:-127.0.0.1}:"), (
            f"published port {mapping!r} does not bind loopback by default. The "
            f"Omit-the-prefix form binds every interface."
        )
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "HOST_BIND=127.0.0.1" in env, (
        ".env.example must document the opt-out, or nobody can find it"
    )
    assert "0.0.0.0 only on a network you trust" in env, (
        "the opt-out needs its warning next to it. Someone who sets HOST_BIND "
        "without reading SECURITY.md should still be told what it exposes."
    )


@pytest.mark.slow
def test_a_changed_decision_is_caught_by_the_gate() -> None:
    """The end-to-end proof: mutate a decision, and `--check` must fail.

    Everything else in this file asserts the serialised form is *correct*. This
    asserts it is *load-bearing* — that a change to `_variant_type` produces a
    drift failure rather than a silently different address space. Marked `slow`
    because it starts a server and rewrites a source file; the restore is in a
    `finally` so a failure cannot leave the tree modified.
    """
    src = ROOT / "softplc" / "servers" / "opcua.py"
    original = src.read_text(encoding="utf-8")
    try:
        src.write_text(
            original.replace("return ua.VariantType.Double",
                             "return ua.VariantType.Int32"),
            encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-m", "tools.opcua_address_space", "--check"],
            capture_output=True, text=True, check=False, cwd=ROOT)
        assert r.returncode == 1, (
            "the gate passed with a changed variant type, so it is not watching "
            "the thing it exists to watch"
        )
        # The message goes through `logging`, so it is on stderr, not stdout.
        said = r.stdout + r.stderr
        assert "data_type" in said, (
            f"the drift message does not mention the field that changed:\n"
            f"{said[-500:]}"
        )
        assert "Double" in said and "Int32" in said, (
            f"the drift message does not quote both sides, so a reviewer has to "
            f"diff it by hand:\n{said[-500:]}"
        )
    finally:
        src.write_text(original, encoding="utf-8")

    r = subprocess.run(
        [sys.executable, "-m", "tools.opcua_address_space", "--check"],
        capture_output=True, text=True, check=False, cwd=ROOT)
    assert r.returncode == 0, "the restore did not put the tree back"


def test_build_is_deterministic() -> None:
    """Two runs, byte-identical.

    Without this the file is a nuisance rather than a gate: every run would
    produce a diff and nobody would read it. Node order is tree order, which is
    derived from the contract, so it must not vary between runs.
    """
    first = asyncio.run(build())
    second = asyncio.run(build())
    assert first == second, (
        "two runs of the serialiser differ, so the file would show drift every "
        "time. Find the source of the non-determinism before trusting the gate."
    )
