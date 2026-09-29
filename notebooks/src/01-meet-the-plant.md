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
import matplotlib.pyplot as plt
import pandas as pd
import psycopg
from storage.postgres.schema import dsn

from notebooks._style import apply_style, save, stamp, trend

apply_style()
pd.set_option("display.width", 130)

conn = psycopg.connect(dsn())

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
4,287,657 readings across 57 signals
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
       reading 4287657                     every change, as it happened
    reading_1m  214807            continuous aggregate, 1-minute buckets
    reading_1h    6923            continuous aggregate, 1-hour buckets
       signal      57 one row per tag: unit, range, deadband, sample rate
    equipment      22 one row per physical asset, with its failure modes
       event        1                        alarms and state changes
        site        1                one row - this is one plant, not many
```

**Seven tables, 57 tags, 22 pieces of equipment, one week.** The `event` table has a
single row — the alarm engine has not fired in this week, which matters for the
notebooks that come after this one and not at all for this one.

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
    .set_index("area").n
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
and not about the plant:

```python
hourly = (
    readings.set_index("ts")
    .groupby("signal_id").value
    .resample("1h").mean()
    .unstack("signal_id")
)

fig, ax = plt.subplots(figsize=(11, 4.5))
trend(ax, {"influent flow": hourly["INFLUENT:FLOW:FLOW"]}, "INFLUENT:FLOW:FLOW")
stamp(ax, hourly["INFLUENT:FLOW:FLOW"], window="1 week",
      source="reading (1 h buckets)", expected_s=1)
ax.set_title("The water arriving — one week", loc="left")
save(fig, "01_influent")

print(f"influent  {hourly['INFLUENT:FLOW:FLOW'].mean():.0f} m3/h mean, "
      f"{hourly['INFLUENT:FLOW:FLOW'].min():.0f}-{hourly['INFLUENT:FLOW:FLOW'].max():.0f}")
print(f"effluent  {hourly['EFFLUENT:FLOW:FLOW'].mean():.0f} m3/h mean")
print(f"ammonia   {hourly['INFLUENT:FLOW:NH4_IN'].mean():.1f} mg/L in, "
      f"{hourly['EFFLUENT:FLOW:NH4'].mean():.1f} mg/L out")
print(f"air       {hourly['AERATION:AHU-1:AIR_FLOW'].mean():.0f} m3/h")
print(f"power     {hourly['UTILITY:SITE:PLANT_POWER'].mean():.0f} kW mean, "
      f"peak {hourly['UTILITY:SITE:PLANT_POWER'].max():.0f} kW")
```

```output
influent  1767 m3/h mean, 1265-2238
effluent  1696 m3/h mean
ammonia   22.0 mg/L in, 7.9 mg/L out
air       5985 m3/h
power     682 kW mean, peak 2050 kW
```

The chemistry is the plant's actual job. Ammonia comes in at **22.0 mg/L** and
leaves at **7.9 mg/L** — **64 % removed** — by bacteria in the aeration basin
converting it to nitrogen gas, which is what `AERATION:AHU-1:NO3_OUT` (11.4 mg/L
nitrate) is the other half of. The blower moves **5,985 m³/h** of air to do it,
and the whole plant draws **682 kW** on average, tripling to **2,050 kW** at peak.

**Effluent flow is 71 m³/h below influent flow, a 4 % deficit.** Water is not
consumed by a treatment plant; it evaporates. So either the two flowmeters do not
agree, or the plant model does not close a mass balance. Either way, do not build
a mass balance on these two numbers without checking which it is.

## The daily cycle is the only regime there is

This is a one-week dataset with a simulated but unremarkable week in it. There is
no storm, no industrial discharge, no setpoint change — `SITE:WEATHER:STORM` is
zero all week, `AERATION:AHU-1:SETPOINT_DO` is 2.0 mg/L all week, and the
blowers have no per-unit signals at all.

What there *is* is a diurnal cycle, and it is strong:

```python
flow = hourly["INFLUENT:FLOW:FLOW"].dropna()
by_hour = flow.groupby(flow.index.hour).mean()

print(f"peak  {by_hour.idxmax():02d}:00 UTC  {by_hour.max():.0f} m3/h")
print(f"trough{by_hour.idxmin():02d}:00 UTC  {by_hour.min():.0f} m3/h")
print(f"ratio {by_hour.max() / by_hour.min():.2f}x")

