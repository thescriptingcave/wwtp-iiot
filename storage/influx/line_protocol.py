"""InfluxDB line protocol, and the schema decisions behind it.

Line protocol is a text format for points:

    measurement,tag=value,tag=value field=value,field=value timestamp

Three decisions in that one line shape everything downstream, and each of them
is a trade-off rather than a rule.

## Measurement, tags and fields are three different jobs

A **tag** is indexed. It is what you filter and group by, it is stored as a
dictionary key, and every distinct combination creates a series. A **field** is
the value. It is what you plot, and it is stored as an unindexed column.

So the question "is this an identity or a value?" answers itself:

- **Identity** — dissolved oxygen *of AHU-1*, in the *aeration* area, measured
  in mg/L, sampled every second. All of that is identity. It belongs in tags.
- **Value** — the reading itself. It belongs in a field.

The trap is putting a value in a tag because it is "easy to filter on". A tag
whose value changes creates a new series every time, and InfluxDB's own
retention and compaction work on series. A float that lands in a tag turns a
signal into an unbounded series generator and quietly destroys the database.

**The rule this project enforces:** a tag is something that would still be true
tomorrow. `area`, `equipment`, `measurement`, `eu`. Never the reading.

## Metadata does not go here

Description, vendor, model, install date, calibration history, permit limits —
none of that is time series, and putting it in line protocol means writing it
again on every single point. In this project it goes to Couchbase, keyed by tag,
and is read once when a dashboard needs it.

The concrete cost of getting this wrong is easy to calculate: 57 signals x 1 Hz
x 86 400 = 4.9 million points a day. At 200 bytes of repeated description per
point that is roughly a gigabyte a day of copying a string that never changes.

## Precision, and why this format is lossy on purpose

The timestamp is an integer with a unit suffix: `s`, `ms`, `us`, `ns`. Writing
`ms` when the scan is 20 ms is not a rounding detail — it decides the resolution
of every query you will ever run. This project's contract carries `sample_ms` per
signal, and the writer uses the **coarsest** timestamp that still distinguishes
consecutive samples, because a timestamp more precise than the sample interval
invites you to ask questions the data cannot answer.

Line protocol also distinguishes integers (`1i`) from floats (`1.0`). A float
tagged as an integer is truncated, silently. Counters must be written as
integers, and :func:`format_value` makes that a decision rather than a hope.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from softplc.contract import Contract

#: Fields every point carries, whatever the signal.
#:
#: ``quality`` is the important one. A historian that stores a reading without
#: its validity has thrown away the only thing that distinguishes "the process
#: did this" from "the instrument thinks this". Both look like a number.
#: ``source`` records which protocol face delivered it, which is what makes
#: "Modbus and OPC UA disagree" a queryable question rather than a hunch.
COMMON_FIELDS: Final = ("value", "quality", "source")

#: Tag keys, in a fixed order, so the encoded output is byte-identical for
#: identical input — which makes the format testable by string comparison and the
#: line cache effective.
#:
#: ``field`` is the one that matters, and it was missing the first time this was
#: written. The contract's ``measurement`` is a *group* — every aeration signal
#: is ``measurement: aeration`` — so a key of (area, equipment, measurement, eu)
#: is **identical for six different signals** on AHU-1. InfluxDB identifies a
#: field by series + field + timestamp, so those six would have written to the
#: same field at the same timestamps and overwritten each other. Silently.
#:
#: The contract's own rule says "tag by identity, field by value", and ``field``
#: (``do_mg_l``, ``nh4_out_mg_l``) is the identity of a signal. ``measurement``
#: is a grouping for querying, not an identity.
TAG_KEYS: Final = ("area", "equipment", "field", "eu", "site")

#: The InfluxDB measurement is the contract's measurement *group*, so
#: ``SELECT mean(value) FROM aeration WHERE time > now() - 1h`` is one scan over
#: one stage. A measurement per signal — what most tutorials do — makes that
#: query a UNION of eleven measurements and puts the signal name where InfluxDB
#: treats it as a partition key.
DEFAULT_MEASUREMENT: Final = "signal"

SITE: Final = "PLANT-A"


class LineProtocolError(ValueError):
    """Raised for input that cannot be encoded. Never for a bad *value*."""


@dataclass(frozen=True, slots=True)
class PointKey:
    """The identity of a series, resolved from the contract.

    Held separately from the value because this is the part that must never
    change, and keeping it in its own type makes the separation enforceable
    rather than aspirational.
    """

    signal_id: str
    area: str
    equipment: str
    field: str
    measurement: str
    eu: str
    site: str = SITE

    @property
    def influx_measurement(self) -> str:
        """The measurement this point is written to: the contract's group name."""
        return self.measurement or DEFAULT_MEASUREMENT

    def tags(self) -> dict[str, str]:
        return {
            "area": self.area,
            "equipment": self.equipment,
            "field": self.field,
            "eu": self.eu,
            "site": self.site,
        }

    @property
    def series_key(self) -> str:
        """Measurement plus tags — the identity of the series, without the value.

        Two points with the same series key belong to the same series. Used by the
        tests to prove that changing a value does not change the series, which is
        the property the whole schema depends on.
        """
        tags = ",".join(f"{k}={self.tags()[k]}" for k in TAG_KEYS)
        return f"{self.influx_measurement},{tags}"


