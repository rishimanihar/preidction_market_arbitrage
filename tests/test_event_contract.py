"""Collector events and ``pmarb report`` share one event-kind contract.

The collector writes ``adapter_error``, ``recording_backpressure``, and
``collector_restart``. Health rows carry per-venue ``fresh`` and ``valid``.
The report has to count those rows, not a parallel vocabulary.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

import pytest

from pmarb import cli
from pmarb.collector.books import BookMeta, market_key
from pmarb.collector.clock import FakeClock
from pmarb.config import Settings
from pmarb.events import (
    ADAPTER_ERROR,
    COLLECTOR_RESTART,
    FRESH,
    HEALTH,
    RECORDING_BACKPRESSURE,
    VALID,
    VENUES,
)
from pmarb.models import Book, Venue
from pmarb.storage.db import DbWriterTask, open_reader
from pmarb.storage.rawlog import Envelope
from tests.test_collector import T0, build, dummy_env


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("PMARB_", "KALSHI_", "POLYUS_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("pmarb.collector.freshness.in_maintenance", lambda _now: False)
    monkeypatch.setattr("pmarb.collector.runtime.in_maintenance", lambda _now: False)


def _put(collector, clock: FakeClock, venue: Venue, market_id: str) -> None:
    stamped = BookMeta(
        received_mono=clock.monotonic(),
        received_wall=clock.wall(),
        connection_generation=collector.store.generation(venue),
        source="poll",
        cached=False,
    )
    book = Book(
        venue=venue,
        market_id=market_id,
        yes_asks=[],
        no_asks=[],
        received_utc=clock.wall(),
        received_monotonic=clock.monotonic(),
        connection_generation=stamped.connection_generation,
        valid=True,
    )
    assert collector.store.put(book, stamped)


async def test_report_reads_collector_events_and_per_venue_freshness(
    tmp_path, monkeypatch, capsys
) -> None:
    clock = FakeClock(wall=T0)
    db_path = tmp_path / "pmarb.sqlite"
    db_queue: asyncio.Queue[object] = asyncio.Queue()
    db_task = DbWriterTask(
        db_path,
        db_queue,
        commit_interval_s=3600,
        batch_size=100,
        checkpoint_interval_s=3600,
    )
    raw_queue: asyncio.Queue[Envelope] = asyncio.Queue()
    collector, _gaps, _updates = build(
        clock,
        _Quiet(Venue.KALSHI),
        _Quiet(Venue.POLYMARKET_US),
        db_task=db_task,
        db_queue=db_queue,
        raw_queue=raw_queue,
        queue_limit=10,
        backpressure_sustained_s=0,
        settings=Settings(_env_file=None, data_dir=tmp_path),
    )
    collector._bind()
    collector._start_writers()
    await asyncio.wait_for(db_task.ready.wait(), 2)
    try:
        _put(collector, clock, Venue.KALSHI, "KX")
        _put(collector, clock, Venue.POLYMARKET_US, "pm")
        collector.store.invalidate(market_key(Venue.POLYMARKET_US, "pm"), ADAPTER_ERROR)
        collector._health_tick()
        clock.advance(mono=1, wall=timedelta(seconds=1))
        collector._health_tick()
        collector._record_adapter_error(
            Venue.POLYMARKET_US, RuntimeError("synthetic outage"), ["pm"]
        )
        for index in range(11):
            raw_queue.put_nowait(dummy_env(index))
        await collector._sync_backpressure()
        clock.advance(mono=0.01, wall=timedelta(seconds=0.01))
        await collector._sync_backpressure()
        collector.record_event(None, COLLECTOR_RESTART, {"closed_episodes": 1})
    finally:
        await collector.stop()

    with open_reader(db_path) as conn:
        rows = list(conn.execute("SELECT kind, detail_json FROM collector_events ORDER BY id"))
    kinds = [kind for kind, _detail in rows]
    assert kinds.count(ADAPTER_ERROR) == 1
    assert kinds.count(RECORDING_BACKPRESSURE) == 1
    assert kinds.count(COLLECTOR_RESTART) == 1
    health = [json.loads(detail) for kind, detail in rows if kind == HEALTH]
    assert len(health) >= 2
    assert health[0][VENUES]["kalshi"][FRESH] is True
    assert health[0][VENUES]["kalshi"][VALID] is True
    assert health[0][VENUES]["polymarket_us"][FRESH] is False
    assert health[0][VENUES]["polymarket_us"][VALID] is False

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(_env_file=None, data_dir=tmp_path))
    monkeypatch.setattr(cli, "_repo_root", lambda: None)
    with pytest.raises(SystemExit) as raised:
        cli.main(["report", "--db", str(db_path), "--format", "json", "--out", "-"])
    assert raised.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    coverage = payload["coverage"]
    assert coverage["error_count"] == 1
    assert coverage["gaps_by_kind"]["backpressure"] == 1
    assert coverage["gaps_by_kind"]["restart"] == 1
    assert coverage["venues"]["kalshi"]["fresh_pct"] == "100"
    assert coverage["venues"]["polymarket_us"]["fresh_pct"] == "0"
    assert coverage["venues"]["kalshi"]["fresh_health_events"] == 2
    assert coverage["venues"]["polymarket_us"]["fresh_health_events"] == 0


class _Quiet:
    """Adapter stand-in. This test drives collector methods directly."""

    def __init__(self, venue: Venue) -> None:
        self.venue = venue

    async def get_books(self, market_ids: list[str]) -> list[Book]:
        del market_ids
        return []