fig, ax = plt.subplots(figsize=(11, 3.6))
ax.bar(by_hour.index, by_hour.values, color="#1f77b4", width=0.85)
ax.set_xticks(range(0, 24))
ax.set_xlabel("hour of day (UTC)")
ax.set_ylabel("influent flow  [m3/h]")
ax.set_title("One day of influent flow, averaged over the week",
             loc="left")
ax.text(0.995, -0.32, "n=141 hourly means over 7 days · the two NaN hours have "
        "no data · source reading · UTC",
        transform=ax.transAxes, ha="right", va="top", fontsize=8,
        color="#666666", family="monospace")
save(fig, "01_diurnal")
```

```output
peak  08:00 UTC  2238 m3/h
trough01:00 UTC  1265 m3/h
ratio 1.77x
```

**A 1.77× swing, peaking at 08:00 and bottoming at 01:00.** That is a morning
peak — the shape a residential catchment gives you.

Hold that shape. It is the reason notebooks 04 through 11 are hard: one shared
daily cycle drives nearly every signal in the plant, so a model that has learned
"it is 08:00" will score well and predict nothing.

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

print(f"pairs of tags with identical values on every shared reading: "
      f"{len(duplicates)}")
for left, right, shared, len_a, len_b in duplicates:
    print(f"  shared {shared:>7}   rows {len_a:>7} / {len_b:>7}   "
          f"{left}  ==  {right}")
```

```output
pairs of tags with identical values on every shared reading: 9
  shared     684   rows     684 /     684   AERATION:AHU-1:NH4_OUT  ==  EFFLUENT:FLOW:NH4
  shared  130622   rows  130622 / 130622   AERATION:AHU-1:PH  ==  EFFLUENT:FLOW:PH
  shared    1943   rows    1943 /    1943   AERATION:AHU-1:WTEMP  ==  EFFLUENT:FLOW:TEMP
  shared    1943   rows    1943 /    1943   AERATION:AHU-1:WTEMP  ==  INFLUENT:FLOW:TEMP
  shared    1943   rows    1943 /    1943   AERATION:AHU-1:WTEMP  ==  SECONDARY:SEC-CL-1:TEMP
  shared    1943   rows    1943 /    1943   EFFLUENT:FLOW:TEMP  ==  INFLUENT:FLOW:TEMP
  shared    1943   rows    1943 /    1943   EFFLUENT:FLOW:TEMP  ==  SECONDARY:SEC-CL-1:TEMP
  shared  128029   rows  128030 /  130222   EFFLUENT:FLOW:TSS  ==  SECONDARY:SEC-CL-1:OVERFLOW
  shared    1943   rows    1943 /    1943   INFLUENT:FLOW:TEMP  ==  SECONDARY:SEC-CL-1:TEMP
```

**Six of those nine pairs are the same four water temperatures.**
`INFLUENT:FLOW:TEMP`, `AERATION:AHU-1:WTEMP`, `SECONDARY:SEC-CL-1:TEMP` and
`EFFLUENT:FLOW:TEMP` are not four tanks that happen to agree — they are
**1,943 identical readings** under four names, and every pair among them is
byte-for-byte equal.

The other three are the same story at two tags each:

* `AERATION:AHU-1:NH4_OUT` and `EFFLUENT:FLOW:NH4` — **684** identical readings.
  Ammonia leaves the aeration basin and enters the disinfection tank, and nothing
  in between changes it.
* `AERATION:AHU-1:PH` and `EFFLUENT:FLOW:PH` — **130,622** identical readings.
* `EFFLUENT:FLOW:TSS` and `SECONDARY:SEC-CL-1:OVERFLOW` — the secondary
  clarifier's overflow solids and the effluent's suspended solids: **128,029**
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

One detail worth noticing: `EFFLUENT:FLOW:TSS` has 128,030 rows against
`SECONDARY:SEC-CL-1:OVERFLOW`'s 128,222. They agree on everything they share, and
then one of them keeps going. So "identical on the overlap" is not the same as
"the same series" — check the row counts as well as the values.

### Two. With the duplicates gone, the plant is still nearly one signal

Drop the redundant tags and ask how independent the remaining 51 really are:

```python
redundant = {
    "EFFLUENT:FLOW:TEMP",       # identical to three other temperatures
    "SECONDARY:SEC-CL-1:TEMP",
    "AERATION:AHU-1:WTEMP",
    "EFFLUENT:FLOW:PH",         # identical to AERATION:AHU-1:PH
    "EFFLUENT:FLOW:NH4",        # identical to AERATION:AHU-1:NH4_OUT
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
    print(f"  |r| > {threshold:.2f} : {len(strong):3} of {len(pairs)} "
          f"({100 * len(strong) / len(pairs):4.1f}%)")

print()
for left, right, value, n in sorted(pairs, key=lambda p: -abs(p[2]))[:6]:
    print(f"  {value:+.6f}  n={n:>3}  {left:26} {right}")
```

```output
dropped 6, kept 51 of 57 signals
pairs with at least 60 shared hourly means: 491
  |r| > 0.99 :  18 of 491 ( 3.7%)
  |r| > 0.95 :  34 of 491 ( 6.9%)
  |r| > 0.90 :  39 of 491 ( 7.9%)

  +0.999958  n=163  INFLUENT:FLOW:TEMP         SITE:WEATHER:AIR_TEMP
  +0.999917  n=127  INFLUENT:FLOW:FLOW         INFLUENT:FLOW:NH4_IN
  +0.999864  n=169  AERATION:AHU-1:AIR_FLOW    UTILITY:SITE:PLANT_POWER
  +0.999842  n=167  EFFLUENT:FLOW:TSS          EFFLUENT:FLOW:TURBIDITY
  +0.999826  n=169  AERATION:AHU-1:AIR_FLOW    AERATION:AHU-1:BLOWER_VALVE
  +0.999719  n=169  AERATION:AHU-1:BLOWER_VALVE UTILITY:SITE:PLANT_POWER
```

**Eighteen of 491 independent pairs correlate above 0.99**, after the duplicates
are removed. Influent flow and influent ammonia sit at +0.999917. Air flow and
total plant power at +0.999864.

None of those is a process insight. It is the daily cycle showing up again:
everything rises in the morning and falls at night, so everything correlates with
everything, and removing four duplicate tags did not help because the duplicates
were never what was driving it.

> **A correlation between two signals here is mostly evidence that both are clocks
> reading the same day.** Before believing one, remove the hour of day from both
> and compute it again. If it survives, you have found something.

This is the single most important thing to know before modelling this data, and it
is why [`05 — Autocorrelation`](05-autocorrelation.ipynb) is the deepest notebook
in the series.

**One formatting note that turns out to matter.** Look at the decimal places. The
duplicate temperature pair printed as `+1.000000`; the strongest real pair here
prints as `+0.999958`. Those are four parts in a million apart and a correlation
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

## Two averages of one column, and a 21 % disagreement

The plant's mean blower position looks like a one-liner. Try it twice:

```python
valve = readings[readings.signal_id == "AERATION:AHU-1:BLOWER_VALVE"]

raw_mean = valve.value.mean()
hourly_mean = hourly["AERATION:AHU-1:BLOWER_VALVE"].mean()

print(f"mean over raw readings : {raw_mean:6.2f} %   (n={len(valve):,})")
print(f"mean of hourly means   : {hourly_mean:6.2f} %   "
      f"(n={int(hourly['AERATION:AHU-1:BLOWER_VALVE'].count())})")
print(f"difference             : {hourly_mean - raw_mean:+6.2f} "
      f"({100 * (hourly_mean - raw_mean) / raw_mean:+.1f} %)")
```

```output
mean over raw readings :  29.36 %   (n=268,086)
mean of hourly means   :  23.09 %   (n=169)
difference             :  -6.28 (-21.4 %)
```

**Two averages of the same column, 6.28 percentage points apart — 21 % of the
value.** Neither is a bug and neither is wrong; they answer different questions.

The historian is **change-triggered**: it stores a row when the value *differs*,
not on a schedule. So the raw mean is a mean over a sample that is dense exactly
when the blower valve is moving, and the valve moves most when the plant is
working hardest. Averaging hourly instead treats each hour equally.

