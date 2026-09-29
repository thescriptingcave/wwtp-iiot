---
title: "11 — Dose or flow"
subtitle: "Attributing an effect to one of two things that moved together, with a counterfactual and an honest list of what cannot be split"
---

# 11 — Dose or flow

## The question

> Effluent solids doubled in the storm. Was that the *flow* — the water pushed through
> faster than the plant could settle it — or something the plant *did*: the aeration
> it changed?

Both moved in the same hour, which is why it is hard. Two things that move together
cannot be told apart by looking at them. Attribution needs a counterfactual — what would
have happened otherwise — and a way to separate the two movers. This notebook has both,
and is honest about the part it cannot do.

## Setup

```python
%matplotlib inline
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from notebooks._data import connect, storm_window
from notebooks._style import apply_style, save, stamp

apply_style()
pd.set_option("display.width", 140)

conn = connect()
storm_start, storm_end = (pd.Timestamp(t) for t in storm_window())
grid = pd.date_range(
    "2026-09-22", "2026-09-29", freq="1min", tz="UTC", inclusive="left"
)
minute_means = (
    pd.read_sql("SELECT bucket, signal_id, mean FROM reading_1m", conn)
    .pivot(index="bucket", columns="signal_id", values="mean")
    .reindex(grid)
    .ffill()
)

NAMES = {
    "flow": "INFLUENT:LIFT:FLOW",
    "nh4_in": "INFLUENT:FLOW:NH4_IN",
    "air": "AERATION:AHU-1:AIR_FLOW",
    "tss": "EFFLUENT:FLOW:TSS",
    "nh4_out": "EFFLUENT:FLOW:NH4",
    "power": "UTILITY:SITE:PLANT_POWER",
}
hourly = (
    pd.DataFrame({k: minute_means[v] for k, v in NAMES.items()}).resample("1h").mean()
)
print(f"{len(hourly)} hourly means of {len(NAMES)} signals: {', '.join(NAMES)}")
```

```output
168 hourly means of 6 signals: flow, nh4_in, air, tss, nh4_out, power
```

