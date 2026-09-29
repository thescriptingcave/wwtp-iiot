"""The Makefile hands every recipe's Python `.env`, whatever make does with it.

GNU make has a *fast path*: a recipe line with no shell metacharacters is forked
and exec'd directly, and `$(SHELL)` never starts — so `BASH_ENV` is never read,
`tools/env.sh` never runs, and the command inherits make's own environment. The
symptom is a connection to port 5432 with the wrong password, on a project whose
database is on 55433, in a target whose neighbour works.

These tests pin the mechanism rather than the symptom, because the symptom
(`OperationalError`) is what every other database test in this suite would also
raise, and none of them can say *why*.

Two details are load-bearing, and both were wrong in the first draft of this
file:

* The environment is stripped with `env -i`. A developer shell that has already
  run `set -a; . .env` makes the bug disappear, which is how it survived: the
  author had been running `make` from a shell where the fast path was invisible.
* The probe recipe has **no quotes and no metacharacters at all** — a script
  path, like the real `$(PY) -m tools.notebook_data`. A probe written as
  `$(PY) -c "..."` takes the *shell* path, so it passes whether or not the bug
  exists. It passed against the broken Makefile, and that is why this note is
  here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
WRAPPER = ROOT / "tools" / "py.sh"

#: Prints one environment variable, or `NONE`. A file rather than `python -c`
#: precisely so the recipe line quoting it appears in can contain no quote.
PROBE_SOURCE = """\
import os
import sys
print(os.environ.get(sys.argv[1]) or "NONE")
"""

#: The environment make is given: no `.env` in it, on purpose.
CLEAN_ENV = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "HOME": os.environ.get("HOME", "/"),
}


def _env_value(name: str) -> str:
    """One `KEY=value` out of `.env`, without sourcing the whole file."""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"')
    pytest.skip(f"{name} is not in .env")


def _run(args: list[str], env: dict[str, str] | None = None) -> str:
    make = shutil.which("make") or "/usr/bin/make"
    result = subprocess.run(
        [make, "--no-print-directory", *args],
        capture_output=True, text=True, env=env or CLEAN_ENV, check=False, timeout=300,
    )
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """`(probe.py, probe.mk)`: the two files the fast-path test needs.

    The Makefile is written to a temporary directory and `include`s the real one,
    so the real `PY` and the real `BASH_ENV` are in force while the target under
    test is the only thing that is new. A target added to the real Makefile for
    this purpose would be a target somebody eventually runs by hand.
    """
    directory = tmp_path_factory.mktemp("fastpath")
    script = directory / "probe.py"
    script.write_text(PROBE_SOURCE, encoding="utf-8")
    makefile = directory / "probe.mk"
    # Two recipe lines that differ by one character. The first has no shell
    # metacharacter, so make execs it; the second ends in `;`, so make starts
    # bash. Identical work, identical environment, opposite paths.
    makefile.write_text(
        f"include {MAKEFILE}\n"
        f"FAST := {script}\n"
        "fast-path-probe:\n"
        "\t@$(PY) $(FAST) POSTGRES_PORT\n"
        "slow-path-probe:\n"
        "\t@$(PY) $(FAST) POSTGRES_PORT ;\n",
        encoding="utf-8",
    )
    return script, makefile


def test_wrapper_exists_and_is_executable() -> None:
    """`$(PY)` is the wrapper, so the wrapper has to be there and runnable."""
    assert WRAPPER.is_file(), "tools/py.sh is missing; $(PY) points at it"
    assert os.access(WRAPPER, os.X_OK), "tools/py.sh is not executable"


def test_wrapper_gives_python_the_env_file() -> None:
    """Run directly, with nothing in the environment: the value must arrive.

    This is the whole fix. `env -i` is the point — a shell that has already
    sourced `.env` would pass a broken wrapper too.
    """
    result = subprocess.run(
        [str(WRAPPER), "-c", PROBE_SOURCE, "POSTGRES_PORT"],
        capture_output=True, text=True, env=CLEAN_ENV, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert _env_value("POSTGRES_PORT") in result.stdout, (
        f"tools/py.sh did not load .env into python: "
        f"{result.stdout.strip()!r}. Port 5432 here means .env was never read."
    )


def test_makefile_points_py_at_the_wrapper() -> None:
    """`PY` is a path under `tools/`, not the bare interpreter.

    A regression here is the whole bug returning: the fast path can only be
    beaten by the command itself being a shell, and the interpreter is not.
    """
    assigned = [
        line for line in MAKEFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith(("PY :=", "PY="))
    ]
    assert assigned, "the Makefile no longer defines PY"
    assert "tools/py.sh" in assigned[0], (
        f"PY is {assigned[0]!r}; make's fast path will exec it directly and skip .env"
    )


def test_recipe_without_metacharacters_still_sees_the_env(
    probe: tuple[Path, Path],
) -> None:
    """The failing path: no quotes, no metacharacters, and still `.env`.

    Observed before the fix, against the real target and not a probe:
    `make notebooks-data` asked for port 5432 while `make query` — one line
    away, differing only in containing a `"` — worked.
    """
    _, makefile = probe
    out = _run(["-C", str(ROOT), "-f", str(makefile), "fast-path-probe"])
    assert _env_value("POSTGRES_PORT") in out, (
        f"a recipe with no metacharacters lost .env: {out.strip()!r}. "
        "This is make's fast path exec'ing the command without a shell."
    )


def test_both_recipe_paths_see_the_same_environment(
    probe: tuple[Path, Path],
) -> None:
    """One character of difference must not change what a recipe can see.

    The bisection, kept as a test. Before the fix the `;` line printed the port
    and the line without it printed `NONE` — same target, same make, same `.env`.
    If the two ever disagree again the fast path has come back.
    """
    _, makefile = probe
    fast = _run(["-C", str(ROOT), "-f", str(makefile), "fast-path-probe"])
    slow = _run(["-C", str(ROOT), "-f", str(makefile), "slow-path-probe"])
    assert fast.split() == slow.split(), (
        f"the two make paths disagree: fast={fast.strip()!r} slow={slow.strip()!r}"
    )


def test_bash_env_reaches_a_shell_recipe(probe: tuple[Path, Path]) -> None:
    """The mechanism the wrapper replaces is still worth having working.

    Recipes that are not `$(PY)` — `docker compose`, `psql`, a `curl` — depend on
    `BASH_ENV` alone. If that regressed, the wrapper would hide it: the `$(PY)`
    tests would still pass, because the wrapper sources `.env` itself.
    """
    _, makefile = probe
    out = _run(["-C", str(ROOT), "-f", str(makefile), "slow-path-probe"])
    assert _env_value("POSTGRES_PORT") in out, (
        f"BASH_ENV did not reach a shell recipe: {out.strip()!r}"
    )


def test_a_missing_env_is_a_warning_and_not_a_failure(
    tmp_path: Path,
) -> None:
    """`.env` is gitignored, so a checkout has none — and CI is a checkout.

    `env.sh` is sourced at the top of *every* recipe, so a bare `. .env` made the
    absence total: every target failed with `No such file or directory`, including
    the ones with nothing to do with the environment. It cost a CI run as well —
    four tests in this file invoke the real `$(PY)`, and the runner has no `.env`
    by design.

    Reproduced by pointing the wrapper at a root with no `.env`, rather than by
    deleting the developer's: the file under test reads its own location, so
    `tools/env.sh` copied next to a temporary `tools/py.sh` is a self-contained
    instance of the same two files.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "tools").mkdir()
        (root / ".venv" / "bin").mkdir(parents=True)
        for name in ("env.sh", "py.sh"):
            (root / "tools" / name).write_text(
                (ROOT / "tools" / name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        (root / "tools" / "py.sh").chmod(0o755)
        # A stand-in for the interpreter, so the assertion is about the
        # environment reaching *something*, not about which python is installed.
        (root / ".venv" / "bin" / "python").write_text(
            "#!/bin/sh\nexec /usr/bin/env python3 \"$@\"\n", encoding="utf-8",
        )
        (root / ".venv" / "bin" / "python").chmod(0o755)
        assert not (root / ".env").exists()

        result = subprocess.run(
            [str(root / "tools" / "py.sh"), "-c", "print('ran')"],
            capture_output=True, text=True, env=CLEAN_ENV, check=False, timeout=120,
        )

    assert result.returncode == 0, (
        f"no .env and the wrapper failed anyway: {result.stderr.strip()!r}. "
        "A gitignored file cannot be a hard dependency of every recipe."
    )
    assert "ran" in result.stdout, result.stdout
    assert "no" in result.stderr and ".env" in result.stderr, (
        f"a missing .env has to say so: stderr was {result.stderr.strip()!r}"
    )