def keys_from_contract(contract: Contract) -> dict[str, PointKey]:
    """Resolve every signal in the contract to a series identity.

    Built once at startup and looked up per point afterwards. Doing the contract
    walk per point would be a dict lookup per tag per sample, which at 50 signals
    and 50 scans a second is 2 500 avoidable resolutions a second.
    """
    out: dict[str, PointKey] = {}
    for sid, sig in contract.signals.items():
        out[sid] = PointKey(
            signal_id=sid,
            area=sig.area,
            field=sig.field,
            # A signal whose holder is a grouping (site weather, influent flow)
            # still needs a stable tag value. Using the area keeps the tag
            # populated without inventing a piece of equipment that is not there.
            equipment=sig.equipment or sig.area,
            measurement=sig.measurement,
            eu=sig.eu,
        )
    return out


# ─── escaping ─────────────────────────────────────────────────────────────────


def escape_tag(value: str) -> str:
    """Escape a tag value.

    Line protocol has no escaping — a comma, equals sign or space in a tag value
    silently changes the structure of the line. The result is a point that parses
    and holds the wrong tags, which is worse than a parse error because nothing
    complains.

    Equipment names in this project are already safe, so this is called on
    *every* value rather than trusting that. The test uses a name that would
    break it.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("=", "\\=")
        .replace(" ", "\\ ")
    )


def escape_measurement(value: str) -> str:
    """Escape a measurement name. Commas and spaces only — the other characters
    are legal inside a measurement."""
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def format_value(value: float) -> str:
    """Format a field value.

    Floats always get a decimal point, because ``3`` and ``3.0`` are *different
    types* in line protocol. A value that arrives as ``3.0`` and is written as
    ``3`` becomes an integer field, and a later query that sums or averages the
    column gets a type error — or worse, silently coerces.

    Non-finite values are written as strings, because line protocol has no
    literal for NaN or infinity. Writing the string ``"NaN"`` keeps the fact that
    the instrument broke, which is the whole point of recording it. Dropping the
    point instead would make the gap invisible.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if not math.isfinite(value):
        return f'"{value}"'
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{value:.1f}"
    return repr(float(value))


