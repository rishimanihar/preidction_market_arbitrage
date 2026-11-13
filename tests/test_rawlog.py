import asyncio
import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmarb.storage.rawlog import (
    CorruptLogError,
    Envelope,
    RawLogTask,
    RawLogWriter,
    disk_free_bytes,
    dumps,
    iter_records,
    list_log_files,
    repair_tail,
)

T0 = datetime(2026, 11, 11, 18, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def env(i: int = 0, *, venue: str = "kalshi", kind: str = "ws_msg", body=None) -> Envelope:
    return Envelope(
        recv_utc="2026-11-11T18:30:00+00:00",
        recv_mono_ns=1000 + i,
        venue=venue,
        kind=kind,
        conn_gen=1,
        endpoint="/book",
        status=200,
        cache_headers={"age": "0"},
        body={"i": i, "px": [1, 2]} if body is None else body,
    )


def test_venue_name_rejects_trailing_newline() -> None:
    with pytest.raises(ValueError, match="unsafe venue"):
        env(venue="kalshi\n")


def write(w: RawLogWriter, n: int, **kw) -> None:
    for i in range(n):
        w.append(env(i, **kw))
    w.flush()


def only_file(root: Path) -> Path:
    files = list(root.rglob("*.jsonl.gz"))
    assert len(files) == 1
    return files[0]


def test_round_trip_and_layout(tmp_path: Path) -> None:
    w = RawLogWriter(tmp_path, clock=Clock())
    w.append(env(1))
    w.append(env(2, kind="checkpoint"))
    assert w.pending == 2
    assert w.flush() > 0 and w.pending == 0
    raw = tmp_path / "raw/kalshi/2026-11-11/18.jsonl.gz"
    cp = tmp_path / "checkpoints/kalshi/2026-11-11/18.jsonl.gz"
    assert [e.recv_mono_ns for e in iter_records([raw])] == [1001]
    assert [e.kind for e in iter_records([cp])] == ["checkpoint"]
    assert next(iter(iter_records([raw]))) == env(1)
    assert list_log_files(tmp_path, "kalshi") == [raw]
    assert w.flush() == 0


def test_many_members_readable_by_gzip(tmp_path: Path) -> None:
    w = RawLogWriter(tmp_path, clock=Clock())
    write(w, 50)
    write(w, 50)
    path = only_file(tmp_path)
    lines = gzip.decompress(path.read_bytes()).splitlines()
    assert len(lines) == 100
    assert json.loads(lines[99])["body"]["i"] == 49
    assert path.read_bytes().count(b"\x1f\x8b\x08") >= 100  # one member per record
    assert len(list(iter_records([path]))) == 100


def test_torn_tail_truncated_mid_member(tmp_path: Path) -> None:
    write(RawLogWriter(tmp_path, clock=Clock()), 10)
    path = only_file(tmp_path)
    full = path.stat().st_size
    path.write_bytes(path.read_bytes()[:-7])
    res = repair_tail(path)
    assert res.repaired and res.original_size == full - 7
    assert res.records_kept == 9 and res.truncated_bytes > 0
    assert path.stat().st_size == res.good_size
    assert [e.recv_mono_ns for e in iter_records([path])] == [1000 + i for i in range(9)]
    assert not repair_tail(path).repaired  # idempotent


def test_torn_tail_bad_crc(tmp_path: Path) -> None:
    write(RawLogWriter(tmp_path, clock=Clock()), 10)
    path = only_file(tmp_path)
    data = bytearray(path.read_bytes())
    data[-8] ^= 0xFF  # CRC32 field of the last member
    path.write_bytes(bytes(data))
    with pytest.raises(CorruptLogError):
        list(iter_records([path]))
    res = repair_tail(path)
    assert res.records_kept == 9 and res.repaired
    assert len(list(iter_records([path]))) == 9


def test_iter_records_tolerates_torn_tail_only_on_request(tmp_path: Path) -> None:
    write(RawLogWriter(tmp_path, clock=Clock()), 5)
    path = only_file(tmp_path)
    path.write_bytes(path.read_bytes()[:-3])
    with pytest.raises(CorruptLogError):
        list(iter_records([path]))
    assert len(list(iter_records([path], tolerate_torn_tail=True))) == 4


def test_mid_file_corruption_is_not_repaired(tmp_path: Path) -> None:
    write(RawLogWriter(tmp_path, clock=Clock()), 10)
    path = only_file(tmp_path)
    data = bytearray(path.read_bytes())
    second = data.find(b"\x1f\x8b\x08", 1)
    data[second + 12] ^= 0xFF
    before = bytes(data)
    path.write_bytes(before)
    with pytest.raises(CorruptLogError):
        repair_tail(path)
    assert path.read_bytes() == before  # untouched
    with pytest.raises(CorruptLogError):
        list(iter_records([path], tolerate_torn_tail=True))


def test_append_after_repair_continues_cleanly(tmp_path: Path) -> None:
    w = RawLogWriter(tmp_path, clock=Clock())
    write(w, 3)
    path = only_file(tmp_path)
    path.write_bytes(path.read_bytes()[:-5])
    repair_tail(path)
    write(w, 2)
    assert len(list(iter_records([path]))) == 4


def test_rotation_across_hour_boundary(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC))
    w = RawLogWriter(tmp_path, clock=clock)
    w.append(env(1))
    w.flush()
    clock.now += timedelta(seconds=2)
    w.append(env(2))
    w.flush()
    a = tmp_path / "raw/kalshi/2026-11-11/23.jsonl.gz"
    b = tmp_path / "raw/kalshi/2026-11-12/00.jsonl.gz"
    assert a.exists() and b.exists()
    assert list_log_files(tmp_path, "kalshi") == [a, b]
    assert [e.recv_mono_ns for e in iter_records([a, b])] == [1001, 1002]