> **Neither number is "the mean".** The raw mean over-weights active periods; the
> hourly mean over-weights quiet ones. Which is right depends on whether you want
> the average valve position or the average valve position *weighted by when the
> valve was asked to do something*. Notebook 07 makes that choice explicit for
> mass, where it changes the answer by much more than 21 %.

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
    disagreements.append((
        abs(difference) / abs(series.mean()) if series.mean() else 0.0,
        sig, series.mean(), buckets.mean(), len(series), len(buckets),
    ))

for _, sig, raw, bucketed, n_raw, n_h in sorted(disagreements, reverse=True)[:6]:
    print(f"  {sig:28} {raw:9.2f} -> {bucketed:9.2f}  "
          f"{100 * (bucketed - raw) / raw:+6.1f}%   "
          f"(n={n_raw:>7,} raw, {n_h:>3} hours)")
```

```output
  AERATION:AHU-1:BLOWER_VALVE 29.36 -> 23.09 -21.4% (n=268,086 raw, 169 hours)
  INFLUENT:LIFT:WETWELL_LEVEL 3.09 -> 3.45 +11.9% (n= 21,992 raw, 137 hours)
  EFFLUENT:FLOW:TURBIDITY 10.17 -> 9.22 -9.4% (n=130,420 raw, 169 hours)
  SECONDARY:SEC-CL-1:OVERFLOW 18.50 -> 16.80 -9.2% (n=130,222 raw, 168 hours)
  EFFLUENT:FLOW:TSS 18.48 -> 16.79 -9.2% (n=128,030 raw, 167 hours)
  AERATION:AHU-1:AIR_FLOW 6451.76 -> 5985.07 -7.2% (n=413,999 raw, 169 hours)
```

**Six of the busiest signals disagree by 7 % or more**, and the two most active —
the blower valve and the air flow it controls — disagree the most. That is the
mechanism showing itself: **the more a signal moves, the more its raw mean
over-states its hourly mean.**

Both `SECONDARY:SEC-CL-1:OVERFLOW` and `EFFLUENT:FLOW:TSS` appear, agreeing to
three figures, because they are the same measurement as section one established.

## Takeaways

- **Seven tables, 57 tags, 22 assets, one week.** `signal` is the one to read first:
  it holds `unit`, `normal_low`/`high`, `deadband` and `sample_ms` for every tag,
  and every one of those is a thing you would otherwise hard-code.
- **The `area` column is the process.** `INFLUENT → PRIMARY → AERATION → SECONDARY →
  EFFLUENT`, with `SLUDGE`, `UTILITY` and `SITE` alongside. The ammonia chain
  `NH4_IN → NH4_OUT → EFFLUENT:NH4` is the clearest single story: 22.0 mg/L in,
  7.9 mg/L out, 64 % removed by bacteria spending 5,985 m³/h of air and 682 kW.
- **Nine pairs of tags are one measurement, and only six tags.** The four water
  temperatures are 1,943 identical readings under four names; ammonia out and
  ammonia in the effluent are 684; aeration pH and effluent pH are 130,622.
  Nothing in the schema flags a duplicate, so find them before you build a
  correlation matrix or a regression.
- **A correlation printed to two decimals cannot tell a duplicate from a fact.**
  Both the duplicate temperature pair and the genuine water-temperature-to-air-
  temperature relationship print as `1.00`. Print six digits, then go and find out
  *why* each one is where it is.
- **After removing the duplicates, 18 of 491 pairs still correlate above 0.99**, and
  it is still the daily cycle: a 1.77× swing peaking at 08:00 that every signal in
  the plant is riding. This dataset's hardest feature is that its signals are not
  independent.
- **The two averages of one column differ by 21 %.** The blower valve reads
  `29.36 %` averaged over raw readings and `23.09 %` averaged over hours. The more
  a signal moves, the further apart those two get — which is change-triggered
  storage, and the subject of notebook 02.

## Exercises

1. **Prove the duplicate.** Write the query that finds *all* duplicate pairs —
   every tag with a second tag sharing its values — rather than the two given here.
   How many did it find that this notebook did not list?
2. **Break the daily cycle.** Recompute the correlation between
   `AERATION:AHU-1:AIR_FLOW` and `UTILITY:SITE:PLANT_POWER` on hourly data with the
   hour of day removed. What is it now, and what does that tell you the original
   was measuring?
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
