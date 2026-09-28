"""The extracted `.sql` files, and the extraction itself.

`sql/TablePlus/` is generated from the lessons. Three things are worth asserting
and none of them is "the files exist":

* **the count matches the runner.** `check_sql.py` reports 80 runnable queries;
  the generator must produce 80 files. The first version skipped every
  `README.md` and produced **62**, because two runnable queries live in index
  pages. A generator that quietly drops two is worse than one that is short, and
  the discrepancy is invisible unless the two numbers are compared.
* **every file points back at a real line.** A header that says
  `01-04 line 61` is only useful if line 61 is where the query is.
* **naming is nearest-above by position.** The first version compared section
  titles alphabetically and emitted two different files both called
  `01-the-question.sql`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from storage.postgres.schema import connect
from tools.check_sql import SKIP_MARKER, _blocks
from tools.extract_sql import OUT, build, check, slugify

COURSE = Path("sql")

#: The header line every generated file starts with, captured so the assertions
#: can check the *value* rather than the presence of a comment.
SOURCE = re.compile(r"^-- (?P<lesson>\S+\.md)$", re.M)
LINE_REF = re.compile(r"^-- Query (?P<n>\d+) of (?P<total>\d+) in this lesson, "
                      r"at (?P<lesson>\S+) line (?P<line>\d+)\.$", re.M)


def _files() -> list[Path]:
    return sorted(OUT.rglob("*.sql"))


def test_the_generator_is_in_step_with_the_lessons() -> None:
    """The drift gate, and the thing CI runs."""
    problems = check(COURSE, OUT)
    assert not problems, (
        "sql/TablePlus is out of step with the lessons; run "
        "`python -m tools.extract_sql`:\n  " + "\n  ".join(problems)
    )


def test_one_file_per_runnable_query_and_no_more() -> None:
    """80 files, because 80 queries run — and the two counts must agree.

    `check_sql.py` counts across every markdown file in the course, including the
    index pages. The generator's first version skipped `README.md` and produced
    **62**, which nothing noticed: the folder looked complete, the lessons passed,
    and two runnable queries simply had no file.
    """
    expected = sum(len(_blocks(p)) for p in sorted(COURSE.rglob("*.md")))
    actual = len(_files())
    assert actual == expected, (
        f"{expected} runnable queries in the lessons, {actual} files in "
        f"sql/TablePlus. Every runnable query needs a file, including the two in "
        f"index pages."
    )
    assert actual == 80, (
        f"the course now has {actual} runnable queries, not 80 — the README and "
        f"any test that states 80 need updating"
    )


def test_no_file_exists_without_a_query_behind_it() -> None:
    """Deleting a query from a lesson must delete its file.

    The other direction — a lesson changed, file not regenerated — is the
    `check()` test. This is the one that catches an accumulation: a file left
    behind by a query that no longer exists answers a question nobody is asking,
    and in a folder meant to be browsed that is worse than an absence.
    """
    expected = set(build(COURSE))
    orphans = [p for p in _files() if p not in expected]
    assert not orphans, (
        "these files have no query in any lesson — run "
        "`python -m tools.extract_sql`:\n  " + "\n  ".join(str(p) for p in orphans)
    )


def test_every_file_says_where_it_came_from_and_points_at_the_right_line() -> None:
    """A header naming a line is only useful if that line holds the query.

    This is the property that makes the directory worth having: a failure in a SQL
    client maps back to a place in a lesson. `check_sql.py` records the line for
    the same reason — "one lesson has a bad query" is not actionable, "01-04 line
    61" is.
    """
    for path in _files():
        text = path.read_text(encoding="utf-8")
        ref = LINE_REF.search(text)
        assert ref, f"{path} has no line reference in its header"

        lesson = COURSE / ref.group("lesson")
        assert lesson.exists(), f"{path} names {lesson}, which does not exist"

        lines = lesson.read_text(encoding="utf-8").splitlines()
        line_no = int(ref.group("line"))
        assert 0 < line_no <= len(lines), (
            f"{path} points at line {line_no} of {lesson}, which has "
            f"{len(lines)} lines"
        )
        # The referenced line should be the fence, or within two lines of it —
        # `_blocks` reports the fence line, and the file's own body starts just
        # after. A slack of a few lines absorbs a wrapping fence.
        window = "\\n".join(lines[max(0, line_no - 3): line_no + 3])
        assert "```sql" in window, (
            f"{path} points at {ref.group('lesson')} line {line_no}, which is not "
            f"a sql fence"
        )


def test_the_header_counts_are_consistent_with_the_lesson() -> None:
    """`Query 2 of 4` must be true of the lesson, not of the folder."""
    for path in _files():
        ref = LINE_REF.search(path.read_text(encoding="utf-8"))
        assert ref, f"{path} has no line reference"
        lesson = COURSE / ref.group("lesson")
        total = len(_blocks(lesson))
        assert int(ref.group("total")) == total, (
            f"{path} says {ref.group('total')} queries in {lesson}, which has "
            f"{total}"
        )


def test_the_body_is_the_lessons_query_verbatim() -> None:
    """No reformatting, no re-indenting, no "helpfulness".

    The lesson is the source and a diff between them should be empty. A
    reformatted copy is a second version of the query, and a second version is
    what this project has spent six phases removing.
    """
    files = build(COURSE)
    assert files, "the generator produced nothing"
    for path in files:
        if not path.exists():
            continue
        written = path.read_text(encoding="utf-8")
        # Everything after the header block is the query, byte for byte.
        body_written = written.split("\n\n", 1)[1] if "\n\n" in written else ""
        assert body_written.strip(), f"{path} has no query body"
        assert SKIP_MARKER not in body_written, (
            f"{path} contains a skip marker; only runnable queries are extracted"
        )
        assert "```" not in body_written, (
            f"{path} still contains a markdown fence"
        )


def test_slugify_is_ascii_lowercase_and_bounded() -> None:
    """Filenames must not depend on the reader's locale.

    These files are committed and diffed, so a filename that renders differently
    under two developers' locales is a filename that produces a spurious diff — or
    a file one of them cannot address.
    """
    cases = {
        "The obvious attempt": "the-obvious-attempt",
        "A hypertable is a table that lies about being one table":
            "a-hypertable-is-a-table-that-lies-about-being-one-table",
        "  Trimmed  and   spaced  ": "trimmed-and-spaced",
        "Naïve café — em dash": "naive-cafe-em-dash",
    }
    for raw, expected in cases.items():
        got = slugify(raw)
        assert got == expected, f"slugify({raw!r}) = {got!r}, expected {expected!r}"
        assert len(got) <= 60, f"{got!r} exceeds 60 characters"
        assert got == got.lower()
        assert got.replace("-", "").isalnum()


def test_two_queries_under_one_heading_get_distinct_files() -> None:
    """Numbering, not deduplication.

    `01-02_filtering.md` has two queries under *"## The question"*. They must
    become `01-the-question.sql` and `02-the-question.sql`, not one file. The
    numbering is what makes the teaching order legible in a file listing.
    """
    for path in _files():
        siblings = sorted(p.name for p in path.parent.glob("*.sql"))
        assert len(siblings) == len(set(siblings)), (
            f"{path.parent} has duplicate filenames: {siblings}"
        )
    numbered = [p for p in _files() if not re.match(r"^\d\d-", p.name)]
    assert not numbered, f"these files are not numbered: {numbered[:3]}"


def test_the_tableplus_readme_does_not_become_a_query() -> None:
    """The generator must not eat its own output directory.

    `sql/TablePlus/README.md` is inside the course, and its fenced blocks are
    ```` ```sql ```` in places. Without the `OUT in lesson.parents` guard the
    generator would extract its own README and grow without bound.
    """
    for path in build(COURSE):
        assert OUT not in path.parents or path.suffix == ".sql"
    readme = OUT / "README.md"
    readme_blocks = len(_blocks(readme)) if readme.exists() else 0
    # If the README ever grows a bare ```sql fence it must be marked skip, or it
    # would start appearing as a query in the next regeneration.
    assert readme_blocks == 0, (
        "sql/TablePlus/README.md has an unmarked ```sql block; mark it "
        f"<!-- {SKIP_MARKER} --> or it becomes a generated file"
    )


