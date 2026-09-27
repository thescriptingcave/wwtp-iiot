"""Tests for the lesson-snippet gate, and for the claims the OPC UA course makes.

Two kinds of test here, and the second kind is the point.

**The gate's own behaviour** — does it find snippets, does it honour the skip
marker, does it stop reporting success when a snippet raises. A gate that cannot
fail is worse than no gate, because it is a green light wired to nothing.

**The lessons' claims** — the OPC UA course states that the address space has
fourteen children, six properties, eight components, and that `_variant_type`
ignores its argument. Those are measurements about code that changes. If somebody
fixes the `Double`-for-everything wart, the lesson that teaches the wart should
fail rather than quietly become a lie. That is the same rule
`tests/test_readme_claims.py` applies to the prose, applied to a course.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "tools" / "check_lessons.py"
COURSE = ROOT / "courses" / "opcua"
sys.path.insert(0, str(ROOT / "tools"))

from check_lessons import SKIP_MARKER, find_snippets  # noqa: E402

# ── the gate's own behaviour ────────────────────────────────────────────────


def test_the_gate_file_exists_and_is_runnable() -> None:
    assert GATE.exists()
    r = subprocess.run([sys.executable, str(GATE), "--list"],
                       capture_output=True, text=True, cwd=ROOT, check=False)
    assert r.returncode == 0, r.stderr


def test_a_fenced_block_without_a_language_is_not_a_snippet(tmp_path: Path) -> None:
    """A block of console output must never be mistaken for code.

    The lessons show expected output in bare fences, and the SQL course does the
    same. Without the language requirement the gate would try to run the *output*
    of every lesson, and the failure would be a confusing syntax error in a
    transcript rather than a clear skip.
    """
    p = tmp_path / "l.md"
    p.write_text("```\nSELECT 1;\n```\n", encoding="utf-8")
    assert find_snippets(p) == []


def test_a_python_fence_is_found_with_its_line_number(tmp_path: Path) -> None:
    p = tmp_path / "l.md"
    p.write_text("intro\n\n```python\nx = 1\n```\n", encoding="utf-8")
    found = find_snippets(p)
    assert len(found) == 1
    assert found[0].code.strip() == "x = 1"
    assert found[0].line == 3


def test_the_skip_marker_only_applies_to_the_block_below_it(tmp_path: Path) -> None:
    """One skip marker must not silently skip every block after it.

    This is the failure mode of the obvious implementation — checking whether the
    marker appears anywhere earlier in the file — and it is silent: the gate
    reports "0 snippets" and looks like it is working.
    """
    p = tmp_path / "l.md"
    p.write_text(
        f"{SKIP_MARKER}\n```python\nskipped = 1\n```\n\n"
        "prose in between\n\n```python\nruns = 1\n```\n",
        encoding="utf-8",
    )
    found = find_snippets(p)
    assert [s.skipped for s in found] == [True, False]


def test_a_marker_several_lines_above_the_fence_does_not_skip(tmp_path: Path) -> None:
    p = tmp_path / "l.md"
    p.write_text(
        f"{SKIP_MARKER}\n\nsome prose\n\nmore prose\n\n```python\nx = 1\n```\n",
        encoding="utf-8",
    )
    assert find_snippets(p)[0].skipped is False


# ── the lessons' claims ────────────────────────────────────────────────────


def test_the_opcua_course_exists_and_has_a_lesson() -> None:
    assert (COURSE / "README.md").exists()
    lessons = [p for p in COURSE.glob("*.md") if p.name != "README.md"]
    assert lessons, "the course has no lessons, which defeats its purpose"


def test_every_lesson_is_linked_from_the_course_readme() -> None:
    readme = (COURSE / "README.md").read_text(encoding="utf-8")
    for lesson in COURSE.glob("*.md"):
        if lesson.name == "README.md":
            continue
        assert lesson.name in readme, f"{lesson.name} is not in the course README"


def test_every_lesson_navigates_onward_and_back() -> None:
    """Each lesson must say what is next and where the course is.

    The SQL course does this at the top of every lesson and it is the only
    reason it is navigable without a table of contents.
    """
    for lesson in COURSE.glob("*.md"):
        if lesson.name == "README.md":
            continue
        text = lesson.read_text(encoding="utf-8")
        assert "**Next:**" in text, f"{lesson.name} has no Next link"
        assert "README.md" in text, f"{lesson.name} does not link back to the course"


def test_the_lesson_claims_the_address_space_really_has() -> None:
    """14 children, 6 properties, 8 components — asserted against a live server.

    These are the numbers lesson 01 teaches. They are the kind of claim that
    rots: change one `add_property` call and the lesson is wrong while still
    reading perfectly.
    """
    code = """
