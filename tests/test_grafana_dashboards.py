"""The Grafana dashboards, checked against a live database.

A dashboard is a JSON file with SQL inside it, and **a JSON file with SQL inside
it is not validated by anything.** It imports, it renders, and a panel whose
query is wrong shows an empty graph — which is indistinguishable from a panel
whose query is right and whose signal has no data.

So every query in `ui/grafana/dashboards/` is executed here, against a real
database, with Grafana's `$__timeFrom()` / `$__timeTo()` macros substituted. This
is the same discipline as `tools/check_sql.py` for the course, and it is the
thing that caught three mistakes in the permit query the first time it ran:

* `reading_1h` has **no `value` column** — it has `mean`, `min`, `max`, `n` — and
  `column "value" does not exist` is a *good* failure. The same mistake against
  the raw table would have been silent and would have meant averaging hourly
  means.
* **pH was read from the TSS signal.** `EFFLUENT:FLOW:PH` exists. Nothing in the
  database objects to averaging the wrong signal, because a unit is carried by
  the signal's *identity* and by nothing on the row — which is why a query naming
  the wrong signal is a wrong number and not an error.
* **The CTE was called `window`**, which is reserved since SQL:2003.

Skipped without a database, like the rest of the integration suite. There is a
*structural* set of checks below that always runs, and they are the ones that
catch a bug in the generator rather than a bug in a query.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest
from softplc.contract import Contract
from softplc.contract import contract as get_contract
from storage.postgres.schema import connect
from ui.grafana import generate_dashboards as gen

DASHBOARDS = gen.DASHBOARDS

#: Grafana's time macros, and what they become when a query is run by hand.
#:
#: The two dashboards have different default ranges — the overview is 6 hours and
#: the permit panel is 30 days — and the substitution is deliberately the
#: *longer* of them. A query that only works over six hours is a query that will
#: break the moment somebody drags the time picker, and the way to find that out
#: is to run it over the range the dashboard actually offers.
MACROS = {
    "$__timeFrom()": "now() - interval '30 days'",
    "$__timeTo()": "now()",
}


def _dashboards() -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(DASHBOARDS.glob("*.json"))
    }


def _substitute(sql: str) -> str:
    for macro, replacement in MACROS.items():
        sql = sql.replace(macro, replacement)
    assert "$__" not in sql, f"unsubstituted Grafana macro in: {sql[:80]}"
    return sql


#: psycopg type codes that Grafana would turn into a separate series. A stat
#: panel draws one large number per one of these, which is why the count
#: matters more than the SQL being valid.
_NUMERIC = {20, 21, 23, 700, 701, 1700}   # int2/4/8, float4/8, numeric


def _queries(dash: dict) -> list[tuple[str, str]]:
    out = []
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            out.append((panel["title"], target.get("rawSql", "")))
    return out


@pytest.fixture(scope="module")
def c() -> Contract:
    return get_contract()


@pytest.fixture(scope="module")
def dashboards() -> dict[str, dict]:
    return _dashboards()


# ── drift ────────────────────────────────────────────────────────────────────


def test_the_dashboards_are_in_step_with_the_contract(c: Contract) -> None:
    assert gen.check(c, DASHBOARDS) == [], (
        "run: python -m ui.grafana.generate_dashboards"
    )


# ── structure, which always runs ─────────────────────────────────────────────


def test_there_is_a_dashboard_for_every_builder(dashboards: dict) -> None:
    assert set(dashboards) == set(gen.BUILDERS)


def test_a_dashboard_is_not_ui_editable(dashboards: dict) -> None:
    """`editable: false` on the dashboard, because they are generated.

    An edit made in the Grafana UI on a generated dashboard is a change that
    `python -m ui.grafana.generate_dashboards` silently reverts.

    This docstring used to end by pointing at `NODE_RED_EDITOR=false` in compose as
    the Node-RED equivalent. That variable was read by nothing — no Node-RED
    setting consumes it and the `nodered/node-red` image does not define it — so
    it was the same "generated artefact, edit is reverted" hazard described with a
    control that does not exist, and a test asserting the variable was set to
    "false" made it look enforced. Removed from compose; see the note in
    `scada/nodered/settings.js` for what is actually true about the editor.
    """
    for name, dash in dashboards.items():
        assert dash["editable"] is False, name
        assert "generated" in dash.get("tags", []), name


def test_every_panel_has_a_unique_id_and_a_grid_position(dashboards: dict) -> None:
    """Grafana silently overwrites a panel that shares an id, and a panel with
    no `gridPos` is stacked at 0,0 on top of the first one.

    Both import without complaint and both look like a rendering bug.
    """
    for name, dash in dashboards.items():
        ids = [p["id"] for p in dash["panels"]]
        assert len(ids) == len(set(ids)), f"{name}: duplicate panel id"
        occupied = set()
        for panel in dash["panels"]:
            grid = panel.get("gridPos")
            assert grid, f"{name}: panel {panel['id']} has no gridPos"
            cell = (grid["x"], grid["y"])
            assert cell not in occupied, (
                f"{name}: two panels at {cell}; Grafana stacks them and it looks "
                "like a rendering bug"
            )
            occupied.add(cell)


def test_a_stat_panel_returns_exactly_one_numeric_field(
    dashboards: dict, live_db: bool,
) -> None:
    """A stat panel draws one large number *per numeric field*, so one field.

    **This is the bug that shipped.** The first at-a-glance row reused
    `_raw_query`, which returns `time`, `value`, `quality` and `samples`. The
    postgres datasource turns every numeric column into its own series, so each
    tile rendered as three stacked numbers:

        1588.23 m3/h
        quality      0.00 m3/h
        samples      1.00 m3/h

    Nothing errored. The SQL was valid, `test_every_dashboard_query_runs`
    passed, and the JSON passed every structural check — because every one of
    those checks looks at the query text or the panel shape, and the fault is in
    how Grafana *interprets* a correct frame. It was found by somebody opening
    the dashboard.

    Asserted by executing the panel's query and counting numeric fields in the
    returned frame, which is the only place the fault is visible.
    """
    for name, dash in dashboards.items():
        for panel in dash["panels"]:
            if panel["type"] != "stat":
                continue
            sql = _substitute(panel["targets"][0]["rawSql"])
            with connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
                numeric = [
                    d.name for d in cur.description if d.type_code in _NUMERIC
                ]
            assert numeric == ["value"], (
                f"{name}: the {panel['title']!r} stat panel returns "
                f"{numeric}. A stat panel draws one large number per numeric "
                f"field, so {len(numeric)} of them is {len(numeric)} numbers "
                f"where there should be one — and the unit is applied to all of "
                f"them. Use `_latest_query`, which returns a single value "
                f"column."
            )


def test_every_stat_panel_shows_a_stored_value(
    dashboards: dict, live_db: bool,
) -> None:
    """A tile that reads *No data* is worse than no tile.

    The second fault. The row originally carried effluent ammonia, whose
    deadband is 0.1 mg/L and which moves by less than that over six hours, so the
    gateway stored almost nothing and the tile had nothing to show. **No data**
    beside three live numbers is read as a failed instrument, which is the one
    misreading this plant's whole fault library exists to avoid.

    **Twenty-four hours, not one hour -- and the difference is the point.** An
    earlier version of this test demanded a reading in the last hour and failed
    on two of the four tiles, which is not a bug in the tiles. Effluent flow and
    aeration DO are the two numbers a plant manager most wants, and neither
    stored anything in the last hour because the plant had genuinely settled.

    That is not a reason to change the tiles; it is the reason the overview now
    carries a *How old is the reading behind each number?* table beside them. A
    stat panel shows one number and no timestamp, so it cannot distinguish a
    live reading from a five-hour-old one, and the reader needs to be able to.
    This test guards the floor -- a tile that can show *nothing* -- and the
    table carries the currency question where it can be answered honestly.
    """
    for name, dash in dashboards.items():
        for panel in dash["panels"]:
            if panel["type"] != "stat":
                continue
            sql = _substitute(panel["targets"][0]["rawSql"])
            with connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
            assert row is not None and row[0] is not None, (
                f"{name}: the {panel['title']!r} stat panel reads No data against "
                f"the live historian. A status row is read in five seconds and "
                f"must show something -- check the signal's deadband against how "
                f"fast it changes, not just that the id exists."
            )


def test_the_freshness_table_covers_exactly_the_stat_tiles(
    dashboards: dict,
) -> None:
    """The table that answers "how old is that number?" must list the numbers.

    Two lists of the same four signals and nothing joins them, so renaming a tile
    without touching the table leaves the reader unable to check the one thing a
    stat panel cannot tell them -- and no other check notices.
    """
    overview = dashboards["wwtp-overview.json"]
    tiles = {
        re.search(r"signal_id = '([^']+)'", p["targets"][0]["rawSql"]).group(1)
        for p in overview["panels"] if p["type"] == "stat"
    }
    freshness = next(
        p for p in overview["panels"]
        if p["title"].startswith("How old is the reading")
    )
    listed = set(re.findall(
        # The last segment needs `_` as well: AERATION:AHU-1:AIR_FLOW.
        r"'([A-Z][A-Z0-9_]*(?::[A-Z0-9_-]+){2})'",
        freshness["targets"][0]["rawSql"],
    ))
    assert listed == tiles, (
        f"the freshness table lists {sorted(listed)} but the stat tiles show "
        f"{sorted(tiles)}. A tile whose age is not shown is a number a reader "
        f"has to take on trust."
    )



def test_a_panel_fits_the_grid_and_does_not_overlap_its_neighbour(
    dashboards: dict,
) -> None:
    """24 columns wide, and no two panels in a row band may overlap.

    **This test exists because `gridPos` height and width were transposed and
    every other gate passed.** Four panels were written `grid=(0, 12, 12, 8)`
    where the tuple is `(x, y, h, w)` — so a panel meant to be 12 across and 8
    tall was 8 across and 12 tall, which renders as a column of narrow tall
    panels instead of the row they were laid out as.

    The existing check above only compares `(x, y)`, because that is the failure
    Grafana reports *silently*: two panels at the same origin stack on top of
    each other and look like a rendering bug. A transposed `h`/`w` does not
    stack, does not error, and does not violate anything a reader of the JSON
    would notice without a picture — so the check that would have caught it did
    not exist.

    Three assertions, because each catches a different mistake:

    * **fits** — `x + w <= 24`. A panel wider than the grid is silently clipped,
      and the missing pixels look like a rendering fault.
    * **no overlap** — two panels occupying the same columns of the same row
      band. Grafana draws both and the second covers the first, so content
      vanishes rather than erroring.
    * **every row band tiles all 24 columns** — which is what actually catches
      a transposition, because `h` and `w` swapped inside a sane aspect ratio
      changes nothing about the shape and everything about the row.
    """
    for name, dash in dashboards.items():
        bands: dict[int, list[tuple[int, int, int]]] = {}
        for panel in dash["panels"]:
            g = panel["gridPos"]
            x, y, h, w = g["x"], g["y"], g["h"], g["w"]
            assert x >= 0 and y >= 0, f"{name}: panel {panel['id']} negative position"
            assert w >= 1 and h >= 1, f"{name}: panel {panel['id']} has zero size"
            assert x + w <= 24, (
                f"{name}: panel {panel['id']} spans x={x} w={w}, past the 24 "
                f"columns Grafana lays out. The overflow is clipped silently "
                f"and reads as a rendering fault."
            )
            bands.setdefault(y, []).append((x, x + w, panel["id"]))

        for y, spans in bands.items():
            spans.sort()
            for left, right in itertools.pairwise(spans):
                assert right[0] >= left[1], (
                    f"{name}: panels {left[2]} and {right[2]} overlap on row {y} "
                    f"({left[0]}-{left[1]} and {right[0]}-{right[1]}). Grafana "
                    f"draws both and one covers the other."
                )
            # **Every row band tiles the full width, with no gap and no ragged
            # edge.** This is the assertion that catches a transposed `h`/`w`,
            # and it replaces a ratio check that could not. The bug turned a
            # 12-wide, 8-tall panel into an 8-wide, 12-tall one, and those are
            # both 1.5:1 — so "is this shape sensible" is the wrong question.
            # What actually changed is the *row*: the band stopped adding up.
            #
            # Full-width tiling is a real property of these dashboards rather
            # than a style preference. The panels are grouped in threes, in
            # fours, and as full-width singles on purpose, and a ragged edge is
            # a panel an operator has to hunt for.
            cursor = 0
            for start, end, pid in spans:
                assert start == cursor, (
                    f"{name}: row {y} has a gap at column {cursor} — panel {pid} "
                    f"starts at {start} instead. The panels in this dashboard "
                    f"tile their row band exactly, and a hole here is usually a "
                    f"transposed gridPos: the tuple is (x, y, h, w)."
                )
                cursor = end
            assert cursor == 24, (
                f"{name}: row {y} ends at column {cursor}, not 24"
            )


def test_every_target_names_the_provisioned_datasource(
    dashboards: dict,
) -> None:
    """The uid must match `provisioning/datasources/postgres.yaml`.

    A mismatched uid is a panel that says "datasource not found" on every
    dashboard, and provisioning reports the datasource as healthy.
    """
    provisioning = Path(
        "ui/grafana/provisioning/datasources/postgres.yaml"
    ).read_text(encoding="utf-8")
    assert f"uid: {gen.DS}" in provisioning, (
        f"the dashboards use uid {gen.DS!r}, which the datasource provisioning "
        "does not declare"
    )
    for name, dash in dashboards.items():
        for panel in dash["panels"]:
            ds = panel.get("datasource", {})
            assert ds.get("uid") == gen.DS, f"{name}: panel {panel['id']}"


def test_every_query_names_a_signal_the_contract_declares(
    dashboards: dict, c: Contract
) -> None:
    """The one that would have caught the pH-from-TSS mistake structurally.

    Every `signal_id = '...'` in a query must name a real signal. A wrong signal
    id is a *valid* query against a valid table, and the only way to catch it is
    to check the identity against the contract.
    """
    pattern = re.compile(r"signal_id\s*=\s*'([^']+)'")
    for name, dash in dashboards.items():
        for title, sql in _queries(dash):
            for signal_id in pattern.findall(sql):
                assert signal_id in c.signals, (
                    f"{name} / {title}: query names {signal_id!r}, which is not "
                    "a signal in the contract"
                )


def test_no_query_touches_a_table_outside_the_reviewed_set(dashboards: dict) -> None:
    allowed = {"reading", "reading_1m", "reading_1h", "signal", "equipment",
               "event", "site"}
    for name, dash in dashboards.items():
        for title, sql in _queries(dash):
            for table in re.findall(
                r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", sql, re.I
            ):
                assert table in allowed, (
                    f"{name} / {title}: touches {table!r}, not in {sorted(allowed)}"
                )


def test_the_rollup_queries_use_a_column_that_exists(dashboards: dict) -> None:
    """`reading_1h` has `mean`, not `value`.

    The permit query wrote `avg(value)` against the hourly tier and it failed
    loudly — which is the good outcome. This pins the column names so the
    mistake cannot come back in a query whose *other* line is wrong too, where it
    would be masked.
    """
    schema = Path("storage/postgres/schema.sql").read_text(encoding="utf-8")
    start = schema.index("CREATE MATERIALIZED VIEW IF NOT EXISTS reading_1h")
    body = schema[start:schema.index("WITH NO DATA", start)]
    for column in ("bucket", "mean", "min", "max", "n"):
        assert column in body, f"reading_1h has no column {column!r}"

    for name, dash in dashboards.items():
        for title, sql in _queries(dash):
            if "reading_1h" not in sql:
                continue
            # No bare `value` and no bare `ts` against the rollup.
            assert not re.search(r"\bavg\(value\)", sql), (
                f"{name} / {title}: averages `value` from reading_1h, which has "
                "`mean`. Use avg(mean)."
            )
            assert "bucket" in sql, f"{name} / {title}: reading_1h is bucketed"


def test_a_band_threshold_only_appears_on_a_single_signal_panel(
    dashboards: dict, c: Contract
) -> None:
    """A threshold is in the signal's own units, so a shared axis cannot have one.

    Two signals on one panel have two units, and a step at 1.5 means mg/L on one
    series and is meaningless on the other. The generator turns the band off for
    multi-signal panels; this checks it stayed off.
    """
    for name, dash in dashboards.items():
        for panel in dash["panels"]:
            if panel["type"] != "timeseries":
                continue
            steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
            has_band = any(
                s.get("op") in ("lt", "gt") for s in steps
            )
            series = len(panel.get("targets", []))
            assert not (has_band and series > 1), (
                f"{name}: panel {panel['id']!r} has {series} series and a unit "
                "threshold, so the threshold is only meaningful for one of them"
            )
            for step in steps:
                if step.get("op") in ("lt", "gt"):
                    assert step["value"] != 0, (
                        f"{name}: a threshold at 0 is almost certainly a "
                        "placeholder rather than a number"
                    )


def test_a_banded_panel_uses_the_contracts_own_normal_band(
    dashboards: dict, c: Contract
) -> None:
    """The band on a panel is the contract's, not a number somebody typed."""
    for name, dash in dashboards.items():
        for panel in dash["panels"]:
            if panel["type"] != "timeseries":
                continue
            steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
            ops = [s for s in steps if s.get("op") in ("lt", "gt")]
            if not ops:
                continue
            assert len(ops) == 2, (
                f"{name}: panel {panel['id']!r} has {len(ops)} band steps; a "
                "band is a floor and a ceiling"
            )
            low = next(s["value"] for s in ops if s["op"] == "lt")
            high = next(s["value"] for s in ops if s["op"] == "gt")
            assert low < high, f"{name}: band {low}..{high} is inverted"
            # And the values are the contract's, for a signal it declares.
            assert low in {s.normal_low for s in c.signals.values()}
            assert high in {s.normal_high for s in c.signals.values()}


