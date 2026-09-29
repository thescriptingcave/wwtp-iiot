---
title: "10 — Change points"
subtitle: "Dating an event to the minute: CUSUM on the right residual, and what the rollup tier does to the answer"
---

# 10 — Change points

## The question

> Notebook 09 found the events. When did each one *start*?

Detection and dating are different jobs. Detection asks "is something wrong now?" and
wants to answer as soon as it can with as few false alarms as it can. Dating asks "when
did it begin?" and wants the *first* minute of the excursion, not the minute it became
obvious. The distinction matters because the answer to the second question is what you
take to an operator — "the oxygen probe started drifting at 02:24" — and what you set
against the maintenance log.

## Setup

```python
%matplotlib inline
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from notebooks._data import connect, known_events
from notebooks._style import apply_style, save, stamp

apply_style()
pd.set_option("display.width", 140)

conn = connect()
grid = pd.date_range(
    "2026-09-22", "2026-09-29", freq="1min", tz="UTC", inclusive="left"
)
rollup = pd.read_sql("SELECT bucket, signal_id, mean, n FROM reading_1m", conn)
held = (
    rollup.pivot(index="bucket", columns="signal_id", values="mean")
    .reindex(grid)
    .ffill()
)
readings_per_minute = (
    rollup.pivot(index="bucket", columns="signal_id", values="n")
    .reindex(grid)
    .fillna(0)
)
deadband = pd.read_sql("SELECT id, deadband FROM signal", conn).set_index("id").deadband
events = known_events()
SINCE = pd.Timestamp("2026-09-23", tz="UTC")  # notebook 09: the first day is a start-up
print(f"{len(grid):,} minutes; events: {', '.join(events)}")
```

```output
10,080 minutes; events: do_sensor_drift, effluent_tss_stuck, sensor_dead, wet_weather_storm
```

## The tool

```python
def standardised_residual(series, floor, index=None):
    """How many robust standard deviations from this signal's own daily profile.

    The profile is the median at each time of day over the week, so one event cannot
    move it (notebook 06). The scale is floored at `floor`, normally the deadband: a
    change the instrument cannot report is not a change.
    """
    index = series.index if index is None else index
    clock = index.hour * 60 + index.minute
    residual = series - series.groupby(clock).transform("median")
    spread = 1.4826 * np.median(np.abs(residual - np.median(residual)))
    return residual / max(spread, floor, 1e-9)


def cusum(z, k=0.5, h=5.0):
    """One-sided CUSUM. Returns `(alarm position, estimated onset position)` pairs.

    `S` accumulates how far each point is above `k` standard deviations and is held
    at zero from below. An alarm is `S > h`. The *onset* is the position after the last
    time `S` was zero — where the excursion began, which is not where it was noticed.
    `S` restarts after every alarm.
    """
    s, last_zero, found = 0.0, -1, []
    for i, value in enumerate(np.asarray(z, dtype=float)):
        s = max(0.0, s + value - k)
        if s == 0.0:
            last_zero = i
        if s > h:
            found.append((i, last_zero + 1))
            s, last_zero = 0.0, i
    return found
```

## Dating three events

```python
def minutes(delta):
    return delta.total_seconds() / 60


def date_event(signal, start, end, statistic):
    if statistic == "level":
        series, floor, sign = held[signal], deadband[signal], 1
    else:  # a frozen signal stops writing rows
        series, floor, sign = (
            readings_per_minute[signal].rolling(5).sum().bfill(),
            1.0,
            -1,
        )
    z = (sign * standardised_residual(series, floor))[series.index >= SINCE]
    for alarm, onset in cusum(z.to_numpy()):
        if start <= z.index[alarm] < end:
            return {"alarm": z.index[alarm], "onset": z.index[onset]}
    return None


rows = []
for event, statistic in (
    ("do_sensor_drift", "level"),
    ("effluent_tss_stuck", "reading rate"),
    ("wet_weather_storm", "level"),
):
    signal, start, end = events[event]
    found = date_event(signal, start, end, statistic)
    rows.append(
        {
            "event": event,
            "statistic": statistic,
            "true start": start.strftime("%m-%d %H:%M"),
            "alarm": found["alarm"].strftime("%H:%M"),
            "delay [min]": minutes(found["alarm"] - start),
            "estimated onset": found["onset"].strftime("%H:%M"),
            "error [min]": minutes(found["onset"] - start),
        }
    )
dated = pd.DataFrame(rows).set_index("event")
print(dated.to_string())
```

