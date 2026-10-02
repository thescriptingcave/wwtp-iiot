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

from storage.seed import schedule
from workshops.ml import measure_long_window as measure

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


#: The comment that introduces the long window, in the Makefile. Scoping to a
#: target's own recipe is not enough here: the cost reasoning sits in a comment
#: block *above* `LONG_DB`, separated from the recipe by other lines, and a check
#: that cannot see both the number and its explanation cannot hold them together.
LONG_WINDOW_ANCHOR = "# **Do not size a disk from an interrupted build.**"


def long_window_block() -> str:
    """The Makefile's whole long-window section: anchor comment through the recipe."""
    lines = MAKEFILE.splitlines()
    try:
        first = next(
            i for i, line in enumerate(lines) if line.startswith(LONG_WINDOW_ANCHOR)
        )
    except StopIteration:
        raise AssertionError(
            f"the Makefile no longer has a {LONG_WINDOW_ANCHOR!r} comment; the "
            "long-window reasoning moved and this test needs to follow it"
        ) from None
    try:
        start = next(
            i for i, line in enumerate(lines) if line.startswith("workshop-long:")
        )
    except StopIteration:
        raise AssertionError("no workshop-long target; the Makefile moved") from None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            end = i
            break
    return "\n".join(lines[first:end])


def _readme_section(needle: str) -> str:
    """The `###`-delimited section of the workshop README containing `needle`.

    Section-scoped rather than whole-file so that a test about one section does
    not pass because some *other* section happens to contain the right words.
    """
    text = (ROOT / "workshops" / "ml" / "README.md").read_text()
    parts = text.split("\n### ")
    for part in parts:
        if needle in part:
            return part
    return text


