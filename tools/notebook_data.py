"""Create and seed the notebooks' database.

    python -m tools.notebook_data            # create if missing, seed, replace
    python -m tools.notebook_data --status   # say whether it is seeded, change nothing

See `notebooks/_data.py` for why the notebooks have a database of their own. This
is the only thing that writes to it, and it is the same command on a laptop and in
CI, so the two cannot drift apart on what "the notebook data" is.

It always replaces: `--reset` empties `reading` first, and the pinned `--end` means
the result is identical however many times it runs. `make seed` used to append and
double the data; this cannot.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notebooks import _data  # noqa: E402


def ensure_database() -> bool:
    """Create the database if it is absent. Returns True if it created it."""
    import psycopg  # noqa: PLC0415
    from psycopg import sql  # noqa: PLC0415

    admin = _data.dsn().replace(f"dbname={_data.NOTEBOOK_DB}", "dbname=postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (_data.NOTEBOOK_DB,)
        ).fetchone()
        if exists:
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(
            sql.Identifier(_data.NOTEBOOK_DB)))
        return True


def fingerprint() -> tuple[int, ...] | None:
    """The integer fingerprint of `reading`, or None if it cannot be read."""
    try:
        with _data.connect() as conn:
            row = conn.execute(_data.FINGERPRINT_SQL).fetchone()
    except Exception:
        return None
    return tuple(int(v) if v is not None else 0 for v in row)


def status() -> str:
    """One line: is it there, and is it *this* seed."""
    try:
        with _data.connect() as conn:
            first, last = conn.execute(
                "SELECT min(ts), max(ts) FROM reading").fetchone()
    except Exception as exc:
        return f"not available ({type(exc).__name__})"
    got = fingerprint()
    if not got or not got[0]:
        return "empty"
    same = got == tuple(_data.EXPECTED.values())
    if same:
        tag = "the pinned seed"
    else:
        want = tuple(_data.EXPECTED.values())
        tag = f"NOT the pinned seed (expected {want}, got {got})"
    return (
        f"{got[0]:,} readings, "
        f"{first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M} UTC — {tag}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notebook_data")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)

    if args.status:
        print(f"  {_data.NOTEBOOK_DB}: {status()}")
        return 0

    os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
    made = ensure_database()
    print(f"  {_data.NOTEBOOK_DB}: {'created' if made else 'exists'}")

    # Two things, and they are not the same thing.
    #
    # The environment variable is for **this module's own** `dsn()` calls —
    # `ensure_database()` and `connect()` both read it, so it has to say
    # `wwtp_notebooks`.
    #
    # `--database` is for **the seeder**, and it is an argument rather than the
    # environment because the seeder refuses `--reset` without one. That refusal
    # exists because `POSTGRES_DB=... in a recipe` is overridden by `.env` and names
    # the wrong database — and this call was the first thing it broke, on a clean
    # checkout, with an error that blamed the guard rather than the caller.
    #
    # The comment this replaces said "the seeder reads its target from the
    # environment", which was true, and was the assumption behind six separate bugs.
    os.environ["POSTGRES_DB"] = _data.NOTEBOOK_DB

    from storage.seed.main import main as seed  # noqa: PLC0415

    code = seed([
        "--database", _data.NOTEBOOK_DB,
        "--days", str(_data.SEED_DAYS),
        "--end", _data.SEED_END,
        "--storm-after", str(_data.STORM_AFTER_H),
        "--reset",
    ])
    print(f"  {_data.NOTEBOOK_DB}: {status()}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
