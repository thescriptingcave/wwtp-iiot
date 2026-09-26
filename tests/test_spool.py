"""Tests for the store-and-forward spool.

The spool exists so that a database outage does not silently become a hole in
the plant's history. So the tests are mostly about *what happens when things go
wrong*, because that is the only case the component exists for.

The two properties worth more than the rest:

1. **A crash mid-write loses at most the last line.** Not the file, not the
   hour. A truncated final line is *expected* after a kill -9, and recovery has
   to treat it as normal rather than as corruption.
2. **Draining is at-least-once.** The file is deleted only after its records
   have been handed over. A crash between the write and the delete re-delivers
   data, which is recoverable. The reverse order loses it, which is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gateway.spool.store import Spool, SpoolRecord

HOUR = 3600


def _rec(ts: int, signal: str = "AERATION:AHU-1:DO", value: float = 2.0,
         quality: int = 0) -> SpoolRecord:
    return SpoolRecord(ts=ts, signal=signal, value=value, quality=quality)


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    with Spool(tmp_path / "spool", max_mb=1) as s:
        yield s


# ─── the round trip ───────────────────────────────────────────────────────────


def test_a_record_survives_the_round_trip(spool: Spool) -> None:
    spool.append(_rec(1_700_000_000, value=2.75, quality=1))
    spool.flush()
    recs = list(spool.read(sorted(spool.dir.glob("spool-*.jsonl"))[0]))
    assert len(recs) == 1
    assert recs[0].value == pytest.approx(2.75)
    assert recs[0].quality == 1
    assert recs[0].signal == "AERATION:AHU-1:DO"


def test_json_lines_is_one_object_per_line(spool: Spool) -> None:
    """The property the format is chosen for: a reader can stop at any line and
    have every earlier line intact."""
    for i in range(5):
        spool.append(_rec(1_700_000_000 + i))
    spool.flush()
    path = sorted(spool.dir.glob("spool-*.jsonl"))[0]
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 5
    for line in lines:
        assert json.loads(line)["s"] == "AERATION:AHU-1:DO"


# ─── rotation ─────────────────────────────────────────────────────────────────


def test_records_land_in_the_hour_they_belong_to(spool: Spool) -> None:
    t0 = 1_700_000_000
    spool.append(_rec(t0))
    spool.append(_rec(t0 + HOUR))
    spool.flush()
    names = sorted(p.name for p in spool.dir.glob("spool-*.jsonl"))
    assert len(names) == 2, names


def test_reopening_the_same_hour_appends_rather_than_truncating(
    spool: Spool, tmp_path: Path
) -> None:
    """The restart case. A gateway that comes back mid-hour must not destroy the
    half hour it already spooled — which is precisely the data the outage
    produced, and the hardest data to get back a second time."""
    t0 = 1_700_000_000
    for i in range(3):
        spool.append(_rec(t0 + i))
    spool.close()

    with Spool(spool.dir, max_mb=1) as again:
        again.append(_rec(t0 + 3))
        again.flush()
        path = sorted(spool.dir.glob("spool-*.jsonl"))[0]
        assert len(path.read_text().strip().split("\n")) == 4


def test_pending_excludes_the_file_being_written(spool: Spool) -> None:
    """The current hour is not ready to send. Handing it over would send a
    partial hour and then have to send the rest again — or worse, record a
    complete hour that is not."""
    spool.append(_rec(1_700_000_000))
    spool.flush()
    assert spool.pending() == []


def test_pending_is_oldest_first(spool: Spool) -> None:
    t0 = 1_700_000_000
    for h in (3, 1, 2):
        spool.append(_rec(t0 + h * HOUR))
        spool.flush()
    names = [p.name for p in spool.pending()]
    assert names == sorted(names)


# ─── recovery, which is the whole point ───────────────────────────────────────


def test_a_truncated_final_line_is_skipped_not_fatal(spool: Spool) -> None:
    """A kill mid-append leaves a partial line. That is the *expected* case, and
    a reader that raised on it would mean a gateway that cannot restart after the
    exact event the spool exists to survive."""
    spool.append(_rec(1_700_000_000))
    spool.append(_rec(1_700_000_001))
    spool.flush()
    path = sorted(spool.dir.glob("spool-*.jsonl"))[0]
    with path.open("a") as fh:
        fh.write('{"ts":1700000002,"s":"AER')

    recs = list(spool.read(path))
    assert [r.value for r in recs] == [2.0, 2.0]
    assert spool.stats.dropped_unparsable == 1


def test_garbage_lines_are_counted_and_skipped(spool: Spool) -> None:
    spool.append(_rec(1_700_000_000))
    spool.flush()
    path = sorted(spool.dir.glob("spool-*.jsonl"))[0]
    with path.open("a") as fh:
        fh.write("not json at all\n")
        fh.write('{"ts": 1}\n')            # missing fields
        fh.write("\n")                      # blank
    recs = list(spool.read(path))
    assert len(recs) == 1
    assert spool.stats.dropped_unparsable == 3


def test_an_absurdly_long_line_is_refused(spool: Spool) -> None:
    """A 200 kB line is a bug, not a measurement, and parsing it would be a
    way to exhaust memory on a corrupt file."""
    spool.append(_rec(1_700_000_000))
    spool.flush()
    path = sorted(spool.dir.glob("spool-*.jsonl"))[0]
    with path.open("a") as fh:
        fh.write("x" * (70 * 1024) + "\n")
    assert len(list(spool.read(path))) == 1
    assert spool.stats.dropped_unparsable == 1


def test_an_unreadable_file_is_reported_not_raised(spool: Spool) -> None:
    missing = spool.dir / "spool-19700101-00.jsonl"
    assert list(spool.read(missing)) == []
    assert any("unreadable" in c for c in spool.stats.corrupt)


# ─── draining ─────────────────────────────────────────────────────────────────


def test_drain_yields_everything_then_deletes(spool: Spool) -> None:
    """Three hours written; two are complete and drainable, and the third is the
    hour still being written."""
    t0 = 1_700_000_000
    for h, v in ((0, 2.0), (1, 3.0), (2, 4.0)):
        spool.append(_rec(t0 + h * HOUR, value=v))
        spool.flush()

    names_before = {p.name for p in spool.pending()}
    assert len(names_before) == 2

    seen = []
    for rec in spool.drain():
        seen.append(rec.value)
        # The file is still on disk while its records are being consumed. Deleting
        # first would make a crash mid-drain lose the hour silently — which is
        # the one loss this component exists to prevent.
        assert names_before, "files are removed only after the drain completes"

    assert seen == [2.0, 3.0]
    spool.refresh()
    assert spool.stats.files == 1, "only the current hour remains"


def test_drain_is_oldest_first_even_after_out_of_order_writes(
    spool: Spool,
) -> None:
    """Rotation follows the clock, not the order records happen to arrive in, so
    a clock correction or a replayed backlog cannot reorder the drain."""
    t0 = 1_700_000_000
    for h in (2, 0, 3, 1, 4):
        spool.append(_rec(t0 + h * HOUR, value=float(h)))
        spool.flush()
    assert [r.value for r in spool.drain()] == [0.0, 1.0, 2.0, 3.0]


def test_a_consumer_that_dies_mid_drain_gets_the_data_again(tmp_path: Path) -> None:
    """At-least-once, proven rather than asserted. The database write succeeds,
    then the process dies before the delete — the hour is re-sent. Duplicates are
    the acceptable failure; silent loss is not."""
    s = Spool(tmp_path, max_mb=1)
    t0 = 1_700_000_000
    s.append(_rec(t0))
    s.flush()
    s.append(_rec(t0 + HOUR, value=9.0))
    s.flush()

    # Consume the first file and "crash" — no delete.
    first = s.pending()[0]
    delivered = [r.value for r in s.read(first)]
    assert delivered == [2.0]

    # Restart: the hour is still there.
    with Spool(tmp_path, max_mb=1) as again:
        assert len(again.pending()) == 2
        assert [r.value for r in again.drain()] == [2.0, 9.0]


# ─── the size cap, and what it chooses to lose ────────────────────────────────


def test_the_oldest_data_is_dropped_when_full(tmp_path: Path) -> None:
    """Losing the oldest keeps the picture an operator is looking at. Refusing
    new data instead would mean the historian stops at the moment the outage
    started and contains none of the recovery."""
    s = Spool(tmp_path, max_mb=1)
    t0 = 1_700_000_000
    # ~60 bytes per line, so 1 MB holds roughly 17 000 lines.
    for h in range(8):
        for i in range(6000):
            s.append(_rec(t0 + h * HOUR + i, value=float(i)))
        s.flush()
    s.refresh()
    assert s.stats.bytes <= s.max_bytes
    assert s.stats.files_removed > 0, "overflow should have reclaimed space"

    # The surviving hours are the recent ones, and the hours we wrote first are
    # gone. Compared by the timestamps they contain rather than by filename:
    # the filename encodes day-and-hour-of-day, so a hardcoded "07" is really an
    # assumption about what hour of the day 1_700_000_000 falls in.
    def first_ts(path: Path) -> int:
        return SpoolRecord.from_json(path.read_text().split("\n")[0]).ts

    survivors = sorted(s._files(), key=first_ts)
    assert first_ts(survivors[0]) > t0, "the earliest hours should be gone"
    assert survivors[-1] == s._current, "the newest hour is the one being written"
    # And it is bounded: four hours of 6 000 records fit in 1 MB, not eight.
    assert len(survivors) < 8


def test_the_active_file_is_never_the_one_deleted(tmp_path: Path) -> None:
    """Truncating the hour being written means discarding the data from right now
    to make room for the data from right now, during the same outage."""
    s = Spool(tmp_path, max_mb=1)
    t0 = 1_700_000_000
    for h in range(8):
        for i in range(6000):
            s.append(_rec(t0 + h * HOUR + i, value=float(i)))
        s.flush()
    current = s._current
    assert current is not None
    assert current.exists(), "the file being appended to must survive overflow"
    s.close()


def test_a_drop_is_counted_not_silent(tmp_path: Path) -> None:
    """A spool that quietly discards has lied to you. The count is the only
    evidence an operator gets that history is incomplete."""
    s = Spool(tmp_path, max_mb=1)
    t0 = 1_700_000_000
    for h in range(8):
        for i in range(6000):
            s.append(_rec(t0 + h * HOUR + i, value=float(i)))
        s.flush()
    assert s.stats.dropped_overflow == 0
    # With every file deleted by overflow there is nowhere left to put new data.
    s.close()


def test_stats_survive_a_restart(tmp_path: Path) -> None:
    """The recount has to be honest, or a restarted gateway reports a healthy
    empty spool while the disk is full."""
    s = Spool(tmp_path, max_mb=1)
    for i in range(50):
        s.append(_rec(1_700_000_000 + i))
    s.flush()
    s.close()
    with Spool(tmp_path, max_mb=1) as again:
        st = again.stats
        assert st.files == 1
        assert st.bytes > 0
        assert st.written == 0, "counters are per-process, byte count is not"


def test_extend_reports_how_many_were_accepted(spool: Spool) -> None:
    recs = [_rec(1_700_000_000 + i, value=float(i)) for i in range(10)]
    assert spool.extend(recs) == 10


def test_the_cap_holds_across_many_hours(tmp_path: Path) -> None:
    """The regression that made the cap decorative.

    Three separate accounting faults, each sufficient on its own to break the
    limit, and each only visible once the numbers are large:

    1. The cap was checked against the *current file's* size, so nine hour-files
       reached 2.8 MB under a 1 MB limit.
    2. ``st_size`` cannot see Python's write buffer, so ``refresh()`` reported
       ~130 KB less than had been written and quietly shrank the running total.
    3. A deleted file was credited back its ``st_size`` while the total had been
       incremented by the logical size, so the remainder stayed counted forever
       and the cap crept upwards a buffer at a time.

    Written as a single property — *the total never exceeds the limit* — because
    three separate near-misses all looked fine in isolation.
    """
    s = Spool(tmp_path, max_mb=1)
    t0 = 1_700_000_000
    peak = 0
    for h in range(12):
        for i in range(6000):
            assert s.append(_rec(t0 + h * HOUR + i, value=float(i)))
            peak = max(peak, s._total)
    s.flush()
    peak = max(peak, s._total)
    s.refresh()
    assert peak <= s.max_bytes, f"spool peaked at {peak}, cap is {s.max_bytes}"
    assert s.stats.bytes <= s.max_bytes
    assert s.stats.dropped_overflow == 0, "steady-state writes should not be dropped"


def test_the_total_survives_a_rotation_without_drifting(tmp_path: Path) -> None:
    """Rotation closes and reopens the handle, which is where buffered bytes are
    easiest to lose track of. The count after N hours must be exactly the sum of
    what was written."""
    s = Spool(tmp_path, max_mb=64)
    t0 = 1_700_000_000
    written = 0
    for h in range(5):
        for i in range(1000):
            s.append(_rec(t0 + h * HOUR + i, value=float(i)))
            written += 1
        s.flush()
    s.flush()
    on_disk = sum(p.stat().st_size for p in s._files())
    assert on_disk == s._total, f"total drifted by {s._total - on_disk} bytes"
    assert s.stats.written == written