class TestTheLongWindowIsMeasuredNotExtrapolated:
    """Three claims about the long window, each of which was once wrong.

    Built and measured on 2026-10-01. Getting there took three corrections, and
    each correction is a claim that a later writer could quietly undo:

    - **The 23 GB was an interrupted build.** `reading` carries `drop_after:
      '7 days'` against `now()`, so it trims itself mid-build. A killed build
      accumulates raw rows faster than a daily retention job removes them.
    - **"Superlinear growth" was a name for a shape two points do not have.**
      Rows per day went 142,654 -> 604,341 -> 557,522: up 4.2x, then down 8%.
    - **The panel was built from complete data.** It sums to 33,843,057 of the
      33,843,069 readings the seeder reported.
    """

    #: Rows per day, measured. Three points, and they are not a power law.
    ROWS_PER_DAY = ("142,654", "604,341", "557,522")

    def test_the_interrupted_build_is_labelled_as_one(self) -> None:
        """Every mention of 23 GB must sit in a block that says it was interrupted.

        Checked per **block**, not per line and not within a fixed character
        window. The Makefile refers backwards -- "the 23 GB above is the honest
        counterexample" -- which is correct prose about a figure explained 500
        characters earlier, and both a per-line check and a 400-character window
        reject it. So the unit is the paragraph, which is where the explanation
        and the number belong together.
        """
        blocks = {
            "the Makefile": long_window_block(),
            "the workshop README": _readme_section("23 GB"),
        }
        for where, block in blocks.items():
            assert "23 GB" in block, (
                f"{where} no longer mentions 23 GB. If the figure has genuinely "
                "gone, delete this test rather than leaving it to fail."
            )
            flat = block.replace("*", "")
            assert "interrupt" in flat or "killed" in flat, (
                f"{where} states 23 GB in a block that never says the build was "
                "interrupted. That figure is an artefact of killing the seed at "
                "92.5%; a completed 8-week build leaves 1,040 MB, so a reader who "
                "sizes a disk from it brings a disk they did not need."
            )

    def test_the_refuted_growth_claim_is_recorded_as_refuted(self) -> None:
        """Three measured rates, and an explicit statement that it is not a law.

        The superseded claim was "superlinear and unexplained", which sounded
        rigorous and described a shape the third data point does not have.
        """
        for where, text in (
            ("the Makefile", MAKEFILE),
            (
                "the workshop README",
                (ROOT / "workshops" / "ml" / "README.md").read_text(),
            ),
        ):
            flat = text.replace("*", "")
            for rate in self.ROWS_PER_DAY:
                assert rate in flat, (
                    f"{where} no longer gives the measured rows-per-day rate "
                    f"{rate}. All three are needed: two points suggest a law, three "
                    "refute one."
                )
        readme = (ROOT / "workshops" / "ml" / "README.md").read_text().replace("*", "")
        assert "not a power law" in readme, (
            "the README no longer says the growth is not a power law. Without that, "
            "three points that do not fit one look like a curve somebody fitted."
        )

    def test_the_eight_week_result_is_the_headline(self) -> None:
        """62 positives, and a 13x ratio that is still not enough.

        The point of building it was to find out whether more data settles beat 3.
        It does not, and that is the result worth keeping -- so it has to be
        written down where the trainer will read it.
        """
        trainer = (ROOT / "workshops" / "ml" / "TRAINER.md").read_text()
        for figure in ("62", "13x", "54 weeks"):
            assert figure in trainer, (
                f"TRAINER.md no longer states {figure!r}. Beat 3's remedy was "
                "tested and the answer is 'more data moves the needle and does not "
                "move the conclusion'; a trainer who is not told that will sell a "
                "fix they have not tested."
            )

    def test_the_power_threshold_still_rejects_thirteen_x(self) -> None:
        """13x must not be quietly reclassified as power.

        The temptation after building something is to round it toward the
        conclusion you wanted. `NO_POWER_ABOVE` is 10.0 and 13x is above it.
        """
        assert measure.NO_POWER_ABOVE == 10.0, (
            "the no-power threshold moved, which would reclassify a 13x ratio as "
            "power. 13x is what 8 weeks measures and it is still not enough."
        )
        assert measure.verdict(13.0).startswith("NO POWER"), (
            "verdict(13) no longer says NO POWER, but 13x is the measured 8-week "
            "ratio and the honest reading of it."
        )

    def test_the_panel_integrity_check_is_recorded(self) -> None:
        """33,843,057 of 33,843,069 — the panel is built from complete data.

        Worth stating because it was the live worry: `reading` gets trimmed by
        retention, so the panel could have been built from a table that had
        already lost 51 of its 56 days. It was not, because the builder reads
        `reading_1h`.
        """
        readme = (ROOT / "workshops" / "ml" / "README.md").read_text()
        assert "33,843,057" in readme, (
            "the README no longer records the panel-integrity check. It is the "
            "evidence that retention did not damage the build, and without it a "
            "reader cannot tell whether the long panel is trustworthy."
        )
        flat = readme.replace("*", "")
        assert "reading_1h" in flat, (
            "the README does not say which table the panel is built from. It is "
            "`reading_1h`."
        )
        # Checking only the table name was a real gap, found by mutation testing:
        # the sentence explaining *why the panel survives* could be deleted
        # entirely and every check still passed. The durability claim is the
        # reason a reader knows they can rebuild, so it is checked as a claim and
        # not as a keyword.
        assert "no retention policy" in flat, (
            "the README no longer says that `reading_1h` carries no retention "
            "policy. That clause is the whole reason the panel stays rebuildable "
            "after `reading` has been trimmed -- without it a reader is told the "
            "database is disposable and does not know the CSV can be rebuilt."
        )
        assert "5 days of 56" in flat, (
            "the README no longer records that `reading` was trimmed to 5 days of "
            "56 while `reading_1h` kept all 56. That measured pair is the evidence "
            "for the durability claim; without it the claim is an assertion."
        )