```output
                       statistic   true start  alarm  delay [min] estimated onset  error [min]
event                                                                                         
do_sensor_drift            level  09-24 02:24  02:30          6.0           02:25          1.0
effluent_tss_stuck  reading rate  09-25 20:24  20:24          0.0           20:24          0.0
wet_weather_storm          level  09-27 12:00  12:00          0.0           12:00          0.0
```

CUSUM dates all three events to within a minute. The drift — the hard one, a ramp that
begins gently — is *alarmed* **6** minutes after it starts but *dated* to **02:25**, one
minute after the true 02:24. That gap between the two columns is the whole idea: the
statistic has been quietly accumulating since the first minute, and its estimate of the
onset is the last time it was at zero.

The frozen TSS and the storm are dated exactly, and that deserves suspicion, not pride.
Both are abrupt in this simulator — the reading rate falls to nothing, the flow starts to
climb — so the first minute is unambiguous. A real frozen gauge and a real storm are both
softer than this. The drift is the fairer test, and its error of **1.0** minute is the
number to remember.

## Why the residual, and not the series

```python
CALM = (grid >= SINCE) & (grid < pd.Timestamp("2026-09-27", tz="UTC"))
rows = []
for signal, floor in (("INFLUENT:LIFT:FLOW", 2.0), ("UTILITY:SITE:PLANT_POWER", 1.0)):
    raw = held[signal][CALM]
    raw_z = (raw - raw.mean()) / raw.std()
    residual_z = standardised_residual(held[signal], floor)[CALM]
    rows.append(
        {
            "signal": signal,
            "raw series": len(cusum(raw_z.to_numpy())),
            "daily-profile residual": len(cusum(residual_z.to_numpy())),
        }
    )
false_alarms = pd.DataFrame(rows).set_index("signal")
print(
    "CUSUM alarms over four calm days, 23 to 26 September"
    " — nothing happens to these signals"
)
print(false_alarms.to_string())
```

```output
CUSUM alarms over four calm days, 23 to 26 September — nothing happens to these signals
                          raw series  daily-profile residual
signal                                                      
INFLUENT:LIFT:FLOW               204                       0
UTILITY:SITE:PLANT_POWER         222                      10
```

The residual is not decoration. The same CUSUM on the raw series raises **204** alarms
in four calm days on the lift-station flow and **222** on plant power, where nothing
happened to either signal. It is reading the daily cycle as a sequence of change points,
every morning and every evening. On the residual it raises **0** and **10**. The ten on
plant power are not explained here; exercise 3 asks you to find them.

## What the tier does to the answer

```python
signal, start, _ = events["do_sensor_drift"]
rows = []
for label, rule in (("1 minute", "1min"), ("10 minutes", "10min"), ("1 hour", "1h")):
    coarse = held[signal].resample(rule).mean()
    z = standardised_residual(coarse, 0.02)[coarse.index >= SINCE]
    after = [(a, o) for a, o in cusum(z.to_numpy()) if z.index[a] >= start.floor("1h")]
    alarm, onset = after[0]
    # A bucket is labelled by its left edge but is only *known* when it closes.
    knowable = z.index[alarm] + pd.Timedelta(rule)
    rows.append(
        {
            "resolution": label,
            "bucket label": z.index[alarm].strftime("%H:%M"),
            "knowable at": knowable.strftime("%H:%M"),
            "real delay [min]": minutes(knowable - start),
            "estimated onset": z.index[onset].strftime("%H:%M"),
            "onset error [min]": minutes(z.index[onset] - start),
        }
    )
print(f"true start of the drift: {start:%m-%d %H:%M}")
print(pd.DataFrame(rows).set_index("resolution").to_string())
```

