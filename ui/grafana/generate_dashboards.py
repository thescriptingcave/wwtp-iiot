"""Generate the Grafana dashboards from the contract.

    python -m ui.grafana.generate_dashboards          # write the JSON
    python -m ui.grafana.generate_dashboards --check  # report drift

## Why the dashboards are generated

A Grafana dashboard is JSON with a panel layout, a query, and a set of thresholds
— and the *thresholds* are the part that matters. A hand-written dashboard holds
numbers somebody typed, which is the same situation as a hand-written alarm
threshold: correct on the day it is written, and **plausibly** wrong after the
process is re-tuned, because nothing objects and the panel keeps rendering.

So the numbers come from `contracts/tags.yaml`, which is the same source the
SQL course, the Node-RED tag list, the Modbus register map and the OPC UA address
space all come from. Change `normal_low` there and the dashboard's band moves.

## What the dashboards are honest about

**They are not the alarm system.** A red band here is "the value is outside the
band the contract declares", which is true most of the time and is not a fault
detector. `alarms/rules.py` decides what is wrong, from a *measured* healthy
distribution, and writes an `event` when it decides — and
`docs/ALARM-TUNING.md` is the long explanation of why a band drawn from a
specification is a poor alarm threshold. The band on a panel is drawn so an
operator can see whether the process is where the contract says it should be,
and nothing more.

**The deadband makes "no data" ambiguous**, so no panel here has a line that
simply stops. Thirteen of the 57 signals produce exactly one reading in a seeded
week; a panel that connects no points over such a signal looks like an outage and
is not one. `sql/02-04` is the long version, and it is the reason
`01-overview` carries a table of "how long since this last reported" next to the
trends rather than relying on the trends to show absence.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from softplc.contract import Contract
from softplc.contract import contract as get_contract

log = logging.getLogger("ui.grafana.dashboards")

DASHBOARDS = Path("ui/grafana/dashboards")

#: The datasource uid, matching `provisioning/datasources/postgres.yaml`.
DS = "wwtp-postgres"

#: Grafana's version the JSON is written for. Dashboard JSON is not versioned in
#: any useful way and Grafana upgrades it on load, so this is a note rather than a
#: constraint.
SCHEMA_VERSION = 39

#: Shown in the top-left of every panel that has one.
UNIT_NONE = "short"


def _tag(c: Contract, signal_id: str) -> Any:
    """The contract's signal, or a loud failure.

    Not `c.signals.get(...)` with a default. A dashboard panel for a signal that
    does not exist renders an empty graph forever, and an empty graph is not an
    error anybody notices.
    """
    if signal_id not in c.signals:
        raise KeyError(
            f"{signal_id} is not in the contract. A panel for a signal that does "
            "not exist renders an empty graph, and an empty graph is not an error "
            "anybody notices."
        )
    return c.signals[signal_id]


def _raw_query(c: Contract, signal_id: str) -> str:
    """A trend query against the **raw** table.

    For the last day or so. `reading_1m` and `reading_1h` are described in
    `sql/03-advanced/03-01` and exist for long ranges; at a one-day range the raw
    table is smaller than the rollup for a sparse signal and more precise for a
    fast one, and a panel showing the last 6 hours wants the actual samples.
    """
    s = _tag(c, signal_id)
    return (
        "SELECT time_bucket(INTERVAL '5 seconds', ts) AS time, "
        "       avg(value) AS value "
        "FROM reading "
        f"WHERE signal_id = '{signal_id}' "
        f"  AND ts >= $__timeFrom() AND ts <= $__timeTo() "
        "  AND value IS NOT NULL "
        f"GROUP BY time ORDER BY time -- {s.field}, raw tier"
    )


def _rollup_query(c: Contract, signal_id: str) -> str:
    """A trend query against the **hourly** tier, for long ranges.

    This is the query `sql/03-advanced/03-01` exists to justify: a week of raw
    dissolved oxygen is 1 645 rows and a week of hourly buckets is 168, and the
    answer is the same to three decimal places because the tier is a true mean
    over the raw rows rather than a mean of means.
    """
    s = _tag(c, signal_id)
    return (
        "SELECT bucket AS time, mean AS value "
        "FROM reading_1h "
        f"WHERE signal_id = '{signal_id}' "
        "  AND bucket >= $__timeFrom() AND bucket <= $__timeTo() "
        f"ORDER BY time -- {s.field}, hourly tier"
    )


def _target(ref_id: str, sql: str, legend: str) -> dict[str, Any]:
    return {
        "datasource": {"type": "postgres", "uid": DS},
        "refId": ref_id,
        "format": "time_series",
        "rawQuery": True,
        "rawSql": sql,
        "interval": "5s",
        "legendFormat": legend,
    }


def _timeseries_panel(
    c: Contract,
    panel_id: int,
    title: str,
    signal_ids: list[str],
    *,
    grid: tuple[int, int, int, int],
    rollup: bool = False,
    description: str = "",
    unit: str | None = None,
    show_band: bool = True,
) -> dict[str, Any]:
    """One time-series panel, with the contract's normal band as a threshold.

    The band is a **threshold step on the same axis**, not a second query. That
    matters: two queries for "the value" and "the band" means two different
    aggregations, and the band can disagree with the line at a point where they
    were not computed from the same rows.
    """
    query_fn = _rollup_query if rollup else _raw_query
    targets = [
        _target(chr(ord("A") + i), query_fn(c, sid), _tag(c, sid).field)
        for i, sid in enumerate(signal_ids)
    ]

    steps: list[dict[str, Any]] = [{"color": "text", "value": "thresholds"}]
    if show_band and len(signal_ids) == 1:
        s = _tag(c, signal_ids[0])
        # `off` at zero, then the band. A step *below* normal_low and a step
        # *above* normal_high, both absolute, in the signal's own units — which
        # is the only kind of threshold that means the same thing on a signal
        # measured in mg/L and one measured in rev/min.
        steps = [
            {"color": "text", "value": "thresholds"},
            {"color": "red", "value": s.normal_low, "op": "lt"},
            {"color": "text", "value": s.normal_low},
            {"color": "green", "value": s.normal_high, "op": "gt"},
        ]

    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": {"type": "postgres", "uid": DS},
        "gridPos": {
            "h": grid[2], "w": grid[3], "x": grid[0], "y": grid[1],
        },
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit or UNIT_NONE,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "fillOpacity": 8,
                    "showPoints": "never",
                    "spanNulls": False,
                },
                "thresholds": {
                    "mode": "absolute",
                    "steps": steps,
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
    }


def _table_panel(
    panel_id: int,
    title: str,
    sql: str,
    *,
    grid: tuple[int, int, int, int],
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "table",
        "title": title,
        "description": description,
        "datasource": {"type": "postgres", "uid": DS},
        "gridPos": {"h": grid[2], "w": grid[3], "x": grid[0], "y": grid[1]},
        "targets": [{
            "datasource": {"type": "postgres", "uid": DS},
            "refId": "A",
            "format": "table",
            "rawQuery": True,
            "rawSql": sql,
        }],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


def _permit_query(permit: dict[str, Any]) -> str:
    """The compliance table, and the only place a permit number is computed.

    Three mistakes this query made and what caught them, because all three are
    the kind that render a plausible number:

    * **`reading_1h` has no `value` column.** It has `mean`, `min`, `max` and `n`.
      The first version wrote `avg(value)`, and it failed with
      `column "value" does not exist` — which is the *good* outcome, because a
      wrong column is loud. Had the tier been the raw table, the same mistake
      would have been silent and would have meant averaging hourly means.
    * **pH was read from the TSS signal.** `EFFLUENT:FLOW:PH` exists; the query
      asked `reading_1h` for `EFFLUENT:FLOW:TSS`. There is nothing in the
      database that would object to averaging the wrong signal's pH, because the
      column is a float and the *unit* is not stored on the reading. A unit is
      carried by the signal's identity and by nothing else, which is why a query
      naming the wrong signal is a wrong number rather than an error.
    * **The permit minimum and maximum are two rows, not one.** pH has a floor
      and a ceiling, and a `min() <= limit` test on its own would pass a
      compliance failure.
    * **The CTE was called `window`.** Reserved, since SQL:2003 introduced
      `OVER window_name`. `syntax error at or near "window"`, which is loud, and
      which is the third time this query has failed loudly and the first time any
      of these would have failed quietly.

    Every query in `ui/grafana/dashboards/` is executed against a live database
    by `tests/test_grafana_dashboards.py`. A dashboard JSON file that is never
    run is a screenshot of somebody's guess.
    """
    nh4 = permit["eff_nh4_mg_l_30d_mean"]
    tss = permit["eff_tss_mg_l"]
    ph_lo, ph_hi = permit["eff_ph_min"], permit["eff_ph_max"]
    bacti = permit["dis_bacti_geomean"]
    return f"""
