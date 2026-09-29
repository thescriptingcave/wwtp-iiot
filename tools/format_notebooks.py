"""Format the Python fences in `notebooks/src/*.md` with ruff.

    python -m tools.format_notebooks           # rewrite the sources
    python -m tools.format_notebooks --check   # exit 1 if any would change

## Why this exists

`ruff` lints `notebooks/*.ipynb`, and those are *generated*. So a lint finding in a
notebook has to be fixed in the markdown source, by hand, and the next
`build_notebooks` writes the generated file again — `ruff --fix` on the notebook fixes
nothing that lasts (see `tools/build_notebooks.py`). Seventy-two line-length findings
in the first batch of new notebooks is what that looks like at scale.

This formats the *source*, which is the only place a fix survives. It leaves the
prose, the `output` fences and the `sql` fences alone.

## Two details

* A leading `%matplotlib inline` is IPython syntax and is not Python, so it is lifted
  out before formatting and put back after.
* It formats with `--stdin-filename` inside the repository, so ruff finds this
  project's `line-length` instead of its own default.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "notebooks" / "src"

PYTHON_FENCE = re.compile(r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def format_code(code: str) -> str:
    magics: list[str] = []
    body: list[str] = []
    for line in code.split("\n"):
        (magics if line.startswith("%") else body).append(line)
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename",
         str(ROOT / "notebooks" / "_cell.py"), "-"],
        input="\n".join(body), capture_output=True, text=True, cwd=ROOT, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"ruff could not format a cell:\n{result.stderr}\n{code}")
    formatted = result.stdout.rstrip("\n")
    return "\n".join([*magics, formatted]) if magics else formatted


def format_source(text: str) -> str:
    return PYTHON_FENCE.sub(
        lambda m: f"```python\n{format_code(m.group(1).rstrip(chr(10)))}\n```", text
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="format_notebooks")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    stale = []
    for path in sorted(SRC.glob("*.md")):
        original = path.read_text(encoding="utf-8")
        formatted = format_source(original)
        if formatted != original:
            stale.append(path.name)
            if not args.check:
                path.write_text(formatted, encoding="utf-8")
    if args.check and stale:
        print(
            "  not formatted (run: python -m tools.format_notebooks):",
            file=sys.stderr,
        )
        for name in stale:
            print(f"    {name}", file=sys.stderr)
        return 1
    print(f"  {len(stale)} source(s) {'would change' if args.check else 'reformatted'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
