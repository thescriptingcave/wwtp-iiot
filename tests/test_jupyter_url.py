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

import re
import socket
import subprocess
from pathlib import Path

import pytest
from tools import jupyter_url
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


# ── the browser, which is the part that differs on every machine ─────────────


def test_linux_with_no_display_is_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case this whole piece of code exists for.

    Verified for real as well as here: the same function returns `True` in a
    `python:3.13-slim` container with `DISPLAY` unset and `False` in the same
    image with `DISPLAY=:0`. A mock would have proved only that the mock agreed
    with itself.
    """
    monkeypatch.setattr(jupyter_url.sys, "platform", "linux")
    for name in (*jupyter_url.DISPLAY_VARS, "BROWSER"):
        monkeypatch.delenv(name, raising=False)
    assert jupyter_url.headless() is True

    monkeypatch.setenv("DISPLAY", ":0")
    assert jupyter_url.headless() is False


def test_a_configured_browser_wins_over_a_missing_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BROWSER` is the standard override and it is respected.

    An SSH session with a forwarded browser, or a remote desktop, has no
    `DISPLAY` and does have a way to open URLs. Calling that machine headless
    tells a user who has already solved the problem that they have not.
    """
    monkeypatch.setattr(jupyter_url.sys, "platform", "linux")
    for name in jupyter_url.DISPLAY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BROWSER", "/usr/bin/firefox")
    assert jupyter_url.headless() is False


def test_macos_and_windows_are_never_reported_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is Linux-only, and inventing a rule elsewhere would break it.

    macOS and Windows have no `DISPLAY`, so a platform-agnostic version of this
    function would report *every* laptop as headless and refuse to open a
    browser anywhere. The platform test is the whole design, and the part most
    likely to be "simplified" away by somebody tidying the function later.
    """
    for name in (*jupyter_url.DISPLAY_VARS, "BROWSER"):
        monkeypatch.delenv(name, raising=False)
    for platform in ("darwin", "win32"):
        monkeypatch.setattr(jupyter_url.sys, "platform", platform)
        assert jupyter_url.headless() is False, f"{platform} was called headless"


def test_headless_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """No browser on a server is a normal state, and it exits 0.

    The distinction that matters: a headless box has *nothing to fail at*, so
    returning 1 there would make `make` report a broken gate for a server that is
    working perfectly. The other half — a browser that was supposed to open and
    did not — is exit 1. Both are asserted because they are the same function
    and the first is the easy one to get backwards.
    """
    monkeypatch.setattr(jupyter_url, "headless", lambda: True)
    monkeypatch.setattr(jupyter_url.webbrowser, "open", lambda url: True)
    assert jupyter_url._open("http://127.0.0.1:8899/lab?token=x") == 0

    # A desktop, and a browser that could not be launched. **The mock is changed
    # too**, because leaving it returning True made the second half of this test
    # assert against the first half's stub — a test that would have passed
    # whatever `_open` did, which is the failure mode this file exists to avoid.
    monkeypatch.setattr(jupyter_url, "headless", lambda: False)
    monkeypatch.setattr(jupyter_url.webbrowser, "open", lambda url: False)
    assert jupyter_url._open("http://127.0.0.1:8899/lab?token=x") == 1, (
        "webbrowser.open returning False means the browser did not open, and "
        "that is the case that has to be reported as a failure"
    )


def test_a_browser_that_will_never_open_does_not_hang_forever() -> None:
    """`--wait` gives up, and gives up saying so.

    The opener is backgrounded by `make notebooks-open` and races the server it
    is meant to open. Without a deadline that race is a hang on a machine where
    the server never comes up — and a hang in a background subshell is the worst
    possible failure: no output, no exit, nothing to diagnose.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    assert jupyter_url.wait_for_server(port, 0.5) is False, (
        "a port nothing is listening on was reported as ready"
    )


def test_the_opener_waits_for_a_server_that_is_listening() -> None:
    """The other half, so the first is not only a timer.

    A real listener on a real port, opened by this test. `wait_for_server` is
    what stands between the browser and a connection refused, and a test that
    only proved the timeout fires would pass if the function always returned
    False.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        assert jupyter_url.wait_for_server(server.getsockname()[1], 5.0) is True


def _recipe_of(target: str) -> str:
    r"""A Makefile target's **executed** recipe - and nothing else.

    The fifth appearance of the same trap in this repository, and the fifth time
    it was caught by mutating the code back to the bug. Two versions of this
    check were green with the flag deleted:

    * `"--no-browser" in makefile` - the word is in the comment block **above**
      the recipe, explaining why the flag is there.
    * `"--no-browser" in _recipe_of(...)` - and the recipe itself carries `@#`
      comment lines, which is where most of its explanation lives, including the
      flag's name.

    So this strips the two things a recipe can contain that make will not
    execute: lines that are not tab-indented (the file's prose), and lines whose
    first non-tab character is `#` or `@#`. What is left is what a shell
    receives. A `\` continuation is joined, or the flag can move to a second line
    and the same hole reappears.

    The general lesson, by now the most repeated in this repository: **a check
    that reads text cannot tell the thing from the sentence about the thing**,
    and prose about a flag always sits next to the flag.
    """
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    inside = False
    for line in lines:
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


def test_jupyter_is_still_told_not_to_open_its_own_browser() -> None:
    """`--no-browser` stays, and this is why it cannot simply be dropped.

    JupyterLab masks the token in the URL it prints *and* the one it opens
    (`token=...`), so letting it do the opening lands the user on a URL that
    cannot authenticate. This Makefile sends the real URL instead, from a tool
    that knows the token. Remove the flag without adding that and the browser
    opens a page that asks for a password.

    Read from the **executed recipe**. The flag is named in the comment above the
    recipe *and* in comment lines inside it, so both a whole-file and a
    whole-recipe substring check pass with the flag deleted. Both did.
    """
    recipe = _recipe_of("notebooks-open")
    assert recipe, "Makefile has no executed notebooks-open recipe"
    assert "--no-browser" in recipe, (
        "the notebooks-open recipe no longer passes --no-browser, so JupyterLab "
        "will open its own masked-token URL instead of this project's real one. "
        "The comment above the recipe explains why the flag has to stay."
    )
    assert "tools.jupyter_url --open" in recipe, (
        "nothing in the recipe opens the real URL, so --no-browser would mean "
        "no browser at all"
    )


def test_the_opener_is_not_silenced() -> None:
    """Its output is not sent to /dev/null.

    The first version backgrounded it with `>/dev/null 2>&1`, which is tidy and
    which makes the original complaint worse: "the browser does not start, what
    do I do" becomes unanswerable when the attempt itself says nothing. The whole
    value of this subprocess is the line it prints when it cannot open one.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    opener = [
        line for line in makefile.splitlines()
        if "tools.jupyter_url --open" in line and line.startswith("\t")
    ]
    assert opener, "no recipe line in the Makefile opens a browser"
    for line in opener:
        assert "/dev/null" not in line, (
            f"the browser opener's output is discarded: {line.strip()!r}. If it "
            "cannot open a browser, the reason and the URL are the answer, and "
            "discarding them leaves the reader with nothing."
        )