def format_timestamp(ts_ms: int, resolution: str = "ms") -> str:
    """Render a timestamp in the requested resolution.

    The resolution is part of the *data*, not a display preference. A value
    written with an ``s`` suffix has been quantised to the second, and a query
    that buckets it finer is reading precision that does not exist.
    """
    if resolution not in ("ns", "us", "ms", "s"):
        raise LineProtocolError(
            f"timestamp resolution must be ns, us, ms or s, got {resolution!r}"
        )
    divisor = {"s": 1000, "ms": 1, "us": 1 / 1000, "ns": 1 / 1_000_000}[resolution]
    if resolution == "s":
        return str(int(ts_ms) // 1000)
    return str(int(ts_ms / divisor))


# ─── the encoder ──────────────────────────────────────────────────────────────


def encode_point(
    key: PointKey,
    value: float,
    ts_ms: int,
    *,
    quality: int = 0,
    source: str = "opcua",
    resolution: str = "ms",
) -> str:
    """Encode one point as a line-protocol line.

    ``quality`` and ``source`` are separate parameters rather than being folded
    into the value, because they are facts *about* the reading and a caller that
    had to remember them would eventually forget.
    """
    tags = key.tags()
    tag_part = ",".join(
        f"{k}={escape_tag(tags[k])}" for k in TAG_KEYS
    )
    field_part = ",".join(
        (
            f"value={format_value(value)}",
            f"quality={quality}i",
            f"source={escape_tag(source)}",
        )
    )
    return (
        f"{escape_measurement(key.influx_measurement)},{tag_part} "
        f"{field_part} {format_timestamp(ts_ms, resolution)}"
    )


def encode_batch(points: list[str]) -> str:
    """Join lines into one write payload.

    Batched because one HTTP request per point turns 57 points a scan into 57
    round trips. The join is newline-separated and the trailing newline is
    included, which is what InfluxDB's own examples do and what avoids a
    truncated final line.
    """
    if not points:
        return ""
    return "\n".join(points) + "\n"


def decode_line(line: str) -> dict[str, Any]:
    """Parse a line back into its parts.

    This exists for the tests, and it is worth being explicit that it is a
    *test* tool rather than a general parser. A real line-protocol parser has to
    handle every escape, every field type and the full grammar, and a partial
    implementation used only by tests can quietly disagree with the server about
    what a line means.

    It is still here, because "the encoder round-trips" is a claim worth being
    able to check, and checking it against a second implementation written from
    the specification catches disagreements that comparing to a hardcoded string
    cannot.
    """
    # Split on *unescaped* spaces. An escaped space inside a tag value is still a
    # literal space character in the string, so a naive partition(" ") cuts a
    # tag value in half and produces a line that parses into nonsense: right
    # measurement, wrong tags, and no error. Written from the specification, this
    # parser and the encoder above it disagreed until the escape handling was
    # made symmetric — which is the entire argument for having a second
    # implementation to check the first.
    head, _, rest = _partition_unescaped(line, " ", first=True)
    fields_part, _, ts_part = _partition_unescaped(rest, " ", first=False)
    measurement, _, tag_part = head.partition(",")
    tags = {}
    for pair in _split_unescaped(tag_part, ","):
        k, _, v = pair.partition("=")
        tags[k] = _unescape(v)
    fields = {}
    for pair in _split_unescaped(fields_part, ","):
        k, _, v = pair.partition("=")
        fields[k] = v
    return {
        "measurement": _unescape(measurement),
        "tags": tags,
        "fields": fields,
        "timestamp": int(ts_part),
    }


def _partition_unescaped(text: str, sep: str, *, first: bool) -> tuple[str, str, str]:
    """Split at the first (or last) *unescaped* separator.

    ``first=False`` mirrors ``str.rpartition``: the last unescaped separator, so a
    timestamp containing an escaped space stays intact.
    """
    positions = [
        i for i, ch in enumerate(text) if ch == sep and not _is_escaped(text, i)
    ]
    if not positions:
        return text, "", ""
    i = positions[0] if first else positions[-1]
    return text[:i], sep, text[i + len(sep):]


def _is_escaped(text: str, i: int) -> bool:
    """True if the character at ``i`` is preceded by an odd run of backslashes."""
    backslashes = 0
    j = i - 1
    while j >= 0 and text[j] == "\\":
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


def _split_unescaped(text: str, sep: str) -> list[str]:
    out, buf, escaped = [], [], False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == sep:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _unescape(text: str) -> str:
    return (text.replace("\\,", ",")
                .replace("\\=", "=")
                .replace("\\ ", " ")
                .replace("\\\\", "\\"))