def test_the_permit_dashboard_reports_every_permit_condition(
    dashboards: dict, c: Contract
) -> None:
    """Five conditions, five rows.

    The permit block has five limits and pH has *two*, so a query with one row
    per condition is four rows short if it treats pH as one. Missing a compliance
    limit is the most consequential thing this project could get wrong, because
    the number that goes missing is a permit.
    """
    permit = c.permit
    expected = {
        "eff_nh4_mg_l_30d_mean": 1,
        "eff_tss_mg_l": 1,
        "eff_ph_min": 1,
        "eff_ph_max": 1,
        "dis_bacti_geomean": 1,
    }
    assert set(expected) == set(permit), (
        f"the permit block changed: {sorted(permit)}"
    )
    total = sum(expected.values())
    sql = next(
        q for title, q in _queries(dashboards["wwtp-permit.json"])
        if "permit_value" in q
    )
    selects = sql.upper().count("SELECT '")
    assert selects == total, (
        f"the permit table has {selects} condition rows for {total} conditions"
    )
    # Every limit appears, so a limit added to the contract without a row fails.
    for key, value in permit.items():
        assert repr(value) in sql, f"permit limit {key}={value!r} is not in the query"


def test_the_coliform_figure_is_a_geometric_mean(dashboards: dict) -> None:
    """`dis_bacti_geomean` is 200 MPN/100mL and MPNs are log-normal.

    An arithmetic mean of microbiological counts is not a smaller number, it is a
    different and meaningless one: a single 10 000 spike dominates an arithmetic
    mean of a hundred counts around 50, while the geometric mean of the same set
    is barely moved. The permit is written as a geometric mean and the query has
    to match it.
    """
    sql = next(
        q for title, q in _queries(dashboards["wwtp-permit.json"])
        if "coliforms" in q
    )
    assert "exp(avg(ln(" in sql, (
        "the coliform figure is averaged arithmetically; the permit is a "
        "geometric mean and the two are not the same measurement"
    )
    assert "h.mean > 0" in sql, (
        "ln() of zero is -inf, which would silently drag the geometric mean to "
        "zero and report a *pass*. A zero count is not a geometric mean and the "
        "row must say so rather than exclude itself quietly."
    )


