"""`make workshop-long` must not overwrite the panel the six notebooks read.

**The bug this exists to stop.** `workshop-dataset` had `--out workshops/ml/dataset.csv`
hardcoded, so `WORKSHOP_WEEKS=25 make workshop` worked perfectly and replaced the
3-week panel with the 25-week one. Nothing failed. Every number in `TRAINER.md`,
every `output` fence in all six workshop notebooks, and every bold claim checked
against them were measured on a dataset that no longer existed on disk -- and the
gate still passed, because the gate compares a notebook against its own output,
not against the panel that produced it.

That is the shape of the failure: **silent, and the checks all stay green.** So the
defence has to be structural rather than a convention someone remembers.

Three claims, one per test:

1. the output path is a variable, not a literal;
2. the long build redirects *both* the database and the CSV;
3. the long panel is ignored by git, because an un-ignored 240k-row CSV is a
   30 MB commit that nobody asked for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text()
GITIGNORE = (ROOT / ".gitignore").read_text()


def target(name: str) -> str:
    """The recipe of one Makefile target, up to the next target or variable block.

    The boundary is the next line that starts in column 0 with a target name.
    Reading to the next top-level definition rather than to the end of the file
    matters: `workshop-seed` runs `$(MAKE) workshop-dataset`, so a whole-file
    search for `--out` cannot tell which invocation it came from.
    """
    match = re.search(rf"^{re.escape(name)}:.*?(?=^\S|\Z)", MAKEFILE, re.M | re.S)
    assert match, f"no {name} target; the Makefile moved and this test is now vacuous"
    return match.group(0)


class TestPanelPathIsAVariable:
    def test_dataset_writes_to_the_variable_not_a_literal(self) -> None:
        """The literal path is the bug. Its absence is the fix."""
        body = target("workshop-dataset")
        assert "--out $(WORKSHOP_PANEL)" in body, (
            "workshop-dataset no longer writes to $(WORKSHOP_PANEL), so building a "
            "non-default window overwrites workshops/ml/dataset.csv again -- the "
            "panel every workshop notebook and every TRAINER.md number was "
            "measured on. Nothing fails when that happens; the numbers just stop "
            "describing anything on disk."
        )
        assert "--out workshops/ml/dataset.csv" not in body, (
            "workshop-dataset still hardcodes --out workshops/ml/dataset.csv. "
            "A variable and a literal both present means one of them is dead and "
            "the other is what runs; which one is not something a reader can tell."
        )

    def test_the_variable_exists_and_defaults_to_the_old_path(self) -> None:
        """Defaulting to the literal keeps `make workshop` byte-identical."""
        assert re.search(
            r"^WORKSHOP_PANEL\s*\?=\s*workshops/ml/dataset\.csv\s*$",
            MAKEFILE,
            re.M,
        ), (
            "WORKSHOP_PANEL must default to workshops/ml/dataset.csv. If the "
            "default moves, `make workshop` silently starts writing a different "
            "file and `make workshop-has-data` checks a file nobody built."
        )

    def test_the_name_matches_the_env_var_the_data_module_reads(self) -> None:
        """`WORKSHOP_PANEL` is read by `workshops/ml/_data.py` as well as by make.

        Two spellings of one idea is how a reader ends up writing the CSV to one
        path and running the gate against another. The Makefile's variable and the
        data module's environment variable are deliberately the same word.
        """
        data = (ROOT / "workshops" / "ml" / "_data.py").read_text()
        assert 'os.environ.get("WORKSHOP_PANEL")' in data, (
            "workshops/ml/_data.py no longer reads WORKSHOP_PANEL. The Makefile "
            "comment claims the two agree; if the env var is gone, that comment is "
            "a lie and `WORKSHOP_PANEL=... make workshop-notebooks` cannot work."
        )

    def test_has_data_checks_the_same_path_the_builder_writes(self) -> None:
        """A gate that checks a different file from the one that gets written."""
        body = target("workshop-has-data")
        assert "$(WORKSHOP_PANEL)" in body, (
            "workshop-has-data no longer tests $(WORKSHOP_PANEL). It would pass on "
            "the 3-week panel while workshop-dataset wrote the 25-week one, which "
            "is the exact failure this whole change exists to prevent."
        )
        assert "workshops/ml/dataset.csv" not in body, (
            "workshop-has-data still names the literal path, so it validates a file "
            "the builder may no longer have written."
        )


class TestTheLongWindowIsSeparate:
    """`make workshop-long` exists to be safe *by construction*, not by care."""

    def test_it_redirects_the_database(self) -> None:
        body = target("workshop-long")
        assert "WORKSHOP_DB=$(LONG_DB)" in body, (
            "workshop-long no longer seeds a separate database. Sharing one with "
            "the 3-week build means `--reset` on a 6 GB table destroys the window "
            "the notebooks read."
        )
        assert "LONG_DB    ?= wwtp_ml25" in MAKEFILE, (
            "the long database must be wwtp_ml25; sharing wwtp_ml means --reset "
            "destroys the 3-week window."
        )

    def test_it_redirects_the_panel(self) -> None:
        body = target("workshop-long")
        assert "WORKSHOP_PANEL=$(LONG_PANEL)" in body, (
            "workshop-long no longer redirects the CSV. Without this it writes "
            "dataset.csv and the 3-week panel is gone -- which is the whole reason "
            "this target exists."
        )
        assert "workshop/ml/dataset.csv" not in body, (
            "workshop-long names workshops/ml/dataset.csv directly, which is the "
            "literal this target was written to avoid."
        )

    def test_it_defers_to_the_two_real_targets(self) -> None:
        """Two `$(MAKE)` calls, not a third copy of the seed command.

        A duplicated recipe is a second thing to keep in step. The `WORKSHOP_*`
        mismatch that silently produces confidently-wrong labels is exactly what
        happened three times during the seeder work; the defence was to have one
        place that decides the arguments.
        """
        body = target("workshop-long")
        assert "$(MAKE) workshop-seed" in body, (
            "workshop-long must call the real workshop-seed target rather than "
            "repeating its command, or the two can drift apart silently."
        )
        assert "$(MAKE) workshop-dataset" in body, (
            "workshop-long must call the real workshop-dataset target for the "
            "same reason."
        )
        assert "--fault-every-hours" not in body, (
            "workshop-long repeats a seeding argument instead of inheriting it. "
            "WORKSHOP_HOURS must come from one place, because a mismatch between "
            "the seed and the label derivation produces a panel whose labels are "
            "wrong and which fails silently."
        )

    def test_the_long_panel_is_git_ignored(self) -> None:
        """~240k rows is a 30 MB commit that no review would survive."""
        assert re.search(r"^workshops/ml/dataset-\*\.csv\s*$", GITIGNORE, re.M), (
            "workshops/ml/dataset-*.csv is not git-ignored, so `make workshop-long` "
            "leaves a 30 MB CSV staged for commit."
        )


#: What a sentence must say for a superseded figure to be a correction rather than
#: a claim. Both the Makefile and the README name 6.1 GB *while explaining that it
#: was wrong*, and a bare `not in text` check fails on that explanation.
#:
#: This is the recurring failure in this repository in its purest form: a check that
#: reads text cannot tell the thing from the sentence about the thing. Naming the
#: bad figure in order to refute it is the correct thing to do in a document, and it
#: is exactly what a substring test forbids.
CORRECTION = ("predicted", "wrong", "understated", "extrapolat")


#: How much text either side of the figure counts as "the same claim". Markdown
#: wraps wherever the source did, so the refutation is routinely on the previous
#: line from the figure it refutes -- which is exactly what happened here.
WINDOW = 160


def _assert_only_as_a_correction(text: str, where: str) -> None:
    """Every mention of 6.1 GB must sit near a word marking it superseded."""
    at = text.find("6.1 GB")
    assert at != -1, f"{where} no longer mentions 6.1 GB at all"
    while at != -1:
        near = text[max(0, at - WINDOW) : at + WINDOW]
        assert any(word in near for word in CORRECTION), (
            f"{where} states 6.1 GB without saying it was superseded. That figure "
            "was measured wrong by 4x -- the observed cost is 23 GB at 92.5% of the "
            "window. Delete it, or keep it only where the mistake is explained."
        )
        at = text.find("6.1 GB", at + 1)


class TestTheCostIsMeasuredNotProjected:
    """The 25-week cost was wrong by 4x once, and it was a projection.

    Extrapolating from the 3-week database's 731 MB predicted 6.1 GB. Running it
    gave 23 GB at 92.5% of the window. The number that reached the reader was
    arithmetic dressed as a measurement, and it would have sent someone to a
    machine with 8 GB free to build something needing 30.

    Two things are pinned here rather than left to good intentions:

    - the figure is the **observed** one, so a later writer cannot quietly restore
      the extrapolation;
    - the Makefile and the README say the **same** thing, because they are read by
      different people at different times and one of them is always stale.
    """

    def test_the_default_window_clears_the_power_threshold(self) -> None:
        """8 weeks, because it puts ~12 positives in a 20% test fold.

        Notebook 03's threshold is 10. Three weeks puts 4 there and 25 puts 37,
        so the default is the smallest window that answers the question. Sizing
        the default to the largest window anyone ever mentioned is how a dataset
        nobody can build ends up being the documented one.
        """
        match = re.search(r"^WORKSHOP_LONG_WEEKS \?= (\d+)", MAKEFILE, re.M)
        assert match, "WORKSHOP_LONG_WEEKS is no longer set in the Makefile"
        weeks = int(match.group(1))
        # 22 positives in 3 weeks, measured; the fault schedule is in hours, so
        # positives scale with duration.
        positives = 22 / 3 * weeks
        assert positives * 0.2 >= 10, (
            f"the default {weeks}-week window puts only "
            f"{positives * 0.2:.0f} positives in a 20% test fold, and notebook 03 "
            "needs 10 for a recall figure to mean anything."
        )
        assert weeks <= 12, (
            f"the default is {weeks} weeks. Above 12 the disk cost grows faster "
            "than the statistical return -- 25 weeks is 37 positives at up to "
            "25.7 GB, three times what is needed for three times nothing extra."
        )

    def test_the_panel_name_follows_the_week_count(self) -> None:
        """`WORKSHOP_LONG_WEEKS=25` must not write `dataset-8wk.csv`."""
        assert "LONG_PANEL ?= workshops/ml/dataset-$(WORKSHOP_LONG_WEEKS)wk.csv" in (
            MAKEFILE
        ), (
            "LONG_PANEL no longer derives from WORKSHOP_LONG_WEEKS, so overriding "
            "the weeks writes a file named after the default. A file called "
            "`dataset-8wk.csv` holding 25 weeks of data is how a later reader "
            "stops trusting both numbers."
        )

    #: What was actually observed, at 92.5% of a 175-day window at 1 s sampling.
    OBSERVED: ClassVar[dict[str, str]] = {
        "rows": "97.5 M",
        "disk": "23 GB",
        "minutes": "66 minutes",
    }

    def test_the_makefile_states_the_observed_figures(self) -> None:
        for value in self.OBSERVED.values():
            assert value in MAKEFILE, (
                f"the Makefile no longer states the observed figure {value!r}. It "
                "must carry the measured cost, not the 6.1 GB projection that "
                "understated it by 4x."
            )
        _assert_only_as_a_correction(MAKEFILE, "the Makefile")

    def test_the_readme_states_the_observed_figures(self) -> None:
        readme = (ROOT / "workshops" / "ml" / "README.md").read_text()
        for value in self.OBSERVED.values():
            assert value in readme, (
                f"the workshop README no longer states the observed figure "
                f"{value!r}; it must carry the measured cost."
            )
        _assert_only_as_a_correction(readme, "the workshop README")

    def test_the_target_warns_before_it_starts(self) -> None:
        """The warning is the only thing that arrives before the disk fills."""
        body = target("workshop-long")
        assert "GB" in body, (
            "workshop-long does not say how much free disk it needs. The estimate "
            "was wrong by 4x, so the warning has to be the measured one and it has "
            "to be printed *before* the seed starts, not after."
        )
        assert "1.9-8.2 GB" in body, (
            "the warning does not carry the bounded cost of the default 8-week "
            "window. A bound, not a point estimate, because rows per day is "
            "superlinear and the two measured points only bracket the truth."
        )

    def test_the_recipe_and_the_comment_agree(self) -> None:
        """Two copies of the cost figure, and mutation testing found only one pinned.

        The Makefile states the cost in a comment *and* in the recipe's `echo`.
        Changing the comment passed every other check in this file, because the
        echo still said the right thing and the comment's `6.1 GB` sat within the
        correction window of the sentence explaining the old mistake.

        So both are pinned. The default is the one that matters, and both copies
        must name it, because a reader who reads one and runs the other finds out
        from the filesystem.
        """
        comment = MAKEFILE[: MAKEFILE.index("workshop-long:")]
        assert "1.9-8.2 GB" in comment, (
            "the comment above workshop-long no longer carries the 8-week cost. "
            "The comment and the recipe both state it, and they have to state the "
            "same one."
        )

    def test_the_nonlinear_growth_is_recorded_rather_than_explained(self) -> None:
        """8.3x the duration stored 33x the rows, and nobody knows why.

        That is recorded, not asserted as understood. The honest form of the claim
        is "these two measurements disagree about the storage ratio and here is
        both", and the dishonest form is a confident mechanism invented to fill the
        gap. This test cannot tell a real explanation from an invented one, so it
        only checks that the disagreement is still written down -- losing that is
        how a future reader concludes the two figures were a typo.
        """
        readme = (ROOT / "workshops" / "ml" / "README.md").read_text()
        assert "superlinear" in readme, (
            "the README no longer records that the storage growth is superlinear. "
            "Without it, 2.99 M and 97.5 M look like one of them is a typo."
        )
        assert "not explained here" in readme, (
            "the README claims to explain the superlinearity it does not explain. "
            "The claim must stay a record."
        )
        # The two measured rates, and the *direction*. An earlier draft said rows
        # per day fell from 998 k to 557 k; it rose, from 142,654 to 602,726. A
        # number that is right in shape and wrong in sign is the hardest kind to
        # notice, because the sentence still reads sensibly.
        assert "142,654" in readme and "602,726" in readme, (
            "the README no longer gives both measured rows-per-day rates. They are "
            "what makes the superlinearity checkable rather than asserted."
        )
        # Emphasis markers are stripped before matching: the README writes `*fell*`
        # with asterisks, and a substring check that forgets that fails on correct
        # documentation -- which is how a check gets "fixed" by deleting the record.
        flat = readme.replace("*", "")
        assert "fell from" in flat and "wrong in the direction" in flat, (
            "the README no longer records that an earlier draft had the direction "
            "of the growth backwards. It was: rows per day rose 4.2x, not fell."
        )
