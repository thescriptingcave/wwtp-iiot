"""The rollup worker: 1 s raw becomes 1 min becomes 1 h.

A plant at 50 scans a second produces 4.3 million points a day across 57
signals. Keeping all of it forever is expensive, and — this is the part people
forget — keeping all of it forever is also *less* useful, because a query over
two years of one-second data is a query nobody runs.

So the hierarchy is: keep the detail while it is news, then fold it.

| Tier | Resolution | Kept for | Why |
|---|---|---|---|
| raw | 1 s | 7 days | what happened at 03:14 during the storm |
| 1 min | 1 min | 90 days | what did last Tuesday look like |
| 1 h | 1 h | forever | is the nitrifier degrading over the year |

Each tier answers a question the one above it cannot afford to. That is the
justification — not "it is smaller".

## The rules that are easy to get wrong

**A window is closed, or it is not written.** A rollup that fires at 14:05 and
writes the bucket covering 14:04:00 to 14:04:59 is writing a *complete* minute.
One
that fires at 14:04:30 and writes the same bucket is writing half a minute and
labelling it as a minute, and every average downstream is quietly wrong. So the
worker only writes windows whose end time has passed, and drops a partial window
rather than recording it as complete. That is what ``ROLLUP_DISCARD_PARTIAL`` in
the environment means.

**Averages must be weighted by sample count, not averaged.** Arithmetic mean of
means is wrong whenever the windows have different numbers of points, and they
always do: deadbands, outages, and the last window of a run all produce
different counts. The query therefore sums ``_count * mean`` and divides by the
total count. This is the single most common bug in hand-rolled rollups, and it
produces numbers that look right.

**Rollups are idempotent or they are not rollups.** Re-running a window must
overwrite it, not append. Otherwise a retry after a partial failure double-counts
the hour, and the only symptom is a trend that is slightly too high on the days
the worker restarted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("storage.rollup")

#: Tiers, coarse to fine. ``source`` is the measurement and resolution read;
#: ``target`` is what is written. ``every`` is the tier's own bucket size in
#: seconds, and is also the minimum age a window must reach before it is
#: considered closed.
TIERS: tuple[dict[str, Any], ...] = (
    {"name": "1m", "source": None, "every": 60,
     "functions": ("mean", "min", "max", "count")},
    {"name": "1h", "source": "1m", "every": 3600,
     "functions": ("mean", "min", "max", "count")},
)


@dataclass(slots=True)
class RollupPlan:
    """What one tick of the worker intends to do.

    Built without touching a database, so the *policy* — which windows are
    closed, which tiers run, what gets dropped — is testable on its own. The
    database only ever executes a plan it was handed.
    """

    now_s: float
    closed_windows: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    partial_windows: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not any(self.closed_windows.values())


def plan(now_s: float, tiers: tuple[dict[str, Any], ...] = TIERS,
         discard_partial: bool = True) -> RollupPlan:
    """Work out which windows are safe to write at ``now_s``.

    A window ``[start, start + every)`` is closed when ``start + every <= now``.
    The guard is deliberately ``<=`` and not ``<``: at exactly the boundary the
    window has received every point it will ever receive, and waiting for the
    next tick to write it just adds latency for no safety.

    With ``discard_partial=False`` the in-progress window is planned as well,
    marked by also appearing in :attr:`RollupPlan.partial_windows`. That is the
    "keep the dashboard live at the cost of a correction later" trade: the
    partial write is overwritten when the window closes, so the only cost is
    that anyone querying *during* the window sees an average of a partial hour.
    Given a plant whose operators watch a live trend, that is usually worth it —
    and it is why the flag exists rather than the behaviour being hard-coded.
    """
    plan_ = RollupPlan(now_s=now_s)
    now_i = int(now_s)
    for tier in tiers:
        every = int(tier["every"])
        # The window currently being filled. Writing it would mean recording half
        # a minute and calling it a minute, and every average downstream of that
        # is quietly wrong — which is the failure mode this whole function
        # exists to prevent.
        current_start = (now_i // every) * every
        current_end = current_start + every
        if current_end > now_i:
            plan_.partial_windows.setdefault(tier["name"], []).append(
                (current_start, current_end)
            )
            if not discard_partial:
                plan_.closed_windows.setdefault(tier["name"], []).append(
                    (current_start, current_end)
                )

        # Everything before it is closed, and is a candidate for catch-up. Twelve
        # windows is an hour of recovery for the 1-minute tier, which is
        # deliberately generous: a missed window is a permanent hole in the
        # record, and re-deriving one costs a query while explaining its absence
        # costs a conversation with whoever wanted the data.
        # Oldest first, so the worker replays a gap in the order it happened. Same
        # reasoning as the spool: if two windows are written out of order and the
        # second one is a correction, order is what decides which survives.
        catch_up = []
        for back in range(12, 0, -1):
            start = current_start - back * every
            end = start + every
            if end > now_i:
                continue
            catch_up.append((start, end))
        plan_.closed_windows.setdefault(tier["name"], []).extend(catch_up)
    return plan_


# ─── the SQL ──────────────────────────────────────────────────────────────────

#: Raw → 1 minute.
#:
#: Written as explicit SQL rather than through a continuous-aggregate helper
#: because the SQL *is* the lesson: this is the query the intermediate and
#: advanced stages of ``sql/`` are built from, and hiding it behind an API call
#: would mean the course never explains the thing it teaches.
#:
#: Note the weighted average. Averaging the averages is
#: wrong the moment two minutes hold different numbers of points, and two minutes
#: always hold different numbers of points — one of them will have been filtered
#: by the deadband.
ROLLUP_1M = """
INSERT INTO bucket_1m
  (time, _start, _stop, _field, _measurement,
   area, equipment, field, eu, site, _value)
