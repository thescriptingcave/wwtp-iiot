---
title: "01 — Meet the plant"
subtitle: "What is instrumented here, what it does, and two facts that only appear when you compute them"
---

# 01 — Meet the plant

## The question

> You have been handed a database and asked to answer a question about a
> wastewater treatment plant. Before you write a query: what is actually here, and
> what does it mean?

This notebook is the orientation. It is the one you may not need, and it is the one
you will come back to when a number surprises you.

It ends with two findings that change how you work — one that would put a spurious
`1.0000` into a correlation matrix, and one that means a regression you fit on the
full signal list is not the model you think it is.

## Setup

```python
%matplotlib inline
import matplotlib.pyplot as plt
import pandas as pd

from notebooks._data import connect, storm_window
from notebooks._style import apply_style, save, stamp, trend

apply_style()
pd.set_option("display.width", 130)

conn = connect()

readings = pd.read_sql(
    """
    SELECT signal_id, ts, value, quality
    FROM reading
    WHERE ts >= (SELECT max(ts) FROM reading) - interval '7 days'
    """,
    conn,
)
signals = pd.read_sql("SELECT * FROM signal ORDER BY area, id", conn)
print(f"{len(readings):,} readings across {readings.signal_id.nunique()} signals")
```

```output
4,239,284 readings across 57 signals
```

The other tables, and what each is for:

```python
counts = pd.read_sql(
    """
    SELECT 'reading' AS table_name, count(*) AS rows FROM reading
    UNION ALL SELECT 'reading_1m', count(*) FROM reading_1m
    UNION ALL SELECT 'reading_1h', count(*) FROM reading_1h
    UNION ALL SELECT 'signal', count(*) FROM signal
    UNION ALL SELECT 'equipment', count(*) FROM equipment
    UNION ALL SELECT 'event', count(*) FROM event
    UNION ALL SELECT 'site', count(*) FROM site
    ORDER BY rows DESC
    """,
    conn,
)
counts["what it is"] = [
    "every change, as it happened",
    "continuous aggregate, 1-minute buckets",
    "continuous aggregate, 1-hour buckets",
    "one row per tag: unit, range, deadband, sample rate",
    "one row per physical asset, with its failure modes",
    "alarms and state changes",
    "one row — this is one plant, not many",
]
print(counts.to_string(index=False))
```

```output
table_name    rows                                          what it is
   reading 4239284                        every change, as it happened
reading_1m  185455              continuous aggregate, 1-minute buckets
reading_1h    5882                continuous aggregate, 1-hour buckets
    signal      57 one row per tag: unit, range, deadband, sample rate
 equipment      22  one row per physical asset, with its failure modes
      site       1                            alarms and state changes
     event       0               one row — this is one plant, not many
```

**Seven tables, 57 tags, 22 pieces of equipment, one week.** The `event` table is
**empty**, and that is not a finding about the plant: alarms are written by the
alarm engine as it consumes the *live* stream, and the seeder writes readings only.
Anything in these notebooks that would like an alarm history has to derive one from
the readings, which is what notebook 09 does.

## The treatment train

Wastewater treatment is a sequence of tanks, and the `area` column is that sequence.
Water enters at `INFLUENT`, solids are settled out at `PRIMARY`, the biological
step happens in `AERATION`, the flocs are settled at `SECONDARY`, and what is left
is `EFFLUENT`. `SLUDGE` handles what came out, `UTILITY` is what it costs, `SITE` is
the weather.

```python
areas = signals.groupby("area").agg(
    tags=("id", "size"),
)
areas["equipment"] = (
    pd.read_sql("SELECT area, count(*) AS n FROM equipment GROUP BY area", conn)
    .set_index("area")
    .n
)
areas = areas.fillna(0).astype({"equipment": int})
print(areas.sort_values("tags", ascending=False).to_string())
```

```output
           tags  equipment
area                      
AERATION     14          7
INFLUENT     11          6
EFFLUENT      9          2
SLUDGE        9          3
PRIMARY       5          2
SECONDARY     4          2
SITE          4          0
UTILITY       1          0
```