WITH span AS (SELECT now() - interval '30 days' AS since)
SELECT 'ammonia, 30-day mean' AS parameter,
       'mg/L'  AS unit,
       {nh4!r}::float AS permit_value,
       'max'  AS direction,
       round(avg(h.mean)::numeric, 3) AS measured,
       avg(h.mean) <= {nh4} AS compliant
FROM reading_1h h, span w
WHERE h.signal_id = 'EFFLUENT:FLOW:NH4' AND h.bucket >= w.since
HAVING count(*) > 0
UNION ALL
SELECT 'suspended solids, 30-day mean', 'mg/L', {tss!r}::float, 'max',
       round(avg(h.mean)::numeric, 3), avg(h.mean) <= {tss}
FROM reading_1h h, span w
WHERE h.signal_id = 'EFFLUENT:FLOW:TSS' AND h.bucket >= w.since
HAVING count(*) > 0
UNION ALL
SELECT 'pH, minimum', 'pH', {ph_lo!r}::float, 'min',
       round(min(h.mean)::numeric, 3), min(h.mean) >= {ph_lo}
FROM reading_1h h, span w
WHERE h.signal_id = 'EFFLUENT:FLOW:PH' AND h.bucket >= w.since
HAVING count(*) > 0
UNION ALL
SELECT 'pH, maximum', 'pH', {ph_hi!r}::float, 'max',
       round(max(h.mean)::numeric, 3), max(h.mean) <= {ph_hi}