import asyncio
from collections import Counter
from softplc.servers.opcua import OpcUaServer

async def main():
    s = OpcUaServer(endpoint="opc.tcp://127.0.0.1:48401/x/")
    await s.start(); await s.wait_ready()
    n = len(await s.space.folder.get_children())
    refs = Counter(r.ReferenceTypeId.Identifier
                   for r in await s.space.folder.get_references())
    print(n, refs[46], refs[47], sum(refs.values()))
    await s.stop()
asyncio.run(main())
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=ROOT, check=False)
    assert r.returncode == 0, r.stderr
    children, props, comps, total = r.stdout.strip().split()[-4:]
    assert int(children) == 14, f"lesson says 14 children, server has {children}"
    assert int(props) == 6, f"lesson says 6 properties, server has {props}"
    assert int(comps) == 8, f"lesson says 8 components, server has {comps}"
    assert int(total) == 16, f"lesson says 16 references, server has {total}"


def test_the_lesson_claims_every_value_is_a_double() -> None:
    """The wart lesson 02 is about must still be true, or lesson 02 is a lie.

    If somebody teaches the type system properly this test fails, which is the
    correct outcome: the course needs rewriting, and a silent pass would let it
    drift. The check is the *absence* of the fix, asserted deliberately — it is
    the one test in this file that would be embarrassing to keep.
    """
    src = (ROOT / "softplc" / "servers" / "opcua.py").read_text(encoding="utf-8")
    body = src.split("def _variant_type", 1)[1].split("\n\n", 1)[0]
    assert "return ua.VariantType.Double" in body
    assert "eu" not in body.split("return", 1)[0].split('"""', 2)[-1], (
        "_variant_type must still ignore its argument for lesson 02 to be true"
    )


def test_the_lesson_claims_bad_quality_is_never_published() -> None:
    """Lesson 03's second claim: `Bad` is documented and never produced."""
    src = (ROOT / "softplc" / "servers" / "opcua.py").read_text(encoding="utf-8")
    assert "StatusCodes.Bad" not in src, (
        "Bad is now published, so the course README's claim 2 is stale"
    )
    assert "StatusCodes.Uncertain" in src


def test_the_course_readme_does_not_claim_lessons_that_do_not_exist() -> None:
    """'Planned' must mean planned.

    The SQL course lists an unwritten `04-expert` stage and says so in words. The
    same rule here: a lesson listed without a status, or marked written without
    a file, is the kind of drift `tests/test_readme_claims.py` exists to catch,
    and a course is exactly where it does the most damage.
    """
    readme = (COURSE / "README.md").read_text(encoding="utf-8")
    for row in re.findall(r"^\| (\d\d) \| \[(.+?)\]\((.+?)\) \| (.+?) \|$",
                          readme, re.MULTILINE):
        num, title, target, status = row
        exists = (COURSE / target).exists()
        written = "written" in status and "unwritten" not in status
        assert exists == written, (
            f"lesson {num} ({title}): file exists={exists}, status says "
            f"{status!r} — the course README and the directory disagree"
        )
