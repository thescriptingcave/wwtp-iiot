"""One mechanism starts a kernel for a notebook, and this says which.

`make notebooks-read` shelled out to `jupyter nbconvert --to html --execute`.
It failed on cell one of every notebook with

    ModuleNotFoundError: No module named 'notebooks'

which reads like a broken import and was neither. `nbconvert`'s **command line**
resolves a kernel by *name*, every notebook in this repository declares
`kernelspec.name = "python3"`, and on a machine with more than one Python that is
a name several interpreters answer to. Here it answered with

    /opt/homebrew/anaconda3/bin/python

— no `psycopg`, no `pandas`, no project.

`tools/check_notebooks.py` runs the same eleven notebooks through **nbclient**,
in-process, and gets the venv interpreter, because the kernel it starts is the
process it is running in. So the gate was green and the read path was red, from
the same notebooks, on the same machine, on the same day — and `notebooks/README.md`
was meanwhile telling the reader that the red one was the one to use.

These tests are source-level on purpose. The failure was not reproducible by
running the gate: the gate passed. What was wrong was a *second* mechanism, and
the only thing that catches a second mechanism is a rule about having one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from tools import build_notebooks, check_notebooks

ROOT = Path(__file__).resolve().parents[1]

#: Anything that hands a notebook to a *command* that will pick a kernel itself.
#: `--execute` is the flag that does it; the name pattern is belt and braces for
#: a future `--to notebook --execute` written differently.
CONVERTERS = re.compile(
    r"nbconvert[^\n]*(?:--execute|-{1,2}to\s+notebook)", re.IGNORECASE
)


def _makefile_recipe_lines(path: Path) -> list[tuple[int, str]]:
    """The Makefile's *recipe* lines as logical lines, and nothing else.

    A `#` line is a comment whatever it says, and a variable assignment is
    inert. Only a tab-indented line can run something.

    **Continuations are joined**, and getting that wrong is not hypothetical:
    the first version of this function returned physical lines, and a mutation
    that restored the original bug —

        $(PY) -m jupyter nbconvert \\
            --to html --execute --no-prompt "$$nb" \\

    — passed. `nbconvert` was on one line and `--execute` on the next, so a
    per-line pattern could not see both. A shell command spread over three lines
    is one command, and a rule that reads it as three is a rule with a hole in
    the shape of the most common way Makefiles are written. Found by mutating
    the code back to the bug and checking that the test noticed.
    """
    logical: list[tuple[int, str]] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.startswith("\t"):
            continue
        if logical and logical[-1][1].rstrip().endswith("\\"):
            start, previous = logical[-1]
            logical[-1] = (start, previous.rstrip().rstrip("\\") + " " + line.strip())
        else:
            logical.append((number, line.strip()))
    return logical


def _python_string_literals(path: Path) -> list[tuple[int, str]]:
    """Every string constant in a Python file that is *not* a docstring.

    `ast` rather than a text scan, because the first version of this rule
    matched its own explanation. `tools/notebook_read.py` has to *quote*
    `jupyter nbconvert --to html --execute` in its module docstring — that is the
    bug, written down at the site of the fix — and a line-based rule flagged its
    own justification, leaving exactly two ways out: delete the explanation, or
    `# noqa` the line. Both are worse than the rule, and the second is how a rule
    stops being a rule.

    A docstring is prose about the code. A string literal inside a function is
    part of the code, and that is the distinction worth making.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _sources() -> list[Path]:
    """Every place in this repository that could start a kernel for a notebook.

    The Makefile, because that is where the recipe lived. And `tools/*.py`,
    because "just call nbconvert from Python" is the same mistake with a
    different spelling — and the fix here (`HTMLExporter` without `--execute`)
    is only durable if the rule covers the module that replaced it too.
    """
    paths = [ROOT / "Makefile"]
    paths.extend(sorted((ROOT / "tools").glob("*.py")))
    paths.extend(sorted((ROOT / "tests").glob("*.py")))
    return paths


def _executable_lines(path: Path) -> list[tuple[int, str]]:
    if path.suffix == ".py":
        return _python_string_literals(path)
    return _makefile_recipe_lines(path)


def test_nothing_shells_out_to_nbconvert_with_execute() -> None:
    """The mechanism that picked Anaconda, by name, is gone.

    Not "the command fails" — it does not fail, it *succeeds* at running a
    notebook under whatever `python3` means on the host, and that is worse. A
    check that only asserted success would have passed the whole time this was
    broken.

    Only executable text is examined: recipe lines in the Makefile, non-docstring
    string literals in Python. The prose in this file and in
    `tools/notebook_read.py` that describes the bug is allowed to name it.
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()[:90]}"
        for path in _sources()
        if "test_notebook_kernels" not in path.name
        for number, line in _executable_lines(path)
        if CONVERTERS.search(line)
    ]
    assert not offenders, (
        "something runs a notebook through nbconvert's command line, which "
        "resolves a kernel by NAME. Every notebook here declares "
        'kernelspec.name = "python3" and on a machine with more than one Python '
        "that is not this project's:\n  " + "\n  ".join(offenders)
    )


def _notebook_client_kernel_names(path: Path) -> set[str]:
    """The `kernel_name=` each `NotebookClient(...)` call actually passes.

    Read from the syntax tree, so a docstring that *mentions* `kernel_name` —
    which both of these modules do, at length, because it is the bug — cannot
    stand in for the argument. Returns a set so a module that calls
    `NotebookClient` twice with two different names fails visibly rather than
    passing on whichever one matched.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "NotebookClient"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "kernel_name" and isinstance(keyword.value, ast.Constant):
                names.add(str(keyword.value.value))
            elif keyword.arg == "kernel_name":
                names.add("<not a literal>")
    return names


