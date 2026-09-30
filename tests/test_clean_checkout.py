"""A clean checkout must be able to run `make`, and that is a claim about files.

The first thing this project was asked to do on a *fresh clone* was `make`, and
it failed:

    warning: no /home/rharding/developer/wwtp-iiot/.env
    ── starting the database
    error while interpolating services.db.environment.POSTGRES_PASSWORD:
    required variable POSTGRES_PASSWORD is missing a value
    make: *** [Makefile:414: db-up] Error 1

`tools/env.sh` now creates `.env` from `.env.example`, so the reported failure
is fixed. **What is not fixed is why it was reachable at all**: `compose.yaml`
declares variables *required* with `${VAR:?…}`, and nothing checked that the
tracked template defines them. So adding one to compose, forgetting the template,
and breaking every clean checkout is a two-line change with no gate.

That is the shape of bug this repository keeps meeting — a claim that is only
true until the next edit, checked only on the machine where it was last true.
These are file-to-file claims, so they cost nothing to check and need no
database, no container and no clock.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `${VAR:?message}` — compose's "required, and tell the operator". The `:?` form
#: is the one that produces the error above; `${VAR:-default}` and `${VAR}` are
#: optional and cannot break a checkout.
REQUIRED_IN_COMPOSE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")

#: `VAR=value` in the template, at the start of a line and not commented out.
DEFINED_IN_EXAMPLE = re.compile(r"^([A-Z_][A-Z0-9_]*)=", re.MULTILINE)

#: Targets that reach the environment and therefore need `.env` to exist first.
#: `setup` is a prerequisite of each; this is the list that says so, and
#: `test_every_env_needing_target_depends_on_setup` is what checks it.
NEEDS_ENV = (
    "db-up", "sync", "check", "test", "integration", "sql", "lessons",
    "notebooks", "notebooks-open", "up", "seed",
)


def _required() -> set[str]:
    return set(REQUIRED_IN_COMPOSE.findall(
        (ROOT / "compose.yaml").read_text(encoding="utf-8")
    ))


def _defined() -> set[str]:
    return set(DEFINED_IN_EXAMPLE.findall(
        (ROOT / ".env.example").read_text(encoding="utf-8")
    ))


def _prerequisites(makefile: str) -> dict[str, set[str]]:
    """`target -> its direct prerequisites`, for every target in the file.

    Parsed off the `target: a b c ## doc` line rather than with make's own
    `--dry-run`, because the whole question is what a *reader* of the file can
    see. `make -n` expands the graph and would answer a different question: it
    would tell me what runs, not what is written down, and a dependency that only
    exists in an expansion is a dependency nobody can find.
    """
    out: dict[str, set[str]] = {}
    for line in makefile.splitlines():
        match = re.match(r"^([a-z][a-z-]*):(?!\=)(.*)$", line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2).split("##")[0]
        out[name] = {p for p in rest.split() if p and not p.startswith("$")}
    return out


def _reaches(prereqs: dict[str, set[str]], start: str, goal: str) -> bool:
    """Is `goal` reachable from `start` through the prerequisite graph?

    **Transitive, and that is the property that matters rather than a
    convenience.** Make finishes every prerequisite before it runs a recipe, so
    if `sql` reaches `setup` through `db-still` → `db-up`, then `setup` has
    already run by the time `sql`'s own recipe asks for a port. Requiring a
    *direct* `setup:` on all eleven targets would assert a shape nobody writes and
    would be deleted the first time somebody tidied the list.

    The guard is therefore on reachability, which is what actually determines
    whether the recipe sees an environment — and the breadth is bounded by the
    explicit `NEEDS_ENV` list, so this cannot quietly pass by checking nothing.
    """
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(prereqs.get(current, set()))
    return False


def test_every_env_needing_target_reaches_setup() -> None:
    """`.env` is made by `setup`, and every target that needs it can reach it.

    **This is the test that makes the Makefile shape safe.** Putting the
    bootstrap in a target rather than in `tools/env.sh` was the right call —
    `env.sh` is sourced by `tools/py.sh` and by every recipe, so writing a file
    from it created one part-way through a test run and made the suite
    order-dependent. The cost of a target is that the next person can forget the
    prerequisite, and this is what covers that: add a target that reads
    `POSTGRES_*` and cannot reach `setup`, and this fails.

    It reads the literal prerequisite list rather than running `make`, so what it
    checks is what the file *says* — the thing a reader has to be able to find.
    """
    prereqs = _prerequisites((ROOT / "Makefile").read_text(encoding="utf-8"))
    missing = [
        target for target in NEEDS_ENV
        if target in prereqs and not _reaches(prereqs, target, "setup")
    ]
    assert not missing, (
        f"Makefile targets {missing} reach the environment but cannot reach "
        "`setup`, so a clean checkout fails in them with a docker compose error "
        "naming a password."
    )


def test_setup_creates_env_from_the_template_and_never_overwrites(
    tmp_path: Path,
) -> None:
    """Both halves, on a temporary copy so nothing here is touched.

    "Creates it" and "does not overwrite it" are one behaviour from the operator's
    point of view and two from the code's, and only the second is dangerous if it
    is wrong: a bootstrap that clobbers a real `.env` replaces working
    credentials with `replace-me` and the failure looks like a database problem.
    """
    if not shutil.which("make"):
        return  # nothing to run it with; the shape test above still holds

    (tmp_path / "Makefile").write_text(
        "include " + str(ROOT / "Makefile") + "\n", encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text(
        "POSTGRES_PASSWORD=from-the-template\n", encoding="utf-8",
    )

    def run_setup() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["make", "--no-print-directory", "-C", str(tmp_path), "setup"],
            capture_output=True, text=True, check=False, timeout=120,
        )

    first = run_setup()
    created = tmp_path / ".env"
    assert first.returncode == 0, first.stderr
    assert created.is_file(), f"setup did not create .env: {first.stdout}"
    assert "POSTGRES_PASSWORD=from-the-template" in created.read_text()

    # Now make it look like a real one, and check it survives.
    created.write_text("POSTGRES_PASSWORD=mine\n", encoding="utf-8")
    second = run_setup()
    assert second.returncode == 0, second.stderr
    assert created.read_text() == "POSTGRES_PASSWORD=mine\n", (
        "setup overwrote an existing .env, which would replace real credentials "
        "with the template's placeholders"
    )


def test_every_required_compose_variable_is_in_the_template() -> None:
    """The reported bug, as a standing check.

    `compose.yaml` requires a variable; `.env.example` has to define it, because
    that file is what a clean checkout is built from. The failure when it does
    not is the one at the top of this file, and it is reported by
    `docker compose` as a password problem several steps away from its cause.

    This is the *reachable* version of the bug even now that `setup` exists: a
    variable added to `compose.yaml` with a `${VAR:?…}` and forgotten in the
    template means `make setup` succeeds, prints a reassuring line, and the
    failure arrives at the first `docker compose` call instead.
    """
    missing = sorted(_required() - _defined())
    assert not missing, (
        f"compose.yaml requires {missing} but .env.example does not define "
        "them, so every clean checkout fails at the first docker compose call. "
        "Add them to .env.example."
    )


def test_the_template_defines_nothing_empty() -> None:
    """A variable present but blank fails exactly like a missing one.

    `${VAR:?…}` treats `VAR=` as unset — compose's own test is "is there a
    non-empty value", not "is the name present". So a line like
    `POSTGRES_PASSWORD=` in the template would satisfy a name-based check and
    still break every checkout, which makes this the half that the previous test
    cannot see.
    """
    empty = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() in _required() and not value.strip().strip('"\''):
            empty.append(name.strip())
    assert not empty, (
        f".env.example defines {empty} with no value. compose's ${{VAR:?}} is "
        "satisfied by a non-empty value, not by the name being present, so this "
        "breaks a clean checkout in the same way as a missing line."
    )


#: Variables whose *name* says they are a credential. Scoped by name rather than
#: by value shape, because the first version of this scanned every value and
#: flagged `GATEWAY_DB_ROLE=wwtp_gateway` — a role name, not a secret. A check
#: that fires on ordinary configuration is a check that gets deleted, and this is
#: the fourth time in this repository that has happened.
CREDENTIAL_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|SECRET|TOKEN|_KEY|APIKEY|API_KEY)$"
)


def test_env_example_holds_no_real_credentials() -> None:
    """It is copied to disk on a clean machine, so it must not be a secret.

    `tools/env.sh` now writes this file verbatim into `.env` on a fresh checkout.
    That is only acceptable while it holds *no* real credential: `.env` is
    gitignored and the template is not, so anything real in the template is a
    credential published to a public repository by a convenience added for the
    sake of a first run.

    So every credential-named variable in the template must be a **placeholder**,
    and "placeholder" is judged by an explicit list of markers. A whitelist
    rather than a heuristic, because the failure is a secret in a public history
    and a heuristic that is merely good is not good enough.
    """
    placeholders = ("replace-me", "changeme", "change-me", "example", "your-")
    suspicious = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        if not CREDENTIAL_NAME.search(name):
            continue
        value = value.strip().strip('"\'')
        if not value:
            continue
        if any(marker in value.lower() for marker in placeholders):
            continue
        suspicious.append(f"{name}={value[:4]}…")
    assert not suspicious, (
        f".env.example holds a non-placeholder for {suspicious}. It is copied to "
        "disk verbatim by tools/env.sh on a clean checkout and it is tracked in "
        "a public repository, so a real value here is a published credential."
    )