`SITE` and `UTILITY` have no equipment because they are not machines: the weather
is not a pump, and the power meter is not a blower. `UTILITY:SITE:PLANT_POWER` is
the plant's total electrical draw — the single most useful non-process signal here,
because it is the one thing that responds to *every* process decision at once.

## What the plant does

A weekly hourly mean, because a raw reading count tells you about the historian
and not about the plant. The shaded band is the storm this seed contains, which the
next section is about:

```python
hourly = (
    readings.set_index("ts")
    .groupby("signal_id")
    .value.resample("1h")
    .mean()
    .unstack("signal_id")
)
storm_start, storm_end = (pd.Timestamp(t) for t in storm_window())

fig, ax = plt.subplots(figsize=(11, 4.5))
trend(ax, {"influent flow": hourly["INFLUENT:FLOW:FLOW"]}, "INFLUENT:FLOW:FLOW")
ax.axvspan(storm_start, storm_end, color="#d62728", alpha=0.18, label="storm")
ax.legend(loc="upper left")
stamp(
    ax,
    hourly["INFLUENT:FLOW:FLOW"],
    window="1 week",
    source="reading (1 h buckets)",
    expected_s=1,
)
ax.set_title("The water arriving — one week, one storm", loc="left")
save(fig, "01_influent")

influent = hourly["INFLUENT:FLOW:FLOW"].mean()
effluent = hourly["EFFLUENT:FLOW:FLOW"].mean()
nh4_in = hourly["INFLUENT:FLOW:NH4_IN"].mean()
nh4_out = hourly["EFFLUENT:FLOW:NH4"].mean()
print(
    f"influent  {influent:.0f} m3/h mean, "
    f"{hourly['INFLUENT:FLOW:FLOW'].min():.0f}-{hourly['INFLUENT:FLOW:FLOW'].max():.0f}"
)
print(f"effluent  {effluent:.0f} m3/h mean")
print(
    f"balance   effluent is {influent - effluent:.0f} m3/h "
    f"({100 * (influent - effluent) / influent:.1f} %) below influent"
)
print(
    f"ammonia   {nh4_in:.1f} mg/L in, {nh4_out:.1f} mg/L out, "
    f"{100 * (1 - nh4_out / nh4_in):.0f} % removed"
)
print(f"nitrate   {hourly['AERATION:AHU-1:NO3_OUT'].mean():.1f} mg/L")
print(f"air       {hourly['AERATION:AHU-1:AIR_FLOW'].mean():.0f} m3/h")
print(
    f"power     {hourly['UTILITY:SITE:PLANT_POWER'].mean():.0f} kW mean, "
    f"peak {hourly['UTILITY:SITE:PLANT_POWER'].max():.0f} kW"
)
```

```output
influent  1798 m3/h mean, 1265-3996
effluent  1708 m3/h mean
balance   effluent is 90 m3/h (5.0 %) below influent
ammonia   21.8 mg/L in, 7.9 mg/L out, 64 % removed
nitrate   11.2 mg/L
air       6188 m3/h
power     697 kW mean, peak 2032 kW
```

The chemistry is the plant's actual job. Ammonia comes in at **21.8 mg/L** and
leaves at **7.9 mg/L** — **64 % removed** — by bacteria in the aeration basin
converting it to nitrogen gas, which is what `AERATION:AHU-1:NO3_OUT` (**11.2 mg/L**
of nitrate) is the other half of. The blower moves **6188 m³/h** of air to do it,
and the whole plant draws **697 kW** on average, peaking at **2032 kW**.

**Effluent flow is 90 m³/h below influent flow, a 5.0 % deficit.** Water is not
consumed by a treatment plant. So either the two flowmeters do not agree, or the
plant model does not close a mass balance. Either way, do not build a mass balance
on these two numbers without checking which it is.

## Two regimes: a storm, and a daily cycle

Most of the week is one regime and two hours of it are another, and the two need
separating before anything else is measured.

