"""Tag contract loader and validator.

The single entry point for reading ``contracts/tags.yaml``. Every component in
this project — the soft PLC, the gateway, the storage layer, the OPC UA and
Modbus servers, the Couchbase seeder, the SQL track — imports from here rather
than re-reading YAML and re-implementing defaults.

The loader is deliberately strict. A contract error must fail loudly at import
time, not surface later as a wrong field name in a time-series database.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml

# softplc/contract.py → parents[0]=softplc, parents[1]=repo root
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "tags.yaml"

#: Equipment run states. Mirrors ``contracts/tags.yaml`` → ``state_equipment``.
STATE_STOPPED = 0
STATE_RUNNING = 1
STATE_FAULT = 2
STATE_STANDBY = 3

STATE_NAMES: dict[int, str] = {
    STATE_STOPPED: "stopped",
    STATE_RUNNING: "running",
    STATE_FAULT: "fault",
    STATE_STANDBY: "standby",
}

#: OPC UA StatusCode classes, carried through as the ``quality`` field.
QUALITY_GOOD = 0
QUALITY_UNCERTAIN = 1
QUALITY_BAD = 2

QUALITY_NAMES: dict[int, str] = {
    QUALITY_GOOD: "Good",
    QUALITY_UNCERTAIN: "Uncertain",
    QUALITY_BAD: "Bad",
}

#: UDT types from the Modbus spec. A float32 occupies two consecutive registers,
#: and the word order is device-specific — this is the classic real-world trap.
UDT_INT16 = "int16"
UDT_UINT16 = "uint16"
UDT_FLOAT32 = "float32"


class ContractError(ValueError):
    """Raised when the contract is internally inconsistent or malformed."""


@dataclass(frozen=True, slots=True)
class Signal:
    """One measured quantity on one instrument.

    ``normal`` is the expected operating band, used to seed baseline alarms and
    to give the simulator a realistic target. ``range`` is the engineering
    range, used for scaling and for rejecting out-of-range values on ingest.
    """

    id: str
    field: str
    eu: str
    range_min: float
    range_max: float
    normal_low: float
    normal_high: float
    deadband: float
    sample_ms: int
    measurement: str
    unit: str
    area: str
    equipment: str | None = None
    writable: bool = False

    @property
    def sample_s(self) -> float:
        return self.sample_ms / 1000.0

    @property
    def range_span(self) -> float:
        return self.range_max - self.range_min

    def in_range(self, value: float) -> bool:
        """Engineering-range check, tolerant of float error at the bounds."""
        if not math.isfinite(value):
            return False
        eps = abs(self.range_span) * 1e-6 + 1e-9
        return (self.range_min - eps) <= value <= (self.range_max + eps)

    def in_normal_band(self, value: float) -> bool:
        return self.normal_low <= value <= self.normal_high

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.id} [{self.field}] {self.eu}"


@dataclass(frozen=True, slots=True)
class Measurement:
    """A group of signals sharing an InfluxDB table and a tag set."""

    name: str
    unit: str
    signals: tuple[Signal, ...]
    equipment: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[Signal]:
        return iter(self.signals)

    def __len__(self) -> int:
        return len(self.signals)


@dataclass(frozen=True, slots=True)
class Equipment:
    id: str
    name: str
    type: str
    area: str
    rated_kw: float | None = None
    duty: str | None = None
    fail_modes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Register:
    """One Modbus holding register or register pair."""

    address: int
    name: str
    type: str
    unit_id: int
    writable: bool = False
    #: ``big`` = high word first, ``little`` = low word first. Only meaningful
    #: for float32. The contract deliberately mixes both orders.
    word_order: str = "big"

    @property
    def width(self) -> int:
        """Registers occupied. float32 spans two; int16/uint16 span one."""
        return 2 if self.type == UDT_FLOAT32 else 1

    @property
    def end_address(self) -> int:
        return self.address + self.width - 1


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """Declarative fault definition. Executed by ``softplc.faults``."""

    id: str
    kind: str
    targets: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)
    start_at_s: float = 0.0
    duration_s: float | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class Contract:
    version: int
    site: dict[str, Any]
    areas: tuple[dict[str, Any], ...]
    equipment: dict[str, Equipment]
    measurements: dict[str, Measurement]
    signals: dict[str, Signal]
    state_equipment: tuple[str, ...]
    registers: tuple[Register, ...]
    faults: tuple[FaultSpec, ...]
    writable: dict[str, dict[str, Any]]
    opcua: dict[str, Any]
    #: The ``modbus:`` block from the contract: endpoint, unit_id, register map.
    #: Carried on the contract rather than read separately, so a consumer has
    #: one source of truth and a renamed key fails loudly at import instead of
    #: silently defaulting.
    modbus: dict[str, Any]

    # ── convenience lookups ──────────────────────────────────────────────────
    def signal(self, signal_id: str) -> Signal:
        try:
            return self.signals[signal_id]
        except KeyError:
            raise ContractError(f"unknown signal id: {signal_id!r}") from None

    def signals_for(self, measurement: str) -> tuple[Signal, ...]:
        try:
            return self.measurements[measurement].signals
        except KeyError:
            raise ContractError(f"unknown measurement: {measurement!r}") from None

    def signals_in_area(self, area: str) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals.values() if s.area == area)

    def register(self, name: str) -> Register:
        for reg in self.registers:
            if reg.name == name:
                return reg
        raise ContractError(f"unknown Modbus register: {name!r}")

    @property
    def area_ids(self) -> tuple[str, ...]:
        return tuple(a["id"] for a in self.areas)

    @property
    def permit(self) -> dict[str, Any]:
        return self.site.get("permit", {})

    @property
    def design_flow_m3h(self) -> float:
        return float(self.site.get("design", {}).get("design_flow_m3h", 1800))

    def measurement_tags(self, measurement: str) -> tuple[str, ...]:
        """The InfluxDB tag columns for a measurement.

        Always ``site, area, unit, instrument_id`` — identity, never values.
        ``docs/DESIGN.md`` rule 1.
        """
        m = self.measurements.get(measurement)
        extra = m.tags if m else ()
        return ("site", "area", "unit", "instrument_id", *extra)

    def summary(self) -> str:
        n_sig = len(self.signals)
        n_reg = len(self.registers)
        traps = sum(1 for r in self.registers if r.word_order == "little")
        lines = [
            f"contract v{self.version}  site={self.site.get('id')} "
            f"({self.site.get('name')})",
            f"  areas       {len(self.areas)}",
            f"  equipment   {len(self.equipment)}",
            f"  measurements{len(self.measurements):>4}  →  {n_sig} signals",
            f"  modbus      {n_reg} registers ({traps} with low-word-first floats)",
            f"  faults      {len(self.faults)}",
            f"  writable    {len(self.writable)}",
        ]
        return "\n".join(lines)


# ─── validation ──────────────────────────────────────────────────────────────


def _parse_signal(
    raw: dict[str, Any], *, measurement: str, unit: str
) -> Signal:
    missing = [
        k for k in ("id", "field", "eu", "range", "normal", "deadband", "sample_ms")
        if k not in raw
    ]
    if missing:
        raise ContractError(
            f"signal {raw.get('id', '<unnamed>')} in {measurement!r} is missing: "
            f"{', '.join(missing)}"
        )

    lo, hi = raw["range"]
    if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float))):
        raise ContractError(f"{raw['id']}: range must be [number, number]")
    if lo >= hi:
        raise ContractError(f"{raw['id']}: range must be ascending, got [{lo}, {hi}]")

    nlo, nhi = raw["normal"]
    if not (isinstance(nlo, (int, float)) and isinstance(nhi, (int, float))):
        raise ContractError(f"{raw['id']}: normal must be [number, number]")
    if nlo > nhi:
        raise ContractError(f"{raw['id']}: normal must be ascending, got [{nlo}, {nhi}]")

    if raw["deadband"] < 0:
        raise ContractError(f"{raw['id']}: deadband must be >= 0")
    if raw["deadband"] >= (hi - lo):
        raise ContractError(
            f"{raw['id']}: deadband {raw['deadband']} is >= range span {hi - lo}; "
            "the signal could never change enough to be stored"
        )
    if raw["sample_ms"] <= 0:
        raise ContractError(f"{raw['id']}: sample_ms must be > 0")

    parts = raw["id"].split(":")
    if len(parts) != 3:
        raise ContractError(
            f"signal id {raw['id']!r} must be AREA:UNIT:EQUIPMENT "
            f"({len(parts)} colon-separated part(s) found)"
        )

    return Signal(
        id=raw["id"],
        field=raw["field"],
        eu=raw["eu"],
        range_min=float(lo),
        range_max=float(hi),
        normal_low=float(nlo),
        normal_high=float(nhi),
        deadband=float(raw["deadband"]),
        sample_ms=int(raw["sample_ms"]),
        measurement=measurement,
        unit=unit,
        area=parts[0],
        equipment=parts[1],
        writable=bool(raw.get("writable", False)),
    )


def _validate_registers(regs: Sequence[Register]) -> None:
    """Reject overlapping register blocks.

    An overlap is the kind of contract bug that produces *plausible garbage*
    rather than an error, so it is worth catching at load time.
    """
    occupied: dict[int, str] = {}
    for reg in regs:
        for addr in range(reg.address, reg.end_address + 1):
            if addr in occupied:
                raise ContractError(
                    f"Modbus address {addr} claimed by both "
                    f"{occupied[addr]!r} and {reg.name!r}"
                )
            occupied[addr] = reg.name

    if not regs:
        raise ContractError("no Modbus registers declared")


def _validate_cardinality(measurements: dict[str, Measurement]) -> None:
    """Guard the cardinality trap from docs/DESIGN.md rule 1.

    A field name that looks numeric is almost always a measurement that was
    pasted into a tag by mistake. This is a cheap heuristic, not a proof.
    """
    for m in measurements.values():
        for sig in m.signals:
            digits = "".join(c for c in sig.field if c.isdigit())
            if digits and not any(c.isalpha() for c in sig.field):
                raise ContractError(
                    f"{sig.id}: field {sig.field!r} is purely numeric — it looks "
                    "like a value was pasted into a tag. Tag by identity, field by "
                    "value (docs/DESIGN.md rule 1)."
                )


def _validate_faults(raw_faults: Any) -> tuple[FaultSpec, ...]:
    if not raw_faults:
        return ()
    if not isinstance(raw_faults, list):
        raise ContractError("faults must be a list")

    out: list[FaultSpec] = []
    seen: set[str] = set()
    for raw in raw_faults:
        for key in ("id", "kind", "targets"):
            if key not in raw:
                raise ContractError(f"fault entry missing {key!r}: {raw}")
        if raw["id"] in seen:
            raise ContractError(f"duplicate fault id: {raw['id']!r}")
        seen.add(raw["id"])
        params = {
            k: v for k, v in raw.items()
            if k not in ("id", "kind", "targets", "start_at_s", "duration_s", "description")
        }
        out.append(
            FaultSpec(
                id=raw["id"],
                kind=raw["kind"],
                targets=tuple(raw["targets"]),
                params=params,
                start_at_s=float(raw.get("start_at_s", 0.0)),
                duration_s=(
                    float(raw["duration_s"]) if raw.get("duration_s") is not None else None
                ),
                description=raw.get("description", ""),
            )
        )
    return tuple(out)


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ContractError(f"contract not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ContractError(f"contract must be a mapping, got {type(data).__name__}")
    return data


def load_contract(path: Path | str | None = None) -> Contract:
    """Parse, validate, and return the contract.

    Raises :class:`ContractError` on any inconsistency. Callers should not
    attempt to continue with a partial contract.
    """
    raw = _load_raw(Path(path) if path else CONTRACT_PATH)

    for key in ("version", "site", "areas", "equipment", "measurements"):
        if key not in raw:
            raise ContractError(f"contract is missing required key: {key!r}")

    areas = tuple(raw["areas"])
    area_ids = {a["id"] for a in areas}
    if len(area_ids) != len(areas):
        raise ContractError("duplicate area id in contract")

    # ── equipment ───────────────────────────────────────────────────────────
    equipment: dict[str, Equipment] = {}
    for e in raw["equipment"]:
        if "id" not in e or "area" not in e:
            raise ContractError(f"equipment entry missing id/area: {e}")
        if e["id"] in equipment:
            raise ContractError(f"duplicate equipment id: {e['id']!r}")
        if e["area"] not in area_ids:
            raise ContractError(
                f"equipment {e['id']!r} references unknown area {e['area']!r}"
            )
        equipment[e["id"]] = Equipment(
            id=e["id"],
            name=e.get("name", e["id"]),
            type=e.get("type", "generic"),
            area=e["area"],
            rated_kw=float(e["rated_kw"]) if "rated_kw" in e else None,
            duty=e.get("duty"),
            fail_modes=tuple(e.get("fail_modes", ())),
        )

    # ── measurements & signals ──────────────────────────────────────────────
    measurements: dict[str, Measurement] = {}
    signals: dict[str, Signal] = {}
    seen_table_field: dict[tuple[str, str], str] = {}

    for m in raw["measurements"]:
        name = m.get("measurement")
        if not name:
            raise ContractError(f"measurement entry missing 'measurement': {m}")
        if name in measurements:
            raise ContractError(f"duplicate measurement: {name!r}")

        unit = m.get("unit")
        if not unit:
            raise ContractError(f"measurement {name!r} missing 'unit'")

        sigs: list[Signal] = []
        for raw_sig in m.get("signals", []):
            sig = _parse_signal(raw_sig, measurement=name, unit=unit)

            if sig.area not in area_ids:
                raise ContractError(
                    f"{sig.id}: area {sig.area!r} is not declared in `areas`"
                )
            if sig.id in signals:
                raise ContractError(f"duplicate signal id: {sig.id!r}")
            key = (name, sig.field)
            if key in seen_table_field:
                raise ContractError(
                    f"field {sig.field!r} appears twice in measurement {name!r} "
                    f"({seen_table_field[key]} and {sig.id})"
                )
            seen_table_field[key] = sig.id

            signals[sig.id] = sig
            sigs.append(sig)

        if not sigs:
            raise ContractError(f"measurement {name!r} declares no signals")

        measurements[name] = Measurement(
            name=name,
            unit=unit,
            signals=tuple(sigs),
            equipment=tuple(m.get("equipment", ())),
            tags=tuple(m.get("tags", ())),
        )

    _validate_cardinality(measurements)

    # ── state equipment must exist ──────────────────────────────────────────
    for eq in raw.get("state_equipment", []):
        if eq not in equipment:
            raise ContractError(
                f"state_equipment references unknown equipment: {eq!r}"
            )

    # ── Modbus registers ────────────────────────────────────────────────────
    modbus = raw.get("modbus", {})
    regs: list[Register] = []
    for r in modbus.get("registers", []):
        for key in ("address", "name", "type"):
            if key not in r:
                raise ContractError(f"register entry missing {key!r}: {r}")
        if r["type"] not in (UDT_INT16, UDT_UINT16, UDT_FLOAT32):
            raise ContractError(
                f"register {r['name']!r} has unsupported type {r['type']!r}; "
                f"expected one of {UDT_INT16}, {UDT_UINT16}, {UDT_FLOAT32}"
            )
        order = r.get("word_order", "big")
        if order not in ("big", "little"):
            raise ContractError(
                f"register {r['name']!r} has word_order {order!r}; "
                "expected 'big' or 'little'"
            )
        if r["type"] == UDT_FLOAT32 and "word_order" not in r:
            raise ContractError(
                f"register {r['name']!r} is float32 and must state word_order "
                "explicitly — this is the trap the gateway must handle"
            )
        regs.append(
            Register(
                address=int(r["address"]),
                name=r["name"],
                type=r["type"],
                unit_id=int(r.get("unit_id", 1)),
                writable=bool(r.get("writable", False)),
                word_order=order,
            )
        )
    _validate_registers(regs)

    # ── writable surface ────────────────────────────────────────────────────
    writable: dict[str, dict[str, Any]] = {}
    for w in raw.get("writable", []):
        wid = w.get("id")
        if not wid:
            raise ContractError(f"writable entry missing id: {w}")
        writable[wid] = w
    for wid in writable:
        if wid in signals and not signals[wid].writable:
            raise ContractError(
                f"{wid}: listed in `writable` but signal.writable is false — "
                "the two must agree"
            )
        if wid in signals and signals[wid].writable and wid not in writable:
            raise ContractError(
                f"{wid}: signal.writable is true but it is absent from `writable`"
            )

    modbus_block = raw.get("modbus", {})
    if modbus_block and "unit_id" not in modbus_block:
        raise ContractError("modbus block must state unit_id")
    if modbus_block and "endpoint" not in modbus_block:
        raise ContractError("modbus block must state endpoint")

    return Contract(
        version=int(raw["version"]),
        site=raw["site"],
        areas=areas,
        equipment=equipment,
        measurements=measurements,
        signals=signals,
        state_equipment=tuple(raw.get("state_equipment", ())),
        registers=tuple(regs),
        faults=_validate_faults(raw.get("faults")),
        writable=writable,
        opcua=raw.get("opcua", {}),
        modbus=modbus_block,
    )


@functools.lru_cache(maxsize=1)
def contract() -> Contract:
    """Process-wide cached contract. The file is read once."""
    return load_contract()


if __name__ == "__main__":  # pragma: no cover
    c = contract()
    print(c.summary())
    print()
    for m in c.measurements.values():
        print(f"  {m.name:<9} {len(m.signals):>2} signals   unit={m.unit}")
        for s in m.signals:
            w = "  [writable]" if s.writable else ""
            print(
                f"      {s.field:<24} {s.eu:<12} "
                f"range={s.range_min:g}..{s.range_max:g} "
                f"normal={s.normal_low:g}..{s.normal_high:g} "
                f"db={s.deadband:g} @{s.sample_ms}ms{w}"
            )