def make_file(root: Path, stream: str, hour: datetime, size: int, venue: str = "kalshi") -> Path:
    p = root / stream / venue / f"{hour:%Y-%m-%d}" / f"{hour:%H}.jsonl.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_retention_by_size_oldest_first(tmp_path: Path) -> None:
    h = datetime(2026, 11, 11, 10, tzinfo=UTC)
    files = [make_file(tmp_path, "raw", h + timedelta(hours=i), 100) for i in range(5)]
    w = RawLogWriter(tmp_path, max_bytes=250, clock=Clock(h + timedelta(hours=8)))
    deleted = w.enforce_retention()
    assert deleted == files[:3]
    assert [p.exists() for p in files] == [False, False, False, True, True]
    assert w.enforce_retention() == []


def test_retention_by_age(tmp_path: Path) -> None:
    now = datetime(2026, 11, 11, 12, 30, tzinfo=UTC)
    old = make_file(tmp_path, "raw", now - timedelta(days=31), 10)
    old_cp = make_file(tmp_path, "checkpoints", now - timedelta(days=40), 10)
    keep = make_file(tmp_path, "raw", now - timedelta(days=29), 10)
    w = RawLogWriter(tmp_path, max_age_days=30, clock=Clock(now))
    deleted = w.enforce_retention()
    assert set(deleted) == {old, old_cp}
    assert keep.exists()
    assert not old.parent.exists()  # empty day dir pruned


def test_retention_never_deletes_current_hour(tmp_path: Path) -> None:
    now = datetime(2026, 11, 11, 12, 30, tzinfo=UTC)
    cur = make_file(tmp_path, "raw", now, 1000)
    w = RawLogWriter(tmp_path, max_bytes=1, clock=Clock(now))
    assert w.enforce_retention() == []
    assert cur.exists()


def test_cancellation_drains_flushes_and_reraises(tmp_path: Path) -> None:
    async def main() -> None:
        w = RawLogWriter(tmp_path, clock=Clock())
        q: asyncio.Queue[Envelope] = asyncio.Queue()
        task = asyncio.create_task(RawLogTask(w, q, flush_interval=30).run())
        await asyncio.sleep(0.05)
        for i in range(20):
            q.put_nowait(env(i))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert q.empty() and w.pending == 0

    asyncio.run(main())
    assert len(list(iter_records([only_file(tmp_path)]))) == 20


def test_task_drain_forces_queue_through_fsync(tmp_path: Path) -> None:
    async def main() -> None:
        writer = RawLogWriter(tmp_path, clock=Clock())
        queue: asyncio.Queue[Envelope] = asyncio.Queue()
        raw_task = RawLogTask(writer, queue, flush_interval=3600)
        task = asyncio.create_task(raw_task.run())
        try:
            for i in range(3):
                queue.put_nowait(env(i))
            await raw_task.drain()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(main())
    assert [record.recv_mono_ns for record in iter_records([only_file(tmp_path)])] == [
        1000,
        1001,
        1002,
    ]


def test_task_flushes_on_interval(tmp_path: Path) -> None:
    async def main() -> None:
        w = RawLogWriter(tmp_path, clock=Clock())
        q: asyncio.Queue[Envelope] = asyncio.Queue()
        task = asyncio.create_task(RawLogTask(w, q, flush_interval=0.05).run())
        q.put_nowait(env(1))
        await asyncio.sleep(0.4)
        assert len(list(iter_records([only_file(tmp_path)]))) == 1  # before any cancel
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_decimal_serialized_as_string_and_nan_rejected(tmp_path: Path) -> None:
    body = {"price": Decimal("0.5500"), "levels": [Decimal(10)]}
    assert json.loads(dumps(body)) == {"price": "0.5500", "levels": ["10"]}
    w = RawLogWriter(tmp_path, clock=Clock())
    write(w, 1, body=body)
    got = next(iter(iter_records([only_file(tmp_path)])))
    assert got.body == {"price": "0.5500", "levels": ["10"]}
    with pytest.raises(ValueError):
        dumps({"x": float("nan")})
    with pytest.raises(ValueError):
        dumps({"x": float("inf")})
    with pytest.raises(ValueError):
        dumps({"x": Decimal("NaN")})
    with pytest.raises(TypeError):
        dumps({"x": object()})
    with pytest.raises(ValueError):
        w.append(env(1, body={"x": float("nan")}))
    assert w.pending == 0


def test_envelope_validation() -> None:
    with pytest.raises(ValueError):
        env(kind="bogus")
    with pytest.raises(ValueError):
        env(venue="../evil")


def test_flush_failure_restores_buffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = RawLogWriter(tmp_path, clock=Clock())
    write(w, 2)
    path = only_file(tmp_path)
    size = path.stat().st_size
    w.append(env(9))

    def boom(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        w.flush()
    monkeypatch.undo()
    assert w.pending == 1 and path.stat().st_size == size
    w.flush()
    assert len(list(iter_records([path]))) == 3


def test_disk_free_bytes(tmp_path: Path) -> None:
    assert disk_free_bytes(tmp_path) > 0
    assert disk_free_bytes(tmp_path / "not" / "yet") > 0