**The storm** first, because it is the loud one. The seed arms a wet-weather event
thirty-six hours before the end of the window:

```python
flow = hourly["INFLUENT:FLOW:FLOW"].dropna()
lift = hourly["INFLUENT:LIFT:FLOW"].dropna()


def in_storm(s):
    return s[(s.index >= storm_start) & (s.index < storm_end)]


a_day_earlier = lift[
    (lift.index >= storm_start - pd.Timedelta(days=1))
    & (lift.index < storm_end - pd.Timedelta(days=1))
]

tracer = readings[readings.signal_id == "INFLUENT:FLOW:CONDUCTIVITY"]
meter = readings[readings.signal_id == "INFLUENT:FLOW:FLOW"]
meter_in_storm = meter[(meter.ts >= storm_start) & (meter.ts < storm_end)]
nh4 = readings[readings.signal_id == "INFLUENT:FLOW:NH4_IN"]
nh4_storm = nh4[(nh4.ts >= storm_start) & (nh4.ts < storm_end)]

print(f"storm window      {storm_start:%Y-%m-%d %H:%M} to {storm_end:%H:%M} UTC")
print(f"influent flow     {in_storm(flow).max():.0f} m3/h at the peak hour")
print(f"same hour, day before, lift station: {a_day_earlier.iloc[0]:.0f} m3/h")
print(
    f"flow meter        {len(meter_in_storm):,} of {len(meter):,} readings "
    f"({100 * len(meter_in_storm) / len(meter):.1f} %) fall in "
    f"{100 * 2 / 168:.1f} % of the week"
)
print(
    f"influent NH4      {nh4_storm.value.mean():.1f} mg/L in the storm, "
    f"{nh4[~nh4.index.isin(nh4_storm.index)].value.mean():.1f} outside it"
)
print(
    f"conductivity      {len(tracer)} readings all week, "
    f"{int(((tracer.ts >= storm_start) & (tracer.ts < storm_end)).sum())} in the storm"
)
```

```output
storm window      2026-09-27 12:00 to 14:00 UTC
influent flow     3996 m3/h at the peak hour
same hour, day before, lift station: 2012 m3/h
flow meter        1,162 of 3,733 readings (31.1 %) fall in 1.2 % of the week
influent NH4      14.1 mg/L in the storm, 22.4 outside it
conductivity      357 readings all week, 355 in the storm
```

**Influent flow doubles at the peak hour: 3996 m³/h against 2012 the day
before.** And the storm *dilutes* the influent — ammonia falls to **14.1 mg/L** against **22.4** outside it —
so the plant's hydraulic load spikes at the same moment its pollutant load per litre
drops. That is the whole reason a storm is hard to see with one threshold, and hard
to average: two things a naive analysis treats as one move in opposite directions.

The line to hold on to is the third. **31.1 % of the flow meter's readings come from
1.2 % of the week.** The historian stores a row when the value changes, so a signal
that moves constantly for two hours writes almost a third of its week in them. That
is not a curiosity about this meter; it is the mechanism behind every disagreement
between averages later in this notebook, and notebook 04 is about it.

`INFLUENT:FLOW:CONDUCTIVITY` is the scenario's declared cheap tracer, and it earns
the description: **357** readings across the week, **355** of them inside the storm. A
signal that says almost nothing for six days and speaks for two hours is exactly what
change-triggered storage is for, and exactly what `dropna` and `ffill` cannot
distinguish from a dead sensor — notebook 02.

**The daily cycle** is the other regime, and it is only visible once the storm is
out of the way. Removing it, and the three hours after it while the plant settles:

```python
calm = flow[
    (flow.index < storm_start) | (flow.index >= storm_end + pd.Timedelta(hours=3))
]
profile = calm.groupby(calm.index.hour).median()

print(f"hours of the week with a flow reading : {len(flow)} of 168")
print(f"hours left once the storm is removed  : {len(calm)}")
print(f"peak   {profile.idxmax():02d}:00 UTC  {profile.max():.0f} m3/h")
print(f"trough {profile.idxmin():02d}:00 UTC  {profile.min():.0f} m3/h")
print(f"ratio  {profile.max() / profile.min():.2f}x")

fig, ax = plt.subplots(figsize=(11, 3.8))
ax.bar(profile.index, profile.values, color="#1f77b4", width=0.85)
ax.set_xticks(range(0, 24))
ax.set_xlabel("hour of day (UTC)")
ax.set_ylabel("influent flow  [m3/h]")
ax.set_title("One day of influent flow — median by hour, storm removed", loc="left")
stamp(
    ax,
    calm,
    window="1 week less the storm",
    source="reading (1 h buckets)",
    expected_s=1,
    extra="hours 11-14 have no reading: the flow was steady",
)
save(fig, "01_diurnal")
```

```output
hours of the week with a flow reading : 143 of 168
hours left once the storm is removed  : 138
peak   07:00 UTC  2206 m3/h
trough 00:00 UTC  1265 m3/h
ratio  1.74x
```

**A 1.74× swing, peaking at 07:00 and bottoming at 00:00.** Four hours of the day
(11:00 to 14:00) have no bar at all, and that is not a plotting defect: the flow was
inside its deadband for those hours on every calm day, so the historian wrote
nothing. Notebook 02 is the difference between that and a dead meter.

Read the *hour* with suspicion. The seeder starts the cycle when the window starts,
and this window starts at midnight, so "peaks at 07:00" describes the generator's
convention and not a catchment. An earlier seed of this same repository, started
fifteen hours later in the day, peaked fifteen hours later — the shape is the
plant's and the phase is the seeder's. Report the amplitude, and be wary of anyone
who reports the hour.

Hold on to the *shape*. It is the reason notebooks 05 through 11 are hard: one
shared daily cycle drives nearly every signal in the plant, so a model that has
learned the calendar will score well and predict nothing.

## Two things that change how you work

Neither is visible from the schema, and both are one query away.

### One. Nine pairs of signals are one measurement

The obvious way to find these is to be told about them. The useful way is to sweep
for them, because nobody is going to tell you:

```python
import itertools

ids = sorted(readings.signal_id.unique())
series = {
    sig: readings[readings.signal_id == sig].set_index("ts").value.sort_index()
    for sig in ids
}

duplicates = []
for left, right in itertools.combinations(ids, 2):
    a, b = series[left], series[right]
    shared = a.index.intersection(b.index)
    if len(shared) < 200:
        continue
    if (a.reindex(shared) - b.reindex(shared)).abs().max() == 0:
        duplicates.append((left, right, len(shared), len(a), len(b)))

print(f"pairs of tags with identical values on every shared reading: {len(duplicates)}")
for left, right, shared, len_a, len_b in duplicates:
    print(f"  shared {shared:>7}   rows {len_a:>7} / {len_b:>7}   {left}  ==  {right}")
```

```output
pairs of tags with identical values on every shared reading: 9
  shared     702   rows     702 /     702   AERATION:AHU-1:NH4_OUT  ==  EFFLUENT:FLOW:NH4
  shared  128373   rows  128373 /  128373   AERATION:AHU-1:PH  ==  EFFLUENT:FLOW:PH
  shared    2027   rows    2027 /    2027   AERATION:AHU-1:WTEMP  ==  EFFLUENT:FLOW:TEMP
  shared    2027   rows    2027 /    2027   AERATION:AHU-1:WTEMP  ==  INFLUENT:FLOW:TEMP
  shared    2027   rows    2027 /    2027   AERATION:AHU-1:WTEMP  ==  SECONDARY:SEC-CL-1:TEMP
  shared    2027   rows    2027 /    2027   EFFLUENT:FLOW:TEMP  ==  INFLUENT:FLOW:TEMP
  shared    2027   rows    2027 /    2027   EFFLUENT:FLOW:TEMP  ==  SECONDARY:SEC-CL-1:TEMP
  shared  125886   rows  125887 /  128079   EFFLUENT:FLOW:TSS  ==  SECONDARY:SEC-CL-1:OVERFLOW
  shared    2027   rows    2027 /    2027   INFLUENT:FLOW:TEMP  ==  SECONDARY:SEC-CL-1:TEMP
```

