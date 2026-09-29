---
title: "09 — Anomaly detection"
subtitle: "Four detectors, four known faults and a storm: which kind of detector sees which kind of event"
---

# 09 — Anomaly detection

## The question

> Something went wrong with the plant this week. Actually, four things did. Which
> ones can I find, how soon, and what does each detector cost me in false alarms?

The plant was seeded with four events, and because the week is pinned we know exactly
when each one started and stopped. That is a luxury no real plant offers, and it turns
anomaly detection from an argument into a measurement: each detector either raised an
alarm while the fault was happening, or it did not.

## Setup

```python
%matplotlib inline
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from notebooks._data import connect, known_events
from notebooks._style import apply_style, save, signal_meta

apply_style()
pd.set_option("display.width", 140)

conn = connect()
grid = pd.date_range(
    "2026-09-22", "2026-09-29", freq="1min", tz="UTC", inclusive="left"
)
rollup = pd.read_sql("SELECT bucket, signal_id, mean, n FROM reading_1m", conn)
mean = rollup.pivot(index="bucket", columns="signal_id", values="mean").reindex(grid)
count = (
    rollup.pivot(index="bucket", columns="signal_id", values="n")
    .reindex(grid)
    .fillna(0)
)
held = mean.ffill()
meta = pd.read_sql(
    "SELECT id, normal_low, normal_high, deadband FROM signal", conn
).set_index("id")
minute_of_day = grid.hour * 60 + grid.minute
print(f"{len(grid):,} minutes, {held.shape[1]} signals")
```

```output
10,080 minutes, 57 signals
```

## The ground truth

Nothing in a real plant tells you when its instruments failed. Here the seeder *does*:
the fault times are fractions of the week, and the storm is armed from the end of it.

```python
events = known_events()
truth = pd.DataFrame(
    [
        (name, sig, a, b, int((b - a).total_seconds() // 60))
        for name, (sig, a, b) in events.items()
    ],
    columns=["event", "signal", "starts (UTC)", "ends (UTC)", "minutes"],
).set_index("event")
print(truth.to_string())
```

```output
                                   signal              starts (UTC)                ends (UTC)  minutes
event                                                                                                 
do_sensor_drift         AERATION:AHU-1:DO 2026-09-24 02:24:00+00:00 2026-09-24 03:54:00+00:00       90
effluent_tss_stuck      EFFLUENT:FLOW:TSS 2026-09-25 20:24:00+00:00 2026-09-25 22:24:00+00:00      120
sensor_dead         INFLUENT:LIFT:CURRENT 2026-09-27 11:02:00+00:00 2026-09-27 12:02:00+00:00       60
wet_weather_storm      INFLUENT:LIFT:FLOW 2026-09-27 12:00:00+00:00 2026-09-27 14:00:00+00:00      120
```

Three instrument faults and a storm, and their *shapes* are the point, because each one
is invisible to a different kind of detector. A **gauge that drifts** while the process is
fine (dissolved oxygen). A **frozen reading** (effluent TSS). An instrument that **stops
answering** (the lift-station current). And a **real process event** that moves many
signals at once (the storm). Note the last two: the sensor dies at 11:02 and the storm
starts at 12:00, an hour later, on the same equipment.

## Four detectors

```python
def by_threshold():
    """The contract's normal band: alarm when the held value leaves it."""
    low, high = meta.normal_low, meta.normal_high
    return held.lt(low, axis=1) | held.gt(high, axis=1)


def by_residual(limit=6.0):
    """Distance from the signal's *own* expectation for this minute of the day.

    The expectation is the median of the seven days at that minute, so a two-hour
    event on one day cannot move it. The scale is the robust spread of the residual,
    floored at the signal's deadband: a change smaller than the deadband is not
    something the instrument can report, so it is not something to alarm on.
    """
    profile = held.groupby(minute_of_day).transform("median")
    residual = held - profile
    spread = 1.4826 * residual.sub(residual.median()).abs().median()
    scale = np.maximum(spread, meta.deadband.reindex(residual.columns).fillna(0)).clip(
        lower=1e-9
    )
    return (residual / scale).abs() > limit


def by_silence(window=30, fraction=0.1, minimum=30):
    """Far fewer readings than this signal usually writes at this time of day."""
    observed = count.rolling(window).sum()
    expected = observed.groupby(minute_of_day).transform("median")
    return (observed < fraction * expected) & (expected >= minimum)


flagged = pd.read_sql(
    "SELECT date_trunc('minute', ts) AS minute, signal_id FROM reading "
    "WHERE quality > 0 GROUP BY 1, 2",
    conn,
)


def by_quality():
    """The instrument's own opinion: any reading not marked Good."""
    out = pd.DataFrame(False, index=grid, columns=held.columns)
    for row in flagged.itertuples():
        out.loc[row.minute, row.signal_id] = True
    return out


DETECTORS = {
    "threshold": by_threshold(),
    "residual": by_residual(),
    "silence": by_silence(),
    "quality flag": by_quality(),
}
print({name: int(alarms.to_numpy().sum()) for name, alarms in DETECTORS.items()})
```

