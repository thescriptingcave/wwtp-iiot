"""The CI workflow is code, and it was wrong in three ways at once.

**Every failure in this repository's CI at the time of writing was caused by a
copy of an invocation living in a YAML file instead of in the Makefile.** The
seeder has a safety rule — *refuse `--reset` unless the run was told which
database it is emptying* — and the workflow was tripping it, because the
workflow spelled out `POSTGRES_DB: wwtp_ml` as an environment variable where the
Makefile passes `--database` as an argument. Two invocations of the same program,
kept in step by hand.

That is the whole class of fault this file exists to catch, and it is the same
one the rest of the repository keeps recording in a different costume: a
generator and a committed artefact, a contract and a doc, a probe and its skip
guard. Two places that must agree, joined by nothing.

So the workflow is checked against the code it runs:

* **the seeder invocation names `--database`**, because the seeder refuses
  `--reset` without it and the refusal is a safety rule about emptying the
  plant's own database rather than a style preference;
* **the node-package probe reaches its container with `docker exec`**, which is
  asserted in `tests/test_scada_contract.py` where the code is -- the workflow
  contains no `docker exec` at all, so a check here would be vacuous;
* **the container name in the probe matches `compose.yaml`**, so renaming one
  side cannot turn the probe into a skip that reads as a pass.

None of these is a test the workflow could have caught itself. They are all
"this file says one thing and the code says another", which is the one thing a
workflow file cannot check by running itself.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "gates.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _run_blocks(pattern: str) -> list[str]:
    """Every `run:` body in the workflow matching `pattern`."""
    out = []
    for match in re.finditer(r"run:[ ]*>-\s*\n((?:[ ]{10,}.*\n)+)", _text()):
        body = " ".join(line.strip() for line in match.group(1).splitlines())
        if re.search(pattern, body):
            out.append(body)
    return out


def test_the_workflow_file_is_the_one_this_file_reads() -> None:
    assert WORKFLOW.exists(), (
        f"{WORKFLOW} does not exist. If the workflow moved, point this file at "
        f"it rather than deleting it -- everything below is a check on that "
        f"file agreeing with the code."
    )


def test_a_reset_seed_must_name_its_database_as_an_argument() -> None:
    """`--reset` without `--database` is refused by the seeder, on purpose.

    The rule exists because `tools/py.sh` sources `.env` *after* the inherited
    environment, so `POSTGRES_DB=wwtp_ml` in a recipe resolves to `wwtp`. A
    command that prints the right database name and passes the right flag can
    therefore empty the plant's own database, which is why the seeder refuses
    rather than warns.

    CI was doing exactly that, and failing on every run with a message naming
    the flag it needed.
    """
    blocks = _run_blocks(r"storage\.seed\.main")
    assert blocks, (
        "no storage.seed.main invocation was found in the workflow; the regex "
        "or the run-block shape has changed"
    )
    for body in blocks:
        if "--reset" not in body:
            continue
        assert "--database" in body, (
            f"the workflow resets a database without naming it: {body!r}\n\n"
            f"storage/seed/main.py::_target_database refuses `--reset` without "
            f"`--database`, because POSTGRES_DB in a recipe resolves to the "
            f"plant's own database once `.env` is sourced over it. Pass "
            f"`--database NAME` as an argument -- an argument cannot be "
            f"overridden that way, which is the entire reason the flag exists."
        )


def test_the_container_name_the_tests_reach_for_is_declared_in_compose() -> None:
    """The probe's hardcoded name must still be the real one.

    `tests/test_scada_contract.py` reaches the Node-RED container by name so it
    does not need `.env`. A hardcoded name is only safe while somebody notices
    a rename, and nothing else would.
    """
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    names = set(re.findall(r"^\s+container_name:\s*(\S+)", compose, re.MULTILINE))
    assert "wwtp-scada" in names, (
        f"compose.yaml no longer declares container_name: wwtp-scada; it has "
        f"{sorted(names)}. The node-package probe would look for a container "
        f"that does not exist and skip, which reads as a pass."
    )


def test_the_unit_job_excludes_the_markers_it_cannot_honour() -> None:
    """A no-database job must deselect the tests that need one.

    The comment beside it says exactly this, so the assertion is the comment:
    if the marker filter is dropped, the job starts reporting connection errors
    as failures, which is the thing the filter exists to prevent.
    """
    block = re.search(
        r"name: pytest, no database.*?run:[ ]*>-\s*\n((?:[ ]{10,}.*\n)+)",
        _text(), re.DOTALL,
    )
    assert block, "the 'pytest, no database' step was not found"
    body = " ".join(line.strip() for line in block.group(1).splitlines())
    assert "not integration" in body and "not slow" in body, (
        f"the no-database step is {body!r}. It must keep `-m \"not slow and "
        f"not integration\"`, or it reports a connection error as a failure."
    )