FROM reading_1h h, span w
WHERE h.signal_id = 'EFFLUENT:FLOW:PH' AND h.bucket >= w.since
HAVING count(*) > 0
UNION ALL
SELECT 'coliforms, geometric mean', '{{MPN}}/100mL', {bacti!r}::float, 'max',
       round(exp(avg(ln(h.mean)))::numeric, 1), exp(avg(ln(h.mean))) <= {bacti}
FROM reading_1h h, span w
WHERE h.signal_id = 'EFFLUENT:DIS-CL-2:BACTI' AND h.bucket >= w.since
  AND h.mean > 0
HAVING count(*) > 0
ORDER BY parameter
""".strip()


def _dashboard(
    uid: str,
    title: str,
    description: str,
    panels: list[dict[str, Any]],
    *,
    refresh: str = "30s",
    time_from: str = "now-6h",
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["wwtp", "generated"],
        "editable": False,
        "schemaVersion": SCHEMA_VERSION,
        "version": 1,
        "graphTooltip": 1,          # shared crosshair: comparing two signals is
                                    # the entire reason to have a dashboard
        "refresh": refresh,
        "timezone": "utc",
        "time": {"from": time_from, "to": "now"},
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": panels,
    }


def build_overview(c: Contract) -> dict[str, Any]:
    return _dashboard(
        "wwtp-overview",
        "WWTP — overview",
        "The plant at a glance. Generated from contracts/tags.yaml; the band on "
        "each panel is the contract's `normal_low`/`normal_high`, which is a "
        "*specification* and not an alarm threshold — see docs/ALARM-TUNING.md.",
        [
            _timeseries_panel(
                c,
                1, "Influent and effluent flow",
                ["INFLUENT:FLOW:FLOW", "EFFLUENT:FLOW:FLOW"],
                grid=(0, 0, 8, 8), unit="m3/h", show_band=False,
                description="Two different quantities on one axis is wrong, and "
                            "they are here anyway because the *shape* comparison "
                            "is the point: effluent above influent means water "
                            "is being added. The unit is the shared one.",
            ),
            _timeseries_panel(
                c,
                2, "Dissolved oxygen",
                ["AERATION:AHU-1:DO"],
                grid=(8, 0, 8, 8), unit="mg/L",
                description="Aeration basin DO. The band is the contract's "
                            "1.5-3.0 mg/L.",
            ),
            _timeseries_panel(
                c,
                3, "Blower speed and air flow",
                ["AERATION:AHU-1:BLOWER_RPM", "AERATION:AHU-1:AIR_FLOW"],
                grid=(16, 0, 8, 8), show_band=False,
                description="Two quantities, two units, **no band**. A "
                            "threshold in rev/min drawn on a panel whose other "
                            "series is m3/h is a number with no meaning, and a "
                            "reader cannot tell which series it belongs to. When "
                            "two units have to share a panel, the band is the "
                            "thing that has to go, and the panel says so rather "
                            "than implying the units agree.",
            ),
            _timeseries_panel(
                c,
                4, "Effluent quality",
                ["EFFLUENT:FLOW:NH4", "EFFLUENT:FLOW:TSS"],
                grid=(0, 8, 12, 8), show_band=False,
                description="Ammonia and suspended solids, hourly tier, so a week "
                            "on screen is 168 points rather than 12 000.",
                rollup=True,
            ),
            _timeseries_panel(
                c,
                5, "Sludge and digester",
                ["SECONDARY:SEC-CL-1:BLANKET", "SLUDGE:DIG-1:PH"],
                grid=(12, 8, 12, 8), show_band=False,
                description="Secondary clarifier blanket depth and digester pH. "
                            "No band: metres and pH are not comparable, and a "
                            "blanket depth band drawn on this panel would "
                            "silently be read as a pH limit. Each needs its own "
                            "panel to have a band, which is a layout decision "
                            "this generator makes by refusing to draw one here.",
            ),
            _table_panel(
                6, "What has stopped reporting",
                """