class TestTheDiskKnobsAreVariables:
    """Two knobs decide whether the long window is buildable on a given machine.

    Both were hardcoded, which made "how much disk does it take" an unanswerable
    question rather than a trade-off:

    - `--sample-interval 1` fixed the resolution, and so fixed ~240 bytes per
      generated reading. 54 weeks is ~55 GB at 1 s and ~1.2 GB at 60 s.
    - `WORKSHOP_HOURS 36` fixed the fault recurrence, and so fixed the positive
      count. The seeder's floor is 2 h -- `minimum_interval_s()` is 7200 s, because
      two overlapping drifts bias a reading twice -- so the recurrence can be cut
      by 18x before anything is refused.

    Buying positives with recurrence instead of duration is the only way to reach
    notebook 03's threshold without a 55 GB disk, and neither knob being a variable
    is what stopped anyone from noticing.
    """

    def test_the_sample_interval_is_a_variable(self) -> None:
        body = target("workshop-seed")
        assert "--sample-interval $(WORKSHOP_SAMPLE_INTERVAL)" in body, (
            "workshop-seed no longer takes the sample interval from a variable. It "
            "was hardcoded to 1, which fixed the disk cost of the long window with "
            "no way to trade resolution for space."
        )
        assert not re.search(r"--sample-interval\s+1\s", body), (
            "a literal --sample-interval is back in workshop-seed alongside the "
            "variable, so one of the two is dead and a reader cannot tell which."
        )
        assert re.search(r"^WORKSHOP_SAMPLE_INTERVAL\s*\?=\s*1\s*$", MAKEFILE, re.M), (
            "WORKSHOP_SAMPLE_INTERVAL must default to 1. The workshop's numbers "
            "were measured at 1 s, and at 60 s the per-signal baseline collapses: a "
            "quiet signal writes zero rows in 24 h, so base_24 is 0 for healthy and "
            "broken alike and the 29x separation disappears."
        )

    def test_the_recurrence_is_inherited_by_the_long_window(self) -> None:
        """`workshop-long` must not pin the recurrence back to the default."""
        body = target("workshop-long")
        assert "WORKSHOP_HOURS" not in body, (
            "workshop-long overrides WORKSHOP_HOURS. It should inherit it, so that "
            "WORKSHOP_HOURS=6 buys three times the positives in a third of the "
            "disk -- which is the only affordable route to notebook 03's threshold."
        )
        assert "WORKSHOP_SAMPLE_INTERVAL" not in body, (
            "workshop-long overrides WORKSHOP_SAMPLE_INTERVAL. It should inherit it, "
            "for the same reason: 60 s sampling turns 55 GB into 1.2 GB."
        )

    def test_the_documented_trade_off_is_recorded(self) -> None:
        """Coarser sampling breaks the mechanism, and that has to be written down.

        Without this, `WORKSHOP_SAMPLE_INTERVAL=60` looks like a free saving. It is
        not: it preserves the fault *count* and destroys the per-signal baseline the
        whole workshop is built on.
        """
        flat = MAKEFILE.replace("*", "")
        assert "base_24" in flat and "collapses" in flat, (
            "the Makefile no longer records that coarser sampling collapses "
            "base_24. Without it, WORKSHOP_SAMPLE_INTERVAL=60 reads as a free "
            "saving rather than a trade of the mechanism for the disk."
        )

    def test_the_seeder_floor_is_two_hours(self) -> None:
        """The recurrence cannot go below 2 h, and the reason is correctness.

        `sensor_drift` compounds: the engine applies every active instance to the
        same value, so two overlapping drifts bias a reading twice. This is a
        property of `storage/seed/schedule.py::minimum_interval_s`, and pinning the
        number here means a change to that guard has to be a deliberate one.
        """
        assert schedule.minimum_interval_s() == 7200.0, (
            "minimum_interval_s() moved. It guards against two overlapping "
            "do_sensor_drift instances biasing one reading twice, so a change here "
            "changes what the workshop's faults mean, not just how fast they recur."
        )
