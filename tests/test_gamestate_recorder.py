from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pmarb.gamestate.kalshi_live import GameState, KalshiLiveClient
from pmarb.gamestate.recorder import GameStateRecorder
from pmarb.http import ObservationHttpClient, TokenBucket
from pmarb.storage.db import DbWriterTask, Write, open_reader, open_writer
from pmarb.storage.rawlog import RawLogWriter


def _state(clock: str, score: int = 0, status: str = "live") -> GameState:
    return GameState(
        source="kalshi_live_data",
        event_ticker="KXNBA-TEST",
        milestone_id="m1",
        league="NBA",
        status=status,
        period=1,
        clock=clock,
        home_points=score,
        away_points=0,
        possession=None,
        last_play=None,
        result_pending=None,
        delayed_flag=None,
        received_utc=datetime(2026, 1, 1, tzinfo=UTC),
        received_mono=1.0,
        raw={},
    )


class FakeClient:
    def __init__(self, batches: list[list[GameState]]) -> None:
        self.batches = batches
        self.raw_log = None

    async def resolve_milestones(self, events: list[str]) -> dict[str, str]:
        return {event: "m1" for event in events}

    async def fetch_live(self, ids: list[str]) -> list[GameState]:
        return self.batches.pop(0)


def test_recorder_writes_only_changes_and_stops_at_final(tmp_path: Path) -> None:
    async def run() -> None:
        client = FakeClient(
            [
                [_state("10:01")],
                [_state("10:00")],  # Same 10-second clock bucket.
                [_state("9:50", score=2)],
                [_state("0:00", score=2, status="final")],
            ]
        )
        queue: asyncio.Queue[object] = asyncio.Queue()
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        raw = RawLogWriter(tmp_path / "raw")
        recorder = GameStateRecorder(
            client,
            ["KXNBA-TEST"],
            {"KXNBA-TEST": "NBA"},
            type("Writer", (), {"queue": queue})(),
            raw,
            sleep=sleep,
        )
        await recorder.run()
        assert len(sleeps) == 3
        assert queue.qsize() == 3
        with open_writer(tmp_path / "db.sqlite") as conn:
            while not queue.empty():
                op = queue.get_nowait()
                assert isinstance(op, Write)
                op.apply(conn)
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM game_state").fetchone() == (3,)

    asyncio.run(run())


def test_recorder_fans_out_shared_milestone_and_finalizes_both(tmp_path: Path) -> None:
    async def run() -> None:
        state = _state("0:00", status="final")

        class SharedClient(FakeClient):
            async def resolve_milestones(self, events: list[str]) -> dict[str, str]:
                return {event: "m1" for event in events}

        queue: asyncio.Queue[object] = asyncio.Queue()

        async def no_sleep(_: float) -> None:
            return None

        client = SharedClient([[state]])
        recorder = GameStateRecorder(
            client,
            ["KXNBA-TEST", "KXNBA-OTHER"],
            {"KXNBA-TEST": "NBA", "KXNBA-OTHER": "NBA"},
            type("Writer", (), {"queue": queue})(),
            RawLogWriter(tmp_path / "raw"),
            sleep=no_sleep,
        )
        await recorder.run()
        assert queue.qsize() == 2

    asyncio.run(run())


def test_recorder_http_rawlog_and_db_writer_integration(tmp_path: Path) -> None:
    async def run() -> None:
        batch_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal batch_count
            if request.url.path.endswith("/milestones"):
                return httpx.Response(
                    200,
                    json={"milestones": [{"id": "m1", "related_event_tickers": ["KXNBA-TEST"]}]},
                )
            if request.url.path.endswith("/live_data/batch"):
                batch_count += 1
                status = "final" if batch_count == 3 else "live"
                body = {
                    "live_datas": [
                        {
                            "milestone_id": "m1",
                            "type": "basketball",
                            "details": {
                                "home_points": 5,
                                "away_points": 3,
                                "period": 2,
                                "period_remaining_time": "04:00",
                                "status": status,
                            },
                        }
                    ]
                }
                return httpx.Response(200, json=body, headers={"Cache-Control": "max-age=10"})
            raise AssertionError(request.url)

        http = ObservationHttpClient(
            transport=httpx.MockTransport(handler),
            buckets={("kalshi", "gamestate"): TokenBucket(100, 100)},
        )
        fixed_utc = datetime(2026, 1, 1, 12, tzinfo=UTC)
        client = KalshiLiveClient(http)
        raw = RawLogWriter(tmp_path / "raw")
        writer = DbWriterTask(tmp_path / "db.sqlite", asyncio.Queue(), commit_interval_s=60)
        writer_task = asyncio.create_task(writer.run())
        await writer.ready.wait()

        async def no_sleep(_: float) -> None:
            return None

        recorder = GameStateRecorder(
            client,
            ["KXNBA-TEST"],
            {"KXNBA-TEST": "NBA"},
            writer,
            raw,
            clock=lambda: fixed_utc,
            monotonic=lambda: 42.5,
            sleep=no_sleep,
        )
        await recorder.run()
        raw.flush()
        writer_task.cancel()
        try:
            await writer_task
        except asyncio.CancelledError:
            pass
        await http.aclose()

        with open_reader(tmp_path / "db.sqlite") as conn:
            assert conn.execute("SELECT COUNT(*) FROM game_state").fetchone() == (2,)
            assert conn.execute("SELECT received_utc FROM game_state LIMIT 1").fetchone() == (
                "2026-01-01T12:00:00Z",
            )
        files = list((tmp_path / "raw" / "raw" / "kalshi").rglob("*.jsonl.gz"))
        records = []
        for path in files:
            with gzip.open(path, "rt", encoding="utf-8") as log:
                records.extend(log)
        assert len(records) == 4  # milestone response plus all three live responses
        live_envelopes = [
            json.loads(line) for line in records if json.loads(line)["kind"] == "gamestate"
        ]
        assert all(
            row["cache_headers"].get("cache-control") == "max-age=10" for row in live_envelopes[1:]
        )
        assert all(row["recv_mono_ns"] == 42_500_000_000 for row in live_envelopes)

    asyncio.run(run())
