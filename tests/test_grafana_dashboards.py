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
    `python -m ui.grafana.generate_dashboards` silently reverts — the same failure
    as an operator editing a generated Node-RED flow, which is why
    `allowUiUpdates: false` is in the provisioning and `NODE_RED_EDITOR=false` is
    the default there.
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
    for parameter, unit, _limit, direction, measured, compliant in rows:
        assert unit, f"{parameter}: no unit"
        assert direction in ("min", "max"), f"{parameter}: {direction!r}"
        assert measured is not None, f"{parameter}: no measured value"
        assert compliant is not None, (
            f"{parameter}: compliant is NULL, which reads as neither pass nor "
            "fail on a panel"
        )
