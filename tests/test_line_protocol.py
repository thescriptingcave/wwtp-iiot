"""Tests for the line-protocol encoder.

The schema decisions in ``storage/influx/line_protocol.py`` are only worth
anything if they are *enforced*. An unenforced schema rule is a comment, and a
comment about data modelling is worth exactly as much as the code that happens
to be there when the next person changes it.

So the important tests here are the ones that would fail if someone put a value
in a tag, dropped the quality field, or let a float be encoded as an integer.
"""

from __future__ import annotations

import math

import pytest
from softplc.contract import contract
from storage.influx.line_protocol import (
    TAG_KEYS,
    LineProtocolError,
    PointKey,
    decode_line,
    encode_batch,
    encode_point,
    escape_tag,
    format_timestamp,
    format_value,
    keys_from_contract,
)

C = contract()
KEYS = keys_from_contract(C)
DO = "AERATION:AHU-1:DO"


# ─── the schema rules ─────────────────────────────────────────────────────────


def test_the_value_is_a_field_and_never_a_tag() -> None:
    """The rule the whole design rests on: identity in tags, value in fields.

    A value in a tag creates a new series per reading. That is not a storage
    problem, it is a correctness one — retention, compaction and every aggregate
    operate on series, so a signal in a tag has no usable aggregates at all.
    """
    line = encode_point(KEYS[DO], 2.03, 1_700_000_000_000)
    parsed = decode_line(line.strip())
    assert set(parsed["tags"]) == set(TAG_KEYS)
    assert "2.03" not in " ".join(parsed["tags"].values())
    assert parsed["fields"]["value"] == "2.03"


def test_identity_tags_are_stable_across_every_reading() -> None:
    """Changing a value must not change the series. Two points with the same
    series key belong to the same series — that is what makes a trend line work.
    """
    keys = {encode_point(KEYS[DO], v, 1_700_000_000_000).split(" ")[0]
            for v in (0.0, 2.03, 7.9, -1.0, 1e9)}
    assert len(keys) == 1, "the series key changed with the value"


def test_every_signal_gets_a_distinct_series() -> None:
    series = {k.series_key for k in KEYS.values()}
    assert len(series) == len(C.signals), "two signals share a series key"


def test_quality_is_always_written() -> None:
    """A reading without its validity has thrown away the only thing that
    distinguishes 'the process did this' from 'the instrument thinks this'.
    Both look like a number on a chart."""
    for q in (0, 1, 255):
        line = encode_point(KEYS[DO], 2.0, 1_700_000_000_000, quality=q)
        assert decode_line(line.strip())["fields"]["quality"] == f"{q}i"


def test_source_is_recorded_so_protocols_can_be_compared() -> None:
    """'Modbus and OPC UA disagree' should be a query, not a hunch."""
    mb = decode_line(
        encode_point(KEYS[DO], 2.0, 1_700_000_000_000, source="modbus").strip()
    )
    ua = decode_line(
        encode_point(KEYS[DO], 2.0, 1_700_000_000_000, source="opcua").strip()
    )
    assert mb["tags"] == ua["tags"]
    assert mb["fields"]["source"] != ua["fields"]["source"]


def test_measurement_carries_no_metadata() -> None:
    """The tag set is fixed. Adding a `description` or `vendor` tag would repeat
    an unchanged string on every point forever - roughly a gigabyte a day at this
    scan rate - for a fact that belongs in Couchbase."""
    assert set(TAG_KEYS) == {"area", "equipment", "field", "eu", "site"}
    parsed = decode_line(encode_point(KEYS[DO], 1.0, 1).strip())
    assert set(parsed["tags"]) == set(TAG_KEYS)
    assert parsed["measurement"] == KEYS[DO].influx_measurement


def test_a_stage_is_one_measurement_and_signals_stay_distinct_within_it() -> None:
    """The measurement is the contract's group - `aeration`, `effluent` - so one
    stage is one scan. The *signals* inside it are separated by the `field` tag,
    not by the measurement. A measurement per signal - what most tutorials do -
    makes 'compare DO to ammonia in the same basin' a UNION of eleven
    measurements.
    """
    measurements = {
        decode_line(encode_point(k, 1.0, 1).strip())["measurement"]
        for k in KEYS.values()
    }
    assert measurements == {s.measurement for s in C.signals.values()}
    assert len(measurements) < len(C.signals), "expected groups, not one per signal"


