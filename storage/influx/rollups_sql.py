"""The rollup SQL, and the shape of what it produces.

Split out from ``storage/rollup_worker/rollup.py`` because these are two
different kinds of thing: that module decides *when* to write, this one says
*what*. Keeping them apart means the timing policy is testable without a
database and the SQL is readable in one place — which matters, because these
queries are also the source material for the advanced stages of ``sql/``.

## Reading a rollup

A rollup point is not a measurement. It has no ``value`` column; it has ``mean``
and ``count``. That is a deliberate difference from the raw tier, and it is why the
two live in different tables: a query that averaged ``value`` across both tiers
would be averaging a reading with a summary of readings, and would return a number
that means nothing.

Only two fields, for the same reason the raw tier has two: InfluxDB 3 caps a point
at two fields. ``min`` and ``max`` were in the first draft and had to go; they are
read from the tier below instead, which is exact.

``count`` is the field that makes the tier composable. It is also what makes the
next rollup correct — see :func:`weighted_mean_sql`.

## The weighted mean, one more time, because it is the bug

```sql
SUM("mean" * "count") / NULLIF(SUM("count"), 0)
```

not ``AVG("mean")``. Two minutes always hold different numbers of points: one
was filtered by the deadband, one contains an outage, one is the last window of
a process run. The error from averaging the averages is proportional to the
spread in counts, so it is largest exactly when the data is most interesting —
during an event, which is when somebody is looking.
"""

from __future__ import annotations

from typing import Any, Final

from storage.influx.line_protocol import MAX_FIELDS_PER_POINT

#: The tag columns every rollup point carries, in the fixed order the line
#: protocol encoder expects. Same six as the raw tier, including ``source`` set to
#: ``rollup``, so a query that unions the two tables by tag does not have to know
#: which is which.
ROLLUP_TAGS: Final = ("area", "equipment", "signal", "eu", "site", "source")

#: The field columns a rollup point has instead of ``value``.
#:
#: Exactly two, because InfluxDB 3 rejects a third — measured, not assumed; see
#: ``storage/influx/line_protocol.py``. The first draft of this file had four
#: (mean, min, max, count) and would have failed every write.
#:
#: ``mean`` and ``count`` are the two that must be stored. ``min`` and ``max`` are
#: *derivable* from the tier below, and a query that needs them can read the finer
#: tier: the minute table answers "what was the hourly minimum" exactly, and
#: answering it from stored columns would mean storing the same information twice
#: and trusting both copies to agree. The trade is real and it is named: an
#: hourly-min query scans more rows than it would if the column existed.
ROLLUP_FIELDS: Final = ("mean", "count")

# Asserted at import, not per point. A rollup that tried to store three aggregates
# would fail at *runtime*, in a worker, against a database, with an error reading
# "found trailing content" that looks like a typo in somebody's line protocol.
# Cheap to check once and impossible to forget.
if len(ROLLUP_FIELDS) > MAX_FIELDS_PER_POINT:  # pragma: no cover - import guard
    raise AssertionError(
        f"ROLLUP_FIELDS has {len(ROLLUP_FIELDS)} columns but InfluxDB 3 allows "
        f"only {MAX_FIELDS_PER_POINT} fields per point"
    )

#: Source bucket per tier. ``None`` means the raw tier.
TIER_SOURCE: Final[dict[str, str | None]] = {"1m": None, "1h": "1m"}

#: Bucket name per tier.
TIER_BUCKET: Final[dict[str, str]] = {"1m": "bucket_1m", "1h": "bucket_1h"}


def interval_for(tier: str) -> str:
    """SQL interval literal for a tier.

    Derived from the name rather than stored separately, so a tier cannot end up
    with an interval that disagrees with its own name — which is the kind of
    inconsistency that produces a bucket named ``1h`` containing five-minute
    points, and a chart that is wrong in a way nobody can explain.
    """
    if tier not in TIER_BUCKET:
        raise KeyError(f"unknown tier {tier!r}; expected one of "
                       f"{', '.join(sorted(TIER_BUCKET))}")
    return {"1m": "1 minute", "1h": "1 hour"}[tier]


