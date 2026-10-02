"""The alarm counts must come from the code, because three documents disagreed.

**What was wrong.** Three numbers, three answers:

- `docs/ALARMS.md` — "Fifteen rules over nine signals"
- the `make coverage` echo in the `Makefile` — "fourteen rules"
- `alarms.rules.rules()` — **sixteen rules over eleven signals**

Only the third is true, and it is the only one anybody measured. Nothing in the
repository checked the other two, which is the same failure
`test_the_documented_unit_suite_total_matches` was written for and which this file
extends to the alarm engine: **a number nobody checks rots**, and the document
about verification is where you would least expect to find an unverified number.

`docs/LEARNING-LOG.md` also says "fifteen rules" in two places. That file is a
historical record and is deliberately **not** corrected — it is a log of what was
believed at the time, and editing it would destroy the thing that makes it useful.
So it is excluded below, by name, rather than quietly tolerated.

`docs/TESTING.md` says "five of its fifteen rules" and is corrected, because that
is a present-tense claim about current behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

ALARMS_DOC = (ROOT / "docs" / "ALARMS.md").read_text()
TESTING_DOC = (ROOT / "docs" / "TESTING.md").read_text()
MAKEFILE = (ROOT / "Makefile").read_text()

#: Historical documents. `LEARNING-LOG.md` records what was believed when something
#: was written, so a count that was true then and is wrong now is the *content* of
#: the file rather than an error in it. Excluded by name so the exclusion is a
#: decision somebody can see, not a pattern that quietly widens.
HISTORICAL = ("docs/LEARNING-LOG.md",)


@pytest.fixture(scope="module")
def built() -> tuple[int, int, int]:
    """`(rules, distinct signals, faults)` as the code and the contract report them."""
    from alarms.rules import rules as build_rules  # noqa: PLC0415

    built_rules = build_rules()
    contract = yaml.safe_load((ROOT / "contracts" / "fault-scenarios.yaml").read_text())
    signals = {r.signal_id for r in built_rules}
    return len(built_rules), len(signals), len(contract["faults"])


class TestTheCountsAreTheRealOnes:
    def test_the_counts_are_not_vacuous(self, built: tuple[int, int, int]) -> None:
        """A fixture returning zeros would make every assertion below pass.

        Every count here is asserted to be positive by the tests themselves, but the
        fixture is the one place a silent failure would hide, so it is checked
        once, loudly.
        """
        rules_n, signals_n, faults_n = built
        assert rules_n > 0 and signals_n > 0 and faults_n > 0, (
            f"the alarm fixture read {built}, which means the import or the contract "
            "parse returned nothing and every count test below would pass on an "
            "empty object."
        )
        assert rules_n == 16, (
            f"the alarm engine now has {rules_n} rules, not 16. That is a real "
            "change -- update docs/ALARMS.md and the `make coverage` echo, and read "
            "what the new rules are before assuming they are an improvement."
        )
        assert signals_n == 11, f"the rules now cover {signals_n} signals, not 11."

    def test_fault_count_matches_the_contract(
        self, built: tuple[int, int, int]
    ) -> None:
        assert built[2] == 12, (
            f"contracts/fault-scenarios.yaml has {built[2]} faults; docs/ALARMS.md and "
            "the coverage echo both say twelve."
        )

    def test_the_makefile_echo_matches_the_code(
        self, built: tuple[int, int, int]
    ) -> None:
        """The echo a tester sees before the coverage run starts.

        A tester who is told "fourteen rules" and then reads a 16-row matrix has to
        decide which one is the bug, and the answer is neither.
        """
        rules_n = built[0]
        words = {
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
            16: "sixteen",
            17: "seventeen",
        }
        spelled = f"{words[rules_n]} rules"
        assert spelled in MAKEFILE, (
            f"the `make coverage` echo does not say there are {spelled}. It "
            "is the first line a tester reads for that step, so it has to be right."
        )
        for wrong in ("fourteen rules", "fifteen rules", "fourteen detectors"):
            assert wrong not in MAKEFILE, (
                f"the Makefile still says {wrong!r}, which is not the number of "
                f"rules in the engine ({rules_n})."
            )

    def test_the_alarms_doc_says_the_right_numbers(
        self, built: tuple[int, int, int]
    ) -> None:
        rules_n, signals_n, _ = built
        words = {
            9: "nine",
            10: "ten",
            11: "eleven",
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
            16: "sixteen",
        }
        flat = ALARMS_DOC.replace("*", "")
        assert f"{words[rules_n]} rules" in flat.lower(), (
            f"docs/ALARMS.md does not say there are {rules_n} rules."
        )
        assert f"over {words[signals_n]} signals" in flat, (
            f"docs/ALARMS.md does not say the rules cover {signals_n} signals."
        )
        for wrong in ("Fifteen rules", "fifteen rules", "over nine signals"):
            assert wrong not in flat, (
                f"docs/ALARMS.md still says {wrong!r}. It is the document a tester is "
                "most likely to read before running the alarm steps."
            )


class TestTheHistoricalLogIsExcludedDeliberately:
    """`LEARNING-LOG.md` keeps its old counts, and that is the point of it."""

    def test_the_exclusion_is_actually_excluded(self) -> None:
        log = (ROOT / "docs" / "LEARNING-LOG.md").read_text()
        assert "fifteen rules" in log, (
            "docs/LEARNING-LOG.md no longer says 'fifteen rules'. If it has been "
            f"edited to match the code, the exclusion in {HISTORICAL[0]!r} is "
            "unnecessary and the exclusion list should go too -- a stale exclusion "
            "hides the next real drift."
        )

    def test_the_exclusion_names_the_file_explicitly(self) -> None:
        assert HISTORICAL == ("docs/LEARNING-LOG.md",), (
            "the historical-document exclusion list changed. It must name files "
            "individually so that adding one is a visible decision."
        )


def _counts(text: str) -> dict[str, str]:
    """Rule/signal/detector counts in words or digits, from a document.

    Both spellings, because this repository writes prose in words ("Sixteen rules
    over eleven signals") and a check that only accepts digits fails the moment
    somebody edits a sentence rather than a table. That was the first version of
    these tests, and it rejected two correct documents.
    """
    found: dict[str, str] = {}
    words = {
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
    }
    flat = " ".join(text.replace("*", " ").split())
    for word, noun in re.findall(
        r"\b([A-Za-z]+|\d+)\s+(rules|signals|detectors)\b", flat, re.I
    ):
        key = noun.lower()
        low = word.lower()
        if low in words:
            found[f"{words[low]} {key}"] = word
        elif low.isdigit():
            found[f"{int(low)} {key}"] = word
    return found


class TestTheSpelledOutCountsAgree:
    """Targeted, not a generic scan.

    **The generic version was wrong and is deliberately not here.** An earlier
    draft scanned every present-tense document for `<number> rules|signals|faults`
    and reported:

        docs/ALARMS.md: 'thirteen signals' (real: 16/11)

    which is a **false positive**. The sentence is "`sql/02-04` exists: thirteen
    signals produce exactly one reading in a seeded week" -- thirteen tags that
    never speak, a fact about the *dataset*, with nothing to do with the sixteen
    alarm rules over eleven signals. A check that flags it would send somebody to
    "fix" a correct sentence, and the natural response to a check that cries wolf
    three times is to stop reading it.

    So the counts are pinned per claim, where the noun is known, rather than
    pattern-matched across prose.
    """

    #: `(document, phrase that must be present)`. Each phrase is a specific claim.
    CLAIMS: ClassVar[list[tuple[str, str]]] = [
        ("docs/ALARMS.md", "Sixteen rules over eleven signals"),
        ("docs/TESTING.md", "five of its sixteen rules"),
    ]

    @pytest.mark.parametrize("relative,phrase", CLAIMS, ids=[c[0] for c in CLAIMS])
    def test_the_claim_is_present_and_correct(
        self, relative: str, phrase: str, built: tuple[int, int, int]
    ) -> None:
        rules_n, signals_n, _ = built
        text = " ".join((ROOT / relative).read_text().replace("*", " ").split())
        assert phrase in text, (
            f"{relative} no longer says {phrase!r}. If the sentence was reworded, "
            "update this test; if the engine changed, update the sentence and the "
            "test together, and read what changed first."
        )
        if "sixteen" in phrase:
            assert rules_n == 16, (
                f"the claim says sixteen rules and the engine has {rules_n}."
            )
        if "eleven" in phrase:
            assert signals_n == 11, (
                f"the claim says eleven signals and the rules cover {signals_n}."
            )
