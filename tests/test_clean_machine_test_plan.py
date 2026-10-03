"""The clean-machine test plan must be executable, not aspirational.

**Why this file needs a test.** A test plan is the one document that is *only*
useful if it is literally true: every command has to exist, and every number has to
be the number the gate prints. Nothing in the repository checks that, because a plan
is prose — and a plan whose step 12 says "expect 78 queries" when the gate now says
81 sends somebody hunting for a bug that is in the plan.

The failure this repo has actually suffered, and the reason for each check:

- **A command that does not exist.** `make workshop-long` and
  `make workshop-url` were both added recently and neither was in the plan when it
  was first written. A reader following the plan hit "No rule to make target".
- **A count that drifted.** The unit-test total moves whenever a module is added,
  and it is guarded by `test_readme_claims.py` everywhere *except* here.
- **A number that is right in one document and wrong in another**, which is the
  specific thing `docs/TESTING.md` and this plan both have to agree about.

**What is not checked, deliberately.** The plan's prose, its failure tables and its
durations. A plan that describes a failure mode in the wrong words is still a usable
plan; a plan that names a target which does not exist is not. So this checks names
and numbers, and leaves the rest to review.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLAN = (ROOT / "docs" / "CLEAN-MACHINE-TEST-PLAN.md").read_text()
TESTING = (ROOT / "docs" / "TESTING.md").read_text()
MAKEFILE = (ROOT / "Makefile").read_text()


def _flat(text: str) -> str:
    """Whitespace collapsed to single spaces.

    Markdown wraps wherever the source did, and the plan's 18-week figure is split
    across a line break mid-figure -- "57 signals \u00d7" / "3,024 hours". An exact
    substring check fails on that, and the obvious "fix" is to rewrap the document,
    which breaks the next person who edits it. Collapsing whitespace is the fix that
    survives editing.
    """
    return " ".join(text.split())


def make_targets() -> set[str]:
    """Every target name declared in the Makefile.

    Anchored at column 0 and ending in a colon, so a `WORKSHOP_DB ?=` assignment and
    a recipe line are both excluded. Read from the Makefile rather than from
    `make help` so the test needs no shell and no working database.
    """
    return set(re.findall(r"^([a-z][a-zA-Z0-9-]*):", MAKEFILE, re.M))


def plan_commands() -> set[str]:
    """Every `make <target>` the plan tells the reader to **run**.

    Only inside ```bash fences. The first version scanned the whole document and
    picked up the prose "make everything pass", and then failed on two targets that
    do not exist -- which reads as a broken repository rather than a broken check.

    Fences are also the right scope for a different reason: the plan's prose
    discusses commands it does not instruct the reader to type, and a step that
    mentions one is not a step that runs it.
    """
    found = set()
    for block in re.findall(r"```bash\n(.*?)```", PLAN, re.S):
        for target in re.findall(r"\bmake ([a-z][a-zA-Z0-9-]*)", block):
            if target != "--version":
                found.add(target)
    return found


class TestEveryCommandExists:
    def test_the_plan_actually_uses_make(self) -> None:
        """Guards against the extraction silently matching nothing.

        A parametrised test over an empty set passes, and then the file stops
        checking anything while still reporting green.
        """
        assert len(plan_commands()) >= 12, (
            f"only found {len(plan_commands())} make targets in the plan. Either the "
            "plan lost its commands or this extraction is wrong; both mean the rest "
            "of this file is vacuous."
        )

    @pytest.mark.parametrize("target", sorted(plan_commands()))
    def test_the_target_is_declared(self, target: str) -> None:
        assert target in make_targets(), (
            f"the test plan tells the reader to run `make {target}` and no such "
            "target exists. A plan step that fails on `No rule to make target` is "
            "worse than no plan, because it looks like the repository is broken."
        )

    def test_the_plan_covers_every_phase(self) -> None:
        """Clone, plant, gates, workshop, teardown. A gap is a hole in the run."""
        for needed in ("git clone", "make up", "make check", "make down"):
            assert needed in PLAN, (
                f"the plan never mentions `{needed}`. A clean-machine run that "
                "skips setup or teardown does not prove reproducibility, which is "
                "the entire point of the plan."
            )


class TestTheExpectedCountsAreTheRealOnes:
    """Each figure the plan tells the reader to expect, checked against TESTING.md.

    The counts live in `docs/TESTING.md` because that is the document about
    verification; the plan quotes them so a reader does not have to hold two files
    in their head. That only works if they agree, which is what these are for.
    """

    #: What the plan claims, and the pattern in TESTING.md that must agree.
    COUNTS: ClassVar[dict[str, tuple[str, str]]] = {
        "lint findings": (r"155 findings, baseline 155", r"155 tracked findings"),
        "unit tests": (r"\*\*1179 passed\*", r"Unit, no database \| 1179 \|"),
        "integration tests": (r"\*\*48 passed\*\*", r"Integration \| 48 \|"),
        "sql queries": (
            r"78 queries in 26 files: 78 ok",
            r"78 queries in 26 files",
        ),
        "lesson snippets": (r"87/87 snippets ran", r"87 snippets in 18 lessons"),
        "analyst notebooks": (
            r"\*\*11 notebooks, built, executed",
            r"Analyst notebooks \| 11 notebooks",
        ),
        "workshop notebooks": (
            r"\*\*6 notebooks, built, executed",
            r"Workshop notebooks \| 6 notebooks",
        ),
    }

    @pytest.mark.parametrize("label", sorted(COUNTS))
    def test_the_figure_agrees_with_testing_md(self, label: str) -> None:
        in_plan, in_testing = self.COUNTS[label]
        assert re.search(in_plan, PLAN), (
            f"the plan no longer states the {label} figure this test checks for "
            f"({in_plan}). Either the figure changed or the sentence was reworded; "
            "update both."
        )
        assert re.search(in_testing, TESTING), (
            f"docs/TESTING.md no longer states the {label} figure the plan quotes "
            f"({in_testing}). The plan and the verification document have drifted, "
            "and a reader following one will expect the wrong number from the other."
        )

    def test_the_plan_never_claims_a_gate_it_did_not_check(self) -> None:
        """Every 'expect' number has a command above it in the same step.

        Cheap to state and easy to break by adding prose: an expectation with no
        command is a claim with nothing to check it against.
        """
        steps = re.split(r"\*\*Step \d+\*\*", PLAN)[1:]
        assert len(steps) >= 25, (
            f"only {len(steps)} steps found. The plan is a top-to-bottom run; if it "
            "has fewer than 25 it is not covering the repository."
        )
        for step in steps:
            if "Expect" not in step:
                continue
            assert "```bash" in step or "make " in step, (
                "a step says 'Expect' but contains no command to run against it. "
                "An expectation without a command cannot fail, and therefore cannot "
                "be evidence that the run passed."
            )


