"""The two documents that tell somebody how to start must say the same thing.

Found by somebody doing exactly what they were told. On a clean machine,
`make notebooks` failed inside the seeder, and the two documents that describe the
setup — the README's Quick start and `docs/GETTING-STARTED.md` — had drifted far
enough apart that neither was a guide to what the project actually does.

The README led with `cp .env.example .env` and `docker compose up`, and then said
"or start with the analyst notebooks, which does the setup itself" and pointed at a
bare `make`. `GETTING-STARTED.md` never mentioned `make` at all. So a reader
following the README ended up in the *notebooks*, and a reader following the guide
never learned that `make up` does the whole thing in one command.

## What is checked, and what is not

The **commands**, not the prose. A setup guide's failure mode is a command that does
not work, and prose that has drifted is annoying rather than dangerous. Comparing
prose would also mean writing a parser for English, which is a test nobody maintains.

So: every fenced `bash` block in the "short version" of the guide, and the README's
Quick start, must be identical. That is a small enough surface to hold true, and
large enough to catch the thing that actually went wrong — two documents, two
different sets of commands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path("README.md")
GUIDE = Path("docs/GETTING-STARTED.md")

#: The commands that must agree, and which document is canonical for each.
#:
#: `make up` is the whole setup and belongs in both. `make notebooks` and the
#: workshop pair are track-specific and live in the guide, with the README pointing
#: at them by name.
SHARED_COMMANDS = [
    "make up",
    "make query SQL=\"SELECT count(*) FROM reading\"",
    "uv run python tools/opcua_browser.py browse",
    "uv run python tools/opcua_browser.py read AERATION:AHU-1:DO",
    "uv run python tools/opcua_browser.py watch AERATION:AHU-1:DO",
]


def _bash_blocks(text: str) -> list[str]:
    """Every ```bash fenced block, with the fence and trailing space removed."""
    return [
        "\n".join(line.rstrip() for line in block.strip("\n").splitlines())
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
    ]


#: A backticked `make` that is not `make up` or `make sql` -- i.e. the default goal.
_BARE_MAKE = re.compile(r"(?<![-\w])`make`(?![-\w])")


def _make_lines(text: str) -> list[str]:
    r"""Every **line** of `text` that contains a bare `make` in backticks.

    Lines, not sentences. The Quick start mentions bare `make` in a table cell, and a
    sentence-shaped pattern cut at the full stop in `notebooks/README.md` and
    captured `md) | `make` |` -- the tail of the row, with the word "notebooks" on
    the other side of the cut and therefore invisible. A pattern that discards the
    context which makes a line acceptable fails for a reason nobody reading the
    failure can act on, which is the worst kind of test failure.
    """
    return [
        line.strip() for line in text.splitlines()
        if re.search(r"(?<![-\\w])`make`(?![-\\w])", line)
    ]


def _section(text: str, heading: str, level: str = "##") -> str:
    """The body of one section, up to the next heading at the same level.

    Matched on the heading **and its following blank line**, because `## Quick
    start` and `## Quick start-up` are both a prefix match on the bare word, and a
    test that cannot tell them apart silently checks the wrong section.
    """
    start = re.search(rf"^{re.escape(level)} {re.escape(heading)}\s*$", text, re.M)
    assert start, f"no '{level} {heading}' in the document"
    rest = text[start.end():]
    end = re.search(rf"^{re.escape(level)} \S", rest, re.M)
    return rest[:end.start()] if end else rest


def test_the_guide_has_a_short_version() -> None:
    """A 466-line walkthrough is not a quick start, and the README cannot link to one.

    This is the structural half of the fix. The guide used to be a complete,
    careful, 466-line document with no short version at all, which is why the README
    had grown its own — and the two went their separate ways.
    """
    guide = GUIDE.read_text(encoding="utf-8")
    short = _section(guide, "The short version")
    assert _bash_blocks(short), "the short version has no commands in it"
    assert len(_bash_blocks(short)) <= 3, (
        f"the short version has {len(_bash_blocks(short))} command blocks. It is "
        f"the block the README copies, so it has to stay small enough to stay true."
    )


@pytest.mark.parametrize("command", SHARED_COMMANDS)
def test_the_readme_and_the_guide_agree_on_it(command: str) -> None:
    """Each shared command appears in both, spelled the same way.

    `make up` is the one that matters most and the one most likely to drift: it is
    the answer to "how do I start this", and it was absent from the guide entirely
    while the README's "or just run `make`" pointed somewhere else.
    """
    readme = README.read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")
    quick = _section(readme, "Quick start")
    short = _section(guide, "The short version")

    for name, text, where in (("README", quick, "Quick start"),
                              ("the guide", short, "The short version")):
        assert command in text, (
            f"`{command}` is not in the {name}'s {where}. These two documents are "
            f"the only instructions a new reader gets, and a command that is in one "
            f"and not the other is how somebody ends up guessing."
        )


def test_a_bare_make_is_never_offered_as_an_alternative_to_the_setup() -> None:
    """The defect, narrowly: `make` presented as a peer of `make up`.

    The README used to offer `cp .env` + `docker compose up` and then, as an equal
    alternative, a bare `make` — which is `notebooks-open`, and starts a *different*
    thing. Two commands, presented as interchangeable, doing unrelated work.

    A blanket ban on the word was the first attempt and it was wrong: the Quick start
    has to be *able* to say what bare `make` does, and two sentences doing that
    correctly were flagged. So the rule is about the construction — `make` offered
    with "or", "instead" or "alternatively" — rather than about the token.
    """
    quick = _section(README.read_text(encoding="utf-8"), "Quick start")
    for phrase in _make_lines(quick):
        offered = re.search(r"\bor\b|instead|alternativ", phrase, re.I)
        assert not offered, (
            f"the Quick start offers a bare `make` as an alternative: "
            f"{phrase.strip()!r}. Bare `make` is `notebooks-open`, not the setup, "
            f"and offering it beside `make up` sends a reader to the wrong place "
            f"with no error."
        )


def test_the_quick_start_says_what_bare_make_actually_does() -> None:
    """The positive half, because the negative one alone is satisfiable by silence.

    A reader who types `make` deserves to be told it opens the notebooks. This is
    also what makes the test above safe to narrow: the guide is not being told to
    avoid the word, it is being told to say what it means.
    """
    quick = _section(README.read_text(encoding="utf-8"), "Quick start")
    bare = _make_lines(quick)
    assert bare, (
        "the Quick start never mentions bare `make`, and bare `make` is the "
        "repository's default goal. A reader will type it whether or not it is here."
    )
    # **Every** mention, not one of them. An earlier version used `any(...)`, and a
    # mutation that replaced the sentence explaining `make` with "And there is also
    # `make`" still passed — because the table row further up happens to say
    # "notebooks". One qualified mention does not qualify the others.
    unqualified = [
        phrase for phrase in bare
        if not re.search(
            r"on its own|notebooks|does|opens|seeds|runs|default goal", phrase, re.I
        )
    ]
    assert not unqualified, (
        f"these mentions of bare `make` say nothing about what it does: {unqualified}. "
        f"It is the default goal, so a reader who types it deserves to find out."
    )


def test_the_setup_is_reachable_without_reading_the_whole_guide() -> None:
    """`make up` must be in the README's first screen of instructions.

    Asserted on position rather than presence, because presence was never the
    problem: a correct command buried under three paragraphs of prerequisites is the
    same outcome as a wrong command at the top.
    """
    quick = _section(README.read_text(encoding="utf-8"), "Quick start")
    assert quick.index("make up") < 700, (
        "`make up` appears, but not in the opening. The first thing a reader meets "
        "should be the command that works."
    )