#: Raw → 1 minute. The weighted mean is the whole point of this query.
WEIGHTED_MEAN_1M: Final = """
SELECT
  time_bucket(INTERVAL '1 minute', time)                       AS time,
  time_bucket(INTERVAL '1 minute', time)                       AS _start,
  time_bucket(INTERVAL '1 minute', time) + INTERVAL '1 minute' AS _stop,
  'mean'                                                        AS _field,
  _measurement, area, equipment, field, eu, site,
  SUM("mean" * "count") / NULLIF(SUM("count"), 0)               AS "mean",
  SUM("count")                                                  AS "count"
FROM raw
WHERE time >= $start AND time < $end
GROUP BY time_bucket(INTERVAL '1 minute', time),
         _measurement, area, equipment, field, eu, site
ORDER BY time
"""

#: 1 minute → 1 hour. Identical shape, one tier up. The ``count`` that comes out
#: is the sum of the counts that went in, which is what makes the next hour's
#: mean correct rather than merely plausible.
WEIGHTED_MEAN_1H: Final = """
SELECT
  time_bucket(INTERVAL '1 hour', time)                         AS time,
  time_bucket(INTERVAL '1 hour', time)                         AS _start,
  time_bucket(INTERVAL '1 hour', time) + INTERVAL '1 hour'     AS _stop,
  'mean'                                                        AS _field,
  _measurement, area, equipment, field, eu, site,
  SUM("mean" * "count") / NULLIF(SUM("count"), 0)               AS "mean",
  SUM("count")                                                  AS "count"
FROM bucket_1m
WHERE time >= $start AND time < $end
GROUP BY time_bucket(INTERVAL '1 hour', time),
         _measurement, area, equipment, field, eu, site
ORDER BY time
"""

#: Drop a window before rewriting it.
#:
#: This is what makes a rollup *idempotent*. Without it, a retry after a partial
#: failure appends a second copy of the same window, and the only symptom is a
#: trend that is slightly too high on the days the worker restarted — the kind
#: of error that is invisible for months and then becomes somebody's job.
DELETE_WINDOW: Final = """
DELETE FROM {bucket}
WHERE time >= $start AND time < $end
"""


def statements_for(tier: str, start: int, end: int) -> list[tuple[str, dict]]:
    """The statements that fold one window of one tier.

    Delete first, then insert. A single ``INSERT`` that overwrote on a key
    conflict would be tidier, but relying on the conflict behaviour means relying
    on it *not* changing; an explicit delete is boring and stays correct.
    """
    if tier not in TIER_BUCKET:
        raise KeyError(f"unknown tier {tier!r}")
    bucket = TIER_BUCKET[tier]
    params = {"start": start, "end": end}
    sql = WEIGHTED_MEAN_1M if tier == "1m" else WEIGHTED_MEAN_1H
    return [
        (DELETE_WINDOW.format(bucket=bucket), params),
        (sql, params),
    ]


def rollup_line(measurement: str, tags: dict[str, str], start_ms: int,
                values: dict[str, Any]) -> str:
    """Encode one rollup row as a line-protocol point.

    ``count`` is written as an integer (``123i``) and the statistics as floats.
    Getting that backwards turns a count into a float column, and a later ``SUM``
    over it either errors or coerces — and a coerced count makes every weighted
    mean built on top of it wrong.
    """
    from storage.influx.line_protocol import (
        MAX_FIELDS_PER_POINT,
        escape_tag,
        format_value,
    )

    missing = [t for t in ROLLUP_TAGS if t not in tags]
    if missing:
        raise KeyError(f"rollup point is missing tags: {', '.join(missing)}")

    tag_part = ",".join(f"{t}={escape_tag(tags[t])}" for t in ROLLUP_TAGS)
    parts = []
    for name in ROLLUP_FIELDS:
        if name not in values:
            continue
        raw = values[name]
        if raw is None:
            continue
        parts.append(f"{name}={int(raw)}i" if name == "count"
                     else f"{name}={format_value(float(raw))}")
    if not parts:
        raise ValueError("a rollup point with no fields is not a point")
    # No per-call field-count check, because ROLLUP_FIELDS whitelists the keys and
    # that invariant is asserted once at import. A check here would be unreachable.
    return f"{escape_tag(measurement)},{tag_part} {','.join(parts)} {start_ms}"
