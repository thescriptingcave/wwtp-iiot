"""`browse` reads no values, and three documents said it did.

Asked "what is supposed to happen when I run the browse command", the answer was: a
tree of names, and no numbers. The tool's own docstring said *"the address space as
a tree, with live values"*, and `docs/GETTING-STARTED.md` said the same. Both were
wrong, and the wrongness was not subtle once you had run it — a reader is told to
expect values, gets a list of identifiers, and concludes the OPC UA server is
returning nothing.

Which is the failure this repository is about: a claim about behaviour, checked
against behaviour, except nobody ran it.

## Why a test and not just a correction

Because the correction is three sentences of prose and prose does not fail. These
assert the *distinction* rather than the wording — that wherever the docs talk about
`browse`, the sentence near it does not also promise a value, and that wherever they
promise a value they name `read` or `watch`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = ("README.md", "docs/GETTING-STARTED.md", "tools/opcua_browser.py")

#: The words that mean "a number appears".
VALUE_WORDS = re.compile(
    r"with live values|live values|and its value|shows? (?:the )?values?", re.I
)


def _paragraphs_mentioning(text: str, command: str) -> list[str]:
    """The sentence-or-block around each mention of `command`."""
    out = []
    for block in text.split("\n\n"):
        if command in block:
            out.append(block)
    return out


@pytest.mark.parametrize("path", DOCS)
def test_no_document_promises_values_from_browse(path: str) -> None:
    """The direct claim, in every document that describes the command.

    Checked per block rather than per file, because a document is allowed to say
    *somewhere* that `read` shows a value -- it is not allowed to say that `browse`
    does. A file-level check would pass on a document that had the right sentence in
    one place and the wrong sentence in another, which is the state this was in.
    """
    text = Path(path).read_text(encoding="utf-8")
    offenders = [
        block.strip()[:120]
        for block in _paragraphs_mentioning(text, "opcua_browser")
        if "browse" in block and VALUE_WORDS.search(block)
    ]
    assert not offenders, (
        f"{path} says `browse` produces values, which it does not. It walks the "
        f"address space and prints names and node classes:\n  "
        + "\n  ".join(offenders)
    )


def test_the_tool_docstring_separates_structure_from_values() -> None:
    """`browse` structure, `read` and `watch` values -- all three named, in one place.

    Asserted on the mode list rather than on the module docstring, because that list
    is what a reader sees in `--help`-adjacent context and it is where the wrong
    claim was.
    """
    text = Path("tools/opcua_browser.py").read_text(encoding="utf-8")
    for mode in ("browse", "read", "watch", "write", "diagnose"):
        assert re.search(rf"^\s+{mode}\s+\S", text, re.M), (
            f"the mode list no longer describes `{mode}`. The five modes are the "
            f"tool's whole interface, and one of them has gone."
        )
    browse_line = re.search(r"^\s+browse\s+(\S.*)$", text, re.M)
    assert browse_line, "no browse line in the mode list"
    assert "structure" in browse_line.group(1).lower(), (
        f"the browse line does not say it is structure: {browse_line.group(1)!r}"
    )


def test_the_value_commands_are_the_ones_that_produce_values() -> None:
    """And the docs point at them, so a reader is not left with only `browse`."""
    for path in ("README.md", "docs/GETTING-STARTED.md"):
        text = Path(path).read_text(encoding="utf-8")
        assert "opcua_browser.py read" in text, (
            f"{path} mentions the OPC UA client but never shows a command that "
            f"returns a value"
        )
        assert "opcua_browser.py watch" in text, (
            f"{path} does not show `watch`, which is the command that *follows* a "
            f"tag as it changes -- the one that makes a live plant feel live"
        )
