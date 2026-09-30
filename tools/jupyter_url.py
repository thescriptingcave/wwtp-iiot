"""Find the JupyterLab this project started, and open it.

    python -m tools.jupyter_url            # print the URL
    python -m tools.jupyter_url --open     # print it and hand it to the browser
    python -m tools.jupyter_url --stop     # stop the server it belongs to

## Why this file exists

`make notebooks-open` prints the URL it is about to serve, and then starts
JupyterLab — which logs about twenty lines *after* that, including a URL of its
own with the token masked as `token=...`. So the one line with a usable address
is pushed off the top of the screen within a second, on a terminal of any
ordinary height.

The recovery was a shell incantation, which is what this replaces:

    ps -p $(lsof -tnP -iTCP:8899 -sTCP:LISTEN) -o args= \
      | grep -oE '\\-\\-ServerApp\\.token=\\S+' | cut -d= -f2

That works, and it is not something to ask anyone to type. It also reaches
through the process table to a flag, so it breaks the moment the flag is
renamed — which is not hypothetical: `--ServerApp.token` is deprecated in favour
of `--IdentityProvider.token` in the installed jupyter-server, and the warning is
in the log of every single start.

## Three ways to find the token, in order of how much they trust

1. **The URL file.** `make notebooks-open` writes `notebooks/.jupyter-url`. It is
   a plain file the Makefile put there for exactly this purpose, and it is
   gitignored because it contains a token.
2. **The process's own command line.** Used when the file is missing — a server
   started by hand, or one whose file was deleted. This is the `ps` incantation
   above, and it is tried against *both* flag spellings, because the deprecated
   one is what is in the Makefile today and the supported one is what a future
   edit will produce.
3. **Nothing.** Then it says so, and says how to start one.

## Why it does not simply open a browser on `make`

`--no-browser` is in the Makefile on purpose (commit 04985e7: JupyterLab 4 masks
its own token in the log, so the address worth opening is the Makefile's, not the
server's). But `--no-browser` is also what makes `make` on a headless box, over
SSH, or in a container do something other than hang on a browser that will never
appear. So the browser is opened *on request*, by `--open`, where failing to open
one is a message rather than a stall.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL_FILE = ROOT / "notebooks" / ".jupyter-url"

#: The port the Makefile pins. Kept here as a fallback for the message only —
#: the *token* can never be guessed, which is why this file exists at all.
DEFAULT_PORT = 8899

#: Both spellings of the token flag. The first is deprecated in the installed
#: jupyter-server and the second is what it wants; the Makefile is on the first
#: and will move to the second, and a recovery tool that only knew one of them
#: would work on exactly one of the two versions of this repository.
TOKEN_FLAG = re.compile(r"--(?:ServerApp|IdentityProvider)\.token=(\S+)")


def _from_file() -> str | None:
    """The URL the Makefile wrote, if it is still there."""
    try:
        url = URL_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return url or None


def _ours(command_line: str, port: int) -> bool:
    """Is this process line one of this project's JupyterLabs?

    **Both** conditions, and the pair is the point:

    * it mentions `jupyter`, so a database that happens to sit on the same port
      is not ours;
    * it carries `--ServerApp.port=<port>`, so a jupyter on some *other* port —
      another project, another virtualenv — is not ours either.

    Port alone would kill whatever the developer is running on 8899.
    `jupyter` alone would match a command with the word in an argument
    somewhere unrelated, and `--stop` acts on a match. A kill is the one thing in
    this file that cannot be undone, so the rule it uses is the one that gets
    its own test (`tests/test_jupyter_url.py`) rather than being trusted to this
    function's two `if` statements.
    """
    return "jupyter" in command_line and f"--ServerApp.port={port}" in command_line


def _from_process(port: int) -> str | None:
    """Recover the token from the running server's own command line.

    `ps` rather than reading `/proc`, because this has to work on macOS, where
    there is no `/proc`. The line is matched through `_ours` — the same rule
    `--stop` uses — so a URL is never printed for a process that is not
    something we could actually stop.
    """
    if not shutil.which("ps"):
        return None
    result = subprocess.run(
        ["ps", "-Ao", "pid=,args="],
        capture_output=True, text=True, check=False, timeout=30,
    )
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not _ours(line, port):
            continue
        found = TOKEN_FLAG.search(line)
        if found:
            return f"http://127.0.0.1:{port}/lab?token={found.group(1)}"
    return None


def find(port: int = DEFAULT_PORT) -> str | None:
    """The URL of a running server, or None."""
    return _from_file() or _from_process(port)


def stop(port: int = DEFAULT_PORT) -> int:
    """Stop the server, if it is one of ours.

    Matches on the port **and** on the process being a jupyter, so this cannot
    kill an unrelated listener that happens to be on 8899. That check is the whole
    reason this is a script rather than a `pkill -f 8899` in the docs.
    """
    if not shutil.which("ps"):
        print("  no `ps` on this machine; stop JupyterLab with Ctrl-C", file=sys.stderr)
        return 1
    result = subprocess.run(
        ["ps", "-Ao", "pid=,args="], capture_output=True, text=True, check=False,
        timeout=30,
    )
    pids = [
        line.split(None, 1)[0]
        for line in result.stdout.splitlines()
        if line.strip() and _ours(line, port)
    ]
    if not pids:
        print(f"  nothing of ours is listening on {port}")
        return 0
    for pid in pids:
        subprocess.run(["kill", pid], check=False, timeout=30)
    print(f"  stopped {len(pids)} process(es) on {port}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jupyter-url",
        description="Find the JupyterLab this project started.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--open", action="store_true",
        help="hand the URL to the browser as well as printing it",
    )
    parser.add_argument(
        "--stop", action="store_true", help="stop that server instead",
    )
    args = parser.parse_args(argv)

    if args.stop:
        return stop(args.port)

    url = find(args.port)
    if not url:
        print(
            f"  no JupyterLab of this project is running on {args.port}.\n"
            "  start one:  make          (or: make notebooks-open)\n"
            f"  then:       make notebooks-url",
            file=sys.stderr,
        )
        return 1

    print(f"  {url}")
    if args.open:
        # `webbrowser` returns False rather than raising when there is no browser,
        # and a silent no-op here is the same confusing nothing the user already
        # hit. So say so.
        if not webbrowser.open(url):
            print("  (could not launch a browser — open the URL above by hand)",
                  file=sys.stderr)
            return 1
        print("  opening your browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
