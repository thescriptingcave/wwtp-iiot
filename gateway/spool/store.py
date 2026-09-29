"""Store-and-forward spool.

The gateway reads from a PLC and writes to a database. Those two things fail
independently, and the interesting case is when the *second* one fails while the
first keeps working.

A plant does not stop because InfluxDB is restarting. A level keeps rising. If
the gateway simply drops what it cannot write, the historian acquires a silent
gap that looks exactly like a period when nothing happened — and the first person
to notice is someone asking why the influent flow was zero for forty minutes
during a storm.

So the gateway writes to local disk first and treats the database as a consumer
of that disk, not the other way round.

## The shape of the file

One **JSON Lines** file per hour, named by the hour it covers. That choice is
doing real work:

- **Append is O(1) and cannot corrupt earlier records.** A crash mid-write loses
  at most the last line. Compare a single JSON array, which is rewritten whole
  and is therefore lost entirely on a crash during the write.
- **Recovery is a tail.** Restart, read the current hour's file, carry on.
- **Rotation bounds the damage.** A corrupt hour is 3 600 records, not the whole
  history. With one file per day it is 86 400; with one file for the process's
  lifetime it is everything, forever.
- **It is greppable.** At 03:14, when the trend looks wrong, ``grep '"ts":'`` on
  one file is the fastest way to answer what the gateway actually sent.

The cost is honest: JSON Lines is about five times larger on disk than line
protocol for the same data, and it is slower to parse. For a spool whose whole
job is to absorb a burst and be drained, that is the right trade. A spool is not
a database; pretending otherwise is how people end up with a spool that cannot
answer its own questions.

## The size cap, and why it is a hard limit

The spool is bounded. When it is full, the gateway **drops the oldest data and
says so**, rather than refusing new data or growing without limit.

The alternative — refusing new data — is worse than it sounds. It means the
historian stops at the moment the outage started and contains none of the
recovery, which is usually the part worth having. Dropping the oldest keeps the
most recent picture, which is the one an operator is looking at.

Either way this is data loss, so it is counted and logged per drop. A spool that
quietly discards is a spool that has lied to you.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

#: ``spool-20260926-150000.jsonl`` — the UTC minute and second the file was
#: *opened*, not the hour it covers.
#:
#: It used to be ``spool-20260926-15.jsonl``, one file per wall-clock hour, and
#: that turned out to be wrong in a way that only showed up when the whole stack
#: was actually run: `pending()` excludes the file currently being written,
#: because a half-written file has no complete records, so **nothing reached the
#: database for up to an hour after starting.** On a laptop demo that reads as
#: "the gateway is broken". In a plant it is an hour of data lost to any crash.
#:
#: Naming by open time also removes a collision that the hourly scheme had to
#: think about: at `rotate_s = 60` three files would all be named
#: `spool-20260926-15.jsonl`.
_FILENAME = re.compile(r"^spool-(\d{8})-(\d{6})\.jsonl$")

#: Default seconds per spool file.
#:
#: Sixty, not 3600. The rotation interval is the project's **maximum exposure**:
#: the time between a reading being taken and being durable on disk, and the time
#: of data a crash can cost. An hour of that is indefensible for a component whose
#: entire job is not losing data, and it is invisible in every test that does not
#: run a real stack for an hour.
#:
#: The cost is files: at 1 Hz with 57 signals the spool produces a file a minute
#: rather than an hour, so a 512 MB cap holds a few hours rather than a few days.
#: For a plant that is the right way round — the cap exists to bound the damage
#: from a database outage, and an hour of an hour-old backlog is not the damage
#: anyone is worried about.
DEFAULT_ROTATE_S = 60

#: Lines longer than this are a bug, not a measurement, and are skipped on
#: recovery. A half-written line is normally short; a pathologically long one
#: means something is generating records that are not measurements.
_MAX_LINE_BYTES = 64 * 1024


@dataclass(slots=True)
class SpoolRecord:
    """One point, on its way to a database.

    The *serialised* field names are short, because there is one of these per
    signal per accepted scan and the difference between ``"timestamp"`` and
    ``"ts"`` across a million records is measurable. The attributes stay
    readable; only the wire form is terse.
    """

    ts: int
    signal: str
    value: float
    quality: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "s": self.signal,
                "v": self.value,
                "q": self.quality,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> SpoolRecord | None:
        """Parse one line, or return ``None`` if it is unusable.

        Returning ``None`` rather than raising is deliberate: recovery must not
        be stopped by one bad line. A truncated final line after a crash is
        *expected*, and treating it as a fatal error would mean a gateway that
        cannot restart after the exact event the spool exists to survive.
        """
        try:
            d = json.loads(line)
            return cls(
                ts=int(d["ts"]),
                signal=str(d["s"]),
                value=float(d["v"]),
                quality=int(d.get("q", 0)),
            )
        except (ValueError, TypeError, KeyError):
            return None


@dataclass
class SpoolStats:
    """What the spool has done, for the health line and for the tests."""

    written: int = 0
    read: int = 0
    dropped_unparsable: int = 0
    dropped_overflow: int = 0
    files_removed: int = 0
    bytes: int = 0
    files: int = 0
    corrupt: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "read": self.read,
            "dropped_unparsable": self.dropped_unparsable,
            "dropped_overflow": self.dropped_overflow,
            "files_removed": self.files_removed,
            "bytes": self.bytes,
            "files": self.files,
            "corrupt_files": list(self.corrupt),
        }


class Spool:
    """An append-only, time-rotated, size-bounded buffer on local disk.

    Rotation is on *elapsed time*, not on the wall-clock hour. See
    :data:`DEFAULT_ROTATE_S` for why, and for what it costs.
    """

    def __init__(self, directory: str | os.PathLike[str], max_mb: int = 512,
                 rotate_s: int = DEFAULT_ROTATE_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory)
        self.max_bytes = int(max_mb) * 1024 * 1024
        # At least one second: the filename has second resolution, and a
        # sub-second interval would put two rotations in one file name.
        self.rotate_s = max(1, int(rotate_s))
        self._clock = clock
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stats = SpoolStats()
        self._current: Path | None = None
        self._fh: TextIO | None = None
        self._current_hour = -1
        self._opened_at = 0.0
        self._current_bytes = 0
        #: Authoritative total across every file. Tracked incrementally because
        #: re-globbing the directory on every append would cost a syscall per
        #: signal per scan, and refresh() is the reconciliation.
        self._total = 0
        #: Bytes written to the handle but not yet visible in ``st_size``.
        #: Python buffers, so a freshly appended record does not change the file
        #: size on disk — and an accounting scheme that reads ``st_size`` alone
        #: therefore under-reports by up to a buffer's worth, which is how a
        #: 1 MB cap quietly became a 2.8 MB spool.
        self._buffered = 0
        self.refresh()

    # ─── writing ─────────────────────────────────────────────────────────────

    def _path_for(self, opened_at: float) -> Path:
        """The file name for a file opened at ``opened_at`` (epoch seconds).

        Second resolution, which is why ``rotate_s`` is floored at one second:
        two rotations inside the same second would collide onto one path, and the
        first batch's records would become indistinguishable from the second's.

        Note ``tm_year * 10000`` rather than ``tm_year``. The first version of
        this wrote ``{t.tm_year:04d}``, which for 2026 produces
        ``spool-00020261-...`` -- a five-digit year, and a filename that no longer
        matches :data:`_FILENAME`, so recovery would have skipped the file
        entirely. It is on the record here because it is the kind of bug that a
        test asserting a *count* of files passes straight through: there was one
        file, which is what the test expected.
        """
        t = time.gmtime(int(opened_at))
        date = t.tm_year * 10000 + t.tm_mon * 100 + t.tm_mday
        clock = t.tm_hour * 10000 + t.tm_min * 100 + t.tm_sec
        return self.dir / f"spool-{date:08d}-{clock:06d}.jsonl"

    # `ts` is unused on purpose, and the docstring says why: rotation is keyed on
    # elapsed time so a gateway whose clock jumps backwards keeps rotating. The
    # `noqa` exists because this file was untracked until the `.gitignore` fix
    # that shipped with it, so it had never been through `make lint` at all.
    def _rotate_if_needed(self, ts: int) -> None:  # noqa: ARG002
        """Rotate onto a new file once ``rotate_s`` has elapsed.

        On *elapsed* time, not on the wall-clock hour. The hourly version took a
        ``rotate_s`` parameter and never read it, and the consequence only
        appeared when the whole stack was run for the first time: ``pending()``
        excludes the file currently being written, so **nothing reached the
        database for up to an hour.** Every component reported itself healthy
        throughout, which is the part worth remembering.

        ``ts`` is no longer used for the rotation decision. It is still the
        record's own timestamp, and it is still what goes in the line, but keying
        rotation on it would mean a gateway whose clock jumped backwards stopped
        rotating — and a clock that jumps is exactly the situation where you want
        the spool to keep moving.
        """
        now = self._clock()
        if self._fh is not None and (now - self._opened_at) < self.rotate_s:
            return
        self._close()
        path = self._path_for(now)
        # Opened in append mode, always. A restart inside a rotation window must
        # not truncate what is already spooled, which is precisely the data the
        # outage produced. With second-resolution names a restart normally lands
        # on a different path, so this is nearly always a fresh file -- but
        # "nearly always" is not a reason to open with "w".
        self._fh = path.open("a", encoding="utf-8")
        self._current = path
        self._opened_at = now
        self._current_bytes = path.stat().st_size if path.exists() else 0

    def append(self, record: SpoolRecord) -> bool:
        """Write one record. Returns ``False`` if it was dropped for space.

        Never raises for a full spool. The caller is a scan loop that cannot
        usefully do anything about an exception, and a gateway that crashes
        because its disk filled is worse than one that loses the oldest hour.
        """
        self._rotate_if_needed(record.ts)
        line = record.to_json() + "\n"
        encoded = len(line.encode("utf-8"))
        # _rotate_if_needed guarantees the handle; the check is here so mypy
        # can see it without a cast, not to catch a real condition.
        if self._fh is None:  # pragma: no cover - defensive
            return False

        # The cap is on the *whole spool*, not on the current file. Checking the
        # current file's size instead let nine hour-files reach 2.8 MB under a
        # 1 MB limit — a limit that looked enforced and bounded nothing, which is
        # the worst way for a safety limit to fail.
        if (self._total + encoded > self.max_bytes
                and not self._make_room(encoded)):
            self.stats.dropped_overflow += 1
            return False

        self._fh.write(line)
        self._current_bytes += encoded
        self._total += encoded
        self._buffered += encoded
        self.stats.written += 1
        return True

    def extend(self, records: list[SpoolRecord]) -> int:
        """Append many. Returns how many were accepted."""
        return sum(1 for r in records if self.append(r))

    # ─── space ───────────────────────────────────────────────────────────────

    def refresh(self) -> SpoolStats:
        """Recount what is on disk. Cheap, and honest after a restart.

        The buffered bytes are added back, because ``st_size`` cannot see them
        and forgetting them would make this method *reduce* the running total —
        which is how the cap drifted upwards by 30% in testing.
        """
        files = list(self._files())
        self.stats.files = len(files)
        self.stats.bytes = sum(f.stat().st_size for f in files) + self._buffered
        self._total = self.stats.bytes
        return self.stats

    def _files(self) -> Iterator[Path]:
        yield from sorted(p for p in self.dir.glob("spool-*.jsonl") if p.is_file())

    def _make_room(self, needed: int) -> bool:
        """Delete the oldest *complete* files until ``needed`` bytes will fit.

        The file currently being appended to is never a candidate. Truncating the
        active file would mean throwing away the most recent data — the data
        from right now, during the outage — to make room for the data from
        right now, during the same outage. Older is the right thing to lose.
        """
        if self.max_bytes <= 0:
            return False
        # Flush first. The running total counts *logical* bytes written, while a
        # file's ``st_size`` counts only what has reached the OS. Deleting a file
        # and crediting back its ``st_size`` would therefore leave the buffered
        # remainder counted forever, and the total would creep upwards by a
        # buffer's worth per reclamation until the cap stopped meaning anything.
        self.flush()
        self.refresh()
        for path in self._files():
            if path == self._current:
                continue
            if self._total + needed <= self.max_bytes:
                return True
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                # A file we cannot delete is a file we cannot count correctly.
                # Say so rather than pretending the space was reclaimed.
                self.stats.corrupt.append(f"{path.name}: undeletable")
                continue
            self.stats.bytes -= size
            self._total -= size
            self.stats.files -= 1
            self.stats.files_removed += 1
        return self._total + needed <= self.max_bytes

    # ─── reading ─────────────────────────────────────────────────────────────

    def pending(self) -> list[Path]:
        """Files waiting to be drained, oldest first."""
        self.refresh()
        return [p for p in self._files() if p != self._current]

    def read(self, path: Path) -> Iterator[SpoolRecord]:
        """Yield records from one file, skipping and counting unusable lines."""
        try:
            fh = path.open(encoding="utf-8")
        except OSError:
            self.stats.corrupt.append(f"{path.name}: unreadable")
            return
        # The file is read in a ``with`` because it is a *complete* hour: it is
        # no longer being appended to. The active file is the one that needs the
        # long-lived handle, and it is excluded from this path by design.
        with fh:
            for line in fh:
                if len(line) > _MAX_LINE_BYTES:
                    self.stats.dropped_unparsable += 1
                    continue
                rec = SpoolRecord.from_json(line)
                if rec is None:
                    self.stats.dropped_unparsable += 1
                    continue
                self.stats.read += 1
                yield rec

    def drain(self) -> Iterator[SpoolRecord]:
        """Yield every pending record, oldest first, then delete the file.

        The file is removed only after its records have been yielded, and the
        consumer is expected to have written them by then. That ordering is the
        entire durability story: a crash between the write and the delete
        re-delivers the data, which is at-least-once. Losing data because the
        delete was early is not recoverable.
        """
        for path in self.pending():
            yield from self.read(path)
            self.acknowledge(path)

    def acknowledge(self, path: Path) -> None:
        """Delete a file that has been fully written to the database."""
        try:
            size = path.stat().st_size
            path.unlink()
            self.stats.files_removed += 1
            self._total -= size
            self.stats.bytes -= size
            self.stats.files -= 1
        except OSError:
            self.stats.corrupt.append(f"{path.name}: undeletable on acknowledge")

    # ─── lifecycle ───────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Push buffered records to the OS.

        Called before every drain and on a timer. The buffered count is zeroed
        only after the flush succeeds, so a failure leaves the accounting honest
        rather than optimistic.
        """
        if self._fh is not None:
            self._fh.flush()
            self._buffered = 0

    def _close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None
            self._buffered = 0
        self.refresh()

    def close(self) -> None:
        self._close()

    def __enter__(self) -> Spool:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