def test_both_notebook_paths_execute_through_nbclient() -> None:
    """The gate and the read path must run a notebook the *same* way.

    This is the assertion that would have caught the original bug directly, and
    it is a shape check rather than a behaviour check on purpose: the two tools
    do different things with a notebook afterwards — one checks the claims, one
    writes HTML — and the only thing they have to agree about is how it ran.

    `NotebookClient` is the requirement, because it starts the kernel in-process.
    `kernel_name` is checked with it because the *other* way to get a venv kernel
    is a registered kernelspec, and this project has three unrelated `python3`
    kernels on this machine already, which is the whole difficulty.
    """
    for module in ("check_notebooks", "notebook_read"):
        path = ROOT / "tools" / f"{module}.py"
        text = path.read_text(encoding="utf-8")
        assert "NotebookClient" in text, (
            f"tools/{module}.py does not use nbclient's NotebookClient, so it is "
            "picking a kernel some other way and the two notebook paths can "
            "disagree about which interpreter that is"
        )

        # **`ast`, not `in text`.** The first version of this was a substring
        # check, and a mutation that deleted `kernel_name=` from the call passed
        # it — because both modules *document* the argument in their docstrings,
        # so the string was still there and the check was reading a comment. The
        # third appearance of the same mistake in this repository: a rule that
        # reads text cannot tell a call from the sentence about the call.
        kernels = _notebook_client_kernel_names(path)
        assert kernels == {"python3"}, (
            f"tools/{module}.py calls NotebookClient with kernel_name={kernels} "
            "rather than {'python3'}. nbclient resolves a missing or wrong name "
            "through the kernelspec search, which on this machine answers "
            "'python3' with /opt/homebrew/anaconda3/bin/python."
        )


def test_the_read_path_renders_without_choosing_a_kernel() -> None:
    """`HTMLExporter` is used for rendering, never for executing.

    The fix could have been `--ExecutePreprocessor.kernel_name=…` on the
    command line, which leaves the *rendering* half free to start a kernel again
    later. Using the exporter in-process with no `--execute` means that half has
    no kernel to choose and structurally cannot disagree with the gate.

    Checked over the executable strings only, for the reason given in
    `_python_string_literals`: this module quotes the flag in prose.
    """
    text = (ROOT / "tools" / "notebook_read.py").read_text(encoding="utf-8")
    assert "HTMLExporter" in text, (
        "tools/notebook_read.py no longer renders through nbconvert's exporter"
    )
    offending = [
        (number, line)
        for number, line in _python_string_literals(ROOT / "tools" / "notebook_read.py")
        if "--execute" in line or "-x " in line
    ]
    assert not offending, (
        "tools/notebook_read.py builds a command string containing --execute; "
        f"rendering must not be able to select a kernel: {offending}"
    )


def test_the_readme_tells_the_truth_about_how_to_read_them() -> None:
    """The README pointed at the broken path.

    `notebooks/README.md` said `make notebooks-read` is "the one to use for a
    manual check". That is a claim about which command works, made in the file a
    reader opens first, and it was wrong for as long as the bug existed. A
    document that recommends a broken command is worse than one that says
    nothing, because it costs the reader their time *and* their trust.

    So the recommendation is asserted: the two commands the README tells people to
    run have to exist as targets, and the read path has to be the one the README
    calls recommended.
    """
    readme = (ROOT / "notebooks" / "README.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "make notebooks-read" in readme, (
        "notebooks/README.md no longer says how to read the notebooks without "
        "running them"
    )
    assert re.search(r"^notebooks-read:", makefile, re.MULTILINE), (
        "notebooks/README.md recommends `make notebooks-read` and the Makefile "
        "has no such target"
    )
    assert re.search(r"^notebooks:", makefile, re.MULTILINE), (
        "notebooks/README.md recommends `make notebooks` and the Makefile has "
        "no such target"
    )


# ── the track parameterisation ───────────────────────────────────────────────
#
# `build_notebooks` and `check_notebooks` grew a `--track` so the ML workshop can
# use the same claim-checking gate. The risk is not that it breaks: the risk is that
# adding a track *silently changes* the analyst notebooks, which are the reason the
# tool exists. So these tests are about the default, not about the new thing.


def test_the_default_track_is_the_analyst_notebooks() -> None:
    """`--track` omitted must mean today's behaviour, exactly.

    The alternative -- a required argument, or a default that could be overridden by
    an environment variable -- makes "which track" answerable by something other than
    the command, and then `make notebooks` stops meaning the analyst series.
    """
    assert build_notebooks.track_paths("notebooks") == (
        build_notebooks.ROOT / "notebooks" / "src",
        build_notebooks.ROOT / "notebooks",
    )


def test_an_unknown_track_refuses_rather_than_building_nothing() -> None:
    """A typo must fail loudly.

    `build()` globs a directory. Pointed at one that does not exist it returns an
    empty dict, and an empty dict is indistinguishable from "everything is up to
    date" -- so a typo would give a green build that generated nothing, and the
    Makefile target after it would still run.
    """
    with pytest.raises(SystemExit, match="unknown notebook track"):
        build_notebooks.track_paths("worksho")


def test_the_workshop_track_is_csv_backed_and_the_analyst_one_is_not() -> None:
    """The tracks differ in *kind*, not just in directory, and the flag says so.

    A database-backed track can have its `sql` fences executed and can be
    fingerprinted. A CSV-backed one has neither, and a gate that asked for them
    anyway would either fail on a track with no database to miss, or pass vacuously.
    """
    assert "notebooks" in check_notebooks.DATABACKED_TRACKS
    assert "workshop" not in check_notebooks.DATABACKED_TRACKS
    assert set(build_notebooks.TRACKS) >= set(check_notebooks.DATABACKED_TRACKS)