SELECT s.id AS signal,
       s.area,
       s.unit,
       max(r.ts) AS last_reading,
       now() - max(r.ts) AS silence
FROM signal s
LEFT JOIN reading r ON r.signal_id = s.id
GROUP BY s.id, s.area, s.unit
HAVING max(r.ts) IS NULL
    OR now() - max(r.ts) > interval '1 hour'
ORDER BY silence DESC NULLS FIRST
LIMIT 30
""".strip(),
                grid=(0, 16, 24, 10),
                description="**The deadband's honest limit, shown rather than "
                            "hidden.** A row exists when the value *moved*, so a "
                            "signal whose value never moves produces one row and "
                            "then nothing. Thirteen of the 57 signals do exactly "
                            "that in a seeded week, and on a trend panel that "
                            "looks like an outage and is not one. This table is "
                            "why a panel that simply stops drawing is not "
                            "evidence that a signal has failed — see "
                            "sql/02-intermediate/02-04_gaps.md.",
            ),
        ],
    )


def build_permit(c: Contract) -> dict[str, Any]:
    permit = c.permit
    return _dashboard(
        "wwtp-permit",
        "WWTP — discharge permit",
        "The consent conditions, from the contract's `permit:` block, with the "
        "signals that evidence them. Generated from contracts/tags.yaml.",
        [
            _timeseries_panel(
                c,
                1, f"Ammonia — permit {permit['eff_nh4_mg_l_30d_mean']:g} mg/L "
                   "30-day mean",
                ["EFFLUENT:FLOW:NH4"],
                grid=(0, 0, 12, 9), unit="mg/L",
                description="The permit is a **30-day mean**, and this panel is "
                            "not one. It is here to show the signal, and the "
                            "report against the permit is computed below — "
                            "because a panel of hourly values and a compliance "
                            "number that is a 30-day mean are two different "
                            "facts and putting the second next to the first "
                            "without saying so is how a plant talks itself into "
                            "a breach.",
                rollup=True,
            ),
            _timeseries_panel(
                c,
                2, f"Suspended solids — permit {permit['eff_tss_mg_l']:g} mg/L",
                ["EFFLUENT:FLOW:TSS"],
                grid=(12, 0, 12, 9), unit="mg/L", rollup=True,
                description="Suspended solids against a "
                            f"{permit['eff_tss_mg_l']:g} mg/L permit, hourly "
                            "tier. The band is the contract's normal band and is "
                            "*not* the permit: the permit is a limit and the band "
                            "is where the process usually is, and they are "
                            "different numbers doing different jobs. The permit "
                            "computation is in the table below.",
            ),
            _table_panel(
                3, "Permit compliance, from the contract",
                _permit_query(permit),

                grid=(0, 9, 24, 8),
                description="The **only** place in this project that computes a "
                            "compliance number, and it is the `reading_1h` tier "
                            "on purpose: a 30-day mean over 4.3 million raw rows "
                            "is a query nobody runs, and over 720 hourly buckets "
                            "it is one nobody notices. A false number computed "
                            "quickly is still a false number, so the arithmetic "
                            "is checked in `tests/test_grafana_dashboards.py` "
                            "rather than trusted here.",
            ),
        ],
        time_from="now-30d",
    )


BUILDERS = {
    "wwtp-overview.json": build_overview,
    "wwtp-permit.json": build_permit,
}


def build_all(c: Contract) -> dict[str, dict[str, Any]]:
    return {name: builder(c) for name, builder in BUILDERS.items()}


def render(dash: dict[str, Any]) -> str:
    return json.dumps(dash, indent=2, sort_keys=False) + "\n"


def check(c: Contract | None = None, directory: Path = DASHBOARDS) -> list[str]:
    c = c or get_contract()
    problems: list[str] = []
    for name, builder in BUILDERS.items():
        path = directory / name
        if not path.exists():
            problems.append(f"{path} does not exist")
            continue
        if path.read_text(encoding="utf-8") != render(builder(c)):
            problems.append(
                f"{path} is out of step with the contract "
                "(run: python -m ui.grafana.generate_dashboards)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ui.grafana.generate_dashboards",
        description="Generate the Grafana dashboards from contracts/tags.yaml.",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dir", type=Path, default=DASHBOARDS)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    c = get_contract()

    if args.check:
        problems = check(c, args.dir)
        for line in problems:
            log.error("%s", line)
        if problems:
            return 1
        log.info("all %d dashboards are in step with the contract",
                 len(BUILDERS))
        return 0

    args.dir.mkdir(parents=True, exist_ok=True)
    for name, dash in build_all(c).items():
        (args.dir / name).write_text(render(dash), encoding="utf-8")
        log.info("wrote %s/%s: %d panels", args.dir, name, len(dash["panels"]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
