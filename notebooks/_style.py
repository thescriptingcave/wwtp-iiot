"""Shared plotting style for the analyst-notebook series.

    from notebooks._style import apply_style, trend, stamp, save

    apply_style()
    fig, ax = plt.subplots()
    trend(ax, df, "AERATION:AHU-1:DO")
    stamp(ax, df, window="6 hours", source="reading_1m")
    save(fig, "do_quality")

## Why a module, and not "be careful in each notebook"

Eleven notebooks that each hand-format their axes will each hand-format them
slightly differently, and the drift is invisible until someone puts two figures
side by side. The two requirements this series has to satisfy are mechanical —
every axis labelled, every figure carrying its qualifier — and a rule that lives
in eleven places is a rule that is broken in one. So the rules live here, once,
and a notebook that wants a different look calls `apply_style()` differently.

The trade-off is accepted rather than hidden: **a notebook is no longer
self-contained.** Someone copying one out of context loses the styling. That is
the cost of consistency across eleven notebooks, and it is worth it here because
the alternative is eleven slightly different charts rather than one standard.

## The rule that matters most: never `sns.lineplot`

`seaborn.lineplot` **aggregates repeated x-values into a mean and a 95 % band.**
On this dataset that is not a cosmetic default, it is a lie:

* the historian is change-triggered, so the samples inside any time bucket are
  *not* a representative slice of the bucket — they are a slice sized by how fast
  the signal was moving;
* the confidence band implies statistical inference over observations that are
  almost entirely redundant. Measured on 1-minute aggregates, the lag-1
  autocorrelation of `PRIMARY:PRI-SCR-1:TORQUE` is **+0.92** and of
  `UTILITY:SITE:PLANT_POWER` is **+0.999**. There is one independent observation
  in that hour, not 60.

A reader looking at a `lineplot` chart of this plant would learn something false
about the data, presented with a confidence band that makes it look rigorous.
Use `ax.plot()` and do the aggregation visibly — or, better, plot the samples.

`seaborn` is here for `histplot`, `ecdfplot`, `boxplot` and `heatmap`, where it
is genuinely better than matplotlib and does not transform anything.

## The qualifier stamp

Every figure in this series carries a footer:

    n=721 · window 12h · coverage 97.4% · source reading_1m · UTC

This is the repository's thesis made mechanical. `sql/04-expert/04-01` measured
that `AVG` over irregularly-sampled data is wrong by **2.9 %** on a regulatory
signal; a plot that does not say what it averaged over is the same mistake in a
different medium. A number without a qualifier is a claim you cannot check, and
that rule applies to pictures as much as to text.
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import matplotlib as mpl
import matplotlib.pyplot as plt

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

__all__ = [
    "apply_style",
    "describe",
    "figure",
    "format_time_axis",
    "save",
    "signal_meta",
    "stamp",
    "trend",
]

#: Palette. Colour-blind safe, and distinguishable in greyscale, because these
#: figures get pasted into documents that print. Chosen rather than left to
#: matplotlib's default cycle, which is bright and does not survive a fax.
PALETTE: tuple[str, ...] = (
    "#1f77b4",  # blue
    "#d95f02",  # orange
    "#2ca02c",  # green
    "#7570b3",  # purple
    "#e7298a",  # pink
    "#66a61e",  # olive
    "#a6761d",  # brown
    "#666666",  # grey
)

#: One colour per OPC UA quality state, used by notebook 02 and by anything that
#: plots a signal's quality over time. The `Uncertain`/`Bad` pair is warm on
#: purpose: a reader should be able to spot a degraded reading without reading
#: the legend.
QUALITY_COLOURS: dict[int, str] = {
    0: "#1f77b4",   # Good
    1: "#ff9e00",   # Uncertain
    2: "#d62728",   # Bad
}


def apply_style() -> None:
    """Set the house style. Call once per notebook, before the first figure.

    Deliberately not applied on import. A module that silently reconfigures
    global state the moment it is imported is a module that fights whatever else
    the notebook is doing, and the call is one line.
    """
    mpl.rcParams.update({
        "figure.figsize": (11, 4.5),
        "figure.dpi": 110,
        "savefig.dpi": 150,
        "savefig.bbox": "tight",
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.labelsize": 10,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "grid.linestyle": "-",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.prop_cycle": mpl.cycler(color=PALETTE),
        "lines.linewidth": 1.4,
        "legend.frameon": False,
        "figure.autolayout": False,
    })
    # Date formatting is applied per-axis in `format_time_axis`, not as a global
    # rcParam: `date.autoformatter.hour` wants a *string*, and setting the
    # integer raises a `ValueError` that names the type rather than the mistake.


def figure(
    *,
    title: str = "",
    subtitle: str = "",
    figsize: tuple[float, float] | None = None,
):
    """A figure and axes with the title already set.

    Two lines rather than one, because a figure whose title names the signal is
    readable when it is the only thing in a document, and a figure whose title
    says "plot" is not.
    """
    fig, ax = plt.subplots(figsize=figsize)
    if title:
        ax.set_title(title, loc="left", fontweight="bold")
    if subtitle:
        ax.text(
            0.0, 1.02, subtitle, transform=ax.transAxes, fontsize=9,
            color="#555555", va="bottom",
        )
    return fig, ax


def signal_meta(signal_id: str) -> dict[str, Any]:
    """`unit`, `field` and `area` for a signal id, read from the database.

    **This is why the units are never typed by hand in a notebook.** The units in
    this project are awkward — `{pH}`, `{Boolean}`, `{MPN}/100mL`, `m/(L.h)` — and
    a hand-typed axis label is a label that is wrong the day a unit changes, with
    nothing failing. Reading it means a renamed signal breaks the label loudly
    instead of mislabelling it quietly.

    Falls back to the id rather than raising: a figure about a signal that is
    missing should still render, with an obviously incomplete label, because the
    alternative is an exception in the middle of a notebook.
    """
    try:
        from storage.postgres.schema import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT unit, field, area FROM signal WHERE id = %s",
                (signal_id,),
            )
            row = cur.fetchone()
    except Exception:  # a figure must not fail over a missing label
        row = None

    if row is None:
        return {"unit": "", "field": signal_id.rsplit(":", 1)[-1], "area": ""}
    unit, field, area = row
    return {"unit": unit or "", "field": field or signal_id, "area": area or ""}


def format_time_axis(ax: Axes, *, local: bool = False) -> None:
    """Put a readable, *stated* timezone on the time axis.

    The data is `timestamptz` and psycopg hands back aware datetimes, so pandas
    builds a tz-aware index. `matplotlib` then formats it in **the process's local
    zone**, silently. On a machine set to anything but UTC that shifts every
    chart by an offset nobody can see, and a reader comparing a chart against a
    log line finds a difference that is not there.

    So the zone is always written on the axis, and the default is UTC because
    that is what the database stores.
    """
    zone = "local" if local else "UTC"
    ax.set_xlabel(f"time ({zone})")
    locator = mpl.dates.AutoDateLocator(minticks=4, maxticks=10)
    formatter = mpl.dates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    for label in ax.get_xticklabels():
        label.set_fontsize(8)
    ax.tick_params(axis="x", labelrotation=30)


def trend(
    ax: Axes,
    series: dict[str, Any],
    signal_id: str,
    *,
    ylabel: str = "",
) -> Axes:
    """Plot one or more series for a signal, labelled from the database.

    `series` maps a legend label to something `ax.plot` accepts — a list of
    datetimes, a numpy array, a pandas Series. Passing a dict rather than a
    label/values pair is so a multi-line plot is one call and the labels are
    ordered by their insertion, which is the order a reader wants to read them.
    """
    meta = signal_meta(signal_id)
    for label, values in series.items():
        ax.plot(values, label=label)
    unit = meta["unit"]
    ax.set_ylabel(ylabel or (f"{meta['field']}" + (f"  [{unit}]" if unit else "")))
    if not ax.get_title():
        ax.set_title(f"{meta['field']} — {signal_id}", loc="left", fontweight="bold")
    format_time_axis(ax)
    if len(series) > 1:
        ax.legend(loc="upper left", ncols=min(len(series), 4), fontsize=9)
    return ax


def describe(series: Any, *, expected_s: float | None = None) -> dict[str, Any]:
    """n, coverage and range for a series, ready for :func:`stamp`.

    **Coverage is only meaningful against a stated expected interval.** Left to
    infer it from the data, it measures the data against itself and is always
    100 % — a series decimated to every seventh point reports 100 %, because
    after decimation the gaps *are* regular. That is a coverage figure that
    cannot fail, and a qualifier that cannot fail is decoration.

    So pass `expected_s` — the contract's declared `sample_ms`, or a rate the
    question implies — and coverage becomes the fraction of the observed span
    that the expected cadence would have filled. With no `expected_s` this
    returns `nan` and :func:`stamp` leaves the field out, because an absent
    qualifier is honest and a fabricated one is not.
    """
    import numpy as np
    import pandas as pd

    index = pd.DatetimeIndex(series.index)
    n = len(series)
    if n == 0:
        return {"n": 0, "coverage": float("nan"), "span": "empty"}
    if n == 1:
        return {"n": 1, "coverage": float("nan"), "span": "single point"}

    # `Timedelta.total_seconds()` rather than `np.diff(index.view("int64"))`.
    #
    # The idiom was the standard one for years and it is wrong on pandas 3.x:
    # `.view("int64")` returns **microseconds** there, so dividing by 1e9 gives
    # a gap a thousand times too small. It did not fail. It reported
    # `typical_s = 0.06` for a one-minute series and `span = 0.0 h` for twelve
    # hours, and `coverage` came out at a confident 100.0 % — because the same
    # wrong divisor cancelled in the numerator and the denominator.
    #
    # A ratio that survives a unit error is the dangerous kind, so the
    # conversion is explicit here and the unit is not written down twice.
    deltas = np.array(
        [(b - a).total_seconds() for a, b in itertools.pairwise(index)],
        dtype=float,
    )
    typical = float(np.median(deltas))
    observed = float(deltas.sum())
    span = observed + typical          # the last reading is held for one interval
    if expected_s and expected_s > 0:
        wanted = expected_s * (n - 1)
        coverage = wanted / span if span else float("nan")
    else:
        coverage = float("nan")
    return {
        "n": n,
        "coverage": min(coverage, 1.0) if not math.isnan(coverage) else coverage,
        "span": f"{span / 3600:.1f} h",
        "typical_s": typical,
    }


def stamp(
    ax: Axes,
    series: Any = None,
    *,
    window: str = "",
    source: str = "",
    expected_s: float | None = None,
    extra: str = "",
) -> Axes:
    """Write the qualifier in the bottom-right of the axes.

        n=721 · window 12h · coverage 97.4% · source reading_1m · UTC

    `coverage` is omitted when it is not meaningful — a single point has no
    coverage, and printing `nan` would be noise. A qualifier that is always
    present is a qualifier nobody reads.
    """
    parts: list[str] = []
    if series is not None:
        stats = describe(series, expected_s=expected_s)
        parts.append(f"n={stats['n']}")
        # An explicit `window` wins over the measured span, because "the last 6
        # hours" is a claim about the *question* and the span is a measurement of
        # what came back. They are usually the same number and occasionally not,
        # and when they differ the reader is owed the one that was asked for.
        parts.append(f"window {window}" if window else f"span {stats['span']}")
        if not math.isnan(stats["coverage"]):
            parts.append(f"coverage {stats['coverage'] * 100:.1f}%")
    elif window:
        parts.append(f"window {window}")
    if source:
        parts.append(f"source {source}")
    parts.append("UTC")
    if extra:
        parts.append(extra)

    ax.text(
        0.995, -0.28, " · ".join(parts), transform=ax.transAxes,
        ha="right", va="top", fontsize=8, color="#666666", family="monospace",
    )
    return ax


def save(fig: Figure, name: str, *, directory: str = "notebooks/figures") -> Figure:
    """Write a figure to `notebooks/figures/`, creating the directory.

    Returns the figure rather than a path, so a notebook can `save(fig, "x")` and
    carry on without the call being a statement about control flow.
    """
    Path(directory).mkdir(parents=True, exist_ok=True)
    fig.savefig(Path(directory) / f"{name}.png")
    return fig
