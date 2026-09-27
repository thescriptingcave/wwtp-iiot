"""Reading windows out of the hypertable.

The one place the alarm engine touches a database, and deliberately thin: a
`SELECT` per signal, a `Window` returned. Everything upstream of it is a pure
function (`detectors.py`) and everything downstream is a protocol (`EventSink`),
so this is the only code that would need rewriting for a different store.

## The query, and why it is shaped this way

    SELECT ts, value, quality
    FROM reading
    WHERE signal_id = %s AND ts >= %s
    ORDER BY ts

Three things worth stating, all of them from having got them wrong elsewhere:

**`ORDER BY ts` is explicit even though the primary key starts with it.** A
`trend` rule fits a least-squares line and gets a different answer from a
different row order, and a plan that satisfies the filter by an index scan and
returns rows in physical order is a real thing that happens after a restore.
`docs/LEARNING-LOG.md` has the longer version of this argument.

**The time bound comes from the rule, not from `now()`.** A rule asking for two
hours of history in a database whose newest reading is four hours old should get
the two hours it asked for and a window that says so — which is what
`Window.span_s` and `has_coverage` are for. Anchoring to `now()` would silently
hand it two hours of *nothing* and the silence detectors would be the only ones
that noticed.

**A window is a time span, and the response is a point count.** The store returns
the span it covered alongside the samples, so a caller can tell "no data" from
"data that happens to be normal" without a second query. That distinction is the
subject of half of `sql/02-04`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from alarms.base import AlarmRule, Sample, Window

if TYPE_CHECKING:  # pragma: no cover
    from typing import Protocol

    class Conn(Protocol):
        """The slice of a psycopg connection the alarm store uses."""

        def cursor(self) -> Any: ...
        def commit(self) -> None: ...


log = logging.getLogger("alarms.store")

#: Columns, in the order `Sample` wants them.
_SELECT = (
    "SELECT ts, value, quality FROM reading "
    "WHERE signal_id = %s AND ts >= %s ORDER BY ts"
)

#: Never ask for more than this, whatever a rule claims. A rule with a runaway
#: lookback should be slow, not a memory exhaustion bug in the alarm engine.
MAX_LOOKBACK_S = 24 * 3600.0


def rows_to_window(
    signal_id: str,
    rows: Sequence[tuple[Any, ...]],
    now: float,
) -> Window:
    """Turn result rows into a window. The whole hydration layer, in one function.

    Kept separate from the query so it can be tested with hand-built rows and no
    database — which is the only way to test the `None`-handling, and the `None`
    handling is the interesting part: a `NULL` value is a *Bad reading*, not a
    missing row, and conflating the two is the single most consequential mistake
    available in this whole package.
    """
    return Window(
        signal_id=signal_id,
        points=tuple(
            Sample(ts=row[0].timestamp(), value=row[1], quality=row[2])
            for row in rows
        ),
        now=now,
    )


class PostgresWindowSource:
    """Windows for a set of rules, from a live connection.

    Grouped by signal so a tick costs one query per *watched signal* rather than
    one per rule. Thirteen rules over nine signals is already 1.4:1 and the ratio
    only gets worse as rules are added.
    """

    def __init__(self, conn: Conn, rule_set: Sequence[AlarmRule]) -> None:
        self.conn = conn
        self.rules = tuple(rule_set)
        self.signals = sorted({r.signal_id for r in self.rules})
        #: The longest lookback any rule on a signal needs, keyed by signal.
        self.lookback: dict[str, float] = {}
        for rule in self.rules:
            want = min(rule.lookback_s(), MAX_LOOKBACK_S)
            self.lookback[rule.signal_id] = max(
                self.lookback.get(rule.signal_id, 0.0), want
            )

    def windows(self, now: float) -> dict[str, Window]:
        """One window per watched signal, as of ``now``."""
        out: dict[str, Window] = {}
        with self.conn.cursor() as cur:
            for signal_id in self.signals:
                cur.execute(_SELECT, (signal_id, now - self.lookback[signal_id]))
                out[signal_id] = rows_to_window(signal_id, cur.fetchall(), now)
        return out

    def coverage(self, now: float) -> dict[str, float]:
        """Seconds since each watched signal last reported anything.

        Separate from `windows()` on purpose: this is the number an operator
        wants first ("is the plant talking to me at all?") and it is one cheap
        query over the whole plant rather than one per signal. A signal that has
        not spoken is a finding in its own right, and burying it inside a
        per-signal window makes it invisible.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT signal_id, max(ts) FROM reading "
                "WHERE ts >= %s GROUP BY signal_id",
                (now - MAX_LOOKBACK_S,),
            )
            seen = {sid: row[1].timestamp() for sid, row in cur.fetchall()}
        return {
            sid: (now - seen[sid]) if sid in seen else float("inf")
            for sid in self.signals
        }


class PostgresEventSink:
    """Writes events to the `event` table.

    The table was designed before the alarm engine existed — `kind`, `severity`
    and the two foreign keys were there for exactly this — and the `CHECK` on
    `severity` is why the engine is allowed to pass a string around instead of
    inventing a numeric scale nobody would remember the order of.
    """

    def __init__(self, conn: Conn) -> None:
        self.conn = conn
        self.written = 0

    def emit(self, *, kind: str, severity: str, message: str,
             signal_id: str | None = None, equipment_id: str | None = None,
             detail: dict[str, Any] | None = None) -> None:
        from storage.postgres.schema import _json

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event (ts, kind, severity, message,
                                   signal_id, equipment_id, detail)
                VALUES (now(), %s, %s, %s, %s, %s, %s)
                """,
                (kind, severity, message, signal_id, equipment_id,
                 _json(detail or {})),
            )
        self.written += 1
