# Use cases

**For the plant manager.** Written for someone who runs a plant and is deciding
whether this simulator is worth their time — and, if they already know the
domain, whether it is telling the truth.

Every use case is written twice: **the instruction**, which tells you what to do
in the running system, and **how it differs from a real plant**, which tells you
where the simulator stops being a plant and starts being a teaching artifact. Read
the first part to operate it, the second part to know when not to trust it.

The data analyst and the software developer are already served and are not the
subject of this document:

| If you are | Start here |
|---|---|
| a **plant manager** | this document |
| a **data analyst** | [`TASKS.md`](TASKS.md#query-the-plant) — 78 queries, 11 notebooks |
| a **software developer** | [`GUIDE.md`](GUIDE.md) — one value end to end |

---

## Before you start

```bash
make up            # the plant, a week of history, about 2.5 minutes
make web           # the custom dashboard, http://127.0.0.1:3001
make grafana       # Grafana, http://127.0.0.1:3002
```

Four surfaces, and it is worth knowing which is which before you need it:

| Surface | What it is for | Refreshes |
|---|---|---|
| **web** — `/`, `/alarms`, `/permit` | the operator's own view. Three pages. | on demand |
| **Grafana** — *WWTP — overview* | trends, four big numbers on top | 30 s |
| **Grafana** — *WWTP — discharge permit* | the consent conditions | 30 s |
| **Node-RED mimic** — `http://127.0.0.1:18880/scada` | the generated flows, and the only place you can *write* to the plant | 5 s |

**A note on the mimic.** It is a Node-RED flow, not a rendered process diagram.
What it produces is visible on the `mimic status` debug node in the editor. The
project's own [`DASHBOARD-STORIES.md`](DASHBOARD-STORIES.md) says the current
dashboards answer *"what does this signal do?"* where an operator needs *"is the
plant all right, and if not, where do I look?"*, and that document is a proposal
for what should replace them. This document works with what exists and says so
where the gap shows.

---

## Use case 1 — Start of shift: is the plant all right?

**The first question of every shift, and the one the dashboard was rebuilt to
answer in five seconds.**

### The instruction

Open **Grafana → *WWTP — overview*.** Four numbers across the top:

| Panel | Reading it | A normal plant |
|---|---|---|
| **Influent flow** | how much is arriving | 1 900 – 2 200 m³/h |
| **Effluent flow** | how much is leaving | close to influent |
| **Aeration DO** | oxygen in the aeration basin | 1.5 – 3.0 mg/L |
| **Aeration air flow** | what the control loop is commanding | moves with the setpoint |

**Two of those four are comparisons, not values.**

- **Effluent above influent** means water is being added — a dilution step,
  storm runoff entering the wrong place, or a leak. Read it against the
  *Influent and effluent flow* panel just below, which draws both on one axis for
  exactly this.
- **DO in range is not the same as DO true.** If DO looks fine, look at the
  *DO against air flow* panel before you accept it. That is use case 3.

**Then read the freshness table, which is the fourth tile's honest caveat.**
Bottom of the page: *How old is the reading behind each number?* A big number
with no timestamp is history wearing the costume of now. The historian only
stores a reading when it exceeds the signal's deadband, so on a settled plant
two of those four can be **hours** old while the instruments are perfectly
healthy — the gateway reports filtering 94 % of readings, which is exactly what
a deadband is for. It is not the permit number, which is a 30-day mean and is on
the permit dashboard (use case 6).

Then scroll up:

- **What has stopped reporting** — a table, not a chart. Anything here has been
  silent for over an hour, or has never reported at all.
- **Sludge and digester** — blanket depth and digester pH.

### How you know it worked

Effluent flow is below influent flow, aeration DO is inside 1.5–3.0 mg/L, and
*What has stopped reporting* is empty or explains itself.

You can check both of the first two without leaving the dashboard:

```bash
make query SQL="SELECT signal_id, value FROM reading
                WHERE signal_id IN ('INFLUENT:FLOW:FLOW','EFFLUENT:FLOW:FLOW')
                ORDER BY ts DESC LIMIT 2"
```

### How this differs from a real plant

- **A real shift has a handover.** You inherit a verbal or written account: what
  was running, what was switched out, what is on a watch. This simulator has
  machinery for none of it, so there is nothing to hand over and nothing to
  inherit. **The most important thing missing here is also the least
  interesting to simulate.**
- **A real operator knows the plant's personality.** This one is a model with a
  measured healthy distribution behind it, so "normal" is a number in the
  contract. Yours is a feeling built over years, and it will beat this.
- **The four numbers are the right four for *this* model** and were chosen
  because they are what the simulator publishes. A real plant's four are set by
  its permit, its permit, and the two failure modes that actually happen.

---

## Use case 2 — Something is wrong: what is it?

**An alarm has appeared, or a number looks wrong. You have five minutes to decide
whether to go and look.**

### The instruction

**1. Read the alarm.** Open **web → `/alarms`.** Critical alarms latch: they stay
on your list until you acknowledge them, even after the condition has cleared.
That is deliberate — an alarm that vanishes by itself is indistinguishable from
one that never fired.

**2. Check what stopped reporting.** The same page, or Grafana's
*What has stopped reporting* table. If a signal has been silent for over an hour,
the fault may be the **instrument**, not the process. That is the single most
expensive mistake available to you here, because the simulator models it
deliberately: `sensor_flatline` holds a transmitter's last value, which is in
range, changes slowly enough to look alive, and **satisfies every deadband rule**.
Thirteen of the 57 signals never move at all in a seeded week, so a trend panel
that stops drawing is not evidence of a failure.

**3. Decide: the process, or the gauge?** Use case 3. If the number is a
measurement, a frozen one is indistinguishable from a steady one, and the way to
tell is to look at something that *should* move when the process does.

**4. Read the alarm's own reasoning.** Every rule in `alarms/rules.py` carries a
`rationale` field explaining why it exists and what it was measured against, and
`docs/ALARMS.md` carries the fault × rule coverage matrix. This is unusual and
worth using: the alarm tells you how confident it is.

### How you know it worked

You can state, in one sentence, **what is wrong and whether the instrument is
believing you**: *"influent flow is genuinely up, the meter is fine, and the
blower is not keeping up"* — or *"the DO probe is reading high and the plant is
under-aerated"*.

The first is a process problem. The second is an instrument problem, and the fix
is completely different.

### How this differs from a real plant

- **Real alarm systems have rationalisation.** A rationalised alarm is one an
  operator has annotated as "ignore, known". This project has **no alarm
  suppression and no shelving** — `docs/ALARMS.md` lists both as not built. Four
  unacknowledged criticals cannot be ranked here, because there is no ranking.
- **This plant alarms more than a good one would.**
  `secondary_blanket_stuck` fires on **100 % of healthy time** and always will,
  and `docs/ALARM-TUNING.md` says so. That is a recorded finding, not a surprise,
  and it is a fair criticism of the alarm set rather than of you.
- **Every alarm here is real.** There is no sensor that is merely noisy, no
  intermittently disconnected historian, no operator who has learned to ignore
  panel 4 because it lies. Real alarms are partly theatre and partly signal, and
  learning which is which is most of the job.

---

## Use case 3 — Is it the instrument, or the process?

**The most interesting thing in this simulator, and the thing a real dashboard
almost never gives you.**

### The instruction

Look at **Grafana → *DO against air flow — is the probe telling the truth?***

Two series on one axis, no band, and the reading is the *divergence between the
shapes*:

| What you see | What it means |
|---|---|
| Both rise and fall together | Normal. The blower is doing what it was asked. |
| Blower working hard, DO low | The process is under-aerated. Real, and fixable. |
| **Blower working hard, DO comfortably high** | **The probe is reading high. The plant is being under-aerated and the gauge is hiding it.** |

That third row is the `do_sensor_drift` fault. A fouled or ageing probe reads
progressively high, so the plant runs at a genuinely lower DO than the gauge
suggests.

**No threshold on the DO reading can ever catch this**, because the reading never
leaves its range. It is in band the whole time. Catching it needs the *model* —
the aeration basin knows its own oxygen transfer rate — which is why the project
calls it the flagship model-based detection case.

### How you know it worked

You have looked at two series and concluded something about the **instrument**,
which no single-number panel could have told you. The `aeration_do_low` and
`aeration_do_sagging` rules do fire on a drifting probe — but because the drift
eventually drags the indicated value far enough, not because they understand the
drift.

### How this differs from a real plant

- **In a real plant this is much harder and the reason is uncomfortable.** You
  would have independent measurements — a second probe, an oxygen balance, a
  tracer test. This simulator gives you the divergence because the model knows
  the truth; a real plant has to *find out*.
- **`aeration_do_xvalidation` does not catch this, and says it does.** That rule
  declares `detects=("do_sensor_drift",)` and does not fire on it, because
  `cross_validation` compares the Modbus reading to the OPC UA reading and here
  both report the same drifted value. It is genuinely good at one thing — a
  low-word-first float on the wire — and its `detects=` list overclaims. Recorded
  in the LEARNING-LOG; not fixed.
- **`correlation` and `oscillation_detection` are declared in the contract and do
  not exist in the registry.** You cannot use them.

---

## Use case 4 — Acknowledge an alarm, and record that you did

**Acknowledge is the action with an audit trail. It is also the one most likely to
lose information if it is only an in-memory flag, and it is deliberately not.**

### The instruction

On **web → `/alarms`**, press acknowledge. That writes an `alarm_acknowledged`
row to the `event` table. The panel then stops showing the alarm.

The design decision is the interesting part, and it is one you can check:

> The **engine** decides whether something is happening now and holds only what it
> has evaluated. The **replay** holds what the log says is outstanding, and the
> panel reads *that*.

So acknowledgement survives a Node-RED restart, because nothing was in memory to
lose. And a recurrence is a **new** alarm that needs a **new** acknowledgement —
acknowledging an occurrence does not silence the next one.

```bash
make query SQL="SELECT ts, kind, severity, message FROM event
                WHERE kind = 'alarm_acknowledged'
                ORDER BY ts DESC LIMIT 5"
```

### How you know it worked

The alarm is gone from your list, a row exists in `event`, and **stopping and
restarting the SCADA container does not bring the alarm back.** That last part is
the one worth doing once, because it is the entire reason for the replay design.

### How this differs from a real plant

- **Real acknowledgement carries a name and often a comment.** Here it is an
  event row with a rule id. Who acknowledged, and why, is not recorded — which in
  an incident is the first question anyone asks.
- **There is no shelving, so nothing is hidden.** In a real plant an
  acknowledged alarm often leaves the visible panel but stays on a *shelved* list
  with an expiry. Acknowledging here means "I have seen it", which is a weaker
  and more dangerous meaning if nobody is ranking what you have seen.

---

## Use case 5 — Change the one thing you may change

**There is exactly one writable value in this entire system.** Out of 57 signals,
the only thing an operator can command is the aeration dissolved-oxygen setpoint.

**Write range: 0.5 – 6.0 mg/L.** Read it from the contract, not from memory:

```bash
make contract | head -20
```

### The instruction

Use the **Node-RED** flow at `http://127.0.0.1:18880/scada`, tab
`03 — Operator control (setpoint)`, and press the `enter a setpoint` inject
node. **Send a bare number** — `3.4`, not `{"setpoint": 3.4}`. The flow does
`Number(msg.payload)`, so a JSON object is `NaN` and is refused as "not a
number", which looks identical to a range refusal.

Three things happen, and the third is the one that matters:

1. **The permit check** runs in the browser, in the flow. This is the client's own
   courtesy and it is **not** the enforcement — anything that talks to the PLC
   directly skips it.
2. **The write goes out** as two registers, function code 16.
3. **The plant absorbs it**, and — this is the part that is easy to get wrong —
   the setpoint is applied in **two places**, to the plant's field *and* to the
   control block's own copy.

### How you know it worked

**Four checks, and the first one is not the one that matters.**

**1. The audit trail.** A successful write leaves a `setpoint_written` row:

```bash
make query SQL="SELECT ts, kind, message FROM event
                WHERE kind = 'setpoint_written' ORDER BY ts DESC LIMIT 3"
```

Expect `setpoint AERATION:AHU-1:SETPOINT_DO set to 3.4 mg/L (range 0.5-6 mg/L)`.

A refused write must leave **no** row — the audit node runs after the write node,
so an `event` row for a setpoint the plant rejected would be a record of
something that did not happen.

**2. Not by reading the setpoint register back.** That register publishes the
field you set, so it would read correctly even if the plant ignored you entirely.
This is the single most expensive trap in the project and it has happened here.

**3. The evidence that matters — the plant responded:**

```bash
uv run python -c "
import struct, time
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
def air(): return struct.unpack('>f', struct.pack('>HH', *c.read_holding_registers(104, count=2, slave=1).registers))[0]
a = air(); time.sleep(20); print(a, '->', air())
c.close()"
```

**Air flow must rise when the setpoint rises and fall when it falls.** Set the
setpoint to 5.5, wait for it to settle, and watch. Then 0.75.

Two checks worth running once, because they test the *server* rather than the
client:

```bash
# an out-of-range setpoint must be refused by the PLC, not only by the flow
uv run python -c "
import struct
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
b = struct.pack('>f', 9.0); hi, lo = struct.unpack('>HH', b)
r = c.write_registers(102, [hi, lo], slave=1)
print('9.0 ->', 'REFUSED' if r.isError() else 'ACCEPTED')
c.close()"
```

```bash
# and a half-width write — function code 6 instead of 16 — must also be refused
uv run python -c "
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
r = c.write_register(102, 1, slave=1)
print('FC6 ->', 'REFUSED' if r.isError() else 'ACCEPTED')
c.close()"
```

That second one matters because the operator flow used to get it wrong, silently,
for as long as the project existed. `ModbusTcpServer` now refuses a short write
with a message naming the width it wanted:

```
modbus write refused: AERATION_SETPOINT_DO is 2 register(s) of float32; got 1
```

### How this differs from a real plant

- **One control, permanently.** A real plant has dozens, and most of them are
  hazardous to get wrong. A single writable value is a teaching choice, and it
  means you cannot explore the interesting failure modes.
- **There is no interlock, no permissive, no operator role.** Modbus has no
  authentication at all, so anyone who can reach the port can write. The server
  refuses what the *contract* forbids, in software, by the same process that
  would be compromised — which is better than no check and is not a boundary.
- **The audit row records the number you asked for.** It does not record who
  asked, from where, or with what authority.

---

## Use case 6 — Night shift and the permit

**The compliance question, which is the one that has real consequences.**

### The instruction

Open **web → `/permit`**, or **Grafana → *WWTP — discharge permit*.** Five consent
conditions, all read from the contract's `permit:` block.

The **Permit compliance, from the contract** panel is the one that matters, and
it now carries **`pct_of_permit`** as well as pass or fail:

| Parameter | Direction | Read it as |
|---|---|---|
| ammonia, 30-day mean | max | under 100 % is compliant |
| suspended solids, 30-day mean | max | under 100 % is compliant |
| **pH, minimum** | min | **over 100 %** is compliant — it is a floor |
| pH, maximum | max | under 100 % is compliant |
| coliforms, geometric mean | max | under 100 % is compliant |

**The good side is not the same side on every row**, and that is why the table
gives a number rather than colouring it green or amber. On the seeded week it
reads roughly 76 % for ammonia and 41 % for coliforms.

Two things to know before you trust it:

- **The permit is a 30-day mean.** The trend panel above the table is *not* — it
  is hourly values. The mean is what is compliant; the panel is what you use to
  see it going the wrong way.
- **Coliforms are a geometric mean.** MPN counts are log-normal, so an arithmetic
  mean of them is a different and meaningless number.

### How you know it worked

Every row shows a `pct_of_permit` and a `compliant`, and the two agree once you
account for direction. On the seeded week all five pass, ammonia at about 76 %.

```bash
make query SQL="SELECT parameter, measured, pct_of_permit, compliant
                FROM (SELECT 1) x LIMIT 0"   # the table is a Grafana query
```

Better, straight from the table:

```bash
docker compose exec -T db psql -U wwtp -d wwtp -tAc \
  "SELECT max(rounded_mean) FROM reading_1h
   WHERE signal_id='EFFLUENT:FLOW:NH4' AND bucket >= now() - interval '30 days'"
```

### How this differs from a real plant

- **Nothing here is a legal document.** This is a simulator's invented consent
  conditions. A real permit has averaging windows that vary per parameter,
  load-dependent limits, and reporting obligations with a regulator attached.
- **There is no compliance *record*.** A real discharge report is signed,
  submitted, and immutable. Here the 30-day mean is recomputed on every dashboard
  refresh, which is convenient and is not what a regulator wants to see.
- **The seeded week always passes**, so you will never see a breach by accident.
  To see one, arm `high_ammonia_load`.

---

## Use case 7 — Practise before it happens

**The reason a simulator exists: you cannot stop a storm on a real plant.**

### The instruction

```bash
make coverage-json      # the whole fault x rule matrix, no database needed
make coverage           # the same, rendered, about eight minutes
```

Twelve faults, six scenarios. The scenarios are `baseline`, `wet_weather`,
`aeration_loss`, `night_shift_compliance`, `bad_instrument` and
`everything_at_once`.

To run one against the live plant:

```bash
docker compose --profile scada up -d softplc
docker compose exec softplc python -m softplc.main --scenario wet_weather
```

Three worth practising on, because each is a different *kind* of problem:

| Fault | Why it is in the library |
|---|---|
| **`blower_failure`** | **The signature is delayed.** Effluent ammonia does not rise for hours. An operator alarming only on ammonia gets a four-hour warning; one alarming on DO gets a four-hour head start. |
| **`do_sensor_drift`** | No threshold on the reading can ever catch it. Use case 3. |
| **`sensor_flatline`** | A frozen value satisfies the deadband. Use case 2. |

### How you know it worked

You can state, for the fault you armed, **which rule fired, how long after it
started, and whether it fired on the healthy baseline as well.** `make coverage`
prints all three, and the third is the one people skip.

### How this differs from a real plant

- **These faults are clean.** Each has one signature, developed on purpose, and
  the noise around it is generated from a measured healthy distribution. Real
  faults overlap, arrive at night, and coincide with the one you did not model.
- **You know which fault you armed.** This is the biggest difference and the most
  important thing to hold onto while practising: **knowing the answer in advance
  is most of what makes detection feel easy.** The useful exercise is to arm a
  scenario, *forget which one*, and see how long you take.
- **Arming is instantaneous and free.** A real blower fails once.

---

## What this simulator cannot tell you

Stated plainly, because a use-case document that only describes what works leaves
you to assume the gaps were your mistake.

| Not available | Why |
|---|---|
| **Equipment state — "which pump is running?"** | The `equipment` table has no state column *by design*; a row that changes every scan is write amplification. And there are **zero run-state signals in the contract**, so there is no data for a panel. Run state is on the Modbus coils and the OPC UA address space only. Getting it into the historian is an architecture decision about 21 assets at 50 Hz. |
| **Alarm suppression and shelving** | `docs/ALARMS.md`, not built. |
| **`correlation` and `oscillation_detection`** | Declared in the contract, absent from the registry. |
| **`min_span_s` on trend rules** | Declared and never used. `docs/ALARM-TUNING.md`. |
| **Who acknowledged an alarm, and why** | The event row records the rule, not the person. |
| **Anything about OPC UA writes** | Writes over OPC UA go nowhere — see the next section. |

---

## The asymmetry you should know about

**The same setpoint is writable over Modbus and completely inert over OPC UA.**
Same contract, same server process, same operator.

The reason is architectural: the OPC UA address space is rebuilt from the plant
snapshot every 20 ms, so a write is overwritten within one scan, and no control
loop reads the address space — it reads the plant's own fields. So the write was
never read by anything, not merely overwritten.

If you are evaluating this as a system design, that is the most interesting fact
in the repository: the protocol with the better permission model is the one you
cannot use to change the plant. `docs/LEARNING-LOG.md` has the full reasoning.

---

## Where the machine checks this document

So it is not decoration, every claim above has something that fails when it stops
being true.

| Claim | Checked by |
|---|---|
| Acknowledgement works, survives a restart, and a recurrence is new | `tests/test_alarm_replay.py` — 20 pure-function tests on `alarms.replay` |
| Every panel, route, signal, scenario and event kind named here exists | `tests/test_plant_manager_use_cases.py` |
| The setpoint reaches the plant and the loop responds | `tests/test_modbus_writeback.py` |
| The permit percentage agrees with the verdict beside it | `tests/test_grafana_dashboards.py` |
| Which rule catches which fault | `make coverage` — eight minutes, and not re-derived here |
| The commands above still run | `tests/test_task_index.py` |

**What is *not* checked:** how the panels look. There is no headless browser in
this project, so the four new at-a-glance panels are verified as SQL and as JSON
and have never been seen rendered. If one of them is blank, that is why.