def test_field_is_what_separates_signals_sharing_a_group() -> None:
    """The regression this schema rule exists to prevent.

    Six aeration signals on AHU-1 are all `measurement: aeration, eu: mg/L`.
    Keyed without `field`, they are the same series, and InfluxDB identifies a
    field by series + field + timestamp - so they overwrite each other. No error,
    no warning, and a chart that looks plausible because only one of the six
    survived.
    """
    aeration = {k.series_key for k in KEYS.values() if k.measurement == "aeration"}
    assert len(aeration) > 1, "aeration signals collapsed into one series"
    grouped: dict[tuple[str, str], list[str]] = {}
    for k in KEYS.values():
        grouped.setdefault((k.measurement, k.eu), []).append(k.field)
    for (m, eu), fields in grouped.items():
        assert len(fields) == len(set(fields)), (
            f"{m}/{eu}: two signals share a field name"
        )


# ─── values ───────────────────────────────────────────────────────────────────


def test_a_whole_float_stays_a_float() -> None:
    """`3` and `3.0` are different types in line protocol. A value written as an
    integer loses its decimal point permanently, and a later average of the
    column gets a type error or a silent coercion."""
    assert format_value(3.0) == "3.0"
    assert format_value(3) == "3.0"
    assert format_value(0.0) == "0.0"
    assert format_value(-2.0) == "-2.0"
    assert decode_line(
        encode_point(KEYS[DO], 3.0, 1).strip()
    )["fields"]["value"] == "3.0"


def test_fractional_values_keep_their_precision() -> None:
    assert format_value(2.03) == "2.03"
    assert format_value(0.001) == "0.001"


def test_nan_and_infinity_are_recorded_as_strings_not_dropped() -> None:
    """Line protocol has no literal for either. Writing the string keeps the fact
    that the instrument broke, which is the point of recording it at all.
    Dropping the point would make the gap invisible - indistinguishable from a
    period when nothing happened."""
    assert format_value(math.nan) == '"nan"'
    assert format_value(math.inf) == '"inf"'
    assert format_value(-math.inf) == '"-inf"'
    line = encode_point(KEYS[DO], math.nan, 1).strip()
    assert '"nan"' in line


def test_booleans_are_not_numbers() -> None:
    """Equipment state is a boolean and gets written as a boolean. A pump that
    reads 1 is a pump; a pump that reads true is a pump. Only one of those
    averages."""
    assert format_value(True) == "true"
    assert format_value(False) == "false"


# ─── escaping ─────────────────────────────────────────────────────────────────


def test_a_comma_in_a_tag_value_does_not_create_a_new_tag() -> None:
    """Line protocol has no escaping mechanism of its own, so an unescaped comma
    silently changes the structure of the line. The result parses fine and holds
    the wrong tags, which is worse than a parse error because nothing complains.
    """
    escaped = escape_tag("PUMP 1, DUTY")
    # Spaces are escaped too, and have to be: an unescaped space ends the
    # measurement-and-tags section, so a tag value containing one splits the line
    # into a tag and a phantom field. Nothing rejects it.
    assert escaped == "PUMP\\ 1\\,\\ DUTY"
    assert "," not in escaped.replace("\\,", "")
    assert " " not in escaped.replace("\\ ", "")


def test_equals_and_backslash_are_escaped() -> None:
    assert escape_tag("a=b") == "a\\=b"
    assert escape_tag("a\\b") == "a\\\\b"


def test_awkward_names_round_trip() -> None:
    key = PointKey(
        signal_id="X",
        field="flow_rate",
        area="AREA, WITH COMMA",
        equipment="UNIT = 1",
        measurement="flow_rate",
        eu="m3/h",
    )
    parsed = decode_line(encode_point(key, 1.5, 1_700_000_000_000).strip())
    assert parsed["tags"]["area"] == "AREA, WITH COMMA"
    assert parsed["tags"]["equipment"] == "UNIT = 1"


