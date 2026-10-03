"""No runnable documentation may pair `POSTGRES_DB=` with `--reset`.

**The bug this exists to stop, which was in the documentation and not the code.**
`workshops/ml/README.md` used to teach this as the way to build the workshop
dataset:

```text
POSTGRES_DB=wwtp_ml tools/py.sh -m storage.seed.main --days 175 ... --reset
```

It does not seed `wwtp_ml`. `tools/py.sh` sources `tools/env.sh`, which sources
`.env` **after** the inherited environment, so the assignment in front of the command
is overwritten by whatever `.env` says -- which is `POSTGRES_DB=wwtp`. The command
therefore seeds *the plant's own database*, with `--reset`, and a reader following
the workshop's own README loses their data.

Two defences exist and neither is this one:

- `make workshop-seed` passes `--database` as an **argument**, which the environment
  cannot override, and `storage/seed/main.py::_target_database` refuses `--reset`
  without one. The code is safe.
- The README now shows `make workshop`, and quotes the old command inside a ```text
  fence rather than a ```bash one.

**Why the fence language is the whole test.** A naive grep for `POSTGRES_DB=` fails,
because the README still contains that string -- inside the warning explaining why
not to use it. This is the recurring failure in this repository: a check that reads
text cannot tell a thing from the sentence about the thing. Parsing the fences and
keying on the *language* separates advice from a runnable example, which is the only
distinction Markdown actually has.

**Scope: every markdown file in the repo**, not just the workshop README, because the
failure is not specific to one page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: A fenced block, capturing its info string. Fences are ``` or ~~~ with >=3 chars,
#: and the info string is everything up to the closing fence.
FENCE = re.compile(
    r"^(?P<ticks>`{3,}|~{3,})(?P<lang>[^\n`]*)\n(?P<body>.*?)^(?P=ticks)\s*$",
    re.M | re.S,
)

#: Shell fences, plus no-arg fences, which are the ones a reader will run.
RUNNABLE = {"", "bash", "sh", "shell", "console", "shell-session", "zsh"}

#: The two halves of the mistake. Either alone is fine: plenty of commands reset a
#: database, and plenty mention POSTGRES_DB. Together on one line they mean the
#: environment is being asked to choose the target of a destructive operation.
DESTRUCTIVE = re.compile(r"POSTGRES_DB\s*=")
RESET = re.compile(r"--reset\b|\bdropdb\b|\bDROP\s+DATABASE\b", re.I)


def markdown_files() -> list[Path]:
    """Every markdown file in the project, skipping vendored and generated trees.

    **The docstring used to say "tracked", and the code did not check.** It
    skipped a list of vendor directories and globbed everything else, so the
    corpus depended on what happened to be lying around in the working copy --
    and `docs/TESTING.md`'s count for this file was therefore measuring my
    machine rather than the repository. `make test` runs pytest with
    `-p no:cacheprovider` and CI does too, so `.pytest_cache/README.md` exists
    locally and not there, and the file collected 180 tests on one and 178 on
    the other. That is a documented figure failing in CI for a reason that has
    nothing to do with the thing it documents.

    Two ways to fix it: glob only tracked files with `git ls-files`, or skip the
    trees that are not the project's. **Skipping is right here** -- `git` is not
    a dependency of a test, and a source tarball has no index -- so the docstring
    is corrected to describe what the code does rather than the other way round.
    A comment that overstates what the code checks is a small lie that costs an
    afternoon.
    """
    skip = {".venv", "node_modules", ".git", "__pycache__", "dist", "build",
            ".pytest_cache", ".ruff_cache", ".mypy_cache", "evidence"}
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in skip for part in path.parts)
    )


FILES = markdown_files()


def runnable_blocks(path: Path) -> list[tuple[int, str]]:
    """`(line number, body)` for every fence a reader could plausibly run."""
    text = path.read_text()
    found = []
    for match in FENCE.finditer(text):
        lang = match.group("lang").strip().split(" ")[0].lower()
        if lang not in RUNNABLE:
            continue
        line = text[: match.start()].count("\n") + 1
        found.append((line, match.group("body")))
    return found


def test_the_corpus_is_not_empty() -> None:
    """Otherwise a rename to `.md` would make every other test here vacuous."""
    assert len(FILES) > 20, f"only found {len(FILES)} markdown files; the glob is wrong"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_runnable_block_assigns_the_database_it_resets(path: Path) -> None:
    """Checked per **block**, not per line.

    Mutation testing found the gap: a bash fence with the assignment and the reset
    on separate lines passed a per-line check, because no single line contained
    both. That is still the same mistake -- the reader runs one fence, so the
    block is the unit that has to be safe.

    The false-positive cost is one loud message telling an author to split the
    fence, which is a much better failure than a reader running `POSTGRES_DB=`
    against the wrong database.
    """
    for line, body in runnable_blocks(path):
        offenders = [
            row.strip()
            for row in body.splitlines()
            if DESTRUCTIVE.search(row) or RESET.search(row)
        ]
        has_assignment = DESTRUCTIVE.search(body)
        has_reset = RESET.search(body)
        assert not (has_assignment and has_reset), (
            f"{path.relative_to(ROOT)}:{line} is a runnable block that assigns "
            f"POSTGRES_DB and resets a database:\n"
            + "".join(f"    {row}\n" for row in offenders)
            + "`.env` is sourced after the inherited environment, so the assignment "
            "does not win -- this resets the database named in `.env`, which for "
            "POSTGRES_DB is the plant's own `wwtp`. Pass `--database` as an "
            "argument instead."
        )


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_workshop_readme_quotes_the_mistake_as_text_not_bash(path: Path) -> None:
    """The counter-example must stay quotable without becoming runnable again.

    Without this, someone tidying the README can "fix" the ```text fence back to
    ```bash, every check still passes, and the advice becomes a command.
    """
    if path.name != "README.md" or "workshops" not in path.parts:
        return
    text = path.read_text()
    assert "POSTGRES_DB=wwtp_ml" in text, (
        "the workshop README no longer quotes the command it warns against, so the "
        "warning has lost the specific thing that makes it convincing."
    )
    for match in FENCE.finditer(text):
        if "POSTGRES_DB=wwtp_ml" not in match.group("body"):
            continue
        lang = match.group("lang").strip().split(" ")[0].lower()
        assert lang not in RUNNABLE, (
            f"the counter-example is in a `{lang or 'plain'}` fence. It must be "
            "`text`, so that it is quotable and not runnable -- that distinction "
            "is what the fence language is for."
        )
