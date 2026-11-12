"""Crash-safe raw message log: one gzip member per record, hourly files.

Layout (UTC)::

    <root>/raw/<venue>/<YYYY-MM-DD>/<HH>.jsonl.gz
    <root>/checkpoints/<venue>/<YYYY-MM-DD>/<HH>.jsonl.gz

Every record is its own gzip member, so a crash can tear at most the final member of a file and
every earlier member stays independently decodable (and standard ``gzip`` reads the concatenation).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import shutil
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pmarb.osutil import fsync_directory

KINDS = frozenset({"rest_book", "ws_msg", "market", "conn", "checkpoint", "gamestate"})
DEFAULT_MAX_BYTES = 20 * 1024**3
DEFAULT_MAX_AGE_DAYS = 30
SUFFIX = ".jsonl.gz"
_VENUE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_GZIP_MAGIC = b"\x1f\x8b\x08"
_CHUNK = 1 << 20


class RawLogError(Exception):
    """Base class for raw-log failures."""


class CorruptLogError(RawLogError):
    """A raw-log file is corrupt somewhere other than a repairable torn tail."""

    def __init__(self, path: Path, offset: int, detail: str) -> None:
        super().__init__(f"{path}: corrupt gzip member at byte {offset}: {detail}")
        self.path = path
        self.offset = offset


def json_default(value: object) -> str:
    """JSON ``default`` hook: Decimal becomes its exact string; anything else is an error."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal not serializable: {value}")
        return str(value)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def dumps(obj: Any) -> str:
    """Compact JSON; Decimal as string; NaN/Infinity floats rejected (ValueError)."""
    return json.dumps(obj, default=json_default, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Envelope:
    recv_utc: str
    recv_mono_ns: int
    venue: str
    kind: str
    conn_gen: int
    endpoint: str
    status: int | None
    cache_headers: dict[str, Any] = field(default_factory=dict)
    body: Any = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown envelope kind: {self.kind!r}")
        if not _VENUE_RE.match(self.venue):
            raise ValueError(f"unsafe venue name: {self.venue!r}")

    def to_json(self) -> str:
        return dumps(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Envelope:
        return cls(
            recv_utc=data["recv_utc"],
            recv_mono_ns=data["recv_mono_ns"],
            venue=data["venue"],
            kind=data["kind"],
            conn_gen=data["conn_gen"],
            endpoint=data["endpoint"],
            status=data["status"],
            cache_headers=data["cache_headers"],
            body=data["body"],
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def disk_free_bytes(path: Path) -> int:
    """Free bytes on the filesystem holding ``path`` (nearest existing ancestor if missing)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def _encode_member(env: Envelope) -> bytes:
    # mtime=0 keeps output deterministic; each call yields exactly one gzip member.
    return gzip.compress((env.to_json() + "\n").encode(), compresslevel=6, mtime=0)


def _fsync_dir(path: Path) -> None:
    # Skipped on Windows, where directories cannot be fsynced; see fsync_directory.
    fsync_directory(path)


def _hour_of(path: Path) -> datetime | None:
    """Start of the hour a log file covers, parsed from ``.../<YYYY-MM-DD>/<HH>.jsonl.gz``."""
    try:
        day = datetime.strptime(path.parent.name, "%Y-%m-%d")
        hour = int(path.name.removesuffix(SUFFIX))
        return day.replace(hour=hour, tzinfo=UTC)
    except ValueError:
        return None


class RawLogWriter:
    """Buffered writer. ``append`` is cheap; ``flush`` writes and fsyncs (call every <=500 ms)."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_age_days = max_age_days
        self._clock = clock
        self._buf: list[tuple[Path, bytes]] = []
        self._buf_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self.flush_seconds = 0.0
        self.flush_count = 0
        self.flush_bytes = 0

    def path_for(self, env: Envelope, when: datetime) -> Path:
        stream = "checkpoints" if env.kind == "checkpoint" else "raw"
        when = when.astimezone(UTC)
        return self.root / stream / env.venue / when.strftime("%Y-%m-%d") / f"{when:%H}{SUFFIX}"

    @property
    def pending(self) -> int:
        with self._buf_lock:
            return len(self._buf)

    def append(self, env: Envelope) -> None:
        """Buffer one record. The target hour is fixed now, from the writer's clock."""
        item = (self.path_for(env, self._clock()), _encode_member(env))
        with self._buf_lock:
            self._buf.append(item)

    def flush(self) -> int:
        """Write all buffered records and fsync. Returns bytes written.

        On an I/O error each file is truncated back to its pre-write size where possible and the
        whole batch is put back at the head of the buffer, then the error is re-raised.
        """
        started = time.monotonic()
        with self._io_lock:
            with self._buf_lock:
                batch, self._buf = self._buf, []
            if not batch:
                self.flush_seconds += time.monotonic() - started
                self.flush_count += 1
                return 0
            grouped: dict[Path, list[bytes]] = {}
            for path, data in batch:
                grouped.setdefault(path, []).append(data)
            undo: list[tuple[Path, int]] = []
            written = 0
            try:
                for path, chunks in grouped.items():
                    fresh = not path.parent.exists() or not path.exists()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "ab") as f:
                        undo.append((path, os.fstat(f.fileno()).st_size))
                        payload = b"".join(chunks)
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())
                        written += len(payload)
                    if fresh:
                        _fsync_dir(path.parent)
            except BaseException:
                for path, size in undo:
                    try:
                        os.truncate(path, size)
                    except OSError:
                        pass
                with self._buf_lock:
                    self._buf[:0] = batch
                raise
            self.flush_seconds += time.monotonic() - started
            self.flush_count += 1
            self.flush_bytes += written
            return written

    def _log_files(self) -> list[tuple[datetime, Path, int]]:
        out: list[tuple[datetime, Path, int]] = []
        for stream in ("raw", "checkpoints"):
            for p in (self.root / stream).glob(f"*/*/*{SUFFIX}"):
                hour = _hour_of(p)
                if hour is not None:
                    out.append((hour, p, p.stat().st_size))
        out.sort(key=lambda t: (t[0], str(t[1])))
        return out

    def enforce_retention(self) -> list[Path]:
        """Delete oldest hour files until under both the age and size limits.

        Files for the current (still-open) hour or later are never deleted. Returns deleted paths.
        """
        now = self._clock().astimezone(UTC)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        cutoff = now - timedelta(days=self.max_age_days)
        files = self._log_files()
        total = sum(size for _, _, size in files)
        deleted: list[Path] = []
        for hour, path, size in files:
            if hour >= current_hour:
                break
            too_old = hour + timedelta(hours=1) <= cutoff
            if not too_old and total <= self.max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size
            deleted.append(path)
            for d in (path.parent, path.parent.parent):
                try:
                    d.rmdir()
                except OSError:
                    break
        return deleted


@dataclass(frozen=True, slots=True)
class RepairResult:
    path: Path
    original_size: int
    good_size: int
    records_kept: int

    @property
    def truncated_bytes(self) -> int:
        return self.original_size - self.good_size

    @property
    def repaired(self) -> bool:
        return self.truncated_bytes > 0


@dataclass(frozen=True, slots=True)
class _Scan:
    members: list[tuple[int, int]]  # (start, end) of each good member
    bad_offset: int | None  # start of the first bad/truncated member, if any
    detail: str = ""


def _scan(path: Path, on_payload: Callable[[bytes], None] | None = None) -> _Scan:
    """Walk gzip members sequentially, stopping at the first bad or truncated one."""
    size = path.stat().st_size
    members: list[tuple[int, int]] = []
    d = zlib.decompressobj(wbits=31)
    start = 0
    pos = 0  # file offset of the next unread byte
    out = bytearray()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            data = chunk
            base = pos
            pos += len(chunk)
            while data:
                try:
                    out += d.decompress(data)
                except zlib.error as exc:
                    return _Scan(members, start, str(exc))
                if not d.eof:
                    break
                end = base + len(data) - len(d.unused_data)
                members.append((start, end))
                if on_payload is not None:
                    on_payload(bytes(out))
                out.clear()
                start = base = end
                data = d.unused_data
                d = zlib.decompressobj(wbits=31)
    if start < size:
        return _Scan(members, start, "truncated member")
    return _Scan(members, None)


def _has_good_member_after(path: Path, offset: int) -> bool:
    """True if some gzip member after ``offset`` decodes cleanly (so it's not a torn tail)."""
    with open(path, "rb") as f:
        f.seek(offset + 1)
        rest = f.read()
    idx = rest.find(_GZIP_MAGIC)
    while idx != -1:
        d = zlib.decompressobj(wbits=31)
        try:
            d.decompress(rest[idx:])
            if d.eof:
                return True
        except zlib.error:
            pass
        idx = rest.find(_GZIP_MAGIC, idx + 1)
    return False


def repair_tail(path: Path) -> RepairResult:
    """Truncate a torn final member (truncated or bad CRC) off ``path``.

    Complete earlier members are never touched. If a bad member is followed by decodable data the
    damage is mid-file, not a torn tail, and ``CorruptLogError`` is raised without modifying the
    file. The caller should log a ``collector_events`` gap when ``result.repaired``.
    """
    path = Path(path)
    original = path.stat().st_size
    scan = _scan(path)
    if scan.bad_offset is None:
        return RepairResult(path, original, original, len(scan.members))
    if _has_good_member_after(path, scan.bad_offset):
        raise CorruptLogError(path, scan.bad_offset, scan.detail + " (not at tail)")
    with open(path, "r+b") as f:
        f.truncate(scan.bad_offset)
        f.flush()
        os.fsync(f.fileno())
    return RepairResult(path, original, scan.bad_offset, len(scan.members))


def list_log_files(root: Path, venue: str, *, checkpoints: bool = False) -> list[Path]:
    """All hourly files for a venue, oldest first."""
    stream = "checkpoints" if checkpoints else "raw"
    return sorted((Path(root) / stream / venue).glob(f"*/*{SUFFIX}"))


def iter_records(paths: Iterable[Path], *, tolerate_torn_tail: bool = False) -> Iterator[Envelope]:
    """Yield envelopes from ``paths`` in order.

    Corruption raises ``CorruptLogError`` (after yielding the good prefix of that file); nothing is
    skipped silently. With ``tolerate_torn_tail`` a bad final member (e.g. a file still being
    written) ends that file quietly; mid-file damage still raises.
    """
    for path in map(Path, paths):
        payloads: list[bytes] = []
        scan = _scan(path, payloads.append)
        for payload in payloads:
            yield Envelope.from_dict(json.loads(payload))
        if scan.bad_offset is None:
            continue
        if tolerate_torn_tail and not _has_good_member_after(path, scan.bad_offset):
            continue
        raise CorruptLogError(path, scan.bad_offset, scan.detail)


class RawLogTask:
    """Consumes envelopes from a queue and flushes on an interval (<=500 ms by default).

    On cancellation it drains the queue, flushes + fsyncs, and re-raises ``CancelledError``.
    """

    def __init__(
        self,
        writer: RawLogWriter,
        queue: asyncio.Queue[Envelope],
        *,
        flush_interval: float = 0.5,
    ) -> None:
        self.writer = writer
        self.queue = queue
        self.flush_interval = flush_interval
        self._pending_count = 0
        self._flush_requested = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None

    def _drain(self) -> None:
        while True:
            try:
                self.writer.append(self.queue.get_nowait())
                self._pending_count += 1
            except asyncio.QueueEmpty:
                return

    def _mark_flushed(self) -> None:
        count, self._pending_count = self._pending_count, 0
        for _ in range(count):
            self.queue.task_done()

    async def drain(self) -> None:
        """Force queued envelopes through gzip write and fsync, then return."""
        runner = self._runner
        if runner is None:
            await asyncio.sleep(0)
            runner = self._runner
        if runner is None or runner.done():
            raise RuntimeError("raw log writer is not running")
        self._flush_requested.set()
        joined = asyncio.create_task(self.queue.join())
        try:
            done, _pending = await asyncio.wait(
                {joined, runner}, return_when=asyncio.FIRST_COMPLETED
            )
            if joined in done:
                await joined
                return
            error = runner.exception()
            raise RuntimeError("raw log writer stopped before draining its queue") from error
        finally:
            if not joined.done():
                joined.cancel()
                await asyncio.gather(joined, return_exceptions=True)

    async def run(self) -> None:
        self._runner = asyncio.current_task()
        now = time.monotonic
        next_flush = now() + self.flush_interval
        try:
            while True:
                get_task = asyncio.create_task(self.queue.get())
                flush_task = asyncio.create_task(self._flush_requested.wait())
                received = False
                try:
                    done, _pending = await asyncio.wait(
                        {get_task, flush_task},
                        timeout=max(0.0, next_flush - now()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if get_task in done:
                        self.writer.append(get_task.result())
                        self._pending_count += 1
                        received = True
                    if flush_task in done:
                        self._flush_requested.clear()
                        self._drain()
                        await asyncio.to_thread(self.writer.flush)
                        self._mark_flushed()
                        next_flush = now() + self.flush_interval
                    elif now() >= next_flush:
                        self._drain()
                        await asyncio.to_thread(self.writer.flush)
                        self._mark_flushed()
                        next_flush = now() + self.flush_interval
                except asyncio.CancelledError:
                    if get_task.done() and not get_task.cancelled() and not received:
                        self.writer.append(get_task.result())
                        self._pending_count += 1
                    raise
                finally:
                    for task in (get_task, flush_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(get_task, flush_task, return_exceptions=True)
        except asyncio.CancelledError:
            self._drain()
            self.writer.flush()
            self._mark_flushed()
            raise
