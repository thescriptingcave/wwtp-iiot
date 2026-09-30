"""The `.ipynb` files are generated, and a Jupyter session dirties them.

Notebook 01's first review came with a surprise in the working tree: every one of
the eleven `.ipynb` files modified, 1,399 insertions. Nothing had been edited —
the notebooks had simply been *run*, which is what JupyterLab does on save:

    "execution_count": 13,          was null
    "outputs": [ … ],               was []

`notebooks/*.ipynb` is generated from `notebooks/src/*.md`, and the claims live in
the markdown's ` ```output ` fences, so a session's output is not a result to keep
— it is drift, and `tools/check_notebooks.py` compares the file to a fresh build
and fails.

The recovery is `make notebooks-reset`, and it is worth a test for the same reason
every other target here has one: the thing it does is *discard work*, which is
exactly the kind of target that silently stops doing its job. And the second half
— the reporting — is what distinguishes a deliberate discard from a lost edit.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"


def _recipe_of(target: str) -> str:
    """A target's executed recipe — tab-indented lines, minus its comments.

    The same helper shape as `tests/test_jupyter_url.py`, and for the same reason:
    a whole-file substring check reads the prose around a target rather than what
    make runs, and both files in this project have prose that names things the
    check is looking for.
    """
    body: list[str] = []
    inside = False
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if re.match(rf"^{re.escape(target)}:", line):
            inside = True
            continue
        if not inside:
            continue
        if not line.strip():
            if body:
                break
            continue
        if not line.startswith("\t"):
            break
        stripped = line.strip()
        if stripped.startswith(("#", "@#")):
            continue
        body.append(stripped.rstrip("\\").strip())
    return " ".join(body)


def test_the_reset_target_exists_and_regenerates() -> None:
    """It has to do the work, not just report that there is work.

    A target that prints a diff and stops would be a worse trap than none: it
    would look like a fix, leave the churn in place, and the failure would still
    arrive at the gate.
    """
    recipe = _recipe_of("notebooks-reset")
    assert recipe, "Makefile has no executed notebooks-reset recipe"
    assert "notebooks-build" in recipe, (
        "notebooks-reset does not rebuild, so it would report the churn and "
        "leave it on disk"
    )


def test_the_reset_target_reports_before_it_discards() -> None:
    """The order is the point, and it is the whole reason this target exists.

    A recovery that silently reverts is indistinguishable from one that ate an
    edit. The `.ipynb` files are generated so a hand-edit there is always wrong —
    but "always wrong" is not "never written", and someone who spent an hour in
    a notebook deserves to see the size of what just went before it goes.
    """
    recipe = _recipe_of("notebooks-reset")
    assert "git diff --stat" in recipe, (
        "notebooks-reset does not report what it is about to discard"
    )
    reported = recipe.index("git diff --stat")
    rebuilt = recipe.index("notebooks-build")
    assert reported < rebuilt, (
        "notebooks-reset rebuilds before it reports, so the reader never learns "
        "what was discarded"
    )


def test_the_reset_target_only_touches_notebooks() -> None:
    """It must not be able to revert anything else in the tree.

    The recipe runs `git diff` and then regenerates. Both are narrow, but the
    guard is that the paths are *named* rather than left to a default, because a
    recovery command that silently reverts the source markdown would destroy the
    real work instead of the generated copy of it.
    """
    recipe = _recipe_of("notebooks-reset")
    assert "notebooks/*.ipynb" in recipe, (
        "notebooks-reset does not scope its git diff to the generated notebooks, "
        "so it could revert source the user meant to keep"
    )
    assert "checkout" not in recipe and "reset --hard" not in recipe, (
        "notebooks-reset uses a git command that can revert more than the "
        "generated notebooks"
    )


def test_a_saved_session_is_exactly_what_the_gate_rejects() -> None:
    """The premise, checked against the gate rather than remembered.

    A cell with `execution_count` set and a non-empty `outputs` is what Jupyter
    writes. If the builder ever starts *emitting* those, this premise becomes
    false and the whole recovery dance is pointless — so it is asserted against
    the builder's own output rather than about a file on disk.
    """
    built = subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), "notebooks-build"],
        capture_output=True, text=True, check=False, timeout=900,
    )
    assert built.returncode == 0, built.stderr

    notebook = json.loads(
        (ROOT / "notebooks" / "01-meet-the-plant.ipynb").read_text(encoding="utf-8")
    )
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        assert cell.get("execution_count") is None, (
            "a generated notebook now carries an execution count, so a Jupyter "
            "session no longer dirties it and this file's premise is stale"
        )
        assert not cell.get("outputs"), (
            "a generated notebook now carries outputs, so the claims are in the "
            "file as well as the markdown and the two can disagree"
        )
