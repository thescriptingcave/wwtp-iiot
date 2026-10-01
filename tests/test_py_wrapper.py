"""`tools/py.sh` must not exec an interpreter that is not there.

`$(PY)` is `tools/py.sh`, and it appears in 51 recipes. On a checkout where
`uv sync` has never run there is no `.venv`, so the `exec` fails with

    /path/tools/py.sh: line 63: /path/tools/../.venv/bin/python:
      No such file or directory

which names a line of a shell script, not the missing thing, and not the command
that creates it. Worse, it fails *quietly* in the places that matter most:
`db-live` calls `$(PY)`, so on a fresh clone every one of its sixty polls failed for
the same reason, and `make sql` reported `the database did not become reachable in
60s` about a database that was up and healthy the whole time.

That is the failure this file prevents. It is a *shell* property, so these tests run
the real script with the interpreter hidden — the same way the bug is found.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PY_SH = Path("tools/py.sh")


@pytest.mark.skipif(not shutil.which("bash"), reason="no bash")
def test_a_missing_interpreter_names_the_problem_and_the_fix(tmp_path) -> None:
    """Hide `.venv`, run the real wrapper, and read what it says.

    The venv is moved rather than deleted and it is restored in a `finally`, because
    a test that can leave the repository uninstalled is worse than no test.
    """
    venv = Path(".venv").resolve()
    hidden = Path(tmp_path / "venv-hidden")
    if not venv.exists():
        pytest.skip("no .venv here; nothing to hide")

    shutil.move(str(venv), str(hidden))
    try:
        proc = subprocess.run(
            ["bash", str(PY_SH), "-c", "print(1)"],
            capture_output=True, text=True, check=False, env=dict(os.environ),
        )
    finally:
        shutil.move(str(hidden), str(venv))

    assert proc.returncode != 0, (
        "py.sh exec'd something and exited 0 with no .venv present. The whole point "
        "is that this fails."
    )
    err = proc.stderr
    assert "no project Python" in err, (
        f"the failure does not say what is missing:\n{err}"
    )
    assert "make sync" in err, (
        f"the failure does not say how to fix it:\n{err}"
    )
    assert "line 63" not in err, (
        "the raw `exec` error is still reaching the user, which is the version this "
        "test exists to remove"
    )


@pytest.mark.skipif(not shutil.which("bash"), reason="no bash")
def test_the_wrapper_still_works_when_the_interpreter_is_present() -> None:
    """The other half: a guard that fires when it should not is just a new failure.

    `$(PY)` in 51 recipes is the project's Python. A check on the path that is
    subtly wrong would break every one of them at once, and the first sign would be
    a gate failing somewhere unrelated.
    """
    proc = subprocess.run(
        ["bash", str(PY_SH), "-c", "import sys; print(sys.executable)"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"py.sh fails with a venv present:\n{proc.stderr}"
    assert ".venv" in proc.stdout, (
        f"py.sh ran something other than the project Python: {proc.stdout!r}"
    )


def test_the_targets_that_need_python_depend_on_sync() -> None:
    """`db-up` needs the project installed, and now says so by depending on it.

    The other way round is the trap: `db-up` used `$(PY)` to decide whether the
    project was installed, so on a machine where it was not, the check itself could
    not run. A prerequisite is the only arrangement where the absence of the thing
    can be handled.
    """
    makefile = Path("Makefile").read_text(encoding="utf-8")
    for target in ("db-up", "notebooks-open"):
        found = re.search(rf"^{target}:(.*)$", makefile, re.M)
        assert found, f"no {target} target; the Makefile moved"
        prerequisites = found.group(1)
        assert "sync" in prerequisites, (
            f"`{target}` does not depend on `sync`, so it will run before the project "
            f"is installed and fail in a way that names neither the cause nor the fix"
        )