Six signals as hourly means of the one-minute rollup, held forward: the flow and the
ammonia arriving, the air flow (the plant's *dose*), and the solids, ammonia and power
leaving.

## The counterfactual: the same hour yesterday

Attribution starts with what would have happened otherwise. The cheapest counterfactual
for a plant with a daily rhythm is *the same hour yesterday*, and the thing to do before
trusting it is to check it where it will be used — in the hours just before the event —
and to find where it fails:

```python
yesterday = hourly.shift(24)
before = hourly.loc[
    storm_start - pd.Timedelta(hours=6) : storm_start - pd.Timedelta(hours=1)
]
print(
    "largest difference from the same hour yesterday, in the six hours before the storm"
)
print((before - yesterday.loc[before.index]).abs().max().round(2).to_string())

calm_days = hourly.loc["2026-09-23":"2026-09-26"]
gap = (calm_days.air - yesterday.loc[calm_days.index].air).abs()
print(
    f"\nover 23 to 26 September the air flow's worst hour is"
    f" {gap.idxmax():%d %b %H:%M}: "
    f"{gap.max():,.0f} m3/h away from yesterday"
)
```

```output
largest difference from the same hour yesterday, in the six hours before the storm
flow        0.00
nh4_in      0.00
air        36.04
tss         0.00
nh4_out     0.00
power       2.42

over 23 to 26 September the air flow's worst hour is 23 Sep 08:00: 4,127 m3/h away from yesterday
```

The baseline is good exactly where it is needed. In the six hours before the storm the
same hour yesterday is within **0.00** of today for flow, ammonia and TSS, and within
**36.04** m³/h on air flow, about one per cent, so any difference after 12:00 is the
storm's. It fails elsewhere, and it fails informatively: over the calm days the air flow's
worst miss is **4,127** m³/h at 08:00 on 23 September, because *yesterday* was the plant's
first day, still settling (notebook 09). **A baseline is only as good as yesterday, so
check what yesterday was.**

```python
excess = hourly - hourly.shift(24)  # what changed against yesterday
around = excess[
    (excess.index >= storm_start - pd.Timedelta(hours=2))
    & (excess.index < storm_start + pd.Timedelta(hours=16))
].copy()
around.index = ((around.index - storm_start).total_seconds() / 3600).astype(int)
around.index.name = "hours after the storm began"
print(around.loc[-1:9].round(1).to_string())
```

```output
                               flow  nh4_in    air   tss  nh4_out  power
hours after the storm began                                             
-1                              0.0     0.0   -1.4  -0.0     -0.0   -0.1
 0                           1816.2   -11.0 -424.9  28.2     -0.7   81.4
 1                            552.7    -6.2   62.4   9.0     -1.4   50.3
 2                           -745.3    -0.0  -25.6  -6.3     -1.4   -3.0
 3                              0.3    -0.0  121.7   0.0     -1.2    8.6
 4                             -0.2     0.0  105.5  -0.0     -1.0    7.4
 5                              1.1     0.0   95.9   0.0     -0.8    6.7
 6                             -1.0    -0.0   89.4  -0.0     -0.7    6.3
 7                             -1.0    -0.0   72.7  -0.0     -0.6    5.1
 8                             -0.5    -0.0   51.6  -0.0     -0.5    3.6
 9                              0.3     0.0   38.3   0.0     -0.4    2.7
```

Read the columns in the storm's first hour. Flow is up **1816.2** m³/h. The ammonia
arriving is down **11.0** mg/L, halved: rain dilutes the sewage. And effluent TSS is up
**28.2** mg/L. The air flow, the plant's dose, is *down* **424.9** m³/h: the blowers backed
off, presumably because the diluted ammonia asked for less.

Then watch how TSS and flow move together and air does not. Hour 1: flow **+552.7**, TSS
**+9.0**. Hour 2: flow **-745.3**, an undershoot, and TSS **-6.3**, *below* yesterday's.
TSS follows the flow up and down. From hour 3 the flow is back to normal and so is TSS,
while the air flow stays *above* normal for the next ten hours, starting at **121.7** and
falling. If the air were driving the solids, TSS would still be moving.

## What the week's correlations say, and why not to stop there

```python
calm_only = hourly[
    (hourly.index < storm_start) | (hourly.index >= storm_end + pd.Timedelta(hours=12))
]
profile = calm_only.groupby(calm_only.index.hour).mean()
clock_removed = hourly - profile.reindex(hourly.index.hour).to_numpy()

table = pd.DataFrame(
    {
        "raw hourly means": hourly[["flow", "air"]].corrwith(hourly["tss"]),
        "clock removed": clock_removed[["flow", "air"]].corrwith(clock_removed["tss"]),
    }
).round(3)
print("correlation with effluent TSS")
print(table.to_string())
both = hourly[["flow", "air"]].dropna()
print(
    f"\nflow and air flow correlate {both.flow.corr(both.air):+.3f} with each other, "
    f"so their variance inflation factor is"
    f" {1 / (1 - both.flow.corr(both.air) ** 2):.2f}"
)
```

```output
correlation with effluent TSS
      raw hourly means  clock removed
flow             0.901          0.986
air             -0.117         -0.092

flow and air flow correlate -0.330 with each other, so their variance inflation factor is 1.12
```

Across the week, effluent TSS correlates **0.901** with the flow and **-0.117** with the
air flow, and with the clock removed **0.986** and **-0.092**. The flow and the air flow
correlate only **-0.330** with each other, a variance inflation factor of **1.12**, about
as separable as two regressors get. That is the storm's doing. It moved the flow and not
the air flow: a natural experiment, and the only reason the data can separate the two at
all. Over a week of calm days both would have been the same clock.

## The attribution

```python
window = excess.loc[
    storm_start - pd.Timedelta(hours=2) : storm_start + pd.Timedelta(hours=15)
]
X = window[["flow", "air"]].to_numpy()
y = window["tss"].to_numpy()
coefficients, *_ = np.linalg.lstsq(
    X, y, rcond=None
)  # no intercept: it is already "excess"
print(f"fitted on {len(window)} hourly differences from the storm's own day")
print(
    f"  effluent TSS rises {1000 * coefficients[0]:.2f}"
    f" mg/L per 1000 m3/h of extra flow"
)
print(
    f"  effluent TSS changes {1000 * coefficients[1]:+.3f}"
    f" mg/L per 1000 m3/h of extra air"
)

flow_only = (window.flow @ window.tss) / (window.flow @ window.flow)
first_hour = window.iloc[2]  # the hour the storm began
print(
    f"flow alone, no air term: {1000 * flow_only:.2f} mg/L per 1000 m3/h, "
    f"explaining {flow_only * first_hour['flow']:.1f}"
    f" of the {first_hour['tss']:.1f} mg/L rise"
)
parts = pd.Series(
    {
        "from the extra flow": coefficients[0] * first_hour["flow"],
        "from the change in air": coefficients[1] * first_hour["air"],
    }
)
parts["unexplained"] = first_hour["tss"] - parts.sum()
parts["observed rise in TSS"] = first_hour["tss"]
print("\nthe storm's first hour, against the same hour yesterday  [mg/L]")
print(parts.round(1).to_string())
```

```output
fitted on 18 hourly differences from the storm's own day
  effluent TSS rises 13.51 mg/L per 1000 m3/h of extra flow
  effluent TSS changes -6.426 mg/L per 1000 m3/h of extra air
flow alone, no air term: 14.62 mg/L per 1000 m3/h, explaining 26.5 of the 28.2 mg/L rise

the storm's first hour, against the same hour yesterday  [mg/L]
from the extra flow       24.5
from the change in air     2.7
unexplained                0.9
observed rise in TSS      28.2
```

Regress the change in TSS on the change in flow and the change in air, over the storm's
own day. The flow coefficient is **13.51** mg/L per 1000 m³/h of extra flow. Applied to
the first hour's extra 1,816 m³/h it accounts for **24.5** of the **28.2** mg/L rise. The
air term accounts for **2.7**, and **0.9** is unexplained.

Be careful about what the **2.7** means. The air coefficient is **-6.426** per 1000 m³/h,
so the *fall* in air in that hour is credited with a rise in solids. There is no plausible
mechanism by which less aeration raises effluent TSS within the hour; the regression is
giving a small share to a variable that happened to move in the same hour as the flow
surge. Drop it, and flow alone, at **14.62** mg/L per 1000 m³/h, explains **26.5** of the
**28.2**. So the honest range for the flow's share is 24.5 to 26.5 mg/L, between 87 and 94
per cent, and the dose's is somewhere between nothing and 2.7. The *ordering* is not in
doubt. The precise split is.

## The dose did respond — afterwards

```python
after = excess.loc[
    storm_start + pd.Timedelta(hours=3) : storm_start + pd.Timedelta(hours=15)
]
print(
    f"extra air over the following {len(after)} hours   : {after.air.sum():,.0f}"
    f" m3/h-hours"
)
storm_and_after = excess.loc[storm_start : storm_start + pd.Timedelta(hours=15)]
print(
    "extra power over the storm and after      : "
    f"{storm_and_after.power.sum():,.0f} kWh"
)
low_ammonia = storm_and_after.nh4_out
print(
    f"hours in which effluent NH4 ran below yesterday's by more than 0.1 mg/L: "
    f"{int((low_ammonia < -0.1).sum())} of {len(low_ammonia)}"
)

fig, axes = plt.subplots(3, 1, figsize=(11, 8.6), sharex=True)
for ax, (column, label, colour) in zip(
    axes,
    (
        ("flow", "lift-station flow  [m3/h]", "#1f77b4"),
        ("tss", "effluent TSS  [mg/L]", "#d95f02"),
        ("air", "air flow  [m3/h]", "#2ca02c"),
    ),
    strict=True,
):
    ax.bar(around.index, around[column], color=colour, width=0.8)
    ax.axhline(0, color="#444444", linewidth=0.8)
    ax.set_ylabel(f"{label}\\nchange vs yesterday", fontsize=8)
axes[0].set_title(
    "What changed against the same hour yesterday, hour by hour after the storm began",
    loc="left",
    fontweight="bold",
)
axes[-1].set_xlabel("hours after the storm began (0 = 12:00 UTC on 27 September)")
stamp(
    axes[-1],
    hourly["air"],
    window="18 h",
    source="reading_1m, held, hourly means",
    extra="baseline: the same hour on the previous day",
    expected_s=3600,
)
fig.subplots_adjust(hspace=0.12)
save(fig, "11_excess")
```

```output
extra air over the following 13 hours   : 675 m3/h-hours
extra power over the storm and after      : 176 kWh
hours in which effluent NH4 ran below yesterday's by more than 0.1 mg/L: 16 of 16
```

The dose did respond, and the plant paid for it later. Over the thirteen hours from hour
3 to hour 15 the blowers ran **675** m³/h-hours above yesterday, and the storm and its
aftermath cost **176** kWh in all. Effluent ammonia ran below yesterday's by more than
0.1 mg/L in **16** of the 16 hours after the storm began.

That last line is the part of the story this notebook can tell but cannot split. The
ammonia was lower because the storm diluted what came in *and* because the plant then
aerated harder for half a day. Both effects are real, both are in the same 16 hours, and
there are 16 numbers to separate them with. That is not an attribution problem, it is a
data problem.

## What this cannot tell you

* **Ammonia.** Two causes overlap in time, as above.
* **The coefficient is identified by three hours.** Eighteen rows went into the
  regression, but after the third the flow difference is zero. A different storm — longer,
  or with the blowers held fixed — would say more.
* **One event.** Every number here is about a single storm on a single day.
* **The counterfactual is not free.** "Same hour yesterday" is good where yesterday was
  calm, which is why the storm hours can be read and the plant's first day could not.
* **Causation here rests on timing and a mechanism, not an intervention.** Nobody held
  the blowers constant while the flow was varied.

## Takeaways

- **Build the counterfactual, then check it where you use it.** Against the same hour
  yesterday the six pre-storm hours differ by **0.00** on flow and TSS, and by **36.04**
  on air; its worst miss, **4,127** m³/h, is where yesterday was the start-up.
- **Flow, not dose, explains the solids.** Between **24.5** and **26.5** of the **28.2**
  mg/L rise in effluent TSS, whichever way the regression is written.
- **A regression will hand a share to anything that moved at the same time.** The air
  term's **2.7** comes from a coefficient of **-6.426** identified by three hours, and has no
  mechanism behind it.
- **The storm is what makes the split possible.** Flow and air flow correlate only
  **-0.330** across the week, because the storm moved one and not the other.
- **Say what you cannot split.** Effluent ammonia was low for **16** of 16 hours after the
  storm for two overlapping reasons, and the data cannot apportion them.

## Exercises

1. **Leave one hour out.** Refit the attribution dropping each of the three hours in which
   the flow moved. How far does the flow's share move, and which hour carries the air term?
2. **Run a placebo.** Repeat the whole analysis on a calm day, pretending the storm began
   at 12:00 on 26 September. What should the coefficients be, and what are they?
3. **A better counterfactual.** Replace "yesterday" with the median profile of the four
   calm days. Which of the pre-storm differences shrink, and does the attribution move?
4. **Price the storm.** Turn the extra power and the extra air into money, using a tariff
   you state, and compare it with what the solids excursion cost. Which number is larger,
   and which one would you have missed?

---

**Back to the series** [README](README.md) ·
**Previous:** [10 — Change points](10-change-points.ipynb)
