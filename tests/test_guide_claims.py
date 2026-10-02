"""The gaps in `docs/GUIDE.md` that no other test covers.

**This file was 19 KB and is now 6.** It asserted a dozen claims about the code
by grepping the source for strings, and almost all of them were a worse version
of a test that already worked. `test_modbus_writeback.py` proves the copy trap
by *running* it and watching `duty_pct` move; `test_opcua_course.py:364-395`
already asserts `write_value` has no callers;
`test_a_multi_register_write_uses_the_multi_register_function_code` already
covers the FC6 table against the actual flows. Re-asserting any of those by
matching text is strictly worse: a regex on a source line breaks when somebody
reformats, so it punishes an innocent edit and passes through a semantic one.

So the rule this file applies is narrow on purpose:

* **Enumerate facts that rot silently** — which registers are `little`, which
  architectural boundary holds, whether every link resolves.
* **Leave mechanism prose alone.** "AerationControl copies the setpoint by
  value" is a claim about the code. If the code changes, the guide needs
  *rewriting*, and a failing documentation test is a confusing way to say so.

What is left is what nothing else was watching, and the largest of those is the
import boundary — see `test_the_process_model_still_knows_nothing_about_the_wire`
for why it is worth having at all.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from softplc.contract import contract

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "GUIDE.md"


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


# ── the architectural boundary, which nothing else guards ───────────────────


def test_the_process_model_still_knows_nothing_about_the_wire() -> None:
    """`softplc/process/` must not *import* its way toward the outside world.

    This is the load-bearing claim of the guide — that the plant model is the
    bottom of the stack, pure arithmetic, and that every protocol concern lives
    downstream of it — and **nothing in the repository was enforcing it.**

    That matters because the claim is easy to erode with a change that looks
    harmless and passes every existing test. Someone needs one engineering value
    published that the snapshot does not carry, and writes
    `from softplc.servers.modbus import encode_float32` inside `units.py`. It
    works. It is shorter than threading the value through `snapshot()`. Every
    test still passes.

    And now the model can only be tested in a context that has the server
    importable, the guarantee that a write must pass through the seam is gone,
    and the architecture the guide teaches is a claim rather than a fact.

    **Checked with `ast`, not with `grep`, and the difference is the whole test.**
    A text search for `"ModbusTcpServer"` matches `plant.py:65`, a docstring
    that *describes* the snapshot being handed to the servers. That is the model
    correctly describing its own boundary, and a grep flags it as a violation of
    it. So the first version of this test failed on a comment — which is how a
    boundary check gets deleted rather than fixed, because a check that cries
    wolf about the module's own docstring is not trusted with anything else.
    """
    banned_roots = (
        "pymodbus", "asyncua", "psycopg", "asyncpg", "sqlalchemy",
        "softplc.servers", "gateway", "storage", "ui", "scada",
    )

    offenders: list[str] = []
    for path in sorted((ROOT / "softplc" / "process").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [node.module or ""]
            else:
                continue
            for name in roots:
                if any(name == r or name.startswith(f"{r}.") for r in banned_roots):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} imports {name}"
                    )

    assert not offenders, (
        f"the process model now imports outside itself: {offenders}. "
        f"docs/GUIDE.md is built on the claim that it is pure arithmetic with "
        f"no knowledge of how it is published. If an import is genuinely "
        f"needed, the architecture changed and GUIDE.md, DESIGN.md and "
        f"DATA-FLOW.md need revisiting together -- not silently."
    )


# ── facts that rot silently ─────────────────────────────────────────────────


def test_the_two_little_word_order_registers_are_still_the_two() -> None:
    """The guide names both, so both must still be little.

    Reading a `little` register with `big` order yields a believable small
    number rather than an error, which is why the guide says *"exactly two"*
    and names them. If a third register joins, the count is wrong and a reader
    could read the wrong one of forty.

    `test_modbus.py` already proves word order matters in general; this pins
    which registers are exposed to it.
    """
    little = {
        r.name for r in contract().registers if r.word_order == "little"
    }
    assert little == {"AERATION_BLOWER_VALVE", "AERATION_WASTE_RATE"}, (
        f"the little-word-order registers are now {sorted(little)}; "
        f"docs/GUIDE.md says AERATION_BLOWER_VALVE and AERATION_WASTE_RATE. A "
        f"new one is a new trap and belongs in the guide and in the QA plan."
    )
    # And the guide must actually name them, or it is describing a shape the
    # reader cannot use.
    for name in little:
        assert name in _guide(), (
            f"{name} is little word order but the guide does not name it"
        )


def test_the_guides_writable_surface_claim_is_still_one_signal() -> None:
    """The guide opens on *"the only writable signal in the entire system"*.

    That is the framing sentence for the whole document — it is why the tour
    follows one value rather than several. If a second writable signal is added,
    the guide's opening is wrong, and the honest response is either a second
    chapter or an admission that the tour is now partial.

    **Checked on the signal flags, not on `contract().writable`.** Those are
    different things and the first version of this test used the wrong one.
    `Contract.writable` is built from the top-level `writable:` list in the YAML
    (`contract.py:680`), which is the *declared surface*; the per-signal
    `writable: true` flag is what `generate_tags`, the OPC UA address space and
    every consumer actually read.

    The difference is not academic. Adding `writable: true` to a signal leaves
    `contract().writable` untouched, so the first version passed with a second
    writable signal in the system — it was asserting that the declaration had
    not changed while ignoring the flag the rest of the codebase follows. Both
    are checked, because they can and do drift: the contract loader has a
    validator for a writable register whose signal is *not* writable, and the
    reverse — a writable signal with no register — is exactly the shape
    `FAULT_CODE` and `STORM_FLAG` had until this work.
    """
    c = contract()

    flagged = {sid for sid, sig in c.signals.items() if sig.writable}
    assert flagged == {"AERATION:AHU-1:SETPOINT_DO"}, (
        f"the signals flagged writable are now {sorted(flagged)}. "
        f"docs/GUIDE.md opens by saying there is exactly one and tours it end "
        f"to end; a second needs its own chapter, or the claim has to go."
    )

    declared = set(c.writable)
    assert declared == flagged, (
        f"the declared write surface {sorted(declared)} and the signals flagged "
        f"writable {sorted(flagged)} disagree. docs/GUIDE.md quotes one number "
        f"and the contract carries two — see contracts/tags.yaml."
    )


def test_the_permit_range_the_guide_quotes_is_still_the_permit_range() -> None:
    """0.5 - 6.0 mg/L, quoted in the guide and in hop 1.

    The range is the thing an operator types against, so a change to it changes
    the front page of the guide. It is also a value a reader would copy into
    their own code, which is the highest-consequence kind of stale number in a
    document.
    """
    spec = contract().writable["AERATION:AHU-1:SETPOINT_DO"]
    assert list(spec["range"]) == [0.5, 6.0], (
        f"the permit range is now {spec['range']}; docs/GUIDE.md quotes "
        f"0.5 - 6.0 mg/L in three places. An operator reading that and typing "
        f"6.5 would be refused for no reason they could see."
    )


# ── the guide's own shape ───────────────────────────────────────────────────


def test_the_guide_follows_one_value_rather_than_five_parallel_seams() -> None:
    """It must not drift back into a parallel-by-component layout.

    The guide is organised as a single value's journey, on the argument that
    seams are where this codebase breaks and a story is easier to hold than five
    parallel chapters. That is a design decision, and a design decision that
    nobody checks is a design decision that erodes — usually the next time
    somebody adds a section that does not fit the story.

    Asserted on the chapter numbering, which is the visible spine.
    """
    text = _guide()
    for n in range(1, 8):
        assert re.search(rf"^## {n}\. ", text, re.MULTILINE), (
            f"the guide has no numbered chapter {n}. It follows one value in "
            f"seven hops; losing a hop means the tour no longer reaches "
            f"something, or reaches something twice."
        )
    # The word "seam" is allowed -- it explains why the tour stops where it
    # does -- but it must not be the organising structure.
    assert not re.search(r"^## Seam \d", text, re.MULTILINE), (
        "the guide has reverted to numbered Seam sections. It follows one value."
    )


def test_every_file_and_link_in_the_guide_resolves() -> None:
    """A 404 in a guide is worse than no link.

    The guide is read by someone who cannot see the repository. A path that does
    not resolve teaches them the documentation is unreliable before they have
    learned anything from it.
    """
    text = _guide()

    link = re.compile(r"\]\((?!https?:)([^)#]+)\)")
    missing = [
        target for target in link.findall(text)
        if not (GUIDE.parent / target).resolve().exists()
    ]
    assert not missing, f"docs/GUIDE.md links to files that do not exist: {missing}"

    # Paths with a directory component resolve against the repository root;
    # bare filenames are test modules, which live under tests/.
    paths = set(re.findall(r"`([a-z][a-z0-9_/]*\.py)", text))
    assert paths, "no source paths were extracted; the regex has stopped matching"
    absent = sorted(
        p for p in paths
        if not (ROOT / p).exists() and not (ROOT / "tests" / p).exists()
    )
    assert not absent, f"docs/GUIDE.md names source files that do not exist: {absent}"


def test_the_guide_does_not_accumulate_the_task_index_s_job() -> None:
    """`TASKS.md` answers "what do I run"; `GUIDE.md` answers "why".

    They must not converge. This repository already has 19 documents and the
    complaint about it is that nobody can tell which to open — adding a guide
    that restates the commands would have made that worse, not better.

    So a small number of commands is fine: the guide needs to *show* the one
    check that proves a write reached the plant, because that is the argument of
    hop 4 and it cannot be made in prose.
    """
    blocks = re.findall(r"```bash\n(.*?)```", _guide(), re.DOTALL)
    assert len(blocks) <= 4, (
        f"docs/GUIDE.md has {len(blocks)} bash blocks. It is explaining a "
        f"mechanism; the commands live in docs/TASKS.md. Accumulating them here "
        f"is how a mechanism document turns into a second, stale task index."
    )


def test_the_guide_is_reachable_from_both_entry_points() -> None:
    """A guide nobody is linked to does not solve a navigation problem.

    The original complaint was not missing documentation but no way to tell
    which document to open. An unlinked guide is the twentieth document and the
    same problem with one more file.
    """
    for entry in ("README.md", "docs/TASKS.md"):
        assert "GUIDE.md" in (ROOT / entry).read_text(encoding="utf-8"), (
            f"{entry} does not link docs/GUIDE.md. The guide is the third entry "
            f"point — tasks, then guide — and it has to be reachable or it is "
            f"another file nobody opens."
        )


def test_the_audit_trail_example_is_the_message_the_flow_actually_writes() -> None:
    """The guide quotes an `event` message verbatim, and it was copied from a
    live database — so it is exactly the kind of claim that rots.

    Checked against the generator, because that is where the string is built. A
    drift here would mean the guide's example audit row is no longer something
    an operator would ever see.
    """
    flows = ROOT / "scada" / "flows" / "03-control.json"
    text = flows.read_text(encoding="utf-8")
    for fragment in ("setpoint", "set to", "write_range"):
        assert fragment in text, (
            f"the control flow no longer contains {fragment!r}, so the audit "
            f"message quoted in docs/GUIDE.md is stale. Read a real row out of "
            f"the `event` table and quote that instead."
        )
    assert "setpoint_written" in text, (
        "the event kind quoted in docs/GUIDE.md no longer exists in the flow"
    )


def test_the_flow_still_sends_one_register_of_two_words() -> None:
    """Hop 2's whole argument is that the flow sends two words, correctly.

    This is a *guard rail* rather than a duplicate: the real assertion lives in
    `test_scada_contract.py`, and if that ever gets deleted the guide would still
    have a check asserting the fact it is built on. Cheap, and the claim is
    load-bearing enough to deserve a second witness.
    """
    nodes = json.loads(
        (ROOT / "scada" / "flows" / "03-control.json").read_text(encoding="utf-8")
    )
    writes = [n for n in nodes if n.get("type") == "modbus-write"]
    assert writes, "the control flow has no modbus-write node"
    for node in writes:
        assert node["dataType"] == "MHoldingRegisters", (
            f"{node.get('name')!r} uses dataType={node['dataType']!r}. In "
            f"node-red-contrib-modbus that is function code 6, write SINGLE "
            f"register, which ignores quantity — so half of every setpoint is "
            f"discarded. This is the fault hop 2 of docs/GUIDE.md describes."
        )
        assert node["quantity"] == 2, (
            f"{node.get('name')!r} writes {node['quantity']} register(s); a "
            f"float32 is two"
        )