**Six of those nine pairs are the same four water temperatures.**
`INFLUENT:FLOW:TEMP`, `AERATION:AHU-1:WTEMP`, `SECONDARY:SEC-CL-1:TEMP` and
`EFFLUENT:FLOW:TEMP` are not four tanks that happen to agree — they are
**2,027 identical readings** under four names, and every pair among them is
byte-for-byte equal.

The other three are the same story at two tags each:

* `AERATION:AHU-1:NH4_OUT` and `EFFLUENT:FLOW:NH4` — **702** identical readings.
  Ammonia leaves the aeration basin and enters the disinfection tank, and nothing
  in between changes it.
* `AERATION:AHU-1:PH` and `EFFLUENT:FLOW:PH` — **128,373** identical readings.
* `EFFLUENT:FLOW:TSS` and `SECONDARY:SEC-CL-1:OVERFLOW` — the secondary
  clarifier's overflow solids and the effluent's suspended solids: **125,886**
  shared readings, all identical.

None of this is a defect. It is what a real address space does — a tag per stage,
whether or not that stage changes the value, so that the tag list reads the way
the process diagram does. But the consequence is mechanical:

> **A correlation matrix over all 57 tags contains off-diagonal entries of exactly
> 1.0000, and a regression given two of these columns is perfectly collinear.**

Neither is a fact about the plant. Both are an artefact of the tag list, and
**nothing in the schema distinguishes a duplicate tag from an independent one.**
You have to compute it, which is why the sweep above is nine lines and not one
metadata flag.

One detail worth noticing: `EFFLUENT:FLOW:TSS` has 125,887 rows against
`SECONDARY:SEC-CL-1:OVERFLOW`'s 128,079. They agree on everything they share, and
then one of them keeps going. So "identical on the overlap" is not the same as
"the same series" — check the row counts as well as the values.

### Two. With the duplicates gone, the plant is still nearly one signal

Drop the redundant tags and ask how independent the remaining 51 really are:

```python
redundant = {
    "EFFLUENT:FLOW:TEMP",  # identical to three other temperatures
    "SECONDARY:SEC-CL-1:TEMP",
    "AERATION:AHU-1:WTEMP",
    "EFFLUENT:FLOW:PH",  # identical to AERATION:AHU-1:PH
    "EFFLUENT:FLOW:NH4",  # identical to AERATION:AHU-1:NH4_OUT
    "SECONDARY:SEC-CL-1:OVERFLOW",  # identical to EFFLUENT:FLOW:TSS
}
kept = [c for c in hourly.columns if c not in redundant]

pairs = []
for left, right in itertools.combinations(kept, 2):
    both = hourly[[left, right]].dropna()
    if len(both) < 60:
        continue
    pairs.append((left, right, both[left].corr(both[right]), len(both)))

print(f"dropped {len(redundant)}, kept {len(kept)} of {hourly.shape[1]} signals")
print(f"pairs with at least 60 shared hourly means: {len(pairs)}")
for threshold in (0.99, 0.95, 0.90):
    strong = [p for p in pairs if abs(p[2]) > threshold]
    print(
        f"  |r| > {threshold:.2f} : {len(strong):3} of {len(pairs)} "
        f"({100 * len(strong) / len(pairs):4.1f}%)"
    )

print()
for left, right, value, n in sorted(pairs, key=lambda p: -abs(p[2]))[:6]:
    print(f"  {value:+.6f}  n={n:>3}  {left:26} {right}")
```

