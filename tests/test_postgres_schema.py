"""Does the contract fit ``storage/postgres/schema.sql``?

This file is the drift guard the schema's own comment promises. The reasoning
behind it is worth stating, because the guard moved.

When readings lived in InfluxDB the danger was structural: nine tables' worth of
generated DDL had to stay in step with a text encoder, and a field added to the
contract would either be silently dropped or would create a new table the first
time a point mentioned it. Both failures were quiet.

Now the DDL is five tables with fixed columns, and the danger has moved to the
*rows*: a contract field with nowhere to go, or a contract signal the foreign
key will refuse. That is a much easier thing to check, and it is checkable
without a database — every assertion here is against the contract and the SQL
text, so the guard runs in the ordinary unit suite rather than only when
Postgres happens to be running.
"""

from __future__ import annotations

import re

import pytest
from softplc.contract import Contract
from softplc.contract import contract as get_contract
from storage.postgres.schema import SCHEMA_PATH, _signal_rows

SCHEMA = SCHEMA_PATH.read_text(encoding="utf-8")
SCHEMA_SOURCE = SCHEMA_PATH.with_name("schema.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def c() -> Contract:
    return get_contract()


def _columns(table: str) -> list[str]:
    """The column names in a CREATE TABLE, in order.

    Parsed rather than imported, because the point is to check the *file* — the
    artifact a reviewer reads — against the contract, not against a Python object
    that might be built from the same wrong assumption.

    Multi-line CHECK constraints are stripped first, as balanced parenthesis
    groups. A line-oriented parser does not survive them: a constraint that
    wraps across four lines contributes ``AND`` and ``OR`` as if they were
    columns, which is how this function's first version reported a schema with
    three columns called ``AND``.
    """
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA, re.S
    )
    assert match, f"{table} not found in schema.sql"
    body = match.group(1)

    depth, buf = 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0:
            buf.append(char)
    flat = "".join(buf)

    names = []
    for line in flat.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        name = stripped.split()[0]
        if name.upper() in {
            "CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "EXCLUDE",
        }:
            continue
        names.append(name.rstrip(","))
    return names


# ─── the contract's signals all have a row, and the row has all the fields ───


def test_every_signal_produces_exactly_one_row(c: Contract) -> None:
    rows = _signal_rows(c)
    assert len(rows) == len(c.signals)
    assert {r[0] for r in rows} == set(c.signals)


def test_the_row_tuple_matches_the_signals_insert(c: Contract) -> None:
    """``_signal_rows`` and the INSERT in ``seed_metadata`` must agree.

    Both are in the same file and it is entirely possible to reorder one. The
    INSERT is written out here rather than imported so that a change to the
    query is a change this test notices.
    """
    insert = re.search(
        r"INSERT INTO signal\s*\((.*?)\)\s*VALUES", SCHEMA_SOURCE, re.S
    )
    assert insert, "could not find the signal INSERT in schema.py"
    cols = [c_.strip() for c_ in insert.group(1).replace("\n", " ").split(",")]
    # One placeholder per column, plus the leading comma.
    assert len(cols) == 16, cols
    assert cols[0] == "id" and cols[1] == "equipment_id"


SCHEMA_SOURCE = SCHEMA_PATH.with_name("schema.py").read_text(encoding="utf-8")


def test_every_contract_field_reaches_a_column(c: Contract) -> None:
    """Each attribute the seeder reads has a column of the same name.

    Not a tautology: the seeder builds a positional tuple, so a renamed column
    is only caught by the database at run time — which is why this exists.
    """
    cols = set(_columns("signal"))
    required = {
        "id", "equipment_id", "area", "measurement", "field", "unit",
        "range_min", "range_max", "normal_low", "normal_high",
        "deadband", "deadband_mode", "sample_ms", "writable",
        "modbus_address", "modbus_word_order",
    }
    assert cols == required, (
        f"schema.py writes {sorted(cols - required)} and "
        f"schema.sql lacks {sorted(required - cols)}"
    )


def test_equipment_fields_reach_their_table(c: Contract) -> None:
    cols = set(_columns("equipment"))
    for field in ("id", "site_id", "area", "name", "type", "rated_kw",
                  "duty", "fail_modes"):
        assert field in cols, f"equipment.{field} has no column"


# ─── the constraints the schema claims ───────────────────────────────────────


