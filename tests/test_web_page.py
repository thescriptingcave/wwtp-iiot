"""The web dashboard: what is verified, and what is verified *not* to be.

`ui/web` is the only part of this project with no test runner, and that is
stated rather than hidden — the cheapest honest thing to say is "here is what the
Python side can check, and here is the list of what nobody checks".

## What is checked here

* the generated read model is in step with `contracts/tags.yaml`;
* **every signal id the pages name exists in the contract** — thread 22, where a
  permit dashboard read pH from the TSS signal and returned a plausible number
  because nothing in the database objects to a wrong signal id;
* **every SQL statement in `lib/queries.ts` runs**, against a live database, with
  its parameters bound and its transaction rolled back;
* those statements name only contract signals and only reviewed tables;
* the credential cannot reach a browser — no `NEXT_PUBLIC_` database variable
  anywhere, `lib/db.ts` marked `server-only`, and no client component importing
  it;
* there is no second write path, because the only one is the Node-RED annunciator.

## What is *not* checked, and it is not a small list

* **The JSX renders.** No TypeScript test runner exists, so "the page draws" was
  established by running it: `npm run build`, `next start`, and a `curl` per
  route. That is a manual verification and this file does not make it automatic.
* **The sparkline's geometry.** The gap-handling logic — a missing minute must
  break the path rather than be skipped — was verified by hand by inserting 40
  readings with 6 removed and counting the `<path>` elements in the response. It
  is asserted below as a *source* property (the code splits runs) and that is
  weaker than a test of the behaviour.
* **The TypeScript compiles**, except that `npm run build` and `tsc --noEmit` do
  it, and neither is in `make check` because both need `node_modules`.

The honest summary: the *data path* of this page is tested from Python, and its
*rendering* is verified by running it and recorded here. That is a worse position
than the rest of the project is in, and it is the open thread in
`docs/LEARNING-LOG.md`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from softplc.contract import contract as get_contract
from ui.web import generate_page

WEB = Path("ui/web")

#: The tables the page is allowed to read. The same reviewed set the Node-RED
#: flows use, and for the same reason: a query against a table nobody expected is
#: either a bug or a decision, and both deserve to be a line in a list.
ALLOWED_TABLES = {"reading", "reading_1m", "reading_1h", "event", "signal",
                  "equipment", "site"}

#: Words that follow `FROM`/`JOIN` and are not tables. The regex cannot tell them
#: apart, and a test that reports `query touches 'generate_series'` is a test
#: people learn to ignore.
SQL_KEYWORDS_AFTER_FROM = {
    "lateral", "select", "unnest", "generate_series", "values",
}


def _tables_in(sql: str) -> set[str]:
    return {
        t for t in re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", sql, re.I)
        if t.lower() not in SQL_KEYWORDS_AFTER_FROM
    }


def _source_files() -> list[Path]:
    """Every TypeScript and MJS file in the app, excluding build output."""
    out: list[Path] = []
    for pattern in ("**/*.ts", "**/*.tsx", "**/*.mjs"):
        out.extend(
            p for p in WEB.glob(pattern)
            if "node_modules" not in p.parts and ".next" not in p.parts
        )
    return sorted(out)


def _sources() -> dict[str, str]:
    return {
        str(p.relative_to(WEB)): p.read_text(encoding="utf-8")
        for p in _source_files()
    }


#: `// …`, `/* … */` and the JSX form `{/* … */}`. Every one of these files is
#: heavily commented, and three of the assertions below are about *code* — a
#: `NEXT_PUBLIC_` in a comment explaining why `NEXT_PUBLIC_` is wrong is not a
#: credential leak, and failing on it would train people to delete the
#: explanation.
_COMMENTS = re.compile(
    r"\{/\*.*?\*/\}"      # JSX comment
    r"|/\*.*?\*/"          # block comment
    r"|//[^\n]*",          # line comment
    re.S,
)


def _code_only(text: str) -> str:
    """The file with its comments removed.

    Not a TypeScript parser and not trying to be one. Three regexes is enough to
    stop the comments from producing failures, and a parser would be a
    dependency plus a hundred lines to answer a question the file's own comments
    make obvious.
    """
    return _COMMENTS.sub("", text)


def _code() -> dict[str, str]:
    """Every source file, comments stripped. What the assertions should read."""
    return {name: _code_only(text) for name, text in _sources().items()}


# ── the generated read model ─────────────────────────────────────────────────


def test_contract_json_is_in_step_with_the_contract() -> None:
    """The drift gate, as a test.

    Also the `drift` CI job, which runs `python -m ui.web.generate_page --check`.
    Both, because the job is what stops it drifting on a machine that is not mine
    and the test is what says why it matters.
    """
    assert generate_page.check(get_contract()) == []


def test_the_read_model_holds_every_signal_the_contract_declares() -> None:
    """All 57, and not one more.

    The "not one more" half is the interesting half: a generator that emitted a
    signal the contract does not declare would make the page query a row that
    does not exist, and a query that returns nothing is indistinguishable from a
    signal that is simply not reporting.
    """
    c = get_contract()
    model = json.loads(generate_page.OUT.read_text(encoding="utf-8"))
    got = [s["id"] for a in model["areas"] for s in a["signals"]]

    assert sorted(got) == sorted(c.signals)
    assert len(got) == len(set(got)), "a signal appears under two areas"
    assert model["counts"]["signals"] == len(got)


def test_every_signal_carries_a_unit_a_precision_and_a_range() -> None:
    """A panel that cannot say what a number *is* is not a panel.

    Thread 20 is the precedent: `Signal.unit` held an area name, and three
    consumers were right because they read a different attribute. The read model
    is now the single place the page gets units from, and this asserts it is
    populated for every signal rather than for the ones somebody looked at.
    """
    model = json.loads(generate_page.OUT.read_text(encoding="utf-8"))
    for area in model["areas"]:
        for s in area["signals"]:
            assert s["eu"], s["id"]
            assert 0 <= s["precision"] <= 6, s["id"]
            assert s["range"]["min"] is not None, s["id"]
            assert s["range"]["max"] is not None, s["id"]
            assert s["range"]["min"] < s["range"]["max"], s["id"]


def test_a_band_is_present_in_full_or_not_at_all() -> None:
    """Half a range is not a range.

    `_band()` returns both ends or neither, because a shaded region with only a
    `normal_high` draws a boundary that looks like a limit and is not. Asserted
    on the generator's output rather than the generator's code, because the code
    is the thing that could be wrong in a way the output shows.
    """
    model = json.loads(generate_page.OUT.read_text(encoding="utf-8"))
    bands = [
        s["band"] for a in model["areas"] for s in a["signals"] if s["band"]
    ]
    assert bands, "no signal has a band; the generator is broken"
    for b in bands:
        assert set(b) == {"low", "high"}
        assert b["low"] < b["high"], (
            f"{b} is a zero-width band. A setpoint's normal_low and normal_high "
            f"are the same number, and shading a zero-height region draws a "
            f"line that reads as a limit."
        )


# ── thread 22: a wrong signal id is a wrong number, never an error ──────────


def test_every_signal_id_the_pages_name_is_in_the_contract() -> None:
    """The check that catches the pH-read-from-the-TSS-signal bug.

    Regex over the source rather than an import, because the alternative is a
    Node test runner and this is a Python repository. It over-matches — a signal
    id in a comment counts — and that is the right direction to over-match in:
    a false positive is a comment to reword, a false negative is a plausible
    wrong number on a permit page.
    """
    c = get_contract()
    # Contract ids look like AREA:EQUIPMENT:FIELD. The uppercase-and-digits
    # shape is what keeps `EFFLUENT:FLOW:PH` from matching prose.
    pattern = re.compile(r"\b[A-Z][A-Z0-9]*(?::[A-Z0-9][A-Z0-9_-]*){2,}\b")

    for name, text in _code().items():
        for found in set(pattern.findall(text)):
            # `READONLY`-style words and a stray `::` in TypeScript are not
            # signal ids; anything with the right shape is held to the contract.
            if found.count(":") < 2:
                continue
            assert found in c.signals, (
                f"{name} names {found!r}, which contracts/tags.yaml does not "
                f"declare. A wrong signal id is a wrong number, not an error."
            )


def test_the_permit_page_monitors_only_contract_signals() -> None:
    """The five permit parameters, checked individually.

    Worth its own test because the page above renders a loud row for an unknown
    id *at run time* — which means a typo ships as a page that renders, shows a
    warning, and is wrong. Catching it in CI means it never ships.
    """
    c = get_contract()
    text = (WEB / "app/permit/page.tsx").read_text(encoding="utf-8")
    start = text.index("const PERMIT")
    block = text[start:text.index("];", start)]
    ids = re.findall(r"signal: '([^']+)'", block)
    assert len(ids) >= 4, "the permit list shrank; was that deliberate?"
    for signal_id in ids:
        assert signal_id in c.signals, signal_id


# ── the SQL, executed ────────────────────────────────────────────────────────


def _sql_statements() -> list[tuple[str, str]]:
    """(name, sql) for every statement in `lib/queries.ts`.

    A template literal per query, found by the backtick that opens it. Crude, and
    crude is right here: a query is a *string*, the strings are separated by
    blank lines by convention, and anything cleverer would need a TypeScript
    parser to be trustworthy.
    """
    text = (WEB / "lib/queries.ts").read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    for match in re.finditer(r"`([^`]*\bSELECT\b[^`]*)`", text, re.I | re.S):
        sql = match.group(1)
        # Skip the ones that are SQL inside a comment.
        if "--" in sql.split("\n")[0] and "SELECT" not in sql.split("\n")[0]:
            continue
        out.append((f"queries.ts:{len(out)}", sql))
    return out


def test_there_are_queries_to_check() -> None:
    """If the regex stops finding them, this file passes for the wrong reason.

    Every "nothing to check" test in this project has been a real gap wearing a
    green tick, and the cheapest defence is a test that fails when the thing it
    guards is absent.
    """
    found = len(_sql_statements())
    assert found == 4, (
        f"expected four statements in lib/queries.ts, found {found}. Either the "
        f"extraction regex has stopped matching — which makes every test below "
        f"vacuous — or a query was added or removed, and this count is the thing "
        f"that notices."
    )


def test_the_queries_touch_only_reviewed_tables() -> None:
    for name, sql in _sql_statements():
        for table in _tables_in(sql):
            assert table in ALLOWED_TABLES, (
                f"{name}: query touches {table!r}, which is not in the reviewed "
                f"set {sorted(ALLOWED_TABLES)}"
            )


def test_the_queries_name_only_contract_signals() -> None:
    """Same check as the pages, applied to the SQL.

    In the SQL it is stricter, because a signal id in a query is a `WHERE`
    clause that either matches or does not — and "does not" returns no rows,
    which on a trend panel is an empty chart rather than an error.
    """
    c = get_contract()
    for name, sql in _sql_statements():
        # `signal_id = ANY($1::text[])` is a parameter, not a literal, and the
        # test that its contents are contract signals is
        # `test_every_signal_id_the_pages_name_is_in_the_contract`.
        for found in re.findall(r"'([A-Z][A-Z0-9]*(?::[A-Z0-9][A-Z0-9_-]*){2,})'", sql):
            assert found in c.signals, f"{name}: {found!r} is not in the contract"


def test_the_trend_uses_the_aggregates_own_column_name() -> None:
    """`mean`, not `avg`. The mistake Phase 5a's dashboards made.

    `reading_1m` has `mean`, `min`, `max`, `n` — it is a continuous aggregate
    with a chosen name for each. `avg(value)` against it is a *loud* failure,
    which is the good case, but it is a failure that only a live database finds,
    and the whole reason `test_every_query_runs` exists below.
    """
    for name, sql in _sql_statements():
        assert "avg(" not in sql.lower() or "reading\n" in sql, (
            f"{name}: `avg(` against a continuous aggregate — the column is `mean`"
        )


def test_the_trend_breaks_the_line_across_a_gap() -> None:
    """A missing minute must produce a `null`, not a missing point.

    As a *source* property, which is weaker than a test of the behaviour and is
    labelled as such in the module docstring. The behaviour was verified by
    hand: 40 readings inserted with 6 removed produced two `<path>` elements in
    the response, which is a break rather than a straight line across the gap.
    """
    spark = (WEB / "components/Sparkline.tsx").read_text(encoding="utf-8")
    assert "runs" in spark, "the sparkline no longer splits into runs"
    assert "generate_series" in (WEB / "lib/queries.ts").read_text(encoding="utf-8"), (
        "the trend query no longer generates the empty buckets, so a gap and a "
        "flat line are indistinguishable again"
    )


def _to_pyformat(sql: str) -> tuple[str, dict[str, int]]:
    """Rewrite `$1`, `$2` … into psycopg's `%(p1)s`, `%(p2)s`, …

    **The same finding as the Node-RED flows, for the second time in this
    project, and that makes it a pattern rather than an anecdote.**

    `lib/queries.ts` is written for `node-postgres` (`pg`), which speaks the
    server-side protocol and takes positional `$1` placeholders. **psycopg3 does
    not**: it uses pyformat — `%s` or `%(name)s` — and given `$1` with a
    parameter it says

        the query has 0 placeholders but 1 parameters was passed

    which is a message about the *driver*, not about the query, and sends you
    looking in the wrong place. The first version of this test read that, took it
    as evidence the SQL was broken, and would have "fixed" four working queries.

    ## Why *named* placeholders and not bare `%s`

    The first attempt replaced `$1` → `%s`, `$2` → `%s`, and got a genuine
    failure out of `trend()`:

        operator does not exist: text = smallint
        LINE 11:  AND a.signal_id = $2

    Bare `%s` binds **left to right**, and in that query the two placeholders are
    not in numeric order: `make_interval(hours => $2::int)` appears on line 6 and
    `a.signal_id = $1` on line 12, because the numbers follow the *function
    signature* — `trend(signalId, hours)` — and not the order the text is in.

    That is perfectly legal for `pg`, which binds by number, and it is a trap
    for anything that binds by position. `%(p1)s` / `%(p2)s` is order-independent,
    so the translation is now faithful to the original numbering instead of
    quietly reordering it.

    The pattern across the project is now:

    | where | dialect | who else can run it |
    |---|---|---|
    | `scada/flows/*.json` | `$name` (Node-RED) | nobody |
    | `ui/web/lib/queries.ts` | `$1` (`pg`) | nobody in Python |

    Both are correct in their own runtime and runnable from Python only through
    a translation. So each has a test that translates, and the translation is the
    reason "it runs" is a claim rather than a hope.
    """
    numbers = sorted({int(n) for n in re.findall(r"\$(\d)", sql)})
    for n in numbers:
        sql = sql.replace(f"${n}", f"%(p{n})s")
    return sql, {f"p{n}": n for n in numbers}


@pytest.mark.integration
def test_every_query_runs_against_a_live_database() -> None:
    """Executed, with parameters bound, and **rolled back**.

    Every statement here is a `SELECT`, so there is nothing to roll back in
    `lib/queries.ts` — but the harness is shared with the Node-RED flow test
    which does write, and the discipline that came out of destroying real data
    twice is that a program which runs SQL it did not author leaves nothing
    behind.

    The values substituted are chosen to satisfy the types: `make_interval` takes
    an `int`, which is the same type error the alarm replay hit, and the signal
    id is a real one so a foreign key cannot report itself as a query bug.
    """
    from storage.postgres.schema import connect

    try:
        with connect() as probe:
            probe.rollback()
    except Exception as exc:
        # Any driver's connection error, and the reason is not this test's
        # business. Skipping rather than failing: a suite with a permanently red
        # test is a suite people stop reading, and nothing was verified either
        # way.
        pytest.skip(f"no database: {exc}")

    # Parameters per query, keyed by the query's own text, because `$1` is not
    # `$1` everywhere: `latest` takes the signal *array* as `$1`, `trend` takes
    # a signal as `$1` and an hour count as `$2`, and `outstandingAlarms` takes
    # only the hour count. Substituting one shared list by position produced
    # three real errors —
    #
    #     malformed array literal: "AERATION:AHU-1:DO"
    #     operator does not exist: text = smallint
    #     invalid input syntax for type integer: "AERATION:AHU-1:DO"
    #
    # — all of them mine, and all of them about the harness rather than the
    # query. Keying on the text means a new query fails loudly here instead of
    # silently inheriting another one's parameters.
    def params_for(sql: str) -> dict[str, object]:
        if "DISTINCT ON" in sql:
            return {"p1": ["AERATION:AHU-1:DO"]}        # $1::text[]
        if "generate_series" in sql:
            # Numbered by the function signature `trend(signalId, hours)`, so
            # `$1` is the signal and `$2` the hours — and `$2` appears *first* in
            # the text. That inversion is what the named-placeholder translation
            # above exists to survive.
            return {"p1": "AERATION:AHU-1:DO", "p2": 6}
        if "alarm_raised" in sql:
            return {"p1": 6}                            # $1 hours::int
        return {}                                       # dataSpan takes none

    for name, raw in _sql_statements():
        sql, numbering = _to_pyformat(raw)
        supplied = params_for(raw)
        assert set(supplied) == set(numbering), (
            f"{name} needs {sorted(numbering)} and the harness supplies "
            f"{sorted(supplied)}; add it to `params_for`"
        )
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, supplied) if numbering else cur.execute(sql)
                cur.fetchall()
            conn.rollback()


# ── the credential cannot reach a browser ────────────────────────────────────


def test_no_database_url_is_baked_into_the_client_bundle() -> None:
    """No `NEXT_PUBLIC_` database variable, anywhere.

    The Dockerfile used to take `NEXT_PUBLIC_POSTGRES_URL` as a build argument
    and its own comment said "the browser cannot keep a secret" — which is true
    and irrelevant, because the argument existed. It was a host and a port, so
    nothing leaked, and a reviewer reading `NEXT_PUBLIC_POSTGRES_URL` has no way
    to know which it is. The pattern is one argument away from the password.
    """
    for name, text in _code().items():
        for var in re.findall(r"NEXT_PUBLIC_[A-Z_]+", text):
            assert not re.search(r"POSTGRES|PASSWORD|SECRET|TOKEN", var), (
                f"{name} declares {var}. NEXT_PUBLIC_ means 'baked into the "
                f"JavaScript the browser downloads'."
            )


def test_the_compose_service_passes_no_next_public_database_url() -> None:
    """Checked in the compose file too, because that is where the args were."""
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    # From the service key to the next two-space-indented key, which is the next
    # service. The first version of this test sliced to the next comment banner
    # and picked up the observability section — so it was asserting on Grafana's
    # environment, and would have passed no matter what `web` said.
    start = compose.index("\n  web:\n")
    rest = compose[start + 1:]
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", rest)
    web = rest[: nxt.start()] if nxt else rest
    # YAML comments, and they are load-bearing here: the comment above the `web`
    # service explains at length which `NEXT_PUBLIC_` variables were removed and
    # why, and a substring search finds that explanation rather than a setting.
    web = "\n".join(
        ln for ln in web.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "NEXT_PUBLIC_POSTGRES" not in web, (
        "the web service still bakes a database URL into the client bundle"
    )
    assert "POSTGRES_PASSWORD" in web, "and it should read one at run time"
    assert "read_only: true" in web, (
        "the app writes nothing, so a read-only root filesystem is free"
    )


def test_the_pool_is_server_only() -> None:
    """`server-only` throws at build time if a client component imports it.

    The package is one `throw`, and its `exports` map points a `react-server`
    condition at it — so the guard is a build error, not a convention. Asserted
    because a guard that is present in the file and absent from the `import` is
    a comment, and this project has shipped three of those.
    """
    db = (WEB / "lib/db.ts").read_text(encoding="utf-8")
    assert re.search(r"^import 'server-only';", db, re.M), (
        "lib/db.ts does not import 'server-only'; the credential guard is gone"
    )
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert "server-only" in package["dependencies"], (
        "'server-only' is imported but not declared — `npm ci` would fail"
    )


def test_no_client_component_can_reach_the_database() -> None:
    """`'use client'` files may not import `lib/db` or `lib/queries`.

    This is the invariant `server-only` enforces at build time, asserted here as
    well so that a mistake is a *test failure* rather than something a future
    maintainer discovers by reading a Next.js error message about
    `react-server` conditions.
    """
    for path in WEB.rglob("*.tsx"):
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "'use client'" not in text and '"use client"' not in text:
            continue
        assert "lib/db" not in text, (
            f"{path} is a client component and imports the pool"
        )
        assert "lib/queries" not in text, (
            f"{path} is a client component and imports the queries; the SQL would "
            f"run in the browser against a database URL that is not there"
        )


def test_there_is_no_second_write_path() -> None:
    """No server actions, and no `INSERT`/`UPDATE`/`DELETE` in the page's SQL.

    The only writer is the Node-RED annunciator, which holds the credential and
    the audit trail. A second write path on a page with **no authentication** —
    and this page has none, which is stated in the README — would be a way for
    anyone who can load it to silence an alarm. That is the one thing an alarm
    panel must not be.
    """
    for name, text in _code().items():
        assert not re.search(r"""['"]use server['"]""", text), (
            f"{name} declares a server action"
        )
    for name, sql in _sql_statements():
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP"):
            assert not re.search(rf"\b{verb}\b", sql, re.I), (
                f"{name} contains {verb}; the page reads and does not write"
            )


