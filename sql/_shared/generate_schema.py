"""Generate ``sql/_shared/schema.sql`` from the contract.

The SQL course has to query a schema. If that schema is written by hand it will
drift from ``contracts/tags.yaml`` within a week, and every lesson in
``sql/01-beginner/`` will be querying tables that do not exist — which is the worst
possible failure for a teaching resource, because the student cannot tell whether
they misunderstood the query or the data.

So the schema is *generated*, and this is the generator. Run it after changing the
contract:

    uv run python sql/_shared/generate_schema.py

The output is committed. That is deliberate: a generated file that is not committed
is a file nobody can review, and DDL is exactly the thing a reviewer should look
at.

## What the schema has to be, and why

InfluxDB 3 constrains this heavily, and the constraints were all measured rather
than assumed (see ``docs/LEARNING-LOG.md``):

* **One table per process stage.** ``aeration``, ``effluent``, ``sludge`` and so
  on. So ``SELECT mean(value) FROM aeration`` is one scan over one part of the
  plant, rather than a UNION of fourteen tables.
* **Exactly the same six tag columns in every table**: ``area``, ``equipment``,
  ``signal``, ``eu``, ``site``, ``source``. InfluxDB 3 fixes a table's tag set on
  its first write, so a signal that omitted one would be *rejected*, not nulled.
  This is why they are all present on every row without exception.
* **Exactly two fields**: ``value`` and ``quality``, because InfluxDB 3 rejects a
  third. A failed sensor is stored as ``value`` NULL with ``quality`` 2 — present
  and invalid, which is the representation the project has argued for since the
  beginning and the only one the database will accept.
* **The tag is called ``signal``, not ``field``**, because ``field`` is a reserved
  word in InfluxQL and would have to be quoted in every lesson in the course.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from softplc.contract import contract  # noqa: E402
from storage.influx.line_protocol import TAG_KEYS  # noqa: E402

RAW_BUCKET = "wwtp"
ROLLUP_BUCKETS = {"1m": "bucket_1m", "1h": "bucket_1h"}

HEADER = """\
-- ============================================================================
-- wwtp-iiot — generated schema. DO NOT EDIT BY HAND.
--
--   uv run python sql/_shared/generate_schema.py
--
-- Generated from contracts/tags.yaml so that the course cannot drift from the
-- data. Every constraint below was measured against a real InfluxDB 3 instance;
-- see docs/LEARNING-LOG.md for the errors that established them.
-- ============================================================================
"""


def main() -> int:
    c = contract()
    out: list[str] = [HEADER]

    tables: dict[str, list[str]] = {}
    for sig in c.signals.values():
        tables.setdefault(sig.measurement, []).append(sig.id)

    out.append(f"\n-- Raw tier: 1 s, {len(c.signals)} signals across "
               f"{len(tables)} process stages.\n")
    for stage in sorted(tables):
        ids = sorted(tables[stage])
        out.append(f"-- {stage}: {len(ids)} signal(s)")
        out.append("CREATE TABLE IF NOT EXISTS "
                   f"\"{RAW_BUCKET}\".\"{stage}\" (")
        out.append('  "time" TIMESTAMP NOT NULL,')
        # Tags. Same six everywhere, in the encoder's order, because InfluxDB 3
        # will not accept a table whose tag set differs from its first write.
        out.append('  "area" TEXT NOT NULL,')
        out.append('  "equipment" TEXT NOT NULL,')
        out.append('  "signal" TEXT NOT NULL,')
        out.append('  "eu" TEXT NOT NULL,')
        out.append('  "site" TEXT NOT NULL,')
        out.append('  "source" TEXT NOT NULL,')
        # Fields. A measurement and its validity, and nothing else.
        out.append('  "value" DOUBLE,')
        out.append('  "quality" BIGINT NOT NULL,')
        out.append("  PRIMARY KEY (\"time\", \"area\", \"equipment\", "
                   "\"signal\", \"eu\", \"site\", \"source\"),")
        out.append(");")
        out.append("")

    out.append(f"""
-- ---------------------------------------------------------------------------
-- Rollup tiers
--
-- A rollup point is not a measurement: it has no `value`, it has `mean` and
-- `count`. Keeping them apart matters — a query that averaged `value` across
-- both tiers would be averaging a reading with a summary of readings, and would
-- return a number that means nothing.
--
-- `count` is the field that makes the tiers composable, and it is what makes the
-- weighted mean correct. Averaging the averages is wrong the moment two windows
-- hold different numbers of points, and they always do: the deadband filtered
-- some of them, one contains an outage, and the last window of a run is short.
-- The error is largest exactly when the data is most interesting.
--
-- Only two fields, for the same reason the raw tier has two.
-- ---------------------------------------------------------------------------
""")
    for tier, bucket in ROLLUP_BUCKETS.items():
        interval = {"1m": "1 minute", "1h": "1 hour"}[tier]
        for stage in sorted(tables):
            out.append(f"CREATE TABLE IF NOT EXISTS \"{bucket}\".\"{stage}_{tier}\" (")
            out.append('  "time" TIMESTAMP NOT NULL,')
            out.append('  "area" TEXT NOT NULL,')
            out.append('  "equipment" TEXT NOT NULL,')
            out.append('  "signal" TEXT NOT NULL,')
            out.append('  "eu" TEXT NOT NULL,')
            out.append('  "site" TEXT NOT NULL,')
            out.append('  "source" TEXT NOT NULL,')
            out.append('  "mean" DOUBLE,')
            out.append('  "count" BIGINT NOT NULL,')
            out.append("  PRIMARY KEY (\"time\", \"area\", \"equipment\", "
                       "\"signal\", \"eu\", \"site\", \"source\"),")
            out.append(");")
            out.append("")
        out.append(f"-- {tier} buckets hold time_bucket(INTERVAL '{interval}', time)")
        out.append("")

    out.append(f"""\
-- ---------------------------------------------------------------------------
-- Reading notes that the course keeps repeating
--
-- * ORDER BY accepts `time` and nothing else. `ORDER BY signal` is a parse
--   error that says "invalid ORDER BY, expected TIME column".
-- * `field` is a reserved word. So is `key`, and `AS key` is a parse error.
-- * A timestamp filter needs RFC 3339: `time >= '2026-09-26T00:00:00Z'`.
--   `time >= '1758844800000'` is rejected as "not a valid timestamp".
-- * `count()` is not available. Use `COUNT(*)`.
-- ---------------------------------------------------------------------------
""")

    target = Path(__file__).parent / "schema.sql"
    target.write_text("\n".join(out))
    print(f"wrote {target} — {len(tables)} raw tables, "
          f"{len(tables) * len(ROLLUP_BUCKETS)} rollup tables, "
          f"{len(c.signals)} signals, {len(TAG_KEYS)} tag columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