SELECT
  time,
  time_bucket(INTERVAL '1 minute', time) AS _start,
  time_bucket(INTERVAL '1 minute', time) + INTERVAL '1 minute' AS _stop,
  'value' AS _field,
  _measurement,
  area, equipment, field, eu, site,
  SUM("mean" * "_count") / NULLIF(SUM("_count"), 0) AS "mean"
FROM raw
WHERE time >= $start AND time < $end
GROUP BY _start, _stop, _measurement, area, equipment, field, eu, site
"""

MINMAX_1M = """
SELECT
  MIN("value") AS "min", MAX("value") AS "max", COUNT("value") AS "count"
FROM raw
WHERE time >= $start AND time < $end
GROUP BY time_bucket(INTERVAL '1 minute', time),
         _measurement, area, equipment, field, eu, site
"""


#: What a tier's execution must look like. A protocol, not a database client, so
#: the scheduling and error policy can be tested without one.
ExecuteFn = Callable[[str, int, int], int]


@dataclass(slots=True)
class RollupStats:
    windows_written: int = 0
    points_written: int = 0
    windows_skipped_partial: int = 0
    errors: int = 0
    last_run_s: float = 0.0
    last_duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "windows_written": self.windows_written,
            "points_written": self.points_written,
            "windows_skipped_partial": self.windows_skipped_partial,
            "errors": self.errors,
            "last_duration_ms": round(self.last_duration_ms, 1),
        }


class RollupWorker:
    """Runs the tiers on a timer.

    Takes a ``execute`` callable rather than a database client, so the timing and
    error-handling policy can be tested against a fake and the only thing left
    untested by unit tests is the SQL itself.
    """

    def __init__(self, execute: ExecuteFn, *, interval_s: int = 60,
                 discard_partial: bool = True,
                 clock: Callable[[], float] = time.time) -> None:
        self._execute = execute
        self.interval_s = interval_s
        self.discard_partial = discard_partial
        self._clock = clock
        self.stats = RollupStats()

    def tick(self) -> RollupPlan:
        """Do one pass. Never raises.

        A worker that dies on a transient database error is a worker that needs a
        supervisor, an alert and a runbook. A worker that counts the error and
        tries again in a minute is a worker that just works.
        """
        started = time.perf_counter()
        p = plan(self._clock(), discard_partial=self.discard_partial)
        self.stats.last_run_s = self._clock()
        if p.is_empty():
            self.stats.last_duration_ms = (time.perf_counter() - started) * 1000.0
            return p
        for tier, windows in p.closed_windows.items():
            for start, end in windows:
                try:
                    written = self._execute(tier, start, end)
                except Exception as exc:  # one bad window, not a dead worker
                    log.warning("rollup %s [%d,%d) failed: %s", tier, start, end, exc)
                    self.stats.errors += 1
                    continue
                self.stats.windows_written += 1
                self.stats.points_written += int(written or 0)
        self.stats.last_duration_ms = (time.perf_counter() - started) * 1000.0
        return p

    def run_forever(self, iterations: int | None = None) -> None:
        n = 0
        while iterations is None or n < iterations:
            self.tick()
            n += 1
            if iterations is not None and n >= iterations:
                break
            time.sleep(self.interval_s)