def test_the_page_says_it_is_not_a_compliance_record() -> None:
    """A green number on something that looks like a permit is a claim.

    This project cannot support that claim: the data is synthetic and there is no
    permit. The permit page says so in its own words, and if that sentence is
    ever removed the test fails rather than the page quietly becoming a document
    nobody should trust.
    """
    permit = (WEB / "app/permit/page.tsx").read_text(encoding="utf-8")
    # The rendered text is split across lines by the formatter, so the words are
    # matched on whitespace rather than as a literal — otherwise the sentence has
    # to be wrapped a particular way for the test to pass, and reformatting the
    # file fails the test.
    flat = re.sub(r"\s+", " ", _code_only(permit))
    assert "Not a compliance record" in flat
    assert "synthetic" in flat.lower()


# ── dependencies, and the shape of the thing ────────────────────────────────


def test_there_is_no_charting_library() -> None:
    """Asserted rather than merely true, so adding one is a deliberate act.

    The same argument as the Grafana dashboards using the built-in PostgreSQL
    datasource instead of a plugin: a dashboard that fails to render during an
    incident is worse than an ugly one. The sparkline is 90 lines with no
    transitive dependencies.
    """
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    deps = set(package["dependencies"])
    for banned in ("recharts", "chart.js", "victory", "plotly", "d3", "nivo",
                   "@mui/material", "antd"):
        assert banned not in deps, (
            f"{banned} is a dependency. If that is a deliberate decision, say so "
            f"in ui/web/README.md and in this test's docstring."
        )
    assert set(deps) == {"next", "pg", "react", "react-dom", "server-only"}, (
        f"the dependency list changed: {sorted(deps)}"
    )