def test_a_dashboard_describes_itself_where_a_reader_will_look(
    dashboards: dict,
) -> None:
    """Every panel has a description, and the honest ones say what they are not.

    A panel that says "Dissolved oxygen" and nothing else invites a reader to
    treat the band as an alarm limit. The description is where that is corrected,
    and it is the only place a reader of a dashboard will look.
    """
    for name, dash in dashboards.items():
        for panel in dash["panels"]:
            assert panel.get("description"), (
                f"{name}: panel {panel['title']!r} has no description"
            )


# ── against a live database ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def live_db():
    """A live database, or a skip. Same shape as `tests/integration/conftest.py`.

    Named so these two tests can *request* the skip rather than be collected and
    fail: `@pytest.mark.integration` registers a marker in this project, it does
    not deselect anything, and a test that fails because a container is not
    running is a test that fails for the wrong reason.
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # a connection error is any driver's
        pytest.skip(f"no database: {exc}")
    return True


def test_every_dashboard_query_runs(dashboards: dict, live_db: bool) -> None:
    """The check that cannot be done statically.

    Executed with Grafana's macros substituted, over 30 days — the longer of the
    two dashboards' default ranges, so a query that only works over six hours
    fails here rather than when somebody drags the time picker.
    """
    total = 0
    for name, dash in dashboards.items():
        for title, raw in _queries(dash):
            sql = _substitute(raw)
            total += 1
            try:
                with connect() as conn, conn.cursor() as cur:
                    cur.execute(sql)
                    cur.fetchall()
            except Exception as exc:
                # The message is the point: `pytest.fail` reports it verbatim,
                # so a dashboard query that does not run says which query.
                pytest.fail(
                    f"{name} / {title!r} does not run: {exc}\n\n{sql}"
                )
    assert total > 0, "no queries found; the dashboard files are not parsing"


def test_the_permit_table_returns_one_row_per_condition(
    dashboards: dict, live_db: bool,
) -> None:
    """A compliance table that silently returns nothing is the worst outcome.

    Every row is guarded with `HAVING count(*) > 0`, so a condition with no data
    in the window is *absent* rather than *compliant*. That is deliberate and it
    is the right choice — a permit condition with no evidence is not a pass — but
    it means an empty table is ambiguous, and this test says which is which.
    """
    sql = _substitute(next(
        q for title, q in _queries(dashboards["wwtp-permit.json"])
        if "permit_value" in q
    ))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    if not rows:
        pytest.skip("no readings in the last 30 days; nothing to assert")
    assert len(rows) == len(get_contract().permit), (
        f"{len(rows)} rows for {len(get_contract().permit)} permit conditions"
    )
    for parameter, unit, limit, direction, measured, pct, compliant in rows:
        assert unit, f"{parameter}: no unit"
        assert direction in ("min", "max"), f"{parameter}: {direction!r}"
        assert measured is not None, f"{parameter}: no measured value"
        assert compliant is not None, (
            f"{parameter}: compliant is NULL, which reads as neither pass nor "
            "fail on a panel"
        )
        # **The seventh column.** `pct_of_permit` is what turns a boolean into
        # something an operator can act on — `1.06, true` does not say whether
        # that is a comfortable pass or one afternoon from a breach.
        assert pct is not None, f"{parameter}: pct_of_permit is NULL"
        # `measured` comes back as Decimal and `limit` as float; the mixed
        # arithmetic is the point, since that is what the query does too.
        expected = round(100 * float(measured) / float(limit), 1)
        assert abs(float(pct) - expected) < 0.15, (
            f"{parameter}: pct_of_permit is {pct} but measured {measured} "
            f"against a limit of {limit} is {expected}"
        )
        # And it has to agree with the verdict beside it, accounting for
        # direction. A percentage that disagrees with the boolean next to it is
        # worse than either alone: the reader has to decide which to believe,
        # and "which side is good" depends on whether the limit is a floor.
        over = float(pct) > 100
        breaches = over if direction == "max" else not over
        assert breaches == (not compliant), (
            f"{parameter}: pct_of_permit {pct} against a {direction!r} limit "
            f"says it {'breaches' if breaches else 'passes'}, but compliant "
            f"is {compliant}"
        )