```output
dropped 6, kept 51 of 57 signals
pairs with at least 60 shared hourly means: 547
  |r| > 0.99 :  10 of 547 ( 1.8%)
  |r| > 0.95 :  20 of 547 ( 3.7%)
  |r| > 0.90 :  23 of 547 ( 4.2%)

  +0.999976  n=158  INFLUENT:FLOW:TEMP         SITE:WEATHER:AIR_TEMP
  +0.999816  n=168  AERATION:AHU-1:AIR_FLOW    AERATION:AHU-1:BLOWER_VALVE
  +0.999705  n=168  AERATION:AHU-1:AIR_FLOW    UTILITY:SITE:PLANT_POWER
  +0.999503  n=168  AERATION:AHU-1:BLOWER_VALVE UTILITY:SITE:PLANT_POWER
  +0.998885  n=130  INFLUENT:LIFT:RUNTIME      INFLUENT:LIFT:STARTS
  +0.996615  n=161  EFFLUENT:FLOW:TSS          EFFLUENT:FLOW:TURBIDITY
```

**Ten of 547 independent pairs correlate above 0.99**, after the duplicates are
removed. That is a small share of the pairs and a striking one: these are
correlations most analysts would stop at. Before believing any of them, ask the
question this dataset makes unavoidable — *is it the plant, or is it the clock?*
Everything in the plant rises and falls with the day, so two signals can agree only
because both are reading it.

The test is cheap. Subtract each signal's own hour-of-day profile (from the storm-free
hours, so the storm does not leak into the baseline) and correlate what is left:

```python
storm_free = hourly[
    (hourly.index < storm_start) | (hourly.index >= storm_end + pd.Timedelta(hours=3))
]


def without_the_clock(series):
    profile = storm_free[series.name].groupby(storm_free.index.hour).mean()
    return series - series.index.hour.map(profile).to_numpy()


print(f"{'pair':62} {'raw r':>9} {'clock removed':>14}")
for left, right, *_ in sorted(pairs, key=lambda p: -abs(p[2]))[:6]:
    both = storm_free[[left, right]].dropna()
    kept = pd.concat(
        [without_the_clock(both[left]), without_the_clock(both[right])], axis=1
    ).dropna()
    print(
        f"{left + '  x  ' + right:62} "
        f"{both[left].corr(both[right]):+9.4f} "
        f"{kept.iloc[:, 0].corr(kept.iloc[:, 1]):+14.4f}"
    )
```

```output
pair                                                               raw r  clock removed
INFLUENT:FLOW:TEMP  x  SITE:WEATHER:AIR_TEMP                     +1.0000        +0.9786
AERATION:AHU-1:AIR_FLOW  x  AERATION:AHU-1:BLOWER_VALVE          +0.9998        +0.9999
AERATION:AHU-1:AIR_FLOW  x  UTILITY:SITE:PLANT_POWER             +0.9998        +0.9997
AERATION:AHU-1:BLOWER_VALVE  x  UTILITY:SITE:PLANT_POWER         +0.9996        +0.9995
INFLUENT:LIFT:RUNTIME  x  INFLUENT:LIFT:STARTS                   +0.9989        +0.9904
EFFLUENT:FLOW:TSS  x  EFFLUENT:FLOW:TURBIDITY                    +0.9996        +0.9265
```

**Two different things are in that table, and they separate cleanly.**

The air chain **survives**. Air flow against blower valve stays at **+0.9999** with
the clock removed, and the valve against plant power at **+0.9995**. That is not a
shared day. It is a valve opening, air moving and a motor drawing power — a causal
chain, visible in the data as a correlation that cannot be subtracted away.

Other pairs **lose** most of what they had. Effluent TSS against turbidity falls
from **+0.9996** to **+0.9265**: two instruments watching the same solids share a
clock, and most of what looked like agreement was the two of them riding the day.

> **Remove the hour of day before you believe a correlation.** If it survives, you
> have found coupling. If it collapses, you found a calendar. Notebook 06 does this
> properly, with a detrended series and a difference, and notebook 05 says how many
> independent observations any of it rests on.

**One formatting note that turns out to matter.** Look at the decimal places. The
duplicate temperature pair printed as `+1.000000`; the strongest real pair here
prints as `+0.999976`. Those are a few parts in a hundred thousand apart and a correlation
table rounded to two decimals cannot tell them apart:

