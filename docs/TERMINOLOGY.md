# Terminology

The vocabulary of a wastewater treatment plant and of industrial control, defined
once. Standards and specifications are in
[`GLOSSARY.md`](GLOSSARY.md) instead, each with a source to study.

If you know the plant and want the software terms, skip to
[Control and SCADA](#control-and-scada). If you know the software and want the
plant, read the first section straight through — most of this project's modelling
decisions are only explicable once you know what a clarifier is.

---

## The process

A municipal wastewater plant treats what a sewer brings in. This project models a
conventional activated-sludge plant with primary clarification, aeration,
secondary clarification and disinfection, plus a digestion and dewatering line
for the solids. Eight areas, in the order the water meets them:

| Area | What happens |
|---|---|
| `INFLUENT` | Raw sewage arrives. Screened, and lifted into the plant by the lift station. |
| `PRIMARY` | Heavy solids settle out. **Primary clarification.** |
| `AERATION` | Air is blown into the biological basin. Microbes eat the dissolved organics. **The main treatment step.** |
| `SECONDARY` | The biomass is settled out and the clarified water overflows. **Secondary clarification.** |
| `EFFLUENT` | Disinfection — chlorine or UV — then discharge. The permit limits apply here. |
| `SLUDGE` | Primary and waste sludges are thickened, anaerobically digested, and dewatered. |
| `UTILITY` | Plant power, and the site weather station. |
| `SITE` | Site-wide environment: rainfall, temperature, barometric pressure. |

### Dissolved oxygen (DO)

Oxygen dissolved in the aeration basin, in mg/L. **The single most important
number in an activated-sludge plant**, because the biology needs it: too little
and the microbes stop consuming organic matter and the basin goes septic; too much
and you are paying to aerate water that did not need it.

Typical operating range 1.5–3.0 mg/L, and the project models the same. DO is the
control variable for a whole cascade — blower speed, air flow, valve position —
which is why it is the signal the fault scenarios attack first.

`kla_per_h = 5.5` is the oxygen transfer coefficient: the rate at which the
blower delivers oxygen, per hour. The basin holds roughly **40 kg of dissolved
oxygen**, which is the number that matters when a blower trips, because it takes
one to two hours to sag. See [The ordering that alarm design depends on](#the-ordering-that-alarm-design-depends-on).

### Ammonia (NH₄⁺ / NH₃-N)

The nitrogenous waste product of protein breakdown, and the reason aeration
exists. Nitrifying bacteria oxidise it to nitrate; that reaction consumes about
4.6 g of oxygen per gram of ammonia-nitrogen, which is why ammonia breakthrough
and oxygen demand are the same problem seen at different times.

**The permit limit here is a 30-day mean of 10 mg/L at the effluent** — not an
instantaneous limit, and not a daily one. A 30-day mean is why single spikes in
this project are modelled as survivable and sustained ones are not.

### Mixed liquor suspended solids (MLSS)

The mass of biological solids in the aeration basin, in mg/L. It is the *population*
of the treatment, and everything about the biology depends on it: too little and
there is not enough biomass, too much and the clarifier cannot handle the load.

### Sludge retention time (SRT)

How long biomass stays in the system, in days. The single number that sets the
character of the sludge — and therefore how well the plant nitrifies. Long SRT
nitrifies and produces more sludge; short SRT does not.

### Return activated sludge (RAS) and waste activated sludge (WAS)

RAS is settled biomass pumped back to the aeration basin to maintain the
population. WAS is the excess, removed. The ratio between them is how an operator
controls SRT in practice, and `PRIMARY:PRI-CL-1:WASTE_RATE` is exactly that valve.

### Volatile fatty acids (VFA) and alkalinity in the digester

An anaerobic digester runs a delicate balance. Bacteria converting VFA to methane
consume alkalinity; if VFA production outruns that, **pH collapses and the digester
fails** — which takes weeks to recover from, and is one of the most expensive
failures in a plant. The ratio `VFA_ALK_RATIO` is the early-warning number, and it
is a signal in this project for that reason.

### Breakpoint chlorination

Chlorine demand is not linear. Chlorine first oxidises the reducing substances
(phenols, nitrite, iron, manganese) with essentially no disinfection happening.
That is the **breakpoint**. Past it, a small further increase produces a
proportional residual — that is where the disinfection is. Under-dosing at the
breakpoint gives *zero* disinfection while consuming chlorine, which is a failure
mode that looks like a working dose.

### The ordering that alarm design depends on

This is the single most important piece of process reasoning in the project, and
it is asserted by a test:

```
  blower trips
       ↓  (1–2 hours; the basin holds ~40 kg of oxygen)
  DO sags
       ↓  (the biology consumes less oxygen because there is less of it)
  nitrification slows
       ↓  (hours)
  ammonia breaks through at the effluent
```

**A blower trip is not an ammonia event.** It is a dissolved-oxygen event whose
ammonia consequence arrives hours later. An alarm system that pages on effluent
ammonia will page hours after the cause has passed and will have missed the
window in which anyone could have done anything.

`contracts/fault-scenarios.yaml` encodes this: each fault declares the signature an
operator *should* see, and — the part that is unusual and the part that teaches —
`NOT_detectable_by`, listing what the fault will **not** tell you. Knowing what a
fault will not reveal is how you avoid a confident wrong diagnosis.

---

## The software

### Soft PLC

A program that emulates a programmable logic controller: a scan loop, function
blocks, a scan image, and no physical I/O. Here it also solves the process model,
because a real PLC would be fed by instruments and this one is the instrument *and*
the controller.

### Scan image

The register file a PLC keeps for one cycle. Every function block reads from it
and writes to it, in a fixed order, on one thread.

The rule that matters: **a block that reads a value another block wrote earlier in
the same scan sees that value, not the previous one.** Getting the ordering wrong
is the classic PLC bug, and this project asserts the ordering rather than trusting
it.

### Function block

A reusable unit with inputs, outputs and internal state, executed once per scan.
Typed inputs, no implicit globals, and internal state that survives across scans —
which is what distinguishes a block from a function.

### Deadband

A filter that suppresses a reading when the value has not moved by more than a
threshold. It is the difference between a historian that stores 220 million rows a
year and one that stores four million, and it is standard practice in industry.

**Its cost, which this project does not hide:** a row exists when the value moved,
so "no readings" is ambiguous between *the plant was steady* and *the instrument
is dead*. Thirteen of this plant's signals produce exactly one reading in a week for
exactly this reason. See [`sql/02-04`](../sql/02-intermediate/02-04_gaps.md).

### Store-and-forward spool

A durable queue between a device you cannot control and a system that can go down.
The plant does not stop because the database is restarting; a level keeps rising.
The spool is what turns "the historian was down for an hour" from data loss into a
gap.

### Quality

The validity of a reading, as opposed to its value. Three states: **Good**,
**Uncertain**, **Bad**.

A fouled probe is `Uncertain` with a value; a disconnected probe is `Bad` with no
value. **A historian that cannot tell a bad sensor from a bad process cannot be
trusted to alarm on anything** — and the `bad_instrument` fault deliberately does
not damage the process, so the two can be told apart.

### Hypertable

A PostgreSQL table that TimescaleDB has partitioned by time into *chunks*, while
still being one logical relation. Every query you write is an ordinary query. The
payoff is that a query for one hour of one signal touches one chunk rather than
four million rows — and a retention policy is a chunk drop, which takes
milliseconds.

### Continuous aggregate

A materialised rollup maintained incrementally as data arrives. `reading_1m` and
`reading_1h` here.

The decision worth knowing: **both tiers aggregate from the raw table, not from
each other.** Rolling 1 minute into 1 hour by averaging the 1-minute averages is
wrong as soon as two minutes hold different numbers of points, and the error is
largest exactly when the data is most interesting.

---

## Storage and query

### TimescaleDB

An extension that adds hypertables, continuous aggregates, retention policies and
`time_bucket` to PostgreSQL. Not a separate database — it is Postgres, with
time-series structures bolted on, which is why everything in the standard SQL
course works here unchanged.

### `time_bucket` versus `date_trunc`

Two ways to round a timestamp down to a bucket. `date_trunc` is standard Postgres
and only understands calendar units — `date_trunc('7 minutes', ts)` is an error.
`time_bucket(interval, ts)` is TimescaleDB's and handles any interval, plus an
optional origin for non-midnight alignment. **Prefer `time_bucket`** in this
project: same shape for every interval, and the continuous aggregates use it too.

### `NUMERIC` versus `DOUBLE PRECISION`

`NUMERIC` is exact decimal. `DOUBLE PRECISION` is binary floating point, and
floating-point addition is **not associative** — so `sum()` over doubles depends
on the order rows are added in.

Postgres aggregates in parallel by default and does not pin the plan, so the same
`avg()` over the same rows returns `6391.155254170624` on one run and
`6391.155254170615` on the next. Worse, it changes the *order* of a result sorted
by it. Round in the `ORDER BY` as well as the `SELECT`, and tie-break on a stable
column.

### Foreign key

A constraint that a value in one table must exist in another. `reading.signal_id`
is one, so a reading cannot name a signal that does not exist.

**There was no mechanism for that in the previous storage engine** — a tag could
say `equipment=FLOW` and nothing could say there is no such asset. That
constraint found a real modelling error on its first run.