```output
{'threshold': 105170, 'residual': 27613, 'silence': 3105, 'quality flag': 37}
```

The four detectors ask four different questions. Is the value outside the contract's
band? Is it far from *this signal's own* expectation for this minute of the day? Has the
signal gone silent, compared with what it usually writes at this time of day? Has the
instrument itself said it doubts the reading?

Start with the raw volume. The threshold detector raises **105170** alarm-minutes across
the week, against **27613** for the residual detector, **3105** for silence and **37** for the
quality flag. A detector that alarms for nearly a fifth of all signal-minutes is not
detecting anything; it is describing the plant's normal band, which is wider than its
normal behaviour.

## Which detector sees which event

```python
def first_alarm(alarms, signal, start, end, grace=0):
    """Minutes from the start of the fault to the first alarm inside the window.

    `grace` extends the window past the end of the fault. At zero, an alarm that
    arrives after the fault is over does not count as having detected it.
    """
    stop = end + pd.Timedelta(minutes=grace)
    inside = alarms[signal][(alarms.index >= start) & (alarms.index < stop)]
    hits = inside[inside]
    return None if hits.empty else int((hits.index[0] - start).total_seconds() // 60)


def scored(grace):
    table = pd.DataFrame(
        {
            name: {
                event: first_alarm(alarms, sig, a, b, grace)
                for event, (sig, a, b) in events.items()
            }
            for name, alarms in DETECTORS.items()
        }
    )
    return table.astype("Int64").fillna(-1).astype(int).replace(-1, "missed")


print("scored strictly: an alarm counts only while the fault is happening")
print(scored(0).to_string())
print()
print("scored with 30 minutes of grace after the fault ends")
print(scored(30).to_string())
```

```output
scored strictly: an alarm counts only while the fault is happening
                   threshold residual silence quality flag
do_sensor_drift       missed       12  missed            1
effluent_tss_stuck    missed   missed      27       missed
sensor_dead           missed   missed  missed            0
wet_weather_storm          8        0  missed       missed

scored with 30 minutes of grace after the fault ends
                   threshold residual silence quality flag
do_sensor_drift       missed       12  missed            1
effluent_tss_stuck    missed   missed      27       missed
sensor_dead               65       60  missed            0
wet_weather_storm          8        0     136       missed
```

Scored strictly, **no detector sees more than two of the four events, and no event is
seen by more than two detectors.** Read the table by row, because each event has a
detector that owns it:

* **The drift** is caught by the per-signal residual, **12** minutes in, and by the
  instrument's own quality flag, **1** minute in. The contract's band never sees it: the
  oxygen reading stays inside 1.5 to 3.0 mg/L throughout, which is a fault a threshold
  cannot see by construction.
* **The frozen TSS** is caught only by the silence detector, **27** minutes in. Its value
  is plausible, its quality is `Good` and it is inside the band. The only thing wrong is
  that it stopped changing — notebook 02's "no change" kind of nothing, now as an alarm.
* **The dead sensor** is caught only by the quality flag, at minute **0**, because the
  instrument says so itself. No statistical detector on the values sees an outage: there
  are no values.
* **The storm** is caught by the residual at minute **0** and by the band at minute **8**.

Now the second table. Allow a grace period of 30 minutes after the fault ends and the
threshold and residual detectors both "detect" the dead sensor, at **65** and **60**
minutes. That is after it came back. What they saw was the storm arriving and the value
jumping on recovery, and a scoring rule with a grace period credits them with an outage
they never observed. The silence detector's storm "hit" at **136** minutes is likewise
after the storm was over. **How you score a detector decides which detector wins.**

## What each detector costs

```python
KNOWN = [
    (a - pd.Timedelta(minutes=30), b + pd.Timedelta(minutes=30))
    for _, a, b in events.values()
]
STARTUP = pd.Timedelta(hours=12)  # the seeded plant's first half-day
RECOVERY = pd.Timedelta(hours=12)  # the storm's tail is long

quiet = pd.Series(True, index=grid)
for a, b in KNOWN:
    quiet &= ~((grid >= a) & (grid < b))
in_startup = grid < grid[0] + STARTUP
storm_to = events["wet_weather_storm"][2]
in_recovery = (grid >= storm_to) & (grid < storm_to + RECOVERY)

targets = sorted({sig for sig, _, _ in events.values()})
rows = []
for name, alarms in DETECTORS.items():
    a = alarms[targets]
    rows.append(
        {
            "detector": name,
            "in an event window": int(a[~quiet].to_numpy().sum()),
            "start-up transient": int(a[quiet & in_startup].to_numpy().sum()),
            "storm recovery": int(
                a[quiet & ~in_startup & in_recovery].to_numpy().sum()
            ),
            "anywhere else": int(
                a[quiet & ~in_startup & ~in_recovery].to_numpy().sum()
            ),
            "signals that ever alarm": int(alarms.any().sum()),
        }
    )
cost = pd.DataFrame(rows).set_index("detector")
print("alarm-minutes on the four signals that carry an event, by where they fall")
print(cost.to_string())
```