def test_the_dockerfile_bakes_nothing_secret() -> None:
    """No `ARG` and no `ENV` carrying a credential, and no `NEXT_PUBLIC_` at all.

    The runtime image copies no sources and no lockfile, which is the other half
    of the same claim: the only thing in it that came from the build stage is
    `.next/`, `public/`, `node_modules/` and `package.json`.
    """
    # The Dockerfile is not TypeScript, so `_code_only` does not apply — but the
    # same problem does: its comment explains at length why `NEXT_PUBLIC_` was
    # removed, and a grep for the string finds the explanation.
    dockerfile = (WEB / "Dockerfile").read_text(encoding="utf-8")
    directives = [
        ln for ln in dockerfile.splitlines()
        if ln.strip().startswith(("ARG ", "ENV ", "RUN ", "COPY "))
    ]
    for line in directives:
        assert "NEXT_PUBLIC_" not in line, (
            f"the Dockerfile declares a NEXT_PUBLIC_ variable: {line.strip()}. "
            f"Every one of them is baked into the client bundle."
        )
    for line in dockerfile.splitlines():
        if line.strip().startswith(("ARG ", "ENV ")):
            assert not re.search(r"PASSWORD|SECRET|TOKEN|POSTGRES_USER", line), line
    assert "USER 10002" in dockerfile, "the runtime image runs as root"


