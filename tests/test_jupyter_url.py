"""The JupyterLab URL, and the credential inside it.

`make notebooks-open` writes its address to `notebooks/.jupyter-url` so the token
survives a scroll-back, and `make notebooks-url` reads it back. The file contains
a **live credential**: it is the only thing between the address and anyone who
finds it, and this repository is **public**.

That makes the `.gitignore` rule load-bearing rather than tidiness, which is why
it is asserted here. Everything else in this file is ordinary; this one line is
the reason the convenience is safe to offer at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.jupyter_url import TOKEN_FLAG, _ours

ROOT = Path(__file__).resolve().parents[1]
URL_FILE = ROOT / "notebooks" / ".jupyter-url"


def _check_ignored() -> tuple[bool, str]:
    """Would git commit this file? Answered by git, not by reading `.gitignore`.

    `git check-ignore` rather than a regex over the ignore file, because the
    question being asked is the one git will actually act on. A pattern can look
    right and be overridden by a later negation, and reading the file cannot see
    that.
    """
    URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    URL_FILE.write_text("http://127.0.0.1:8899/lab?token=deadbeef\n", encoding="utf-8")
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-v", str(URL_FILE.relative_to(ROOT))],
            capture_output=True, text=True, check=False, cwd=ROOT, timeout=60,
        )
        return result.returncode == 0, result.stdout.strip()
    finally:
        URL_FILE.unlink(missing_ok=True)


def test_the_url_file_cannot_be_committed() -> None:
    """A token, gitignored, in a public repository.

    The failure this guards is not hypothetical and not subtle in its effect: one
    commit, and the token for the developer's JupyterLab — which, in the default
    `--no-browser` local setup, is a full read/write file server on their
    machine — is in a history that cannot be rewritten quietly and has already
    been fetched by everyone who cloned.

    The first version of `tools/jupyter_url.py` had no such rule, because writing
    a file under `notebooks/` looked like it would land inside the existing
    `notebooks/figures/` and `notebooks/read/` rules. It does not: those are
    directory patterns, and a dotfile at the top of `notebooks/` is not in either.
    Checked here rather than assumed, which is the only reason it is right.
    """
    ignored, rule = _check_ignored()
    assert ignored, (
        f"{URL_FILE.relative_to(ROOT)} holds the JupyterLab token and is NOT "
        "gitignored, so the next `git add -A` puts a live credential into a "
        "public history. Add the rule to .gitignore."
    )
    assert "notebooks/.jupyter-url" in rule, (
        f"it is ignored, but by an unexpected rule: {rule!r}. If that rule is "
        "something broad like `*.url` or `.*`, it is hiding the problem rather "
        "than stating the rule for this file."
    )


def test_the_url_file_is_not_tracked_already() -> None:
    """It must never have been committed, not merely be ignored now.

    `.gitignore` has no effect on history. If the file were already tracked,
    adding the rule would change nothing and every clone would still contain the
    token, so this is the check that would catch a rule added too late.
    """
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(URL_FILE.relative_to(ROOT))],
        capture_output=True, text=True, check=False, cwd=ROOT, timeout=60,
    )
    assert result.returncode != 0, (
        f"{URL_FILE.relative_to(ROOT)} is tracked. A .gitignore rule does not "
        "untrack it, and the token is in the history for every clone. Remove it "
        "with `git rm --cached` and treat the token as exposed."
    )


def test_both_token_flag_spellings_are_recognised() -> None:
    """`--ServerApp.token` is deprecated; `--IdentityProvider.token` is not.

    The installed jupyter-server prints

        ServerApp.token config is deprecated in 2.0. Use IdentityProvider.token

    on **every** start, and the Makefile is still on the old spelling. So the two
    versions of this repository will disagree, and a recovery tool that only knew
    one of them would work on exactly one of them — which is a bug that appears
    after a correct fix, and is therefore the worst time to find it.

    Both are matched deliberately, with the reason in the code.
    """
    for flag in ("--ServerApp.token=abc123", "--IdentityProvider.token=abc123"):
        found = TOKEN_FLAG.search(f"jupyterlab {flag} --ServerApp.port=8899")
        assert found, f"{flag} is not matched by TOKEN_FLAG"
        assert found.group(1) == "abc123"


def test_a_process_that_is_not_ours_is_never_matched() -> None:
    """`--stop` kills something, so it must not be able to kill the wrong thing.

    The match requires *both* that the command line mentions jupyter *and* that
    it carries this project's port. Port alone would kill any other server a
    developer happens to be running on 8899; `jupyter` alone would match a command
    with the word in an argument somewhere unrelated.

    This is asserted on the matcher rather than by starting a process, so it says
    something about the rule instead of about the machine's process table at the
    moment the test ran.
    """
    mine = "3412 python3.13 -m jupyterlab --notebook-dir=notebooks " \
           "--IdentityProvider.token=abc --ServerApp.port=8899"
    theirs = "999  postgres -D /var/lib/postgresql --port=8899"
    editor = "888 code --reuse-window --ServerApp.port=8899"

    assert _ours(mine, 8899)
    assert not _ours(theirs, 8899), "a database on our port was matched"
    assert not _ours(editor, 8899), (
        "a process that merely mentions the port and the flag was matched"
    )
    assert not _ours(mine, 9999), "our own jupyter, but on another port"