```output
alarm-minutes on the four signals that carry an event, by where they fall
              in an event window  start-up transient  storm recovery  anywhere else  signals that ever alarm
detector                                                                                                    
threshold                    227                 278             138           1441                       33
residual                     489                 260             241              4                       42
silence                      182                   4              24              0                       20
quality flag                  37                   0               0              0                        3
```

The cost table is where the threshold detector loses. On the four signals that carry an
event it raises **1441** alarm-minutes at times when nothing is wrong, and **33** of the 57
signals alarm at some point in the week. The residual detector raises **4** such minutes.
The silence and quality detectors raise none outside events.

Two of the columns are *not* false alarms, and calling them that would be the mistake.

**The start-up transient**: **278** threshold and **260** residual alarm-minutes fall in the
first twelve hours. The simulated plant starts from initial conditions and takes half a
day to settle, so those alarms describe something real about the data — and a detector
trained on this week would learn it as "normal" if you let it.

**Storm recovery**: the flow does not return to its daily profile when the rain stops.
**241** residual alarm-minutes fall in the twelve hours after the storm, at deviations of a
few per cent, long after the plot looks calm.

```python
fig, axes = plt.subplots(2, 2, figsize=(12, 7.6))
for ax, (event, (sig, a, b)) in zip(axes.flat, events.items(), strict=True):
    view = (grid >= a - pd.Timedelta(hours=2)) & (grid < b + pd.Timedelta(hours=2))
    ax.plot(grid[view], held[sig][view], color="#444444", linewidth=1.3)
    ax.axvspan(a, b, color="#d62728", alpha=0.15)
    for name, colour in (
        ("residual", "#1f77b4"),
        ("silence", "#2ca02c"),
        ("quality flag", "#ff7f0e"),
    ):
        hit = DETECTORS[name][sig][view]
        ax.scatter(
            grid[view][hit], held[sig][view][hit], s=10, color=colour, label=name
        )
    unit = signal_meta(sig)["unit"]
    ax.set_ylabel(f"{signal_meta(sig)['field']}  [{unit}]", fontsize=8)
    ax.set_title(f"{event}: {sig}", loc="left", fontsize=9, fontweight="bold")
    ax.tick_params(axis="x", labelrotation=30, labelsize=7)
axes[0, 0].legend(fontsize=7, loc="upper left")
axes[1, 0].set_xlabel("time (UTC)")
axes[1, 1].set_xlabel("time (UTC)")
fig.suptitle(
    "The four events, with the alarms of three detectors (shaded: the true window)",
    x=0.01,
    ha="left",
    fontweight="bold",
)
fig.tight_layout()
save(fig, "09_events")
```

One panel per event. The grey line is the signal, the red band is the true fault
window, and the coloured dots are alarms. Where a panel has no dot inside the red band,
that detector missed it.

## Takeaways

- **Each kind of fault has a kind of detector.** A drift needs a per-signal baseline
  (**12** minutes), a frozen reading needs a silence detector (**27**), a dead instrument
  needs the quality flag (**0**), and a storm is found by almost anything (**0** to **8**).
- **The contract's band is not a detector.** It missed all three instrument faults and
  raised **105170** alarm-minutes, including **1441** on four signals when nothing was
  wrong.
- **A per-signal baseline is worth two orders of magnitude.** The residual detector's
  "anywhere else" is **4** alarm-minutes against the band's **1441**, from the same data.
- **The scoring rule is part of the result.** A 30-minute grace period credits two
  detectors with the dead sensor at **65** and **60** minutes, after it had recovered.
- **Some alarms are true and not faults.** **278** and **260** alarm-minutes fall in the
  simulated plant's first twelve hours: real, unexplained by any injected fault, and
  exactly what a detector fitted to this week would absorb.

## Exercises

1. **Tune the residual detector.** The limit is 6 robust standard deviations. Sweep it
   from 3 to 12 and plot detected events against alarm-minutes outside events. Where is the
   knee, and is it the same for all four signals?
2. **Build the fifth detector.** Nothing here catches the dead sensor except the quality
   flag. Write a detector that finds "a reading exists, then the signal goes quiet where
   its neighbours do not" using only values, and say what it costs.
3. **Score it as an operator would.** An alarm is useful if it arrives early enough to
   act. Give each event a deadline of one third of its duration and re-score. Which
   detectors survive?
4. **Remove the start-up.** Fit the profile from days 2 to 7 only. What happens to the
   start-up transient's alarms, and to the storm-recovery alarms?

---

**Next:** [10 — Change points](10-change-points.ipynb) ·
**Back to the series** [README](README.md) ·
**Previous:** [08 — Rolling-origin validation](08-rolling-origin-validation.ipynb)
