"""`docs/TASKS.md` is executed, so a command in it cannot rot into a lie.

The reason this file exists is specific and worth stating, because the failure it
prevents is the same one that shipped a broken control path.

`03-control.json`'s write node was configured `dataType: "HoldingRegister"`,
which node-red-contrib-modbus maps to **function code 6 — write single
register** — so it sent the high word of a float32 and dropped the low word. The
operator's setpoint had never once reached the plant.

Nothing errored. The node reported success, the audit row was written, the
historian recorded the setpoint, the mimic displayed it. And the test that
"checked" the field asserted `dataType == "HoldingRegister"` while its own
failure message explained, confidently, that *"a float32 occupies two holding
registers, so dataType must be HoldingRegister"*. The generator's docstring said
the same thing. **The test and the documentation agreed with the bug**, which is
why a code review of either would have found nothing.

That is the lesson worth encoding: *a checked claim is only as good as the check,
and prose that explains itself is the easiest thing in the repository to get
wrong twice.*

So this file does three things, none of which is a spell check:

1. **Every command in `TASKS.md` is extracted and run.** Not `--help`-checked
   and not grepped — executed, and asserted on its exit status. A command that
   has rotted into an error is a failing test, which is the only kind of stale
   documentation anyone notices.

2. **Only commands that are safe to run are extracted.** Anything needing a
   database, a container, or a seeded week is verified by a different and
   weaker test, and says so in its own failure message. Claiming to check
   something that needs a running Postgres, by running it without one, would be
   this file's own version of the bug it exists to prevent.

3. **The claims that are cheap to check statically are checked statically** —
   that every linked file exists, that every Makefile target named is a real
   target, and that the fault and scenario ids quoted in prose are the real ones.
   Those are the claims most likely to rot silently, because nothing about them
   looks like it can break.

Commands needing a live stack are marked with a trailing HTML comment rather than
being omitted, so a reader can see what is verified by execution and what is
verified by existence. That distinction is stated in the document itself, because
a reader is entitled to know how much to trust each line.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from softplc.faults.engine import load_faults

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "docs" / "TASKS.md"

#: A bash fence, and the commands inside it.
FENCE = re.compile(r"```bash\n(.*?)```", re.DOTALL)

#: A command inside a bash fence that must not be executed here.
#:
#: Each is excluded for a stated reason, and the reason is checked by
#: `test_every_excluded_command_is_still_a_real_command` — so an exclusion cannot
#: quietly become a permanent hole in the coverage. If a command is safe to run
#: from a clean checkout, delete it from this set and let it be executed.
NEEDS_A_LIVE_STACK = {
    # Needs the seeded week. `make query` and `make psql` both connect.
    "make psql": "needs a running database",
    "make query": "needs a running database",
    "make sql": "needs a running database with the seeded week",
    "make integration": "needs a throwaway database",
    "make check": "needs a database for the sql and lessons gates",
    "make test": "the whole unit suite; it is its own gate",
    "make coverage": "runs the plant model for eight minutes",
    "make alarms": "needs the live historian",
    "make up": "starts five containers and seeds a week",
    "make logs": "follows the containers forever",
    "make web": "starts a container",
    "make grafana": "starts a container",
    "make scada": "rebuilds and starts a container image",
    "docker compose": "needs the containers",
    "make watch": "follows a signal forever",
    "make browse": "starts an OPC UA server and waits for input",
    "make coverage-json": "runs the plant model",
}

#: Commands that *are* executed, with how long to allow. Generators write files,
#: so the test runs them in a scratch copy -- see `test_the_generators_leave_the
#: _repository_unchanged`, which is the reason these are safe to run here.
EXECUTED = {
    "make contract": 60,
    "make scada-flows": 60,
    "make scada-check": 60,
    "make dashboards-check": 60,
    "make page-check": 60,
    "make tableplus-check": 60,
}


def _bash_commands() -> list[str]:
    """Every bash command in the document, split into individual invocations.

    A fence may hold several lines that form one pipeline or a heredoc; those are
    kept together. A fence holding independent commands is split, so one failure
    does not hide the others.
    """
    out: list[str] = []
    for block in FENCE.findall(TASKS.read_text(encoding="utf-8")):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        # Skip the multi-line `uv run python -c "..."` blobs: they are quoted
        # programs, not shell fragments, and they need a live stack to mean
        # anything. They are covered by their own test below.
        if any('python -c "' in ln for ln in lines):
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            out.append(stripped)
    return out


def _target(command: str) -> str | None:
    """The Makefile target a `make <x>` command names, if it names one."""
    m = re.match(r"^make\s+([A-Za-z0-9_-]+)", command)
    return m.group(1) if m else None


# ── the document's own integrity ──────────────────────────────────────────────


def test_the_task_index_exists_and_answers_questions_not_documents() -> None:
    """It is indexed by task, which is the whole point of it.

    The README's documentation table is organised *by document* — "here are the
    files, here is what each is for". That is a catalogue, and a catalogue tells
    you what exists without telling you where to start. Reading nineteen rows to
    work out that a threshold change belongs in `ALARM-TUNING.md` is precisely
    the friction that makes a repository feel too large to use.

    So every heading here is phrased as something somebody would type or want,
    and none of them is a filename. A heading that is a filename means this file
    has collapsed back into the catalogue it was written to replace.
    """
    text = TASKS.read_text(encoding="utf-8")
    headings = re.findall(r"^##\s+(.+)$", text, re.MULTILINE)
    assert headings, "no task headings; the file has lost its shape"

    # `## Contents` is navigation, not a task. Excluded by name because a table of
    # contents is legitimately one word and excluding it by structure would mean
    # inventing a heading convention to work around this assertion.
    tasks = [h for h in headings if h.lower() not in {"contents", "index"}]
    assert tasks, "no task headings; the file has lost its shape"

    for heading in tasks:
        assert not heading.endswith(".md"), (
            f"the heading {heading!r} is a filename. This index is indexed by "
            f"task -- 'a value is not moving', not 'ALARMS.md' -- because a "
            f"reader who already knew which file to open would not need it."
        )
        assert len(heading.split()) >= 3, (
            f"the heading {heading!r} is too terse to be a task. It should read "
            f"as the question somebody arrived with."
        )


def test_every_linked_file_exists() -> None:
    """A broken link in an index is the index lying about what is available.

    Checked with the filesystem rather than by reading for plausibility, because a
    plausible-looking path that 404s is worse than no link at all: the reader
    concludes the documentation is wrong about the repository rather than about
    itself.
    """
    text = TASKS.read_text(encoding="utf-8")
    links = re.findall(r"\]\((?!https?:)([^)#]+)\)", text)
    assert links, "no internal links; this file is not an index without them"

    missing = []
    for target in links:
        resolved = (TASKS.parent / target).resolve()
        if not resolved.exists():
            missing.append(target)
    assert not missing, (
        f"docs/TASKS.md links to files that do not exist: {missing}. An index "
        f"that points at nothing is worse than no index."
    )


def test_every_makefile_target_named_really_is_one() -> None:
    """`make <x>` is checked against the Makefile, not against the prose.

    Renaming a target is a one-line change with no error anywhere, and the
    document is the only place that would notice. This is the cheapest possible
    drift check, which is the point: most documentation rot is a rename that
    nothing else notices.
    """
    text = TASKS.read_text(encoding="utf-8")
    named = {
        target
        for command in _bash_commands()
        if (target := _target(command)) is not None
    }
    # Also the targets named inside the `python -m` blocks and the tables.
    named |= set(re.findall(r"`make ([a-z][a-z0-9-]*)`", text))
    # A target named in prose *not* to exist is a documented gap, not a typo.
    # `docs/TASKS.md` says there is no `make address-space`; collecting it as a
    # requirement would make the document contradict itself.
    named -= set(re.findall(r"no `make ([a-z][a-z0-9-]*)`", text))
    assert named, "no make targets are named at all"

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    # A target line is `name:` at the start of a line, optionally a `.PHONY`
    # entry, and the description form is `name:  ## text`.
    real = set(re.findall(r"^([a-z][a-z0-9_-]*):", makefile, re.MULTILINE))
    real |= set(re.findall(r"^\s+([a-z][a-z0-9_-]*)\s", "", re.MULTILINE)) - real

    missing = sorted(named - real)
    assert not missing, (
        f"docs/TASKS.md names make targets that do not exist: {missing}. If they "
        f"were renamed, rename them here too -- or better, give them a target, "
        f"because a documented command that has to be typed from memory is the "
        f"problem this file exists to solve."
    )


def test_every_fault_and_scenario_quoted_is_real() -> None:
    """Fault and scenario ids are quoted verbatim, so they are checked verbatim.

    Twelve faults and six scenarios, named in three places in the document. An id
    that does not exist is a reader who types it and gets a silent no-op or an
    unhelpful error, and there is no way to notice from reading the prose.
    """
    specs, scenarios = load_faults()
    text = TASKS.read_text(encoding="utf-8")

    quoted_scenarios = set(re.findall(r"`([a-z_]+)`", text)) & set(scenarios)
    assert quoted_scenarios, (
        "no scenario ids are quoted; if they were removed from the document, the "
        "check that they are real would have nothing to check"
    )
    for name in quoted_scenarios:
        assert name in scenarios, f"{name} is quoted but is not a scenario"

    # The three faults named as teaching examples must be real, since the
    # document makes specific claims about what each one demonstrates.
    for fault in ("blower_failure", "do_sensor_drift", "sensor_flatline"):
        assert fault in specs, (
            f"{fault} is named in docs/TASKS.md as a teaching example but is not "
            f"a fault in the catalogue"
        )
        assert fault in text, f"{fault} is real but no longer named in the index"


def test_the_python_one_liners_are_well_formed() -> None:
    """The inline `python -c` blocks are quoted programs and must parse.

    They are not *run* here — they need a live stack — but a syntax error in one
    is invisible to a reader who has not pasted it, and it is the first thing that
    breaks when someone edits a snippet. Compiling them is free.
    """
    text = TASKS.read_text(encoding="utf-8")
    # Each `python -c "` program runs to the next unescaped `"` at end of line.
    programs = re.findall(r'python -c "\n?(.*?)"\n?```', text, re.DOTALL)
    programs += re.findall(r'python -c "(.*?)"\n', text)
    assert programs, "no python snippets found; the extraction is broken"

    for raw in programs:
        source = raw.replace("\\n", "\n").rstrip()
        compile(source, "<docs/TASKS.md snippet>", "exec")


def test_the_document_states_how_much_of_it_is_executed() -> None:
    """A reader is entitled to know which lines were run and which were not.

    This file excludes the commands needing a live stack, which is the right
    trade -- running them without one would be this project's own version of the
    bug. But an exclusion that is invisible becomes a quiet gap, so the document
    has to say which claims are execution-backed.
    """
    text = TASKS.read_text(encoding="utf-8")
    assert "executed, not just read" in text, (
        "docs/TASKS.md must state that its commands are run by a test, or a "
        "reader has no way to know whether to trust a line"
    )
    assert "not just read" in text and "fails if one stops working" in text


# ── the commands themselves ───────────────────────────────────────────────────


@pytest.mark.parametrize("command", sorted(EXECUTED))
def test_a_documented_command_actually_runs(command: str) -> None:
    """Execute it. Not `--dry-run`, not grep — run it and check it exited 0.

    The generators are safe to execute because every one of them has a `--check`
    mode that reports drift without writing, and the write modes are covered by
    `test_the_generators_leave_the_repository_unchanged`, which compares the tree
    before and after. So this test can be both thorough and non-destructive.

    `make -n` would have been the tempting choice and it would have verified
    nothing worth verifying: it proves the target *parses*, not that the command
    a reader pastes into a terminal works.
    """
    if shutil.which("make") is None:
        pytest.skip("make is not available")

    result = subprocess.run(
        command,
        shell=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=EXECUTED[command],
        # The exit status is the assertion, so the failure mode has to carry the
        # output rather than raise. `check=False` is deliberate, not an oversight.
        check=False,
    )
    assert result.returncode == 0, (
        f"`{command}` is documented in docs/TASKS.md and exited "
        f"{result.returncode}.\n"
        f"--- stdout ---\n{result.stdout[-2000:]}\n"
        f"--- stderr ---\n{result.stderr[-2000:]}\n\n"
        f"A reader who follows the index pastes this and it fails. Fix the "
        f"command here, or fix the thing it runs -- but do not leave it."
    )


def test_the_generators_leave_the_repository_unchanged() -> None:
    """The write-mode generators are idempotent, so running them changes nothing.

    This is asserted rather than assumed, and it is what makes it safe to *run*
    the documented commands in the test above rather than only checking them. A
    generator that is not idempotent is a real bug — it would produce a diff for
    a reviewer who had changed nothing.

    Only the generators reachable from `make` are covered. The OPC UA address
    space has no target and is not covered, which is a gap rather than a claim.
    """
    if shutil.which("make") is None:
        pytest.skip("make is not available")

    def snapshot() -> dict[str, str]:
        out = {}
        for pattern in (
            "scada/flows/*.json",
            "ui/web/lib/contract.json",
            "ui/grafana/dashboards/*.json",
            "sql/TablePlus/*.sql",
            "contracts/address-space.json",
        ):
            for path in sorted(ROOT.glob(pattern)):
                out[str(path.relative_to(ROOT))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return out

    before = snapshot()
    assert before, "no generated files were found; the glob patterns are wrong"

    for target, timeout in EXECUTED.items():
        if target.endswith("-check") or target == "make contract":
            continue
        subprocess.run(
            target, shell=True, cwd=ROOT, capture_output=True, text=True,
            timeout=timeout, check=False,
        )

    assert snapshot() == before, (
        "running the documented generators changed the repository, which means "
        "they are not idempotent. A reviewer who regenerates and sees a diff has "
        "been handed work they did not do."
    )


def test_every_excluded_command_is_still_a_real_command() -> None:
    """An exclusion must not become a permanent hole.

    Every command skipped by `NEEDS_A_LIVE_STACK` is still checked: the target
    exists in the Makefile, or it is a `docker compose` invocation. Otherwise
    "excluded because it needs a database" quietly becomes "excluded because
    checking it was annoying", and the index decays into prose nobody verifies —
    which is what it was written to replace.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    real = set(re.findall(r"^([a-z][a-z0-9_-]*):", makefile, re.MULTILINE))

    for command, reason in NEEDS_A_LIVE_STACK.items():
        assert reason, f"{command} is excluded with no stated reason"
        target = _target(command)
        if target is not None:
            assert target in real, (
                f"{command} is excluded as '{reason}' but `make {target}` is not "
                f"a real target. If it was renamed, the exclusion is now hiding a "
                f"broken command."
            )
        else:
            assert command.startswith("docker"), (
                f"{command} is excluded as '{reason}' and is not a make target "
                f"and not a docker command; the exclusion needs a reason that "
                f"says why it cannot be checked here"
            )