def test_a_source_with_a_comma_does_not_break_the_field_list() -> None:
    line = encode_point(KEYS[DO], 1.0, 1, source="modbus, tcp").strip()
    parsed = decode_line(line)
    assert parsed["fields"]["source"] == "modbus, tcp"
    assert parsed["fields"]["value"] == "1.0"


# ─── timestamps ───────────────────────────────────────────────────────────────


def test_the_timestamp_resolution_is_explicit() -> None:
    """Resolution is part of the data, not a display preference. A value written
    with an `s` suffix has been quantised to the second, and a query that buckets
    it finer is reading precision that does not exist."""
    assert format_timestamp(1_700_000_000_000, "ms") == "1700000000000"
    assert format_timestamp(1_700_000_000_000, "s") == "1700000000"
    assert format_timestamp(1_700_000_000_000, "us") == "1700000000000000"
    assert format_timestamp(1_700_000_000_000, "ns") == "1700000000000000000"


def test_an_unknown_resolution_is_rejected() -> None:
    with pytest.raises(LineProtocolError, match="resolution"):
        format_timestamp(1, "fortnights")


def test_a_scan_uses_a_timestamp_distinct_from_its_neighbour() -> None:
    """The reason the writer picks a resolution from sample_ms: at a 20 ms scan,
    a second-resolution timestamp would collapse every reading in a second into
    one point and silently lose 49 of every 50 samples."""
    base = 1_700_000_000_000
    lines = [
        encode_point(KEYS[DO], float(i), base + i * 20, resolution="ms").strip()
        for i in range(50)
    ]
    stamps = {decode_line(ln)["timestamp"] for ln in lines}
    assert len(stamps) == 50


# ─── batching ─────────────────────────────────────────────────────────────────


def test_a_batch_is_newline_separated_with_a_trailing_newline() -> None:
    payload = encode_batch(["a", "b"])
    assert payload == "a\nb\n"


def test_an_empty_batch_is_empty_not_a_newline() -> None:
    """One stray newline is a malformed write, and a malformed write from an
    empty scan is an easy way to produce one every 20 ms forever."""
    assert encode_batch([]) == ""


def test_a_batch_of_one_scan_is_57_lines() -> None:
    ts = 1_700_000_000_000
    lines = [encode_point(k, 1.0, ts) for k in KEYS.values()]
    payload = encode_batch(lines)
    assert len(payload.strip().split("\n")) == len(C.signals)


# ─── determinism ──────────────────────────────────────────────────────────────


def test_encoding_is_byte_identical_for_identical_input() -> None:
    """So the format can be compared as a string in tests, and so the write path
    can skip identical payloads."""
    a = encode_point(KEYS[DO], 2.03, 1_700_000_000_000, quality=1, source="modbus")
    b = encode_point(KEYS[DO], 2.03, 1_700_000_000_000, quality=1, source="modbus")
    assert a == b


def test_the_line_has_the_shape_the_specification_describes() -> None:
    line = encode_point(KEYS[DO], 2.03, 1_700_000_000_000,
                        quality=1, source="modbus")
    measurement_and_tags, fields, timestamp = line.split(" ")
    assert measurement_and_tags.startswith(f"{KEYS[DO].influx_measurement},")
    assert fields.startswith("value=2.03,quality=1i,source=modbus")
    assert timestamp == "1700000000000"


# ─── the contract really does cover everything ────────────────────────────────


def test_every_contract_signal_resolves_to_a_key() -> None:
    assert set(KEYS) == set(C.signals)


def test_a_signal_with_no_equipment_still_gets_a_populated_tag() -> None:
    """Site weather and influent flow hang off a grouping rather than a piece of
    equipment. Leaving the tag empty would collapse every such signal into one
    series."""
    grouping = [s for s in C.signals.values() if not s.equipment]
    for sig in grouping:
        key = KEYS[sig.id]
        assert key.equipment, f"{sig.id} has an empty equipment tag"
        assert key.equipment == sig.area
