"""Run every snippet in the lesson courses against a live server.

    python tools/check_lessons.py             # check the python courses
    python tools/check_lessons.py --verbose   # show each snippet's output

`tools/check_sql.py` proves every query in the SQL course runs. It cannot prove
this, because a SQL block proves nothing about a protocol: a query that returns
no rows is indistinguishable from a correct query, and an OPC UA snippet that
never connects to a server is indistinguishable from one that does. So this gate
does what the SQL one cannot — it **starts a server, connects a client, and runs
the snippet**, and fails if any of that does not work.

## The convention

A lesson's Python block is executable when it uses the names the runner injects:

| name | what it is |
|---|---|
| `client` | an `asyncua.Client`, already connected |
| `root`   | the plant's root object node — start browsing here |
| `space`  | the server-side `AddressSpace`, for the tests a lesson quotes |
| `server` | the `OpcUaServer`, so a lesson can *drive* the plant |
| `ua`     | `asyncua.ua`, for NodeIds and StatusCodes |
| `asyncio`| so a snippet can sleep to let a subscription deliver |

`server` is there because a lesson about reading needs something to read, and a
lesson about subscriptions needs something to deliver: stage a value with
`server.set_value(...)`, set a run state, `await server.publish()`. Lesson 03
uses it to show a degraded quality, which is otherwise not reachable from a bare
server that nothing is driving.

The runner injects them rather than making every snippet re-implement a
connection, because the connection is the boring part and the browsing is the
part being taught. A snippet that *wants* to connect itself can, and should.

## Skipping a block

Some blocks must not run — a snippet that deliberately provokes an error, or one
that needs a fault injected. Mark it with an HTML comment on the line before, the
same convention `check_sql.py` uses:

    <!-- check: skip -->

`--list` prints what was found and what was skipped, which is the first thing to
run when a block is not being picked up and you cannot see why.

### Two things a snippet cannot assume

**Port 48400 is taken.** The runner starts a server on it for every snippet, so a
snippet that starts a *second* server — lesson 06 does, to show loop affinity —
must pick another port or it dies with `address already in use`.

**`asyncio.run()` cannot be called.** A snippet is already running inside the
runner's loop, and `asyncio.run` creates a new one, which does not nest. For
anything needing a second loop, make one on a background thread — which is what
that lesson wants anyway, since a second loop *is* the demonstration.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import textwrap
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from asyncua import Client, ua
from softplc.servers.opcua import OpcUaServer

ROOT = Path(__file__).resolve().parent.parent
COURSES = ROOT / "courses"

#: `````python`` … ````` ``, non-greedy, with the language required so a fenced
#: block of console output or a tree drawing is never mistaken for code.
FENCE = re.compile(r"^```python[ \t]*\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)

#: The opt-out marker, on its own line anywhere before the block.
SKIP_MARKER = "<!-- check: skip -->"

#: An ephemeral port, so running the gate never collides with the plant on 4840
#: and never needs the port to be free.
PORT = 48400


@dataclass(frozen=True)
class Snippet:
    path: Path
    line: int
    code: str
    skipped: bool
    reason: str = ""


def find_snippets(path: Path) -> list[Snippet]:
    """Every python block in a file, and whether it is opted out.

    The skip marker is matched against the text *before* the block rather than
    inside it, so a lesson can put the comment on the line above the fence the
    way a reader expects. A marker anywhere earlier in the file would skip
    everything after it, which is the failure mode of the naive
    ``"skip" in text[:start]`` check — and it is silent.
    """
    text = path.read_text(encoding="utf-8")
    out: list[Snippet] = []
    for m in FENCE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        # Only a marker in the blank/indented run immediately above the fence.
        before = text[:m.start()].rstrip()
        skipped = before.endswith(SKIP_MARKER) or (
            before.rsplit("\n", 1)[-1].strip() == SKIP_MARKER
        )
        out.append(Snippet(path, line, m.group(1), skipped,
                           "check: skip" if skipped else ""))
    return out


def courses() -> list[Path]:
    """Lesson markdown, sorted so a failure is reported in reading order."""
    if not COURSES.exists():
        return []
    return sorted(
        p for p in COURSES.rglob("*.md")
        if p.name != "README.md"
    )


async def run_snippet(sn: Snippet) -> None:
    """Run one snippet against a freshly started server and client.

    A fresh server per snippet rather than one shared server, because a lesson
    that writes a value must not leave it written for the lesson after it. That
    is the whole reason the gate is slow and the whole reason it is worth having:
    a shared address space would make the suite order-dependent, and an
    order-dependent suite is a suite that passes on Tuesday.
    """
    endpoint = f"opc.tcp://127.0.0.1:{PORT}/wwtp/server/"
    server = OpcUaServer(endpoint=endpoint)
    # `start()` returns the address space, so take it from there rather than
    # reading `server.space` afterwards: the attribute is `AddressSpace | None`
    # and the only way it is non-None here is that `start()` assigned it.
    space = await server.start()
    await server.wait_ready()
    client = Client(url=endpoint)
    try:
        await client.connect()
        # Navigate by the *server's* node id for the plant object. Re-deriving
        # it from the browse name would duplicate the address space's own naming
        # rule in the runner, and a snippet that worked would then be evidence
        # about the runner rather than about the server.
        root = client.get_node(space.folder.nodeid)
        glb: dict[str, Any] = {
            "client": client, "root": root, "space": space, "server": server,
            "ua": ua, "asyncio": asyncio,
        }
        # The snippet is wrapped in an async function so `await` works at its top
        # level, which is what makes a lesson read like a session rather than
        # like a function definition. `ast.PyCF_ALLOW_TOP_LEVEL_AWAIT` would do
        # it too, but it makes `exec` return a coroutine that has to be awaited
        # separately, and the failure mode when that is got wrong is a snippet
        # that passes without having run a single line.
        #
        # The wrapper also means a snippet cannot use module-level `import *` or
        # a triple-quoted string spanning its own indentation. No snippet in the
        # course does, and a snippet that trips it fails loudly.
        wrapped = "async def __snippet__():\n" + textwrap.indent(sn.code, "    ")
        exec(compile(wrapped, f"{sn.path.name}:{sn.line}", "exec"), glb)
        # `exec` populates a dict with no static type information, so the wrapper
        # function has to be pulled back out with a cast. Asserting instead would
        # be worse: a `KeyError` here is a gate bug, not a snippet bug, and it
        # would be reported against whichever lesson happened to run first.
        fn = cast("Callable[[], Coroutine[Any, Any, None]]", glb["__snippet__"])
        await fn()
    finally:
        try:
            await client.disconnect()
        finally:
            await server.stop()


async def main_async(args: argparse.Namespace) -> int:
    if not COURSES.exists():
        print(f"no {COURSES.relative_to(ROOT)} directory yet", file=sys.stderr)
        return 1

    files = courses()
    if not files:
        print("no lessons found", file=sys.stderr)
        return 1

    snippets = [s for f in files for s in find_snippets(f)]
    live = [s for s in snippets if not s.skipped]
    skipped = [s for s in snippets if s.skipped]

    if args.list:
        for s in snippets:
            rel = s.path.relative_to(ROOT)
            mark = "skip" if s.skipped else "run "
            print(f"  {mark}  {rel}:{s.line}")
        print(f"\n  {len(live)} to run, {len(skipped)} skipped, "
              f"across {len(files)} lessons")
        return 0

    print(f"checking {len(live)} snippets in {len(files)} lessons")
    failures: list[tuple[Snippet, str]] = []
    for sn in live:
        rel = sn.path.relative_to(ROOT)
        try:
            await run_snippet(sn)
        except Exception:
            failures.append((sn, traceback.format_exc()))
            print(f"  FAIL  {rel}:{sn.line}")
        else:
            print(f"  ok    {rel}:{sn.line}")
            if args.verbose:
                print("        " + sn.code.strip().replace("\n", "\n        "))

    for sn, tb in failures:
        print(f"\n{'=' * 70}\n{sn.path.relative_to(ROOT)}:{sn.line}\n{'=' * 70}")
        print(sn.code)
        print("-" * 70)
        print(tb)

    print(f"\n{len(live) - len(failures)}/{len(live)} snippets ran, "
          f"{len(skipped)} skipped")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tools/check_lessons.py",
        description="Run the lesson courses' python snippets against a live server.",
    )
    p.add_argument("--list", action="store_true",
                   help="list what would run, and what is skipped")
    p.add_argument("--verbose", action="store_true",
                   help="echo each snippet's source when it passes")
    args = p.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