```python
duplicate = hourly[["INFLUENT:FLOW:TEMP", "AERATION:AHU-1:WTEMP"]].dropna()
genuine = hourly[["INFLUENT:FLOW:TEMP", "SITE:WEATHER:AIR_TEMP"]].dropna()

print(f"the duplicate pair   : {duplicate.iloc[:, 0].corr(duplicate.iloc[:, 1]):.2f}")
print(f"the real relationship: {genuine.iloc[:, 0].corr(genuine.iloc[:, 1]):.2f}")
```

```output
the duplicate pair   : 1.00
the real relationship: 1.00
```

**Both print `1.00`.** One is a tag list artefact and the other is the weather
passing through a tank, and at two decimal places they are indistinguishable. Print
correlations to enough digits that identity and near-identity are different
strings, and when you find one, go and check *why*.

## Two averages of one column, and a 19 % disagreement

The plant's mean blower position looks like a one-liner. Try it twice:

```python
valve = readings[readings.signal_id == "AERATION:AHU-1:BLOWER_VALVE"]

raw_mean = valve.value.mean()
hourly_mean = hourly["AERATION:AHU-1:BLOWER_VALVE"].mean()

print(f"mean over raw readings : {raw_mean:6.2f} %   (n={len(valve):,})")
print(
    f"mean of hourly means   : {hourly_mean:6.2f} %   "
    f"(n={int(hourly['AERATION:AHU-1:BLOWER_VALVE'].count())})"
)
print(
    f"difference             : {hourly_mean - raw_mean:+6.2f} "
    f"({100 * (hourly_mean - raw_mean) / raw_mean:+.1f} %)"
)
```

```output
mean over raw readings :  29.60 %   (n=264,835)
mean of hourly means   :  23.93 %   (n=168)
difference             :  -5.66 (-19.1 %)
```

**Two averages of the same column, 5.66 percentage points apart — 19 % of the
value.** Neither is a bug and neither is wrong; they answer different questions.

The historian is **change-triggered**: it stores a row when the value *differs*,
not on a schedule. So the raw mean is a mean over a sample that is dense exactly
when the blower valve is moving, and the valve moves most when the plant is
working hardest. Averaging hourly instead treats each hour equally.

> **Neither number is "the mean".** The raw mean over-weights active periods; the
> hourly mean over-weights quiet ones. Which is right depends on whether you want
> the average valve position or the average valve position *weighted by when the
> valve was asked to do something*. Notebook 07 makes that choice explicit for
> mass, where it changes the answer by more than 19 %.

And this is not an unlucky signal. Across the whole tag list, ranking by relative
disagreement between the two averages:

```python
disagreements = []
for sig in readings.signal_id.unique():
    series = readings[readings.signal_id == sig].value.dropna()
    buckets = hourly[sig].dropna()
    if len(series) < 1000 or len(buckets) < 60:
        continue
    difference = buckets.mean() - series.mean()
    disagreements.append(
        (
            abs(difference) / abs(series.mean()) if series.mean() else 0.0,
            sig,
            series.mean(),
            buckets.mean(),
            len(series),
            len(buckets),
        )
    )

for _, sig, raw, bucketed, n_raw, n_h in sorted(disagreements, reverse=True)[:6]:
    print(
        f"  {sig:28} {raw:9.2f} -> {bucketed:9.2f}  "
        f"{100 * (bucketed - raw) / raw:+6.1f}%   "
        f"(n={n_raw:>7,} raw, {n_h:>3} hours)"
    )
```

```output
  INFLUENT:FLOW:FLOW             2457.45 ->   1798.04   -26.8%   (n=  3,733 raw, 143 hours)
  AERATION:AHU-1:BLOWER_VALVE      29.60 ->     23.93   -19.1%   (n=264,835 raw, 168 hours)
  INFLUENT:LIFT:WETWELL_LEVEL       3.09 ->      3.44   +11.2%   (n= 22,197 raw, 138 hours)
  EFFLUENT:FLOW:TURBIDITY          10.21 ->      9.31    -8.8%   (n=128,511 raw, 168 hours)
  EFFLUENT:FLOW:TSS                18.49 ->     17.12    -7.4%   (n=125,887 raw, 161 hours)
  SECONDARY:SEC-CL-1:OVERFLOW      18.51 ->     17.14    -7.4%   (n=128,079 raw, 162 hours)
```

