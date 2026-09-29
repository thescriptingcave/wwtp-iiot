"""Where the notebooks read from, and the one seed they all describe.

    from notebooks._data import connect, dsn

## Why a database of their own

The notebooks state numbers — `13 signals`, `29.36 %` — and a gate checks that
each one is what a live run prints. That only works if the data is **the same
data every time**, and the plant's main database cannot promise that:

* `wwtp` is seeded relative to *now*, so every re-seed slides every timestamp.
  The SQL course depends on exactly that (`now() - interval '6 hours'` appears in
  28 places), so it cannot be pinned without breaking the course.
* a running gateway writes into it.

So the notebooks read `wwtp_notebooks`, seeded once at a fixed instant with a
fixed storm, by `python -m tools.notebook_data`. Re-seeding it reproduces the same
rows, timestamps included; the plant model does not read the clock and the anchor
is now an argument. Two audiences, two databases, and neither can move the other.

## Why the notebook chooses its own database, not the environment

`POSTGRES_DB` is `wwtp` in `.env`, and `make` sources `.env` *after* the inherited
environment, so a recipe cannot select another database by exporting one. More to
the point, a notebook opened in an editor or a bare `jupyter` never saw the
Makefile at all. Putting the database name here means "which data does this
notebook describe" is answered by the notebook and not by the shell it happened to
start from. Host, port and credentials still come from the environment, because
those genuinely differ between a laptop and CI.
"""

from __future__ import annotations

import os
from typing import Any

from storage.postgres.schema import dsn as _base_dsn

#: The database the notebooks describe. Override with NOTEBOOK_DB only to point at
#: a copy of it — the numbers in the prose are for this seed.
NOTEBOOK_DB = os.environ.get("NOTEBOOK_DB", "wwtp_notebooks")

#: The instant the seeded window ends at: midnight UTC, so "hour of day" and
#: "hours since the start" are the same thing for these notebooks.
SEED_END = "2026-09-29T00:00:00Z"

#: Length of the window, in days.
SEED_DAYS = 7

#: Hours before the end that the storm is armed. Thirty-six leaves a day and a half
#: of after, so a recovery is visible, and a week less a day and a half of before.
STORM_AFTER_H = 36


#: What the seed above produces, so a notebook can refuse to run on other data.
#:
#: Two independent seeds were compared row by row (md5 over signal, timestamp,
#: value and quality of all 4 239 284 rows) and were identical, so these numbers
#: identify the database and not one particular run of it. A changed seed argument,
#: a different plant model or a stray gateway write moves at least one of them, and
#: every number in the notebooks' prose is then about data that is no longer there.
#:
#: **Integer aggregates only.** The first version used `round(sum(value), 3)` and
#: failed against the very database it was taken from: 5625358740.237 on one
#: query, .221 on the next. Float `sum` is not order-independent and parallel
#: workers add in whatever order they finish — the effect `sql/01-beginner/01-03`
#: teaches, met here by the check that was supposed to prove reproducibility. So
#: each value is rounded to a thousandth *first* and the integers are summed.
EXPECTED = {"rows": 4_239_284, "value_milli_sum": 5_625_358_748_306,
            "quality_sum": 38, "epoch_sum": 7_589_771_859_816_991}

FINGERPRINT_SQL = (
    "SELECT count(*), sum(round(value * 1000)::bigint), sum(quality::bigint), "
    "sum(extract(epoch FROM ts)::bigint) FROM reading"
)

#: The storm's window, derived from the seed rather than typed a second time.
STORM_HOURS = 2


def storm_window() -> tuple[str, str]:
    """`(start, end)` of the seeded storm, as ISO-8601 UTC strings."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    end = datetime.fromisoformat(SEED_END.replace("Z", "+00:00")).astimezone(UTC)
    start = end - timedelta(hours=STORM_AFTER_H)
    return start.isoformat(), (start + timedelta(hours=STORM_HOURS)).isoformat()


#: Which signal each injected instrument fault is applied to. The fault names are the
#: seeder's (`storage.seed.main.DEFAULT_INSTRUMENT_FAULTS`); the signals are the ones
#: named in `contracts/fault-scenarios.yaml`.
FAULT_TARGET = {
    "do_sensor_drift": "AERATION:AHU-1:DO",
    "effluent_tss_stuck": "EFFLUENT:FLOW:TSS",
    "sensor_dead": "INFLUENT:LIFT:CURRENT",
}


def known_events() -> dict[str, tuple[str, Any, Any]]:
    """`{event: (signal, start, end)}` for everything the seed injects.

    The one place the ground truth is derived, so the notebooks that score detectors
    and date change points cannot disagree about when something happened. Times are
    tz-aware `pandas.Timestamp`s, floored to the minute because the rollup is.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415
    from storage.seed.main import DEFAULT_INSTRUMENT_FAULTS  # noqa: PLC0415

    week_start = datetime.fromisoformat(SEED_END.replace("Z", "+00:00"))
    week_start = week_start.astimezone(UTC) - timedelta(days=SEED_DAYS)
    events: dict[str, tuple[str, Any, Any]] = {}
    for spec in DEFAULT_INSTRUMENT_FAULTS:
        offset = spec["at_fraction"] * SEED_DAYS * 86400
        start = pd.Timestamp(week_start + timedelta(seconds=offset))
        end = start + pd.Timedelta(seconds=spec["duration_s"])
        fault = spec["fault"]
        events[fault] = (FAULT_TARGET[fault], start.floor("1min"), end.floor("1min"))
    storm_from, storm_to = (pd.Timestamp(t) for t in storm_window())
    events["wet_weather_storm"] = ("INFLUENT:LIFT:FLOW", storm_from, storm_to)
    return events


def dsn() -> str:
    """`storage.postgres.schema.dsn()` with the database swapped for ours."""
    parts = dict(p.split("=", 1) for p in _base_dsn().split(" ") if "=" in p)
    parts["dbname"] = NOTEBOOK_DB
    return " ".join(f"{k}={v}" for k, v in parts.items())


def connect(**kwargs: Any) -> Any:
    """A psycopg connection to the notebook database."""
    import psycopg  # noqa: PLC0415

    return psycopg.connect(dsn(), **kwargs)
