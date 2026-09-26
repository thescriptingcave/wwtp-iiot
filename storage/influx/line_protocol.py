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

## What InfluxDB 3 actually allows, measured rather than assumed

Written after running the database. It contradicts most InfluxDB documentation
and every tutorial, because those describe InfluxDB 1.x and 2.x. Each constraint
was established by writing to a live `quay.io/influxdb/influxdb3` instance and
reading the error.

### 1. At most two fields per point

    aeration,... value=2.03,quality=0i,source=opcua 1700000010000000000
    -> Could not parse entire line.
       Found trailing content: ',source=opcua 1700000010000000000'

Two is fine. The schema below uses both with nothing to spare, which is why
`source` is a tag rather than a third field.

### 2. A table's tag *set* is fixed by its first write

    Detected a new tag 'source' in write. The tag set is immutable on first
    write to the table.

In InfluxDB 2.x any point could carry any tags and every series was independent.
In InfluxDB 3 a table has a schema and the first write defines it. This is the
biggest single difference, and it is why every signal here writes the *same six*
tag keys, differing only in their values. A signal that omitted one would be
rejected rather than stored with a null.

### 3. A column's type is fixed by its first write

    invalid field value in line protocol for field 'value':
    expected type iox::column_type::field::float

So `value` is a float forever, and a quoted string is not a way to record a
broken sensor.

### 4. A non-finite reading is stored as an *absent* value

`value=NaN` and `value=+Inf` both fail with "No fields were provided" - the format
has no non-finite literal. But a point carrying *only* a quality field is
accepted, and lands with `value` NULL:

    aeration,... quality=2i 1700000014000000000
    -> time 2023-11-14T22:13:34, value NULL, quality 2

That is not a workaround, it is the correct representation, and the database
enforces it: a failed instrument is recorded as *present and invalid*, which is
distinguishable both from "no data at all" and from "a number that happens to be
wrong". InfluxDB 2.x would have accepted a string `"nan"` in a float column, or
a float NaN that plots as a gap and reads as a process that stopped. The refusal
is the feature.

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

from softplc.contract import QUALITY_BAD, Contract

# ``QUALITY_BAD`` is imported rather than redefined. A second constant that has to
# be kept in step with the first is a place for the two to disagree, and a
# disagreement there would mean writing "Bad" for a reading that is merely
# uncertain - the exact failure this project exists to prevent.

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
#:
#: ``source`` was a field until the two-field limit made that impossible. As a tag
#: it is still one of the smallest, best-indexed things on the row, and it is what
#: makes "Modbus and OPC UA disagree" a query rather than a hunch.
#:
#: All six appear on every point without exception. InfluxDB 3 fixes a table's tag
#: set at its first write, so a signal that omitted one is rejected, not nulled.
#:
#: The fifth column is ``signal``, and it was called ``field`` until a live query
#: proved that impossible. ``field`` is a **reserved word in InfluxQL**:
#:
#:     SELECT field, value FROM aeration
#:     -> error in InfluxQL statement: parsing error:
#:        invalid SELECT statement, expected field at pos 7
#:
#: Quoting it works, so the alternative is a column called ``"field"`` that has to
#: be quoted in every query — which for this project means every lesson in the
#: ``sql/`` track, and a learner who has to quote an identifier before their first
#: query has already learned the wrong first lesson. ``signal`` is not reserved,
#: and it is the word this project already uses.
TAG_KEYS: Final = ("area", "equipment", "signal", "eu", "site", "source")

#: Field keys. Exactly two, because two is the maximum. ``quality`` is not optional
#: bookkeeping: a historian that stores a reading without its validity has thrown
#: away the only thing that distinguishes "the process did this" from "the
#: instrument thinks this". Both look like a number.
FIELD_KEYS: Final = ("value", "quality")

#: InfluxDB 3 rejects a third field. Named so the limit is visible where the schema
#: is built rather than discovered in production.
MAX_FIELDS_PER_POINT: Final = 2

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

    def tags(self, source: str = "opcua") -> dict[str, str]:
        """The six tag columns.

        ``source`` is a parameter rather than a field of the key because it
        describes the *delivery*, not the signal: the same reading arrives over
        Modbus and over OPC UA, and which one wrote the row is a property of the
        write.
        """
        return {
            "area": self.area,
            "equipment": self.equipment,
            "signal": self.field,
            "eu": self.eu,
            "site": self.site,
            "source": source,
        }

    @property
    def series_key(self) -> str:
        """Table plus the five identity tags: the series, without the value or the
        delivery.

        Two points with the same series key belong to the same series. Used by the
        tests to prove that changing a value does not change the series, which is
        the property the whole schema depends on.
        """
        tags = self.tags()
        identity = ",".join(f"{k}={tags[k]}" for k in TAG_KEYS if k != "source")
        return f"{self.influx_measurement},{identity}"


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

    Non-finite values raise. They cannot be represented: line protocol has no
    literal for them, and InfluxDB 3 fixes a column's type on its first write, so
    a quoted ``"nan"`` in a float column is a permanent type error rather than a
    record of a broken instrument. :func:`encode_point` writes those readings with
    the value field omitted and ``quality=Bad``, which is both representable and
    more honest.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if not math.isfinite(value):
        # Reached only by a direct call. :func:`encode_point` handles non-finite
        # readings by omitting the value field entirely, because InfluxDB 3 fixes
        # the column type on first write and a quoted string would make every
        # later write to that table a permanent type error.
        raise LineProtocolError(
            f"non-finite value {value!r} cannot be written as a field; "
            "encode_point omits the value and records the quality instead"
        )
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
    table: str | None = None,
) -> str:
    """Encode one point as a line-protocol line.

    ``quality`` and ``source`` are separate parameters rather than being folded
    into the value, because they are facts *about* the reading and a caller that
    had to remember them would eventually forget.

    ``table`` overrides the destination, which is the contract's measurement group
    by default. It exists for two real reasons and one test reason, and the test
    reason is the honest one: InfluxDB 3 fixes a table's schema on its first write
    and *never* releases it, so a table that has been written to incorrectly
    cannot be repaired — only replaced. A test suite that shares the production
    table therefore has no way to recover from its own mistakes, and needs to
    write somewhere disposable. The production reasons are per-environment layouts
    and multi-bucket fan-out.
    """
    tags = key.tags(source)
    tag_part = ",".join(f"{k}={escape_tag(tags[k])}" for k in TAG_KEYS)

    if math.isfinite(value):
        fields = f"value={format_value(value)},quality={quality}i"
    else:
        # A reading that is not a number is recorded as *present and invalid*:
        # the row exists, the quality says Bad, and the value column is NULL.
        # This is the one representation that survives InfluxDB 3's fixed column
        # types, and it happens to be the one that is actually correct — it is
        # distinguishable from "no data" and from "a number that is merely wrong".
        fields = f"quality={QUALITY_BAD}i"

    destination = table or key.influx_measurement
    return (
        f"{escape_measurement(destination)},{tag_part} "
        f"{fields} {format_timestamp(ts_ms, resolution)}"
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
