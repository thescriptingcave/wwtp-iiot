"""Execute every notebook and write it to HTML, using the gate's own machinery.

    python -m tools.notebook_read              # all eleven, into notebooks/read/
    python -m tools.notebook_read 05 09        # only the ones whose names contain these
    python -m tools.notebook_read --no-execute # re-render the committed outputs

## Why this file exists, and what it replaced

`make notebooks-read` used to shell out to `jupyter nbconvert --to html --execute`.
It failed on cell one of the first notebook, with

    ModuleNotFoundError: No module named 'notebooks'

which reads like a broken import and was neither.

`nbconvert`'s **command line** resolves a kernel by *name*. Every notebook in this
repository declares

    "kernelspec": {"name": "python3", "display_name": "Python 3"}

and on a machine with more than one Python, `python3` is a name several things
answer to. On this one it resolved to

    /opt/homebrew/anaconda3/bin/python

— no `psycopg`, no `pandas`, no project. `tools/check_notebooks.py` runs the same
eleven notebooks through **nbclient** in-process and gets the venv interpreter,
because the kernel it starts is the process it is running in. So the gate was
green and the read path was red, from the same notebooks, on the same machine, on
the same day.

That is not a coincidence and it is not a typo. It is two code paths that execute
the same notebooks, written at different times, agreeing nowhere. `notebooks/README.md`
already warns about exactly this hazard for JupyterLab — *"A `jupyter` earlier on
your `PATH` will happily open a notebook in this repository and run it under the
wrong interpreter"* — and `make notebooks-read` was walking into it.

So this file does not shell out. It imports the same `NotebookClient` the gate
uses, executes with `kernel_name="python3"` in-process, and renders the result with
nbconvert's `HTMLExporter` **without** `--execute`, so the rendering half can never
select a kernel at all.

## The rule this encodes

> If a second tool runs the same thing, it must not have its own idea of how.

`make notebooks` and `make notebooks-read` now differ in what they *do* with a
notebook — one checks it, one writes HTML — and not in how they run one. The
alternative was to pass `--ExecutePreprocessor.kernel_name` and hope; this way
there is only one mechanism in the repository that starts a kernel for a notebook
besides JupyterLab, and it is the one the gate already trusts.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT / "notebooks"
OUT = NB / "read"

sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="notebook-read",
        description="Execute the notebooks and write them to HTML.",
    )
    parser.add_argument(
        "only", nargs="*",
        help="name fragments; with none, every notebook is done",
    )
    parser.add_argument(
        "--no-execute", action="store_true",
        help="render the committed outputs instead of running the cells",
    )
    parser.add_argument(
        "--template", default="lab",
        help="nbconvert HTML template (default: lab, which embeds the figures)",
    )
    args = parser.parse_args(argv)

    # Deferred, as in `check_notebooks.py`: importing nbformat and nbclient costs
    # a second, and this module is importable without them.
    import nbformat  # noqa: PLC0415
    from nbconvert import HTMLExporter  # noqa: PLC0415

    os.environ.setdefault("MPLBACKEND", "Agg")

    notebooks = sorted(NB.glob("*.ipynb"))
    if not notebooks:
        print(f"  no notebooks in {NB}", file=sys.stderr)
        print("    build them first: make notebooks-build", file=sys.stderr)
        return 2
    selected = [p for p in notebooks
                if not args.only or any(o in p.name for o in args.only)]

    OUT.mkdir(parents=True, exist_ok=True)
    exporter = HTMLExporter(template_name=args.template)

    written: list[Path] = []
    for path in selected:
        print(f"  {path.name} ...", flush=True)
        notebook = nbformat.read(path, as_version=4)
        if not args.no_execute:
            from nbclient import NotebookClient  # noqa: PLC0415

            try:
                NotebookClient(
                    notebook, timeout=600, kernel_name="python3",
                    allow_errors=False,
                ).execute()
            except Exception as exc:
                # Report and carry on, so one broken notebook does not hide the
                # state of the other ten. `check_notebooks.py` does the same, and
                # for the same reason.
                print(f"  {path.name} FAILED: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                continue

        # `from_notebook_node` rather than `--execute`: this is the rendering half
        # only, so it has no kernel to choose and no way to disagree with the gate
        # about which interpreter that is.
        body, _ = exporter.from_notebook_node(notebook)
        target = OUT / f"{path.stem}.html"
        target.write_text(body, encoding="utf-8")
        written.append(target)

    verb = "rendered" if args.no_execute else "executed"
    print(f"  {verb} {len(written)}/{len(selected)} notebook(s) -> "
          f"{OUT.relative_to(ROOT)}/")
    if not written:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