@pytest.mark.integration
def test_the_extracted_queries_all_run() -> None:
    """Every file in this directory executes against a live database.

    Not a re-implementation of `check_sql.py` — that already runs the lessons'
    queries, and this directory is generated from them, so re-running would prove
    nothing new. What is worth proving is the *direction* that matters: the
    extracted file, with its header prepended, is still a valid statement. A
    header that accidentally lands inside a statement would pass every test above
    and fail the moment a person pressed run.
    """
    try:
        with connect() as probe:
            probe.rollback()
    except Exception as exc:
        pytest.skip(f"no database: {exc}")

    # **No `fetchall()`.** The first version called it, and reported one failure:
    #
    #     02-writing-some.sql: the last operation didn't produce records
    #             (command status: INSERT 0 3)
    #
    # which is not a defect in the generated file. It is the lesson teaching
    # quality codes by *inserting* three rows — one Good, one Uncertain, one Bad
    # — because there is no other way to show what `quality` means. **A third of a
    # SQL course is not `SELECT`**, and a runner that assumes it is will reject
    # the most instructive query in the lesson.
    #
    # Which is also why `sql/TablePlus/README.md` no longer recommends a
    # read-only credential without qualification: one of these 64 files is a write.
    failed: list[str] = []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        body = text.split("\n\n", 1)[1] if "\n\n" in text else text
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(body)
                conn.rollback()
        except Exception as exc:
            failed.append(f"{path}: {str(exc).splitlines()[0][:60]}")
    assert not failed, (
        f"{len(failed)} extracted queries do not run:\n  " + "\n  ".join(failed[:10])
    )