class TestThePanelFiguresAreTheMeasuredOnes:
    """The workshop's own numbers, quoted in the plan.

    These are not in `TESTING.md` -- they are dataset facts, and they are in
    `workshops/ml/README.md`. The plan quotes them so a reader can tell a working
    build from a broken one without a second document open, which is only safe
    while they agree.
    """

    #: The notebook **sources**, not the workshop README. Those files are where the
    #: builder's own output is transcribed as an ```` ```output ```` fence, and the
    #: plan quotes the same lines -- so the transcription and the plan can be
    #: compared. Checking `workshops/ml/README.md` instead was my first attempt and
    #: it found nothing, because that document describes the dataset in prose and
    #: never prints the builder's output.
    NOTEBOOK_SOURCES: ClassVar[list[Path]] = sorted(
        (ROOT / "workshops" / "ml" / "src").glob("*.md")
    )

    @classmethod
    def _notebook_text(cls) -> str:
        return "\n".join(path.read_text() for path in cls.NOTEBOOK_SOURCES)

    #: `(figure as the plan states it, figure as the transcript states it, where)`.
    #:
    #: Three documents hold these numbers and they are not interchangeable:
    #:
    #: - the workshop **notebook sources** transcribe the 3-week builder's output in
    #:   ```output fences, so the 3-week figures are checkable against them;
    #: - `workshops/ml/README.md` carries the 18-week table, because no notebook was
    #:   written against that panel -- the notebooks read the 3-week CSV, so there is
    #:   no fence to check it against.
    #:
    #: The plan writes `x` as `\u00d7` and the builder prints `x`, so the two spellings
    #: differ by one character and each is checked against its own document.
    FIGURES: ClassVar[list[tuple[str, str, str]]] = [
        (
            "28,728 rows = 57 signals \u00d7 504 hours",
            "57 signals x 504 hours",
            "notebook",
        ),
        ("22 positives", "positives       22", "notebook"),
        ("172,368 rows = 57 signals \u00d7 3,024 hours", "172,368", "workshop"),
        ("419 positives", "**419**", "workshop"),
    ]

    #: The 3-week builder's output, as transcribed in the notebook sources.
    @classmethod
    def _notebook_text(cls) -> str:
        return "\n".join(
            path.read_text()
            for path in sorted((ROOT / "workshops" / "ml" / "src").glob("*.md"))
        )

    @classmethod
    def _workshop_readme(cls) -> str:
        return (ROOT / "workshops" / "ml" / "README.md").read_text()

    @pytest.mark.parametrize(
        "figure,transcript,where",
        FIGURES,
        ids=[f[0] for f in FIGURES],
    )
    def test_the_figure_is_in_both_documents(
        self, figure: str, transcript: str, where: str
    ) -> None:
        flat_plan = _flat(PLAN)
        assert _flat(figure) in flat_plan, (
            f"the plan no longer states {figure!r}, which it tells the reader to "
            "expect from a panel build."
        )
        text = self._notebook_text() if where == "notebook" else self._workshop_readme()
        origin = "notebook source" if where == "notebook" else "workshops/ml/README.md"
        assert transcript in text, (
            f"the plan says the build prints {transcript!r} and no {origin} "
            "contains it. One of them has drifted, and the plan is the copy a "
            "first-time reader checks their build against."
        )
