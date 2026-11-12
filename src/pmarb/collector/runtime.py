"""Poll or stream approved groups and keep a fresh book store.

The evaluator, discovery loop, and episode tracker are not imported here.
PR-6b attaches them through ``on_books_updated``, ``on_gap``, and an optional
``discovery`` loop. ``run_once`` is one catalog pass and one REST poll for
every venue (a one-shot run never opens a stream).

Book source per venue (M2). ``venue_modes`` says, for each venue, whether it
streams or polls (see :mod:`pmarb.collector.streams`). A streaming venue
subscribes to the polled (live) groups' markets and stops REST polling them.
Its books are stored with ``source="stream"`` and a collector-scale stream
generation: each stream instance's own generation plus an offset, so a
resubscription or restart never reuses a generation. Stream invalidate events
invalidate the stored stream book with the stream's reason and close episodes
on that market through ``on_invalidated``. When a stream disconnect leaves the
subscribed markets with no valid book, ``on_gap("stream_disconnect")`` closes
every open episode.

While a venue's stream is unhealthy (:class:`~pmarb.collector.streams.StreamHealth`)
the venue falls back to REST for those markets. Fallback books are ``source="poll"``
in the poll generation sequence, never the stream's; stream books are not
stored during the fallback, and the switch back happens once the stream is
healthy again. Every switch is a ``stream_fallback`` / ``stream_recovered``
collector event; the startup mode is a ``stream_mode`` event and each health
row carries the per-venue state.

Age uses ``BookMeta.received_mono`` and ``received_wall``: this collector's
clock, stamped when the book is accepted. Skew across venues must use
``Book.received_monotonic``, which the adapter stamps when it parses the
response. Polled meta timestamps are nearly equal because both legs are
stored after ``asyncio.gather`` returns; that is not an atomic snapshot.
``source == "poll"`` and ``books_atomic_group_id is None`` means the books
are indicative only and must never be qualified.

``BookMeta.discovery_sample`` is set on the 60 s samples.
:meth:`Collector.opens_episodes` is false for those groups. They must not
open episodes.

``on_gap`` reasons are ``clock_jump``, ``recording_backpressure``,
``maintenance`` (once, on entering the window), ``stream_disconnect``, and
``collector_shutdown``. Book invalid reasons also include ``stale``,
``cached``, ``adapter_error``, ``generation``, the streams' own reasons
(``disconnected``, ``sequence_gap``, ``schema_failure``, ...), and
``stream_unhealthy`` / ``stream_resubscribe`` / ``stream_idle``. A heartbeat
does not refresh book age.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from pmarb.collector.books import GENERATION, BookMeta, BookState, BookStore, market_key
from pmarb.collector.clock import Clock
from pmarb.collector.freshness import (
    CLOCK_JUMP,
    MAINTENANCE,
    STALE,
    ClockJumpGuard,
    book_is_cached,
    is_fresh,
    max_age_for,
)
from pmarb.collector.scheduler import PollPlan, build_poll_plan, market_ids
from pmarb.collector.streams import (
    POLL,
    STREAM,
    STREAM_FALLBACK,
    HealthLimits,
    StreamHealth,
    Transition,
    VenueMode,
    poll_mode,
)
from pmarb.collector.supervisor import RestartBackoff, supervise
from pmarb.config import Settings
from pmarb.http import Backoff
from pmarb.models import Book, EquivalenceGroup, GroupStatus, Market, Venue
from pmarb.protocols import BookEvent, VenueAdapter
from pmarb.storage.db import DbWriterTask, Write, insert_collector_event
from pmarb.storage.rawlog import Envelope, RawLogTask, disk_free_bytes
from pmarb.venues.kalshi.ws import WS_PATH as KALSHI_WS_PATH
from pmarb.venues.polymarket_us.maintenance import in_maintenance
from pmarb.venues.polymarket_us.ws import WS_PATH as POLYUS_WS_PATH

_log = logging.getLogger(__name__)

FRESHNESS_INTERVAL_S = 0.25
HEALTH_INTERVAL_S = 10.0
DISK_FREE_MIN_BYTES = 2 * 1024**3
DEFAULT_BACKPRESSURE_HIGH_WATER = 20_000
DEFAULT_BACKPRESSURE_LOW_WATER = 10_000
DEFAULT_QUEUE_LIMIT = DEFAULT_BACKPRESSURE_HIGH_WATER
BACKPRESSURE_SUSTAINED_S = 5.0

RECORDING_BACKPRESSURE = "recording_backpressure"
ADAPTER_ERROR = "adapter_error"
COLLECTOR_SHUTDOWN = "collector_shutdown"
LOOP_CRASH = "loop_crash"
HEALTH = "health"
STREAM_MODE = "stream_mode"
STREAM_DISCONNECT = "stream_disconnect"
STREAM_UNHEALTHY = "stream_unhealthy"
STREAM_RESUBSCRIBE = "stream_resubscribe"
STREAM_IDLE = "stream_idle"
STREAM_ENDED = "stream_ended"
STREAM_ERROR = "stream_error"
_VENUES = (Venue.KALSHI, Venue.POLYMARKET_US)
_WS_PATHS = {Venue.KALSHI: KALSHI_WS_PATH, Venue.POLYMARKET_US: POLYUS_WS_PATH}

# Process-wide invalidations. A later freshness pass must not relabel them.
_PROCESS_REASONS = frozenset({CLOCK_JUMP, RECORDING_BACKPRESSURE, GENERATION, ADAPTER_ERROR})

BooksHook = Callable[[set[str]], Awaitable[None] | None]
GapHook = Callable[[str], Awaitable[None] | None]
InvalidateHook = Callable[[set[str], str], Awaitable[None] | None]
StartupHook = Callable[[], Awaitable[None] | None]
_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _once_sample_key(start: datetime | None, now: datetime) -> tuple[int, float]:
    """Prefer upcoming samples, then the most recent past start, unknown last."""
    if start is None:
        return (2, 0.0)
    seconds = (start - now).total_seconds()
    if seconds >= 0:
        return (0, seconds)
    return (1, -seconds)


class DiscoveryLoop(Protocol):
    """Catalog refresh. The runtime does not import the discovery module."""

    async def run(self) -> None:
        """Refresh until stopped."""

    async def run_once(self) -> None:
        """One catalog pass."""


@dataclass(eq=False, slots=True)
class _StreamRun:
    """One venue's live subscription: the market set and the running stream.

    ``base`` offsets the current stream instance's generation onto the
    collector's per-venue stream sequence. ``gap_reported`` keeps one
    ``stream_disconnect`` gap per outage.
    """

    venue: Venue
    market_ids: tuple[str, ...]
    stream: AsyncIterator[BookEvent] | None = None
    base: int = 0
    task: asyncio.Task[None] | None = None
    gap_reported: bool = False


@dataclass(frozen=True, slots=True)
class CollectorEvent:
    """One collector_events row, also kept on :attr:`Collector.events`."""

    utc: datetime
    venue: str | None
    kind: str
    detail: dict[str, Any]


class Collector:
    """One poll loop, a freshness timer, health, and a clean shutdown.

    Adapters, clock, writers, and the evaluator hooks are injected. ``stop``
    cancels the pollers, calls ``on_gap("collector_shutdown")``, then cancels
    the raw-log and database tasks so they drain and fsync.
    """

    def __init__(
        self,
        *,
        kalshi: VenueAdapter,
        polyus: VenueAdapter,
        clock: Clock,
        on_books_updated: BooksHook,
        on_gap: GapHook,
        settings: Settings | None = None,
        store: BookStore | None = None,
        raw_queue: asyncio.Queue[Envelope] | None = None,
        raw_task: RawLogTask | None = None,
        db_queue: asyncio.Queue[object] | None = None,
        db_task: DbWriterTask | None = None,
        disk_free: Callable[[Path], int] | None = None,
        queue_limit: int = DEFAULT_BACKPRESSURE_HIGH_WATER,
        backpressure_low_water: int | None = None,
        backpressure_sustained_s: float = BACKPRESSURE_SUSTAINED_S,
        backoff: RestartBackoff | None = None,
        groups: Sequence[EquivalenceGroup] = (),
        markets: Sequence[Market] = (),
        install_signals: bool = True,
        discovery: DiscoveryLoop | None = None,
        on_startup: StartupHook | None = None,
        on_stopped: StartupHook | None = None,
        venue_modes: Mapping[Venue, VenueMode] | None = None,
        on_invalidated: InvalidateHook | None = None,
    ) -> None:
        if queue_limit < 0:
            raise ValueError("queue_limit must be >= 0")
        if raw_task is not None:
            if raw_queue is not None and raw_queue is not raw_task.queue:
                raise ValueError("raw_queue must be the RawLogTask queue")
            raw_queue = raw_task.queue
        if db_task is not None:
            if db_queue is not None and db_queue is not db_task.queue:
                raise ValueError("db_queue must be the DbWriterTask queue")
            db_queue = db_task.queue
        self._kalshi = kalshi
        self._polyus = polyus
        self._clock = clock
        self._on_books_updated = on_books_updated
        self._on_gap = on_gap
        self._settings = settings if settings is not None else Settings(_env_file=None)
        self.store = store if store is not None else BookStore()
        self._raw_queue = raw_queue
        self._raw_task = raw_task
        self._db_queue = db_queue
        self._db_task = db_task
        self._disk_free = disk_free if disk_free is not None else disk_free_bytes
        # `queue_limit` remains an explicit small-test/legacy override. In normal
        # operation discovery bursts are allowed to accumulate well past 1,000.
        self._queue_limit = queue_limit
        self._low_water = (
            backpressure_low_water
            if backpressure_low_water is not None
            else min(DEFAULT_BACKPRESSURE_LOW_WATER, self._queue_limit // 2)
        )
        if self._low_water < 0 or self._low_water >= self._queue_limit:
            raise ValueError("backpressure_low_water must be >= 0 and below high water")
        if backpressure_sustained_s < 0:
            raise ValueError("backpressure_sustained_s must be >= 0")
        self._backpressure_sustained_s = backpressure_sustained_s
        self._backpressure_since: float | None = None
        self._backoff = backoff if backoff is not None else Backoff()
        self._install_signals_flag = install_signals
        self._discovery = discovery
        self._on_startup = on_startup
        self._on_stopped = on_stopped
        self._on_invalidated = on_invalidated
        modes = dict(venue_modes or {})
        for venue in _VENUES:
            mode = modes.get(venue)
            if mode is None:
                modes[venue] = poll_mode(venue)
            elif mode.venue is not venue:
                raise ValueError(f"venue_modes[{venue}] describes {mode.venue}")
        self._modes: dict[Venue, VenueMode] = modes
        self._health_limits = HealthLimits.from_settings(self._settings)
        self._health: dict[Venue, StreamHealth] = {}
        self._runs: dict[Venue, _StreamRun] = {}
        self._stream_gen_high: dict[Venue, int] = {}
        self._once = False
        self._groups: tuple[EquivalenceGroup, ...] = ()
        self._markets: tuple[Market, ...] = ()
        self.set_universe(groups, markets)

        self.events: list[CollectorEvent] = []
        self.cycles = 0
        self.freshness_ticks = 0
        self.cadence_s = self._settings.min_poll_cadence_s
        self.sleep_s = self.cadence_s
        self.error_count = 0
        self.http_429_count = 0
        self.plan: PollPlan | None = None

        self._polled_ids: frozenset[str] = frozenset()
        self._discovery_ids: frozenset[str] = frozenset()
        self._index: dict[str, set[str]] = {}
        self._next_discovery_mono: float | None = None
        self._discovery_cursor = 0
        self._backpressure = False
        self._in_maintenance = False
        self._disk_low = False
        self._raw_logging = True
        self._disk_free_bytes: int | None = None
        self._jump = ClockJumpGuard(threshold_s=1.0)
        self._stopping = False
        self._bound = False
        self._stop_lock: asyncio.Lock | None = None
        self._stopped: asyncio.Event | None = None
        self._signals: list[signal.Signals] = []
        self._signal_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._fresh_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._discovery_task: asyncio.Task[None] | None = None
        self._raw_handle: asyncio.Task[None] | None = None
        self._db_handle: asyncio.Task[None] | None = None

    def set_universe(self, groups: Sequence[EquivalenceGroup], markets: Sequence[Market]) -> None:
        """Replace the groups and the markets that carry start times."""
        self._groups = tuple(groups)
        self._markets = tuple(markets)

    def opens_episodes(self, group_id: str) -> bool:
        """False for discovery samples and for anything outside the fast set."""
        return group_id in self._polled_ids

    @property
    def polled_group_ids(self) -> frozenset[str]:
        return self._polled_ids

    @property
    def discovery_group_ids(self) -> frozenset[str]:
        return self._discovery_ids

    @property
    def raw_logging(self) -> bool:
        return self._raw_logging

    @property
    def venue_modes(self) -> Mapping[Venue, VenueMode]:
        """The configured book source per venue (stream or poll, and why)."""
        return dict(self._modes)

    def stream_health(self, venue: Venue) -> StreamHealth | None:
        """The venue's stream health, or ``None`` before its stream first starts."""
        return self._health.get(venue)

    def active_source(self, venue: Venue) -> str:
        """``stream`` when the venue's live markets come from its stream right now."""
        return POLL if self._polls_rest(venue) else STREAM

    def stream_is_current(self, venue: Venue, generation: int) -> bool:
        """True when ``venue`` streams, its stream is healthy, and ``generation`` is live.

        The evaluator calls this per leg: a stream book is atomic only on the
        connection that is delivering right now.
        """
        if self._once or not self._streams(venue):
            return False
        health = self._health.get(venue)
        if health is None or not health.healthy:
            return False
        current = self._current_stream_generation(venue)
        return current is not None and generation == current

    @property
    def backpressure(self) -> bool:
        return self._backpressure

    @property
    def stopping(self) -> bool:
        """True after :meth:`stop` has begun."""
        return self._stopping

    def record_event(self, venue: str | None, kind: str, detail: Mapping[str, Any]) -> None:
        """Append a collector event and queue it for the database."""
        self._emit(venue, kind, detail)

    def _bind(self) -> None:
        if self._bound:
            return
        self._bound = True
        self._stop_lock = asyncio.Lock()
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        """Run until :meth:`stop`. ``CancelledError`` shuts down and propagates."""
        self._bind()
        assert self._stopped is not None
        if self._stopped.is_set() or self._stopping:
            return
        self._install_signals()
        try:
            try:
                await self._serve()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    while current.cancelling():
                        current.uncancel()
                try:
                    await self.stop()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - shutdown must still re-raise CancelledError
                    _log.exception("collector stop failed")
                raise
        finally:
            self._remove_signals()

    async def run_once(self) -> None:
        """One discovery pass and one poll, then the same shutdown as :meth:`stop`."""
        self._bind()
        self._install_signals()
        self._once = True
        started = self._clock.monotonic()
        try:
            await self._run_startup()
            self._announce_modes()
            self._start_writers()
            if self._db_task is not None:
                await self._wait_db()
            if not self._stopping and self._discovery is not None:
                await self._discovery.run_once()
            if not self._stopping:
                await self._drain_db_barrier()
                await self._poll_once(once=True)
        finally:
            self._timing("run_once", max(0.0, self._clock.monotonic() - started))
            try:
                await self.stop()
            finally:
                self._remove_signals()

    async def stop(self) -> None:
        """Stop pollers, report shutdown, then drain the raw log and the database."""
        self._bind()
        assert self._stop_lock is not None and self._stopped is not None
        async with self._stop_lock:
            if self._stopped.is_set():
                return
            self._stopping = True
            try:
                await self._cancel_and_wait(self._service_tasks())
                for run in tuple(self._runs.values()):
                    await self._close_stream(run)
                try:
                    await self._notify_gap(COLLECTOR_SHUTDOWN)
                finally:
                    self._emit(None, COLLECTOR_SHUTDOWN, {})
            finally:
                try:
                    await self._cancel_and_wait([self._raw_handle] if self._raw_handle else [])
                    self._emit_writer_timings()
                    await self._cancel_and_wait([self._db_handle] if self._db_handle else [])
                finally:
                    try:
                        if self._on_stopped is not None:
                            await _call(self._on_stopped)
                    finally:
                        self._stopped.set()

    async def _run_startup(self) -> None:
        if self._on_startup is not None:
            await _call(self._on_startup)

    async def _serve(self) -> None:
        assert self._stopped is not None
        await self._run_startup()
        self._announce_modes()
        self._start_writers()
        if self._db_task is not None:
            await self._wait_db()
        if self._stopping:
            return
        self._start_loops()
        await self._stopped.wait()

    def _start_writers(self) -> None:
        if self._db_task is not None:
            self._db_handle = asyncio.create_task(
                supervise(
                    "db",
                    self._run_db,
                    backoff=self._backoff,
                    clock=self._clock,
                    on_crash=self._on_crash,
                ),
                name="collector-db",
            )
        if self._raw_task is not None:
            self._raw_handle = asyncio.create_task(
                supervise(
                    "raw",
                    self._run_raw,
                    backoff=self._backoff,
                    clock=self._clock,
                    on_crash=self._on_crash,
                ),
                name="collector-raw",
            )

    def _start_loops(self) -> None:
        common = {"backoff": self._backoff, "clock": self._clock, "on_crash": self._on_crash}
        self._poll_task = asyncio.create_task(
            supervise("poll", self._poll_loop, **common), name="collector-poll"
        )
        self._fresh_task = asyncio.create_task(
            supervise("freshness", self._freshness_loop, **common),
            name="collector-freshness",
        )
        self._health_task = asyncio.create_task(
            supervise("health", self._health_loop, **common), name="collector-health"
        )
        if self._discovery is not None:
            self._discovery_task = asyncio.create_task(
                supervise("discovery", self._discovery.run, **common),
                name="collector-discovery",
            )

    async def _run_db(self) -> None:
        assert self._db_task is not None
        await self._db_task.run()

    async def _run_raw(self) -> None:
        assert self._raw_task is not None
        await self._raw_task.run()

    async def _wait_db(self) -> None:
        assert self._db_task is not None and self._db_handle is not None
        ready_task = asyncio.create_task(self._db_task.ready.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready_task, self._db_handle}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            ready_task.cancel()
            raise
        if self._db_handle in done and not self._db_task.ready.is_set():
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
            await self._db_handle
            return
        await ready_task

    async def _poll_loop(self) -> None:
        while not self._stopping:
            await self._poll_once()
            if self._stopping:
                return
            await self._clock.sleep(self.sleep_s)

    async def _freshness_loop(self) -> None:
        while not self._stopping:
            await self._clock.sleep(FRESHNESS_INTERVAL_S)
            if self._stopping:
                return
            await self._freshness_tick()

    async def _health_loop(self) -> None:
        while not self._stopping:
            await self._clock.sleep(HEALTH_INTERVAL_S)
            if self._stopping:
                return
            self._health_tick()

    async def _poll_once(self, *, once: bool = False) -> None:
        if self._stopping:
            return
        poll_started = self._clock.monotonic()
        await self._sync_backpressure()
        self._check_disk()
        now_mono = self._clock.monotonic()
        now_wall = self._clock.wall()
        self._reindex()
        plan = build_poll_plan(
            self._groups,
            self._markets,
            now_wall,
            max_polled_groups=self._settings.max_polled_groups,
            start_window=timedelta(minutes=self._settings.polled_group_start_window_min),
            sample_interval_s=self._settings.sample_interval_s,
            min_cadence_s=self._settings.min_poll_cadence_s,
            polyus_books_rps=self._settings.polyus_books_rps,
        )
        self.plan = plan
        self.cadence_s = plan.cadence_s
        self._polled_ids = frozenset(item.group_id for item in plan.polled)
        self._discovery_ids = frozenset(item.group_id for item in plan.discovery)
        if self._next_discovery_mono is None:
            self._next_discovery_mono = now_mono
        include_discovery = now_mono >= self._next_discovery_mono
        if include_discovery:
            self._next_discovery_mono = now_mono + plan.sample_interval_s
        selected = plan.polled
        if once and not selected:
            selected = tuple(
                sorted(plan.discovery, key=lambda item: _once_sample_key(item.start, now_wall))[
                    : self._settings.max_polled_groups
                ]
            )
        elif not once and include_discovery:
            cold = plan.discovery
            if cold:
                cap = min(self._settings.max_polled_groups, len(cold))
                start = self._discovery_cursor % len(cold)
                selected_cold = cold[start : start + cap]
                self._discovery_cursor = (start + len(selected_cold)) % len(cold)
            else:
                selected_cold = ()
            selected = plan.polled + tuple(selected_cold)
        self._polled_ids = frozenset(item.group_id for item in plan.polled)
        selected_ids = frozenset(item.group_id for item in selected)
        self._discovery_ids = selected_ids - self._polled_ids
        if not once:
            await self._sync_streams(plan)
        hot = tuple(item for item in selected if item.group_id in self._polled_ids)
        cold = tuple(item for item in selected if item.group_id in self._discovery_ids)
        batches = [batch for batch in (hot, cold) if batch]
        if not batches and selected:
            batches = [tuple(selected)]
        touched: set[str] = set()
        for batch in batches:
            # A healthy stream covers the live (hot) markets. Discovery samples
            # stay on REST for every venue.
            streamed = batch is hot
            kalshi_ids = self._rest_ids(batch, Venue.KALSHI, streamed=streamed)
            poly_ids = self._rest_ids(batch, Venue.POLYMARKET_US, streamed=streamed)
            kalshi_result, poly_result = await asyncio.gather(
                self._timed_fetch(self._kalshi, kalshi_ids),
                self._timed_fetch(self._polyus, poly_ids),
            )
            kalshi_books, kalshi_duration = kalshi_result
            poly_books, poly_duration = poly_result
            self._timing(
                "poll_fetch",
                kalshi_duration,
                venue=str(Venue.KALSHI),
                requested=len(kalshi_ids),
                returned=len(kalshi_books),
            )
            self._timing(
                "poll_fetch",
                poly_duration,
                venue=str(Venue.POLYMARKET_US),
                requested=len(poly_ids),
                returned=len(poly_books),
            )
            accepted_mono = self._clock.monotonic()
            accepted_wall = self._clock.wall()
            for book in (*kalshi_books, *poly_books):
                self._accept_book(book, accepted_mono, accepted_wall)
            await self._sync_backpressure()
            if self._backpressure:
                self.store.invalidate(None, RECORDING_BACKPRESSURE)
            self._apply_book_freshness(accepted_mono, accepted_wall)
            await self._sync_maintenance(accepted_wall)
            batch_touched = self._groups_for_ids(
                ((Venue.KALSHI, kalshi_ids), (Venue.POLYMARKET_US, poly_ids))
            )
            batch_touched.intersection_update(item.group_id for item in batch)
            touched |= batch_touched
            await self._notify_books(batch_touched)
        if not batches:
            now_mono = self._clock.monotonic()
            now_wall = self._clock.wall()
            await self._sync_maintenance(now_wall)
            self._apply_book_freshness(now_mono, now_wall)
        self._set_sleep(plan)
        self._timing("poll", max(0.0, self._clock.monotonic() - poll_started), groups=len(touched))
        self._emit_writer_timings()
        self.cycles += 1

    async def _drain_db_barrier(self) -> None:
        """Drain discovery persistence before fetching books for --once."""
        if self._raw_task is not None:
            await self._raw_task.drain()
        if self._db_task is not None:
            await self._db_task.drain()

    async def _freshness_tick(self) -> None:
        self.freshness_ticks += 1
        self._reindex()
        now_mono = self._clock.monotonic()
        now_wall = self._clock.wall()
        changed: set[str] = set()
        if self._jump.check(now_mono, now_wall):
            self.store.invalidate(None, CLOCK_JUMP)
            self._emit(None, CLOCK_JUMP, {"mono": now_mono, "wall": now_wall.isoformat()})
            await self._notify_gap(CLOCK_JUMP)
            changed |= self._all_group_ids()
        await self._sync_backpressure()
        if self._backpressure:
            changed |= self._all_group_ids()
        self._check_disk()
        changed |= await self._check_stream_health(now_mono)
        await self._sync_maintenance(now_wall)
        changed |= self._apply_book_freshness(now_mono, now_wall)
        await self._notify_books(set(self._polled_ids) | changed)

    def _health_tick(self) -> None:
        self._check_disk()
        alarm = "disk_low" if self._disk_low else None
        self._emit(None, HEALTH, self._health_detail(alarm=alarm))

    def _accept_book(self, book: Book, now_mono: float, now_wall: datetime) -> None:
        self._enqueue_raw(_envelope(book, now_mono, now_wall))
        meta = BookMeta(
            received_mono=now_mono,
            received_wall=now_wall,
            connection_generation=book.connection_generation,
            source="poll",
            cached=book_is_cached(book),
            books_atomic_group_id=None,
            discovery_sample=self._market_is_discovery(book.venue, book.market_id),
        )
        if not self.store.put(book, meta):
            _log.warning(
                "rejected %s book %s from generation %s (current %s)",
                book.venue,
                book.market_id,
                book.connection_generation,
                self.store.generation(book.venue),
            )

    async def _fetch(self, adapter: VenueAdapter, ids: Sequence[str]) -> list[Book]:
        if not ids:
            return []
        try:
            books = await adapter.get_books(list(ids))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any adapter failure invalidates that venue
            self._record_adapter_error(adapter.venue, exc)
            return []
        if not isinstance(books, list):
            self._record_adapter_error(adapter.venue, TypeError("get_books must return a list"))
            return []
        return books

    async def _timed_fetch(
        self, adapter: VenueAdapter, ids: Sequence[str]
    ) -> tuple[list[Book], float]:
        started = self._clock.monotonic()
        books = await self._fetch(adapter, ids)
        return books, max(0.0, self._clock.monotonic() - started)

    def _record_adapter_error(self, venue: Venue, exc: Exception) -> None:
        self.error_count += 1
        status = getattr(exc, "status_code", None)
        if status == 429 or "HTTP 429" in str(exc):
            self.http_429_count += 1
        self.store.invalidate_venue(venue, ADAPTER_ERROR)
        _log.warning("book poll failed for %s: %s", venue, type(exc).__name__)
        self._emit(
            str(venue),
            ADAPTER_ERROR,
            {
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "status_code": status if isinstance(status, int) else None,
            },
        )

    def _apply_book_freshness(self, now_mono: float, now_wall: datetime) -> set[str]:
        changed: set[str] = set()
        for state in self.store.items():
            if state.invalid_reason in _PROCESS_REASONS:
                continue
            fresh, reason = is_fresh(state, now_mono, now_wall, self._max_age(state))
            if fresh or reason == state.invalid_reason:
                continue
            self.store.invalidate(state.market_key, reason or STALE)
            changed |= self._index.get(state.market_key, set())
        return changed

    def _max_age(self, state: BookState) -> float:
        settings = self._settings
        if state.meta.source == "stream":
            return max_age_for(
                "stream",
                self.cadence_s,
                stream_max_age_s=settings.stream_max_age_s,
                poll_multiple=settings.poll_max_age_cadence_multiple,
            )
        cadence = settings.sample_interval_s if state.meta.discovery_sample else self.cadence_s
        return max_age_for(
            "poll",
            cadence,
            stream_max_age_s=settings.stream_max_age_s,
            poll_multiple=settings.poll_max_age_cadence_multiple,
        )

    async def _sync_backpressure(self) -> None:
        over_high = self._queues_over_limit(self._queue_limit)
        if self._backpressure:
            if not self._queues_over_limit(self._low_water):
                self._backpressure = False
                self._backpressure_since = None
            else:
                self.store.invalidate(None, RECORDING_BACKPRESSURE)
            return
        if not over_high:
            self._backpressure_since = None
            return
        now = self._clock.monotonic()
        if self._backpressure_since is None:
            self._backpressure_since = now
            return
        if now - self._backpressure_since <= self._backpressure_sustained_s:
            return
        self._backpressure = True
        self.store.invalidate(None, RECORDING_BACKPRESSURE)
        self._emit(
            None,
            RECORDING_BACKPRESSURE,
            {
                "queue_depth_raw": _depth(self._raw_queue),
                "queue_depth_db": _depth(self._db_queue),
            },
        )
        await self._notify_gap(RECORDING_BACKPRESSURE)

    async def _sync_maintenance(self, now_wall: datetime) -> None:
        active = in_maintenance(now_wall)
        if active and not self._in_maintenance:
            self._in_maintenance = True
            self._emit(str(Venue.POLYMARKET_US), MAINTENANCE, {})
            await self._notify_gap(MAINTENANCE)
            return
        if not active:
            self._in_maintenance = False

    def _queues_over_limit(self, limit: int) -> bool:
        raw_over = self._raw_queue is not None and self._raw_queue.qsize() > limit
        db_over = self._db_queue is not None and self._db_queue.qsize() > limit
        return raw_over or db_over

    def _set_sleep(self, plan: PollPlan) -> None:
        if self._backpressure:
            self.sleep_s = max(plan.cadence_s, plan.sample_interval_s)
        else:
            self.sleep_s = plan.cadence_s

    def _check_disk(self) -> None:
        try:
            free = self._disk_free(self._settings.data_dir)
        except OSError as exc:
            self._disk_free_bytes = None
            if not self._disk_low:
                self._disk_low = True
                self._raw_logging = False
                detail = self._health_detail(alarm="disk_check_failed")
                detail["error"] = str(exc)[:500]
                self._emit(None, HEALTH, detail)
            return
        self._disk_free_bytes = free
        low = free < DISK_FREE_MIN_BYTES
        if low and not self._disk_low:
            self._disk_low = True
            self._raw_logging = False
            self._emit(None, HEALTH, self._health_detail(alarm="disk_low"))
        elif not low and self._disk_low:
            self._disk_low = False
            self._raw_logging = True
            self._emit(None, HEALTH, self._health_detail(alarm="disk_recovered"))

    def _health_detail(self, *, alarm: str | None) -> dict[str, Any]:
        return {
            "queue_depth_raw": _depth(self._raw_queue),
            "queue_depth_db": _depth(self._db_queue),
            "last_book_age_s": self._last_book_age(self._clock.wall()),
            "error_count": self.error_count,
            "http_429_count": self.http_429_count,
            "disk_free_bytes": self._disk_free_bytes,
            "raw_logging": self._raw_logging,
            "backpressure": self._backpressure,
            "cadence_s": self.cadence_s,
            "venues": {str(venue): self._venue_status(venue) for venue in _VENUES},
            "alarm": alarm,
        }

    def _last_book_age(self, now_wall: datetime) -> dict[str, float | None]:
        newest: dict[str, datetime] = {}
        for state in self.store.items():
            venue = str(state.book.venue)
            previous = newest.get(venue)
            if previous is None or state.meta.received_wall > previous:
                newest[venue] = state.meta.received_wall
        ages: dict[str, float | None] = {}
        for venue in (Venue.KALSHI, Venue.POLYMARKET_US):
            seen = newest.get(str(venue))
            ages[str(venue)] = None if seen is None else (now_wall - seen).total_seconds()
        return ages

    def _enqueue_raw(self, env: Envelope) -> None:
        if self._raw_queue is None or not self._raw_logging or self._backpressure:
            return
        if self._raw_queue.qsize() > self._queue_limit:
            return
        try:
            self._raw_queue.put_nowait(env)
        except asyncio.QueueFull:
            return

    def _emit(self, venue: str | None, kind: str, detail: Mapping[str, Any]) -> None:
        payload = dict(detail)
        utc = self._clock.wall()
        self.events.append(CollectorEvent(utc=utc, venue=venue, kind=kind, detail=payload))
        if self._db_queue is None or self._db_queue.qsize() > self._queue_limit:
            return
        try:
            self._db_queue.put_nowait(Write(_event_op(utc, venue, kind, payload)))
        except asyncio.QueueFull:
            return

    def _timing(self, phase: str, duration_s: float, **detail: Any) -> None:
        self._emit(None, "timing", {"phase": phase, "duration_s": duration_s, **detail})

    def _emit_writer_timings(self) -> None:
        raw_writer = getattr(self._raw_task, "writer", None)
        if raw_writer is not None:
            self._timing(
                "raw_flush",
                float(getattr(raw_writer, "flush_seconds", 0.0)),
                cumulative=True,
                flushes=int(getattr(raw_writer, "flush_count", 0)),
                bytes=int(getattr(raw_writer, "flush_bytes", 0)),
            )
        if self._db_task is not None:
            self._timing(
                "db_write",
                float(getattr(self._db_task, "write_seconds", 0.0)),
                cumulative=True,
                batches=int(getattr(self._db_task, "write_batches", 0)),
                ops=int(getattr(self._db_task, "write_ops", 0)),
            )

    def _on_crash(self, name: str, exc: BaseException) -> None:
        _log.exception("collector loop %s crashed", name)
        self._emit(
            None,
            LOOP_CRASH,
            {"loop": name, "error_type": type(exc).__name__, "error": str(exc)[:500]},
        )

    def _reindex(self) -> None:
        index: dict[str, set[str]] = {}
        for group in self._groups:
            if group.status is not GroupStatus.APPROVED:
                continue
            for member in group.members:
                key = market_key(member.venue, member.market_id)
                index.setdefault(key, set()).add(group.group_id)
        self._index = index

    def _market_is_discovery(self, venue: Venue, market_id: str) -> bool:
        group_ids = self._index.get(market_key(venue, market_id), set())
        if not group_ids:
            return False
        return all(group_id not in self._polled_ids for group_id in group_ids)

    def _groups_for_ids(self, batches: Sequence[tuple[Venue, Sequence[str]]]) -> set[str]:
        touched: set[str] = set()
        for venue, ids in batches:
            for market_id in ids:
                touched |= self._index.get(market_key(venue, market_id), set())
        return touched

    def _all_group_ids(self) -> set[str]:
        ids: set[str] = set()
        for group_ids in self._index.values():
            ids |= group_ids
        return ids

    async def _notify_books(self, group_ids: set[str]) -> None:
        started = self._clock.monotonic()
        await _call(self._on_books_updated, set(group_ids))
        self._timing("evaluate", max(0.0, self._clock.monotonic() - started), groups=len(group_ids))

    async def _notify_gap(self, reason: str) -> None:
        await _call(self._on_gap, reason)

    def _service_tasks(self) -> list[asyncio.Task[None]]:
        tasks = [
            task
            for task in (
                self._poll_task,
                self._fresh_task,
                self._health_task,
                self._discovery_task,
            )
            if task is not None
        ]
        tasks.extend(run.task for run in self._runs.values() if run.task is not None)
        return tasks

    def _writer_tasks(self) -> list[asyncio.Task[None]]:
        return [task for task in (self._raw_handle, self._db_handle) if task is not None]

    async def _cancel_and_wait(self, tasks: list[asyncio.Task[None]]) -> None:
        live = [task for task in tasks if not task.done()]
        for task in live:
            task.cancel()
        if live:
            await asyncio.wait(live)
        for task in tasks:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                _log.error("task %s ended with %s", task.get_name(), type(exc).__name__)

    # --- streams -----------------------------------------------------------

    def _announce_modes(self) -> None:
        for venue in _VENUES:
            mode = self._modes[venue]
            detail = mode.describe()
            if self._once and mode.mode == STREAM:
                detail["once"] = "a one-shot run polls REST; the stream starts only in a full run"
            _log.info("%s book source: %s (%s)", venue, mode.mode, mode.reason)
            self._emit(str(venue), STREAM_MODE, detail)

    def _streams(self, venue: Venue) -> bool:
        mode = self._modes.get(venue)
        return mode is not None and mode.mode == STREAM

    def _polls_rest(self, venue: Venue) -> bool:
        """True when ``venue``'s live markets come from REST right now."""
        if self._once or not self._streams(venue):
            return True
        health = self._health.get(venue)
        return health is not None and health.fallback

    def _rest_ids(self, batch: Sequence[Any], venue: Venue, *, streamed: bool) -> tuple[str, ...]:
        """REST ids for one batch. A healthy stream's markets never also come from REST."""
        ids = market_ids(batch, venue)
        if self._polls_rest(venue):
            return ids
        if streamed:
            return ()
        run = self._runs.get(venue)
        if run is None:
            return ids
        covered = set(run.market_ids)
        return tuple(market_id for market_id in ids if market_id not in covered)

    def _venue_status(self, venue: Venue) -> dict[str, Any]:
        mode = self._modes[venue]
        status: dict[str, Any] = {
            "mode": mode.mode,
            "reason": mode.reason,
            "active": self.active_source(venue),
        }
        if mode.mode != STREAM:
            return status
        run = self._runs.get(venue)
        health = self._health.get(venue)
        status["subscribed"] = 0 if run is None else len(run.market_ids)
        status["generation"] = self._current_stream_generation(venue)
        if health is None or run is None:
            status["state"] = "idle"
            status["fallback"] = health is not None and health.fallback
        else:
            status.update(health.describe(self._clock.monotonic()))
        return status

    def _current_stream_generation(self, venue: Venue) -> int | None:
        run = self._runs.get(venue)
        if run is None or run.stream is None:
            return None
        local = getattr(run.stream, "connection_generation", None)
        if isinstance(local, bool) or not isinstance(local, int) or local < 1:
            return None
        return run.base + local

    async def _sync_streams(self, plan: PollPlan) -> None:
        """Subscribe each streaming venue to the polled groups' markets.

        A new market restarts that venue's stream; markets that drop out stay
        subscribed until the next restart (fewer reconnects). No polled group
        stops the stream.
        """
        for venue in _VENUES:
            if self._modes[venue].mode != STREAM or self._stopping:
                continue
            wanted = market_ids(plan.polled, venue)
            run = self._runs.get(venue)
            if not wanted:
                if run is not None:
                    await self._stop_stream(venue, STREAM_IDLE)
                continue
            if run is not None and set(wanted) <= set(run.market_ids):
                continue
            if run is not None:
                await self._stop_stream(venue, STREAM_RESUBSCRIBE)
            self._start_stream(venue, wanted)

    def _start_stream(self, venue: Venue, ids: tuple[str, ...]) -> None:
        now = self._clock.monotonic()
        health = self._health.get(venue)
        if health is None:
            self._health[venue] = StreamHealth(venue, self._health_limits, now)
        else:
            health.restart(now)
        run = _StreamRun(venue=venue, market_ids=ids)
        self._runs[venue] = run
        self._emit(str(venue), "stream_subscribe", {"markets": len(ids)})
        run.task = asyncio.create_task(self._stream_loop(run), name=f"collector-stream-{venue}")

    async def _stop_stream(self, venue: Venue, reason: str) -> None:
        run = self._runs.pop(venue, None)
        if run is None:
            return
        if run.task is not None and not run.task.done():
            run.task.cancel()
            await asyncio.wait([run.task])
        await self._close_stream(run)
        health = self._health.get(venue)
        if health is not None and reason == STREAM_IDLE:
            health.idle()
        touched = await self._invalidate_stream_books(venue, reason)
        self._emit(str(venue), "stream_unsubscribe", {"reason": reason})
        if touched:
            await self._notify_books(touched)

    async def _close_stream(self, run: _StreamRun) -> None:
        stream, run.stream = run.stream, None
        if stream is None:
            return
        local = getattr(stream, "connection_generation", None)
        if isinstance(local, int) and not isinstance(local, bool):
            high = self._stream_gen_high.get(run.venue, 0)
            self._stream_gen_high[run.venue] = max(high, run.base + local)
        aclose = getattr(stream, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed close must not stop the collector
            _log.warning("closing %s stream failed: %s", run.venue, type(exc).__name__)

    async def _stream_loop(self, run: _StreamRun) -> None:
        """Consume one venue's stream; restart it with backoff when it ends or fails."""
        venue = run.venue
        factory = self._modes[venue].factory
        assert factory is not None
        attempt = 0
        while not self._stopping and self._runs.get(venue) is run:
            run.base = self._stream_gen_high.get(venue, 0)
            reason = STREAM_ENDED
            try:
                run.stream = factory(run.market_ids)
                async for event in run.stream:
                    if self._stopping or self._runs.get(venue) is not run:
                        return
                    await self._apply_stream_event(run, event)
                    if event.kind == "snapshot":
                        attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - restart the stream, keep the collector
                reason = STREAM_ERROR
                self._on_crash(f"stream:{venue}", exc)
            finally:
                await self._close_stream(run)
            if self._stopping or self._runs.get(venue) is not run:
                return
            await self._stream_lost(run, reason)
            await self._clock.sleep(self._backoff.delay(attempt))
            attempt += 1
            health = self._health.get(venue)
            if health is not None:
                health.restart(self._clock.monotonic())

    async def _stream_lost(self, run: _StreamRun, reason: str) -> None:
        """The stream object ended or raised: treat it as a disconnect of every market."""
        self._emit(str(run.venue), reason, {"markets": len(run.market_ids)})
        for market_id in run.market_ids:
            event = BookEvent("invalidate", None, market_id, run.venue, reason)
            await self._apply_stream_event(run, event)

    async def _apply_stream_event(self, run: _StreamRun, event: BookEvent) -> None:
        venue = run.venue
        health = self._health[venue]
        if event.venue is not venue:
            _log.warning("dropped %s event from the %s stream", event.venue, venue)
            return
        now_mono = self._clock.monotonic()
        now_wall = self._clock.wall()
        key = market_key(venue, event.market_id)
        touched: set[str] = set()
        invalidated: set[str] = set()
        reason = event.reason or "stream_invalidate"
        disconnect = False
        if event.kind == "snapshot":
            book = event.book
            if book is None or book.market_id != event.market_id or book.venue is not venue:
                _log.warning("dropped a malformed %s stream snapshot", venue)
                return
            local = book.connection_generation
            if local < 1:
                _log.warning("dropped a %s stream snapshot without a connection", venue)
                return
            generation = run.base + local
            current = self._current_stream_generation(venue)
            if current is not None and generation != current:
                # Only the connection delivering right now may update a book.
                _log.warning(
                    "dropped a %s stream snapshot from generation %s (current %s)",
                    venue,
                    generation,
                    current,
                )
                return
            high = self._stream_gen_high.get(venue, 0)
            self._stream_gen_high[venue] = max(high, generation)
            self.store.advance_generation(venue, generation, "stream")
            health.on_snapshot(now_mono)
            run.gap_reported = False
            if not health.fallback:
                previous = self.store.get(key)
                stamped = book.model_copy(update={"connection_generation": generation})
                if self._accept_stream_book(stamped, now_mono, now_wall):
                    touched |= self._index.get(key, set())
                    was_valid = (
                        previous is not None
                        and previous.invalid_reason is None
                        and previous.book.valid
                    )
                    if was_valid and not stamped.valid:
                        # A snapshot that arrives invalid (crossed, suspended)
                        # ends the run like an invalidate event does.
                        invalidated.add(key)
                        reason = stamped.invalid_reason or "invalid_book"
        elif event.kind == "invalidate":
            kind = health.on_invalidate(now_mono, reason)
            disconnect = kind == "disconnect"
            self._enqueue_raw(_stream_conn_envelope(venue, event, run, now_mono, now_wall))
            if not health.fallback:
                state = self.store.get(key)
                if state is not None and state.meta.source == "stream":
                    if state.invalid_reason is None:
                        invalidated.add(key)
                    self.store.invalidate(key, reason)
                    touched |= self._index.get(key, set())
        else:
            _log.warning("dropped a %s stream event of kind %r", venue, event.kind)
            return
        transition = health.check(now_mono)
        if transition is not None:
            touched |= await self._apply_transition(venue, transition)
        if invalidated:
            await self._notify_invalidated(invalidated, reason)
        if (
            disconnect
            and not health.fallback
            and not run.gap_reported
            and not self._has_valid_book(venue, run.market_ids)
        ):
            run.gap_reported = True
            self._emit(str(venue), STREAM_DISCONNECT, {"reason": reason})
            await self._notify_gap(STREAM_DISCONNECT)
        if touched:
            await self._notify_books(touched)

    def _accept_stream_book(self, book: Book, now_mono: float, now_wall: datetime) -> bool:
        self._enqueue_raw(_stream_envelope(book, now_mono, now_wall))
        meta = BookMeta(
            received_mono=now_mono,
            received_wall=now_wall,
            connection_generation=book.connection_generation,
            source="stream",
            cached=book_is_cached(book),
            books_atomic_group_id=None,
            discovery_sample=self._market_is_discovery(book.venue, book.market_id),
        )
        if self.store.put(book, meta):
            return True
        _log.warning(
            "rejected %s stream book %s from generation %s (current %s)",
            book.venue,
            book.market_id,
            book.connection_generation,
            self.store.generation(book.venue, "stream"),
        )
        return False

    def _has_valid_book(self, venue: Venue, ids: Sequence[str]) -> bool:
        for market_id in ids:
            state = self.store.get(market_key(venue, market_id))
            if state is not None and state.invalid_reason is None and state.book.valid:
                return True
        return False

    async def _apply_transition(self, venue: Venue, transition: Transition) -> set[str]:
        detail = dict(transition.detail)
        detail["generation"] = self._current_stream_generation(venue)
        self._emit(str(venue), transition.kind, detail)
        if transition.kind != STREAM_FALLBACK:
            _log.info("%s stream healthy again; leaving REST fallback", venue)
            return set()
        _log.warning("%s stream unhealthy (%s); falling back to REST", venue, transition.reason)
        return await self._invalidate_stream_books(venue, STREAM_UNHEALTHY)

    async def _invalidate_stream_books(self, venue: Venue, reason: str) -> set[str]:
        """Invalidate the venue's stream books; close their episodes; return their groups."""
        keys = {
            state.market_key
            for state in self.store.items()
            if state.book.venue is venue
            and state.meta.source == "stream"
            and state.invalid_reason is None
        }
        self.store.invalidate_venue(venue, reason, source="stream")
        if keys:
            await self._notify_invalidated(keys, reason)
        touched: set[str] = set()
        for key in keys:
            touched |= self._index.get(key, set())
        return touched

    async def _check_stream_health(self, now_mono: float) -> set[str]:
        touched: set[str] = set()
        for venue in _VENUES:
            health = self._health.get(venue)
            if health is None or venue not in self._runs:
                continue
            transition = health.check(now_mono)
            if transition is not None:
                touched |= await self._apply_transition(venue, transition)
        return touched

    async def _notify_invalidated(self, keys: set[str], reason: str) -> None:
        if self._on_invalidated is not None:
            await _call(self._on_invalidated, set(keys), reason)

    def _signal_stop(self) -> None:
        if self._signal_task is not None and not self._signal_task.done():
            return
        self._signal_task = asyncio.create_task(self.stop(), name="collector-signal-stop")

    def _install_signals(self) -> None:
        if not self._install_signals_flag:
            return
        loop = asyncio.get_running_loop()
        for sig in _SIGNALS:
            try:
                loop.add_signal_handler(sig, self._signal_stop)
            except (NotImplementedError, RuntimeError):
                return
            self._signals.append(sig)

    def _remove_signals(self) -> None:
        if not self._signals:
            return
        loop = asyncio.get_running_loop()
        for sig in self._signals:
            loop.remove_signal_handler(sig)
        self._signals.clear()


def _depth(queue: asyncio.Queue[Any] | None) -> int:
    if queue is None:
        return 0
    return queue.qsize()


def _envelope(book: Book, now_mono: float, now_wall: datetime) -> Envelope:
    raw = book.raw_native if isinstance(book.raw_native, dict) else None
    headers: dict[str, Any] = {}
    body: Any = None
    if raw is not None:
        body = {key: value for key, value in raw.items() if key != "cache_headers"}
        raw_headers = raw.get("cache_headers")
        if isinstance(raw_headers, dict):
            headers = {str(key): value for key, value in raw_headers.items()}
    return Envelope(
        recv_utc=now_wall.astimezone(UTC).isoformat(),
        recv_mono_ns=round(now_mono * 1_000_000_000),
        venue=str(book.venue),
        kind="rest_book",
        conn_gen=book.connection_generation,
        endpoint=_endpoint(book),
        status=200,
        cache_headers=headers,
        body=body,
    )


def _stream_envelope(book: Book, now_mono: float, now_wall: datetime) -> Envelope:
    return Envelope(
        recv_utc=now_wall.astimezone(UTC).isoformat(),
        recv_mono_ns=round(now_mono * 1_000_000_000),
        venue=str(book.venue),
        kind="ws_msg",
        conn_gen=book.connection_generation,
        endpoint=_WS_PATHS.get(book.venue, "ws"),
        status=None,
        body=book.raw_native if isinstance(book.raw_native, dict) else None,
    )


def _stream_conn_envelope(
    venue: Venue, event: BookEvent, run: _StreamRun, now_mono: float, now_wall: datetime
) -> Envelope:
    local = getattr(run.stream, "connection_generation", None)
    generation = run.base + local if isinstance(local, int) and not isinstance(local, bool) else 0
    return Envelope(
        recv_utc=now_wall.astimezone(UTC).isoformat(),
        recv_mono_ns=round(now_mono * 1_000_000_000),
        venue=str(venue),
        kind="conn",
        conn_gen=generation,
        endpoint=_WS_PATHS.get(venue, "ws"),
        status=None,
        body={"event": event.kind, "market_id": event.market_id, "reason": event.reason},
    )


def _endpoint(book: Book) -> str:
    if book.venue is Venue.KALSHI:
        return "/markets/orderbooks"
    return f"/v1/markets/{quote(book.market_id, safe='')}/book"


def _event_op(
    utc: datetime, venue: str | None, kind: str, detail: dict[str, Any]
) -> Callable[[sqlite3.Connection], None]:
    def apply(conn: sqlite3.Connection) -> None:
        insert_collector_event(conn, utc, venue, kind, detail)

    return apply


async def _call(fn: Callable[..., Any], /, *args: Any) -> None:
    result = fn(*args)
    if inspect.isawaitable(result):
        await result