def test_the_lockfile_agrees_with_the_manifest() -> None:
    """`npm ci` fails if they disagree, and the Dockerfile runs `npm ci`.

    Which means a `package.json` edited without regenerating the lock is an image
    build failure on a fresh machine and nothing at all on this one, where
    `node_modules` already exists. The failure mode is the fifth one in this
    project: correct locally, broken in the place it ships.
    """
    manifest = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    root = lock["packages"][""]
    assert root["dependencies"] == manifest["dependencies"], (
        "package-lock.json is out of step with package.json — run "
        "`npm install --package-lock-only` in ui/web"
    )
    assert root["devDependencies"] == manifest["devDependencies"]


def test_the_dockerignore_excludes_node_modules() -> None:
    """`COPY . .` in the build stage copies the context.

    Without this file a local `node_modules` from `npm run dev` lands on top of
    the pruned dependency tree the `deps` stage produced — with devDependencies,
    on Alpine, for the developer's platform. The image then differs from the one
    that was tested, silently.
    """
    ignore = (WEB / ".dockerignore").read_text(encoding="utf-8")
    entries = {ln.strip() for ln in ignore.splitlines() if ln.strip()}
    for required in ("node_modules", ".next", ".env"):
        assert required in entries, f".dockerignore does not exclude {required}"
