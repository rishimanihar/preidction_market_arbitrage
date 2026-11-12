"""Build the M1 collector: adapters, writers, discovery, review, evaluator, episodes.

The database file is ``<data_dir>/pmarb.sqlite``. Startup closes episodes left
open by the previous process with ``collector_restart`` before the writer
thread opens the file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pmarb.collector.books import BookStore
from pmarb.collector.clock import Clock, SystemClock
from pmarb.collector.discovery import Discovery
from pmarb.collector.episodes import EpisodeTracker, close_open_episodes, max_episode_id
from pmarb.collector.evaluator import Evaluator
from pmarb.collector.persist import WriteSink
from pmarb.collector.registry import Registry, read_human_rejects, read_registry
from pmarb.collector.review_mode import ReviewMode
from pmarb.collector.runtime import Collector
from pmarb.config import Settings
from pmarb.http import ObservationHttpClient
from pmarb.protocols import VenueAdapter
from pmarb.storage.db import DbWriterTask, open_writer
from pmarb.storage.rawlog import Envelope, RawLogTask, RawLogWriter
from pmarb.venues.kalshi.rest import KalshiAdapter
from pmarb.venues.polymarket_us.rest import PolymarketUSAdapter


def build_collector(
    settings: Settings,
    *,
    clock: Clock | None = None,
    kalshi: VenueAdapter | None = None,
    polyus: VenueAdapter | None = None,
    install_signals: bool = True,
) -> Collector:
    """Wire a collector that discovers, reviews, polls, and evaluates.

    Pass adapters to tests. The default adapters share one observation HTTP
    client and read fee settings from ``settings``. Unset precision or Θ stays
    scenario-only.
    """
    clock = SystemClock() if clock is None else clock
    http: ObservationHttpClient | None = None
    if kalshi is None or polyus is None:
        http = ObservationHttpClient(settings)
        if kalshi is None:
            kalshi = KalshiAdapter(http, settings=settings)
        if polyus is None:
            polyus = PolymarketUSAdapter(http, settings=settings)
    assert kalshi is not None and polyus is not None

    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "pmarb.sqlite"
    raw_queue: asyncio.Queue[Envelope] = asyncio.Queue()
    raw_writer = RawLogWriter(
        data_dir,
        max_bytes=settings.raw_log_max_bytes,
        max_age_days=settings.raw_log_max_days,
    )
    raw_task = RawLogTask(raw_writer, raw_queue)
    db_queue: asyncio.Queue[object] = asyncio.Queue()
    db_task = DbWriterTask(db_path, db_queue)
    sink = WriteSink(db_queue)
    registry = Registry()
    store = BookStore()
    holder: list[Collector] = []
    tracker = EpisodeTracker(
        enqueue=sink,
        cadence_s=lambda: holder[0].cadence_s,
        now=clock.wall,
    )
    review = ReviewMode(
        settings=settings,
        registry=registry,
        enqueue=sink,
        clock=clock,
        episodes=tracker,
    )
    discovery = Discovery(
        kalshi=kalshi,
        polyus=polyus,
        registry=registry,
        review=review,
        clock=clock,
        interval_s=lambda: settings.discovery_interval_s,
        enqueue=sink,
        episodes=tracker,
    )
    evaluator = Evaluator(
        registry=registry,
        store=store,
        kalshi=kalshi,
        polyus=polyus,
        settings=settings,
        episodes=tracker,
        enqueue=sink,
        clock=clock,
    )

    async def on_books(group_ids: set[str]) -> None:
        await evaluator.on_books_updated(group_ids)

    def on_gap(reason: str) -> None:
        tracker.on_gap(reason)

    def startup() -> None:
        _startup(db_path, clock, tracker, registry, review, holder)

    async def close_http() -> None:
        if http is not None:
            await http.aclose()

    collector = Collector(
        kalshi=kalshi,
        polyus=polyus,
        clock=clock,
        on_books_updated=on_books,
        on_gap=on_gap,
        settings=settings,
        store=store,
        raw_queue=raw_queue,
        raw_task=raw_task,
        db_queue=db_queue,
        db_task=db_task,
        groups=(),
        markets=(),
        install_signals=install_signals,
        discovery=discovery,
        on_startup=startup,
        on_stopped=close_http,
    )
    holder.append(collector)
    discovery.bind(collector)
    evaluator.bind(collector)
    return collector


def _startup(
    db_path: Path,
    clock: Clock,
    tracker: EpisodeTracker,
    registry: Registry,
    review: ReviewMode,
    holder: list[Collector],
) -> None:
    now = clock.wall()
    conn = open_writer(db_path)
    try:
        closed = close_open_episodes(conn, now)
        tracker.note_high_water(max_episode_id(conn))
        loaded = read_registry(conn)
        registry.markets.update(loaded.markets)
        registry.groups.update(loaded.groups)
        review.remember_rejects(read_human_rejects(conn))
    finally:
        conn.close()
    collector = holder[0]
    collector.set_universe(tuple(registry.groups.values()), tuple(registry.markets.values()))
    if closed:
        collector.record_event(None, "collector_restart", {"closed_episodes": closed})
