"""Where the workshop reads from, and the checks that keep a run honest.

    from workshops.ml._data import load_panel

## Why a module, and not a path in the notebook

Notebook execution happens with the *process's* working directory, not the
notebook's own, so a notebook that opens `dataset.csv` works when a human starts
Jupyter in the right folder and fails in CI, in `make workshop-notebooks`, and on
anyone's machine but the author's. The analyst track solved this once already with
`notebooks/_data.py`; this is the same answer in a smaller room.

`WORKSHOP_PANEL` overrides the location for a participant who downloaded the CSV
somewhere else. The default is relative to *this file*, so it is correct wherever
the repository is checked out.

## What the loaders assert, and why here rather than in the notebook

**The label columns are not a feature.** `FEATURES` is the list a model is given,
and it is defined here so that a notebook cannot quietly add `is_fault` and then
report a perfect score. Notebook 05's whole argument is that this is the easiest
mistake in the workshop to make and the hardest to notice, because the number that
comes out is *too good*.

**The panel is dense.** If it is not, the classifier has no positive examples for
two thirds of the fault classes and everything downstream is a measurement of the
sample rather than of the plant. This is checked on load rather than discovered in
notebook 02, because a participant who hits it here learns nothing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

#: The emitted panel. Relative to this file so it survives being run from anywhere.
#:
#: An **empty** `WORKSHOP_PANEL` falls back to the default rather than becoming
#: `Path("")`, which is `.` — and opening `.` as a CSV raises `IsADirectoryError`
#: from three frames inside pandas, naming neither the variable nor the file. An
#: unset variable and an empty one mean the same thing to a person, so they mean the
#: same thing here. (`WORKSHOP_PANEL= make workshop-notebooks` is an easy accident
#: when a shell expansion comes out empty, which is exactly how this was found.)
PANEL = Path(
    os.environ.get("WORKSHOP_PANEL") or Path(__file__).parent / "dataset.csv"
)

#: What a model is given. No label, no `week`, no `bucket`.
#:
#: A **list, not a tuple**, because `frame[FEATURES]` is how everyone indexes with
#: it and pandas rejects a tuple there with a `KeyError` naming the whole tuple — so
#: the error says "mean, min, max, ..." rather than "you passed the wrong type". The
#: immutability a tuple would buy is worth less than an error message that points at
#: itself.
#:
#: `bucket` is excluded on purpose. The faults run on a fixed 36-hour schedule, so
#: an absolute timestamp is a proxy for the label: a tree can learn "around 12:00 on
#: a Tuesday" instead of learning the symptom, and will score beautifully on a split
#: where the schedule repeats and terribly on one where it does not. That is
#: textbook target-adjacent leakage, and it is invisible unless you look.
FEATURES: list[str] = [
    "mean",
    "min",
    "max",
    "n",
    "row_written",
    "value_is_null",
    "base_24",
    "n_over_base",
    "missing_streak",
]

#: Everything a model must never see.
LABELS: tuple[str, ...] = ("is_fault", "fault_type", "is_storm", "week")

#: The three row states, which a model can tell apart and a person usually cannot.
#: `effluent_tss_stuck` and `sensor_dead` share a state, which is the point.
ROW_STATES = {
    "a value": ("row_written == 1", "value_is_null == 0"),
    "sensor_dead": ("row_written == 0", "value_is_null == 1"),
    "effluent_tss_stuck": ("row_written == 0", "value_is_null == 1"),
}


def load_panel() -> pd.DataFrame:
    """The panel, with the two structural checks done.

    Both refusals are loud, and both are cases where continuing produces numbers
    that look fine and mean nothing.
    """
    import pandas as pd  # noqa: PLC0415

    if not PANEL.exists():
        raise SystemExit(
            f"no panel at {PANEL}\n"
            f"  build it:  make workshop-seed WORKSHOP_DSN=... && "
            f"make workshop-dataset WORKSHOP_DSN=...\n"
            f"  or set WORKSHOP_PANEL to a copy you already have."
        )
    panel = pd.read_csv(PANEL, parse_dates=["bucket"])

    leaked = sorted(set(LABELS) & set(FEATURES))
    if leaked:
        raise SystemExit(f"FEATURES contains label columns: {leaked}")

    missing = [c for c in (*FEATURES, *LABELS, "signal_id", "bucket")
               if c not in panel.columns]
    if missing:
        raise SystemExit(f"the panel is missing {missing}; rebuild it")

    absent = float((panel["row_written"] == 0).mean())
    if absent < 0.05:
        raise SystemExit(
            f"only {absent:.1%} of hours have no row, so this panel was not built "
            f"densely. Querying the hourly table gives a modelling set with no "
            f"positive examples for two faults -- see workshops/ml/README.md."
        )
    return panel.sort_values(["signal_id", "bucket"], ignore_index=True)


def summarise(panel: pd.DataFrame) -> str:
    """The six numbers to read before fitting anything, as one block of text."""
    faults = panel[panel["is_fault"] == 1]
    return "\n".join([
        f"{len(panel):,} rows = {panel['signal_id'].nunique()} signals "
        f"x {panel['bucket'].nunique():,} hours",
        f"  positives       {len(faults)} ({panel['is_fault'].mean():.4%}); "
        f"majority-class score {1 - panel['is_fault'].mean():.4f}",
        f"  by fault        {faults['fault_type'].value_counts().to_dict()}",
        f"  storm hours     {int(panel['is_storm'].sum()):,}",
        f"  no row written  {int((panel['row_written'] == 0).sum()):,} "
        f"({(panel['row_written'] == 0).mean():.1%})",
    ])


def held_out(panel: pd.DataFrame, last: int = 1) -> tuple[Any, Any]:
    """`(train, test)` split by **week**, holding out the final `last` weeks.

    By week rather than by a random split, because a random split of a time series
    puts rows from after the test hour into the training set. Notebook 03 measures
    what that is worth on this exact panel.
    """
    weeks = sorted(panel["week"].unique())
    assert len(weeks) > last, f"only {len(weeks)} week(s); cannot hold out {last}"
    cut = weeks[-last]
    return panel[panel["week"] < cut], panel[panel["week"] >= cut]


def ensure_database(name: str | None = None) -> bool:
    """Create the workshop's database if it is absent. `True` if it created it.

    **The seeder does not do this.** `storage.seed.main` connects to
    `POSTGRES_DB` and applies the schema to whatever is already there, so pointing
    it at a database nobody has created fails with `database "wwtp_ml" does not
    exist` — which is what `make workshop-seed` did on a clean checkout until this
    function existed. `tools/notebook_data.py` has always had one for the analyst
    notebooks; this is the same answer for this track.

    `POSTGRES_DB` names the database, so the connection string is taken from the
    usual place and only the *name* is swapped for `postgres` to get an admin
    handle. Creating a database is not idempotent in the useful direction — a
    second `CREATE DATABASE` raises — so the existence check is the whole point.
    """
    import psycopg  # noqa: PLC0415
    from psycopg import sql  # noqa: PLC0415
    from storage.postgres.schema import dsn as _dsn  # noqa: PLC0415

    database = name or os.environ.get("POSTGRES_DB") or "wwtp_ml"
    admin = _dsn().replace(f"dbname={database}", "dbname=postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                        (database,)).fetchone():
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    return True
