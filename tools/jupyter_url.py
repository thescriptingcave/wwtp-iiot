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
import os
import re
import shutil
import socket
import subprocess
import sys
import time
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


#: Environment variables that mean "there is a desktop to open a window on".
#: Linux and the BSDs; macOS and Windows have no such thing and are not checked,
#: because their `webbrowser` works without one and inventing a requirement would
#: break the machine this was tested on first.
DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY")


def headless() -> bool:
    """Is there no desktop to open a browser on?

    **Linux only, and deliberately.** This is the whole difference between
    working on a laptop and not working on a server, and it is invisible from
    Python: `webbrowser.open` on a headless Linux box raises `Error: could not
    locate runnable browser`, and on some builds returns `False`, and on others
    *succeeds* by handing the URL to `xdg-open`, which then fails silently in the
    background. All three are the same situation and only one of them says so.

    Two ways out, both respected:

    * `BROWSER` set — somebody has told the machine how to open a URL, which is
      the standard override for exactly this case (a remote-desktop wrapper, an
      SSH-forwarded browser). That wins over the absence of `DISPLAY`.
    * no `DISPLAY`/`WAYLAND_DISPLAY` at all.

    macOS and Windows are excluded rather than tested-for, because requiring a
    display variable there would be inventing a rule the platform does not have.
    """
    if not sys.platform.startswith(("linux", "freebsd", "openbsd", "netbsd")):
        return False
    if os.environ.get("BROWSER"):
        return False
    return not any(os.environ.get(name) for name in DISPLAY_VARS)


def _open(url: str) -> int:
    """Open `url`, and say plainly whether that worked.

    Three outcomes, and only the middle one is a success:

    * **no desktop** — a message naming the URL, and exit 0. This is not a
      failure: on a headless box there is nothing to fail at, and returning 1
      would make `make` report a broken gate for a server that is working.
    * **opened** — a confirmation.
    * **tried and could not** — the URL, and exit 1, because something was
      supposed to happen and did not.
    """
    if headless():
        print("  no display on this machine, so no browser to open.")
        print(f"  open it yourself, or forward the port: {url}")
        print("  (ssh -L 8899:127.0.0.1:8899 <this host>, then use 127.0.0.1:8899)")
        return 0
    if not webbrowser.open(url):
        print("  (could not launch a browser — open the URL above by hand)",
              file=sys.stderr)
        return 1
    print("  opening your browser")
    return 0


def wait_for_server(port: int, seconds: float = 30.0) -> bool:
    """Block until the port answers, or the deadline passes.

    `make notebooks-open` starts the server *after* handing us the URL, so an
    opener that does not wait races it and opens a refused connection. Polling
    the port is the smallest thing that removes the race, and it needs no
    dependency: a `socket.connect_ex` that returns 0 is a server accepting
    connections, which is the only fact that matters before a browser is sent.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


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
        "--wait", type=float, metavar="SECONDS", default=None,
        help="wait up to SECONDS for the port to answer, then act",
    )
    parser.add_argument(
        "--stop", action="store_true", help="stop that server instead",
    )
    args = parser.parse_args(argv)

    if args.stop:
        return stop(args.port)

    if args.wait is not None and not wait_for_server(args.port, args.wait):
        print(
            f"  nothing is listening on {args.port} after {args.wait:g}s; "
            "not opening a browser for a server that did not start",
            file=sys.stderr,
        )
        return 1

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
        return _open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