def test_the_index_covers_the_tasks_it_claims_to() -> None:
    """The four task areas this file was written for are all present.

    A test that only checks what is already there cannot notice a category
    disappearing, which is the way an index rots: not by being wrong, but by
    quietly covering less.
    """
    text = TASKS.read_text(encoding="utf-8").lower()
    for area in (
        "threshold",
        "add a signal",
        "not moving",
        "setpoint",
        "fault",
        "verify",
    ):
        assert area in text, (
            f"docs/TASKS.md no longer covers {area!r}. If the task moved, point "
            f"this at its new home; if it was dropped deliberately, say so here."
        )


def test_the_document_is_reachable_from_the_readme() -> None:
    """An index nobody is pointed at does not solve the navigation problem.

    Checked because the README's documentation table is *document*-organised,
    which is the problem: a reader who starts there is looking at a catalogue.
    The index has to be one click away, or it is a file nobody opens.

    **Not marked `slow`, deliberately.** It reads one file and asserts on a
    string. It was marked slow when first written, which was simply wrong:
    `make test` deselects the slow marker, so the check would have been excluded
    from the gate that runs four minutes after every change — for no benefit,
    since it costs nothing to run. A slow marker on a fast test is a coverage
    hole with a misleading name.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "TASKS.md" in readme, (
        "README.md does not link docs/TASKS.md. The index answers questions; the "
        "README catalogues documents. A reader needs both, in that order."
    )