def test_the_null_value_constraint_is_present_and_meaningful() -> None:
    """The invariant the whole quality scale exists to protect.

    Asserted on the *text* because that is what a reviewer reads, and because a
    constraint that has been weakened to `OR true` still parses, still applies,
    and still looks like a constraint in a diff.
    """
    assert "reading_null_is_not_good" in SCHEMA
    body = SCHEMA[SCHEMA.index("reading_null_is_not_good"):]
    line = body[: body.index("\n")]
    assert "OR true" not in line, "a tautological CHECK is a comment, not a rule"
    assert "value IS NOT NULL OR quality <> 0" in line


def test_no_check_constraint_is_a_tautology() -> None:
    """Sweep the whole file.

    Worth doing once, as a rule, because a tautological CHECK is the one piece
    of DDL that is actively misleading: it appears in a diff, it appears in
    the `\\d` view, and it enforces nothing. One was written by accident during this
    migration and caught only by this test.
    """
    for match in re.finditer(r"CHECK \((.*?)\)(?:,|\n)", SCHEMA, re.S):
        expr = " ".join(match.group(1).split())
        assert " OR true" not in expr and " OR TRUE" not in expr, expr


def test_quality_and_source_are_enumerated(c: Contract) -> None:
    """Every quality the contract can produce must be a legal column value.

    The deadband and the plant both write quality codes, and the ones they can
    produce are named in ``softplc.contract``. If the plant ever grows a fourth
    code, this fails here rather than at 3am in the gateway.
    """
    import softplc.contract as mod

    codes = {
        v for k, v in vars(mod).items()
        if k.startswith("QUALITY_") and isinstance(v, int)
    }
    assert codes == {0, 1, 2}, codes
    for code in codes:
        assert re.search(rf"quality IN \([^)]*{code}", SCHEMA), code


# ─── grouping signals and the foreign key ───────────────────────────────────


def test_a_grouping_signal_seeds_a_null_equipment_id(c: Contract) -> None:
    """24 of the 57 signals name a grouping node rather than an asset.

    The foreign key refuses anything else, which is how a genuine contract bug
    was found: the holder ``FLOW`` is not an asset, and nothing in the previous
    storage engine could have said so.
    """
    rows = {r[0]: r[1] for r in _signal_rows(c)}
    groupings = {sid for sid, eq in rows.items() if eq is None}
    assert len(groupings) == 24, len(groupings)
    for sid, eq in rows.items():
        assert eq is None or eq in c.equipment, f"{sid}: {eq} is not equipment"


def test_the_modbus_link_is_attached_to_at_most_one_signal(c: Contract) -> None:
    """A register maps to a signal or to nothing. Never to two.

    ``_signal_rows`` builds a dict from register to signal, so a contract that
    linked two registers to one signal would silently keep the last. This is the
    check that the dict is not hiding a collision.
    """
    linked = [r for r in c.registers if r.signal]
    signals = [r.signal for r in linked]
    assert len(signals) == len(set(signals)), (
        "a signal is exposed on more than one register"
    )
    for signal_id in signals:
        assert signal_id in c.signals, f"{signal_id} is not a declared signal"


def test_word_order_is_recorded_for_every_float_register(c: Contract) -> None:
    """The project's signature bug class, guarded at the schema boundary.

    A 32-bit float whose words arrive low-first reads as roughly ``2.3e-41``
    when decoded high-first: finite, inside every range, and wrong. The defence
    is to carry the word order in the same row as the value, so a query joining
    ``reading`` to ``signal`` can see it. If this fails, the column is gone and
    the trap is back.
    """
    floats = [r for r in c.registers if r.signal and r.word_order]
    assert floats, "expected at least one low-word-first float register"
    rows = {r[0]: r[15] for r in _signal_rows(c)}
    for reg in floats:
        assert rows[reg.signal] == reg.word_order, (
            f"{reg.signal}: schema row says {rows[reg.signal]!r}, "
            f"contract says {reg.word_order!r}"
        )


# ─── retention is not in the DDL, on purpose ────────────────────────────────


def test_retention_intervals_are_not_hardcoded_in_the_schema() -> None:
    """An operational parameter does not belong in a schema file.

    It is applied by ``apply_retention`` from the environment. If an interval
    ever appears here, the file has to be edited to change a policy, and the
    file stops being a schema.
    """
    assert "add_retention_policy" not in SCHEMA
    assert "add_retention_policy" in SCHEMA_SOURCE
