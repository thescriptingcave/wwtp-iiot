# Tasks

Every entry answers one question and gives the commands that answer it. Pick the
question; you do not need to have read anything else.

**This file is executed, not just read.** `tests/test_task_index.py` runs every
command below and fails if one stops working, so a command in here that has gone
stale is a failing test rather than a sentence that quietly lies. That is the
whole reason it exists: the FC6 bug in the operator flow survived because the
test asserted the *wrong value was correct* and the docstring explained why.
A command you can run is worth more than a command that sounds right.

If a command below is wrong, fix it here and in the same commit that broke it.

---

## Contents

- [Start the plant and look at it](#start-the-plant-and-look-at-it)
- [Change a threshold or an alarm rule](#change-a-threshold-or-an-alarm-rule)
- [Add a signal, a register or an asset](#add-a-signal-a-register-or-an-asset)
- [A value is not moving](#a-value-is-not-moving)
- [The operator setpoint does not take](#the-operator-setpoint-does-not-take)
- [Make a fault happen on demand](#make-a-fault-happen-on-demand)
- [Check that a fault is actually detected](#check-that-a-fault-is-actually-detected)
- [Verify the whole thing still works](#verify-the-whole-thing-still-works)
- [Verify it on a machine that has never seen this repository](#verify-it-on-a-machine-that-has-never-seen-this-repository)
- [Query the plant](#query-the-plant)
- [What is deliberately not verified](#what-is-deliberately-not-verified)

---

## Start the plant and look at it

Full walkthrough with screenshots-in-words: [`GETTING-STARTED.md`](GETTING-STARTED.md).

```bash
make up          # a running plant with a week of history, about 2.5 min
make logs        # follow the plant and the gateway
```

Four ways to look at the same value. **These are four different things** and
picking the wrong one is the most common confusion here:

```bash
make psql                     # the database, and it is the record of what happened
make browse                   # OPC UA address space: structure, no values
make watch SIGNAL=AERATION:AHU-1:DO    # one signal, updating
make query SQL="SELECT count(*) FROM reading"
```

Dashboards, if you want them:

```bash
make web        # custom dashboard, http://127.0.0.1:3001
make grafana    # Grafana, provisioned from files in git
```

---

## Change a threshold or an alarm rule

**Alarm rules are code, not configuration.** They live in [`alarms/rules.py`](../alarms/rules.py)
as `AlarmRule(...)` objects. A threshold is a `params={"limit": ...}` inside one.

Why a threshold is 2 000 and not the contract's 2 200 is written next to the
number, and every threshold carries the measurement it came from. Read
[`ALARM-TUNING.md`](ALARM-TUNING.md) before changing one — in particular
"Three things that did not work" and "The false-positive ratchet".

The contract's `range` and `normal` are **instrument limits**, not alarm
thresholds. Copying `range_max` into a threshold is how a limit check ends up
firing on 17 % of a healthy plant.

After changing a rule:

```bash
make coverage    # the fault x rule matrix, about eight minutes
make test        # the rules are unit-tested too
```

`make coverage` is the gate that matters. A rule that detects nothing and a rule
that fires constantly both look fine in a unit test.

---

## Add a signal, a register or an asset

**`contracts/tags.yaml` is the authority.** Everything else is generated from
it. Editing a generated file directly is undone by the next regeneration.

A signal is one line under an area, in the style of its neighbours:

```yaml
- { id: "SITE:WEATHER:AIR_TEMP", field: air_temp_c, eu: "Cel",  range: [-30, 55], normal: [5, 30],   deadband: 0.1, sample_ms: 60000 }
```

Then, in this order — **the check targets tell you when you have missed one**:

```bash
make scada-flows       # regenerate the tag list and the Node-RED flows
make dashboards        # regenerate the Grafana dashboards
make page              # regenerate the web dashboard's read model
uv run python -m tools.opcua_address_space    # regenerate contracts/address-space.json
```

There is **no `make address-space` target** — that generator is only reachable
as the `python -m` line above, which is exactly the kind of gap worth knowing
about before you need it. `make scada-check` and friends will not catch drift in
this file.

Each generator has a `--check` mode that reports drift without writing, and
`make check` runs the ones with targets:

```bash
make scada-check
make dashboards-check
make page-check
make tableplus-check
```

If you add a **writable** signal, one thing is not optional: give it a setter in
`SoftPlc._apply_pending_writes`. A register marked `writable: true` that nothing
applies a write to accepts an operator's value and discards it on the next scan,
which is worse than refusing it. `tests/test_modbus.py::test_every_writable_register_has_a_signal_the_plant_can_apply`
enforces this, and `FAULT_CODE` and `STORM_FLAG` were both read-only for exactly
this reason.

---

## A value is not moving

Work outside in. The layers, in the order data flows:

| Where | Command | What a wrong answer means |
|---|---|---|
| Contract | `make contract` | The signal is not what you think it is |
| PLC, live | `make watch SIGNAL=...` | The model is not producing the value |
| OPC UA | `make browse` | The address space does not match the contract |
| Gateway | `docker compose logs gateway` | It is not polling, or it is dropping |
| Database | `make query SQL="SELECT value, ts FROM reading WHERE signal_id='...' ORDER BY ts DESC LIMIT 8"` | It is not ingesting, or the deadband is eating it |

Two failures that look identical and are not:

- **A frozen value is not a wrong value.** `sensor_flatline` holds its last
  reading, which is in range, changes slowly enough to look alive, and satisfies
  a deadband check. If a value looks suspiciously steady, suspect the
  transmitter before the plant.
- **A plausible wrong number is harder than a missing one.** `do_sensor_drift`
  reads high, so the plant runs at a genuinely lower DO than the gauge suggests
  while the indicated value never leaves its range.

For the full picture of one scan and what each layer can lose:
[`DATA-FLOW.md`](DATA-FLOW.md).

---

## The operator setpoint does not take

The setpoint write is the one path in this project that moves a number into the
plant, and it has three separate failure modes that all present as "the setpoint
did not change".

**Is the PLC accepting it?** The PLC counts writes and answers refusals with a
reason:

```bash
docker compose logs softplc --since 2m | grep -E 'write accepted|write refused'
```

```
modbus write accepted: AERATION_SETPOINT_DO = 3.4 -> AERATION:AHU-1:SETPOINT_DO
modbus write refused: AERATION_SETPOINT_DO is 2 register(s) of float32; got 1
```

The **second** line is a defect in the *flow*, not the plant, and it is the one
worth memorising. It means the flow sent function code 6 (write single register)
instead of 16, so only the high word of the float32 arrived. The flow's `dataType`
must be `MHoldingRegisters`; `HoldingRegister` looks correct and means FC6.

**Write it directly, bypassing the flow**, to find out which layer is at fault:

```bash
uv run python -c "
import struct
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
b = struct.pack('>f', 3.4); hi, lo = struct.unpack('>HH', b)
print(c.write_registers(102, [hi, lo], slave=1))
c.close()"
```

PDU **102** is `AERATION_SETPOINT_DO`, not the contract's 40102 — see
`ModbusTcpServer.wire_offset`. If that succeeds and the flow does not, regenerate
the flows and rebuild the image:

```bash
make scada-flows && make scada
```

`make scada` rebuilds the image. `docker compose up -d scada` alone reuses a
stale one and looks like your change did nothing.

**Then check the plant actually responded.** Reading the register back is *not*
proof: the register publishes the plant's field, so it would read correctly even
if the PI loop were still integrating against the startup value. Aeration air
flow (`AERATION:AHU-1:AIR_FLOW`) must rise with the setpoint and fall with it:

```bash
uv run python -c "
import struct, time
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
def air(): return struct.unpack('>f', struct.pack('>HH', *c.read_holding_registers(104, count=2, slave=1).registers))[0]
a = air(); time.sleep(20); print(air(), '->', air(), 'was', a)
c.close()"
```

---

## Make a fault happen on demand

Twelve faults, six scenarios, in [`contracts/fault-scenarios.yaml`](../contracts/fault-scenarios.yaml).
Each has a docstring in [`alarms/rules.py`](../alarms/rules.py) explaining what it
models and why it is in the library.

```bash
make coverage-json     # the whole matrix, machine-readable, no database
docker compose --profile scada up -d softplc    # then inject, see QA-E2E-TEST-PLAN.md
```

Scenarios: `baseline`, `wet_weather`, `aeration_loss`, `night_shift_compliance`,
`bad_instrument`, `everything_at_once`.

Three that teach something the others do not:

- `blower_failure` — the signature is **delayed**. Effluent ammonia does not rise
  for hours; DO alarms four hours earlier.
- `do_sensor_drift` — **no threshold on the reading can ever catch it**, because
  the reading is never out of range.
- `sensor_flatline` — a frozen value satisfies a deadband check.

---

## Check that a fault is actually detected

```bash
make coverage    # the fault x rule matrix, about eight minutes
make alarms      # the engine, against the live historian
```

Read [`ALARMS.md`](ALARMS.md) for the rule catalogue and the coverage matrix.
Three things are known **not** to work and are named there rather than hidden:
`min_span_s` is declared and never used, the two harnesses disagree by a factor
of seven, and `secondary_blanket_stuck` fires on 100 % of healthy time.

---

## Verify the whole thing still works

```bash
make check       # everything CI would run
```

Individually, cheapest first:

```bash
make lint        # ruff, on the packages that are clean
make lint-debt   # fails if tracked debt grew; never fails if it fell
make types       # mypy, across every package that ships
make test        # unit tests, no database, about four minutes
make integration # against a throwaway database
```

**`make lint-debt` is a ratchet, not a cleanup.** It measures a tracked count
(`lint-debt-baseline.txt`) and fails only when it goes *up*. Fixing findings
lowers it and you must lower the baseline in the same commit.

What is verified and what is deliberately not: [`TESTING.md`](TESTING.md).

---

## Verify it on a machine that has never seen this repository

```bash
docs/CLEAN-MACHINE-TEST-PLAN.md
```

31 numbered steps, each with the command and the expected output. This is the
plan to follow when you do not trust the current environment.

For the full end-to-end QA pass, with evidence capture: [`QA-E2E-TEST-PLAN.md`](QA-E2E-TEST-PLAN.md).

---

## Query the plant

```bash
make psql                                    # a shell
make query SQL="SELECT count(*) FROM reading"
make query SQL="SELECT now(), max(ts) FROM reading"   # is it still ingesting?
```

- **SQL course** — 78 queries across 26 files, in `sql/`. They run against a live
  server via `make sql`.
- **Extractable to TablePlus** — `make tableplus` writes `sql/TablePlus/*.sql`.

---

## What is deliberately not verified

Stated plainly rather than left to be discovered:
[`SECURITY.md`](SECURITY.md) has the threat model and the known gaps, and
[`DESIGN.md`](DESIGN.md) has the rejected alternatives.

The two that most often surprise people:

- **OPC UA writes are inert.** `OpcUaServer.write_value` has no caller; a write
  over OPC UA is overwritten on the next scan. The same setpoint is reachable
  over Modbus and unreachable over OPC UA, on an identical contract.
- **Modbus has no read-only concept.** The server refuses writes the contract
  forbids, in software. The protocol offers no help, so it is enforcement by the
  same process that would be compromised.

---

## Where everything else is

| I want to understand | Read |
|---|---|
| Why it is shaped this way, and what was rejected | [`DESIGN.md`](DESIGN.md) |
| Components, boundaries, the thread model | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| One scan end to end | [`DATA-FLOW.md`](DATA-FLOW.md) |
| The Node-RED flows and how they track the contract | [`scada/README.md`](../scada/README.md) |
| The OPC UA course, and 14 things it gets wrong | [`courses/opcua/`](../courses/opcua/README.md) |
| Every wrong assumption, including when the tests were wrong first | [`LEARNING-LOG.md`](LEARNING-LOG.md) |
| Plant and IIoT vocabulary | [`TERMINOLOGY.md`](TERMINOLOGY.md) |