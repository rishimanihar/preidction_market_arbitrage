"""Poll approved groups and keep a fresh book store.

The evaluator, discovery loop, and episode tracker are not imported here.
PR-6b attaches them through ``on_books_updated``, ``on_gap``, and an optional
``discovery`` loop. ``run_once`` is one catalog pass and one poll.

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
``maintenance`` (once, on entering the window), and ``collector_shutdown``.
Book invalid reasons also include ``stale``, ``cached``, ``adapter_error``,
and ``generation``. A heartbeat does not refresh book age.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from pmarb.collector.scheduler import PollPlan, build_poll_plan
from pmarb.collector.supervisor import RestartBackoff, supervise
from pmarb.config import Settings
from pmarb.http import Backoff
from pmarb.models import Book, EquivalenceGroup, GroupStatus, Market, Venue
from pmarb.protocols import VenueAdapter
from pmarb.storage.db import DbWriterTask, Write, insert_collector_event
from pmarb.storage.rawlog import Envelope, RawLogTask, disk_free_bytes
from pmarb.venues.polymarket_us.maintenance import in_maintenance

_log = logging.getLogger(__name__)

FRESHNESS_INTERVAL_S = 0.25
HEALTH_INTERVAL_S = 10.0
DISK_FREE_MIN_BYTES = 2 * 1024**3
DEFAULT_QUEUE_LIMIT = 1000

RECORDING_BACKPRESSURE = "recording_backpressure"
ADAPTER_ERROR = "adapter_error"
COLLECTOR_SHUTDOWN = "collector_shutdown"
LOOP_CRASH = "loop_crash"
HEALTH = "health"

# Process-wide invalidations. A later freshness pass must not relabel them.
_PROCESS_REASONS = frozenset({CLOCK_JUMP, RECORDING_BACKPRESSURE, GENERATION, ADAPTER_ERROR})

BooksHook = Callable[[set[str]], Awaitable[None] | None]
GapHook = Callable[[str], Awaitable[None] | None]
StartupHook = Callable[[], Awaitable[None] | None]
_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class DiscoveryLoop(Protocol):
    """Catalog refresh. The runtime does not import the discovery module."""

    async def run(self) -> None:
        """Refresh until stopped."""

    async def run_once(self) -> None:
        """One catalog pass."""


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
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        backoff: RestartBackoff | None = None,
        groups: Sequence[EquivalenceGroup] = (),
        markets: Sequence[Market] = (),
        install_signals: bool = True,
        discovery: DiscoveryLoop | None = None,
        on_startup: StartupHook | None = None,
        on_stopped: StartupHook | None = None,
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
        self._queue_limit = queue_limit
        self._backoff = backoff if backoff is not None else Backoff()
        self._install_signals_flag = install_signals
        self._discovery = discovery
        self._on_startup = on_startup
        self._on_stopped = on_stopped
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
        try:
            await self._run_startup()
            self._start_writers()
            if self._db_task is not None:
                await self._wait_db()
            if not self._stopping and self._discovery is not None:
                await self._discovery.run_once()
            if not self._stopping:
                await self._poll_once()
        finally:
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
                try:
                    await self._notify_gap(COLLECTOR_SHUTDOWN)
                finally:
                    self._emit(None, COLLECTOR_SHUTDOWN, {})
            finally:
                try:
                    await self._cancel_and_wait(self._writer_tasks())
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

    async def _poll_once(self) -> None:
        if self._stopping:
            return
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
        kalshi_ids = plan.ids_for(Venue.KALSHI, include_discovery=include_discovery)
        poly_ids = plan.ids_for(Venue.POLYMARKET_US, include_discovery=include_discovery)
        kalshi_books, poly_books = await asyncio.gather(
            self._fetch(self._kalshi, kalshi_ids),
            self._fetch(self._polyus, poly_ids),
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
        self._set_sleep(plan)
        touched = self._groups_for_ids(
            ((Venue.KALSHI, kalshi_ids), (Venue.POLYMARKET_US, poly_ids))
        )
        await self._notify_books(touched)
        self.cycles += 1

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
        if not self._queues_over_limit():
            self._backpressure = False
            return
        self.store.invalidate(None, RECORDING_BACKPRESSURE)
        if self._backpressure:
            return
        self._backpressure = True
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

    def _queues_over_limit(self) -> bool:
        raw_over = self._raw_queue is not None and self._raw_queue.qsize() > self._queue_limit
        db_over = self._db_queue is not None and self._db_queue.qsize() > self._queue_limit
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
        await _call(self._on_books_updated, set(group_ids))

    async def _notify_gap(self, reason: str) -> None:
        await _call(self._on_gap, reason)

    def _service_tasks(self) -> list[asyncio.Task[None]]:
        return [
            task
            for task in (
                self._poll_task,
                self._fresh_task,
                self._health_task,
                self._discovery_task,
            )
            if task is not None
        ]

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