**Six signals disagree by 7 % or more**, and the worst is not the blower valve. It is
the influent flow meter, at **-26.8 %**: a raw mean of 2457 m³/h against an hourly
mean of 1798.

That is the storm from the previous section arriving as arithmetic. The meter writes
**31.1 %** of its readings in the two hours of the storm, so a mean over readings is
a mean weighted towards the storm, and a mean over hours is not. **The more a signal
moves, the more its raw mean over-states its hourly mean** — and the plant's most
important event is precisely when almost everything moves.

Both `SECONDARY:SEC-CL-1:OVERFLOW` and `EFFLUENT:FLOW:TSS` appear, agreeing to
three figures, because they are the same measurement as section one established.

## Takeaways

- **Seven tables, 57 tags, 22 assets, one week.** `signal` is the one to read first:
  it holds `unit`, `normal_low`/`high`, `deadband` and `sample_ms` for every tag,
  and every one of those is a thing you would otherwise hard-code.
- **The `area` column is the process.** `INFLUENT → PRIMARY → AERATION → SECONDARY →
  EFFLUENT`, with `SLUDGE`, `UTILITY` and `SITE` alongside. The ammonia chain
  `NH4_IN → NH4_OUT → EFFLUENT:NH4` is the clearest single story: **21.8 mg/L** in,
  **7.9 mg/L** out, **64 %** removed by bacteria spending **6188 m³/h** of air.
- **There is a storm in the week, and it is most of what is unusual.** Influent flow
  reaches **3996 m³/h** against **2012** a day earlier, and **31.1 %** of the flow
  meter's readings fall in the two hours it lasts. Separate it from the daily cycle
  before measuring either.
- **The daily cycle has a 1.74× swing**, and the *hour* it peaks in belongs to the
  seeder, not the plant. Report the amplitude.
- **Nine pairs of tags are one measurement, and only six tags.** The four water
  temperatures are **2,027** identical readings under four names. Nothing in the
  schema flags a duplicate, so find them before you build a correlation matrix or a
  regression.
- **A correlation printed to two decimals cannot tell a duplicate from a fact**, and
  a correlation of any size cannot tell coupling from a shared clock. Print six
  digits, then remove the hour of day: the air chain stays at **+0.9999**, and TSS
  against turbidity falls to **+0.9265**.
- **Two averages of one column can differ by a quarter.** The influent flow meter
  reads **2457** m³/h over raw readings and **1798** over hours, **-26.8 %**, because
  a change-triggered historian writes most of its rows while the signal moves.

## Exercises

1. **Prove the duplicate.** Write the query that finds *all* duplicate pairs —
   every tag with a second tag sharing its values — rather than the two given here.
   How many did it find that this notebook did not list?
2. **Break the coupling.** The air chain survives removing the hour of day. Find a
   pair that survives *and* is not in the top six, using the `signal` table's
   `equipment_id` to decide in advance which pairs you expect to be causal. How many
   of your predictions hold?
3. **Find the cycle's period.** The signal is daily. Prove it: compute the
   autocorrelation of `INFLUENT:FLOW:FLOW` out to 72 hours and show the peak. Then
   explain why the same plot for a signal sampled at 1 Hz looks nothing like it.
4. **Ask whether the mass balance closes.** Compare total volume in and out over the
   week using `INFLUENT:FLOW:FLOW` and `EFFLUENT:FLOW:FLOW`, flow-weighted rather
   than time-averaged. Does the 4 % deficit survive? Should it?
5. **The tag list is not the measurement list.** `signal` has 57 rows and
   `equipment` has 22, but only some tags measure something a person would ask
   about. Propose a rule that separates them, and say which column you would use.

---

**Next:** [02 — Three kinds of nothing](02-three-kinds-of-nothing.ipynb) ·
**Back to the series** [README](README.md)