```output
true start of the drift: 09-24 02:24
           bucket label knowable at  real delay [min] estimated onset  onset error [min]
resolution                                                                              
1 minute          02:30       02:31               7.0           02:25                1.0
10 minutes        02:30       02:40              16.0           02:20               -4.0
1 hour            02:00       03:00              36.0           02:00              -24.0
```

The rollup tier decides how well you can date. At one-minute resolution the onset
lands **1.0** minute from the truth. At ten minutes it is **-4.0**, and at an hour
**-24.0**: the estimate can only be a bucket edge, and the true start at 02:24 falls
inside the bucket that begins at 02:00.

The column to read is the real delay. The hourly bucket is labelled `02:00`, which is
*before* the drift started, and a careless table would report a detection twenty-four
minutes early. But an hour's mean does not exist until the hour ends. The alarm was
knowable at **03:00**, **36.0** minutes after the drift began, against **7.0** at one minute
and **16.0** at ten. This is notebook 03's rule applied to time: a tier that cannot show a
peak cannot date a start either.

```python
fig, axes = plt.subplots(2, 1, figsize=(11, 7))
for ax, (event, label) in zip(
    axes,
    (
        ("do_sensor_drift", "dissolved oxygen, mg/L"),
        ("wet_weather_storm", "lift-station flow, m3/h"),
    ),
    strict=True,
):
    signal, start, end = events[event]
    view = (grid >= start - pd.Timedelta(hours=3)) & (
        grid < end + pd.Timedelta(hours=2)
    )
    ax.plot(grid[view], held[signal][view], color="#444444", label=label)
    ax.axvline(start, color="#d62728", linestyle="--", label="true start")
    found = date_event(signal, start, end, "level")
    ax.axvline(
        found["onset"],
        color="#1f77b4",
        linestyle=":",
        linewidth=2.2,
        label="CUSUM onset estimate",
    )
    ax.axvline(found["alarm"], color="#2ca02c", linestyle="-.", label="CUSUM alarm")
    ax.set_ylabel(label)
    ax.set_title(f"{event}: {signal}", loc="left", fontsize=10, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8)
axes[-1].set_xlabel("time (UTC)")
stamp(
    axes[-1],
    held["INFLUENT:LIFT:FLOW"][view],
    window="7 h",
    source="reading_1m, held forward",
    expected_s=60,
)
fig.tight_layout()
save(fig, "10_change_points")
```

The dashed red line is the truth, the dotted blue line the CUSUM estimate of the onset
and the dash-dot green line the moment it alarmed. Where the blue and red lines nearly
coincide and the green one is later, you are looking at the difference between dating and
detecting.

## Takeaways

- **Detecting and dating are different.** The drift alarmed **6** minutes after it began
  and was dated to within **1.0** minute, because the estimate is the last time the
  statistic was zero, not the time it crossed the line.
- **Deseasonalise before you look for a change.** CUSUM on the raw series raised **204**
  and **222** alarms on two signals where nothing happened; on the residual, **0** and
  **10**.
- **The rollup tier is the precision of the answer.** Onset errors of **1.0**, **-4.0** and
  **-24.0** minutes at one minute, ten minutes and an hour.
- **A bucket's label is not the time you knew.** The hourly alarm labelled `02:00` was
  knowable at **03:00**, **36.0** minutes after the fault began.
- **Abrupt events are the easy case.** The storm and the frozen TSS were dated with an
  error of **0.0** minutes, and a real plant will not be that kind.

## Exercises

1. **Trade delay for false alarms.** Sweep the alarm threshold from 2 to 20 and plot
   detection delay on the drift against false alarms on the calm days. Where is the knee?
2. **Date the dead sensor.** No statistic on the values can, because there are none. Use
   the one `Bad` row instead: how close to the true 11:02 is its timestamp, and what
   would you have to assume to trust it?
3. **Find the ten.** The residual CUSUM raises ten alarms on plant power over four calm
   days. When do they fall, and what does that say about the daily profile?
4. **Date the end.** The drift ends abruptly. Run the CUSUM on the time-reversed series
   and compare its onset estimate with the true end, 03:54.

---

**Next:** [11 — Dose or flow](11-dose-or-flow.ipynb) ·
**Back to the series** [README](README.md) ·
**Previous:** [09 — Anomaly detection](09-anomaly-detection.ipynb)
