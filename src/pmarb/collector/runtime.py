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

Book polls are bounded and isolated per venue. Each venue's fetch gets a time
budget below the batch's freshness window (``poll_fetch_budget_fraction``).
The HTTP client stops retrying at that deadline, and a clock-based backstop
cancels an adapter that overruns it. Each venue's books are accepted, and
stamped, as soon as that venue's fetch returns, so a slow or failing venue
neither delays nor ages the other venue's books. Cycles run at a fixed rate:
the next one starts ``sleep_s`` after the previous one started, not after it
ended. Failures are counted by cause (``poll_failures_by_cause`` in health).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
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
from pmarb.collector.scheduler import PollPlan, ScheduledGroup, build_poll_plan, market_ids
from pmarb.collector.supervisor import RestartBackoff, supervise
from pmarb.config import Settings
from pmarb.http import Backoff, Deadline, failure_cause, failure_detail, request_deadline
from pmarb.models import Book, EquivalenceGroup, GroupStatus, Market, Venue
from pmarb.osutil import is_windows
from pmarb.protocols import PartialBooksError, VenueAdapter
from pmarb.storage.db import DbWriterTask, Write, insert_collector_event
from pmarb.storage.rawlog import Envelope, RawLogTask, disk_free_bytes
from pmarb.venues.polymarket_us.maintenance import in_maintenance

_log = logging.getLogger(__name__)

FRESHNESS_INTERVAL_S = 0.25
HEALTH_INTERVAL_S = 10.0
DISK_FREE_MIN_BYTES = 2 * 1024**3
DEFAULT_BACKPRESSURE_HIGH_WATER = 20_000
DEFAULT_BACKPRESSURE_LOW_WATER = 10_000
DEFAULT_QUEUE_LIMIT = DEFAULT_BACKPRESSURE_HIGH_WATER
BACKPRESSURE_SUSTAINED_S = 5.0

# The HTTP client stops at the fetch deadline itself. The backstop only fires
# for an adapter that does not honour it, a little later so the client's richer
# error wins the race.
FETCH_BACKSTOP_GRACE_S = 0.25
# A repeated failure with the same cause is logged at WARNING once per this
# many polls (every poll is still a collector event).
ERROR_LOG_REPEAT_EVERY = 10

RECORDING_BACKPRESSURE = "recording_backpressure"
ADAPTER_ERROR = "adapter_error"
ADAPTER_RECOVERED = "adapter_recovered"
COLLECTOR_SHUTDOWN = "collector_shutdown"
LOOP_CRASH = "loop_crash"
HEALTH = "health"
STOP_REQUESTED = "stop_requested"
STOP_REQUEST_STALE = "stop_request_stale"
STOP_REQUEST_INTERVAL_S = 1.0
# A request written this soon before the collector was created still counts.
# It covers coarse file timestamps (FAT keeps 2 s) and `pmarb stop` racing a start.
STOP_REQUEST_GRACE_S = 2.0

# Process-wide invalidations. A later freshness pass must not relabel them.
_PROCESS_REASONS = frozenset({CLOCK_JUMP, RECORDING_BACKPRESSURE, GENERATION, ADAPTER_ERROR})

BooksHook = Callable[[set[str]], Awaitable[None] | None]
GapHook = Callable[[str], Awaitable[None] | None]
StartupHook = Callable[[], Awaitable[None] | None]
_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# Windows has no loop.add_signal_handler. Console Ctrl+C is SIGINT and
# Ctrl+Break is SIGBREAK, which exists only in the Windows signal module.
_WIN32_SIGNAL_NAMES = ("SIGINT", "SIGBREAK")


def _once_sample_key(start: datetime | None, now: datetime) -> tuple[int, float]:
    """Prefer upcoming samples, then the most recent past start, unknown last."""
    if start is None:
        return (2, 0.0)
    seconds = (start - now).total_seconds()
    if seconds >= 0:
        return (0, seconds)
    return (1, -seconds)


class FetchBudgetExceededError(Exception):
    """An adapter's ``get_books`` was still running after its poll budget."""

    def __init__(self, venue: Venue, budget_s: float, requested: int) -> None:
        super().__init__(
            f"{venue} get_books for {requested} markets did not return within its "
            f"{budget_s:.2f}s poll budget"
        )
        self.venue = venue
        self.budget_s = budget_s
        self.requested = requested
        self.cause = "budget_exceeded"


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

    Three things trigger ``stop`` from outside:

    - SIGINT or SIGTERM through ``loop.add_signal_handler`` on POSIX;
    - SIGINT or SIGBREAK through ``signal.signal`` on Windows, which hands the
      stop to the loop with ``call_soon_threadsafe``;
    - a fresh ``stop_file`` (see :mod:`pmarb.collector.stopfile`), checked once
      a second on every platform.
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
        stop_file: Path | None = None,
        stop_poll_interval_s: float = STOP_REQUEST_INTERVAL_S,
    ) -> None:
        if stop_poll_interval_s <= 0:
            raise ValueError("stop_poll_interval_s must be > 0")
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
        self._stop_file = stop_file
        self._stop_poll_interval_s = stop_poll_interval_s
        # Real wall time, because file mtimes are real. A request older than
        # this collector was left behind by an earlier run and is stale.
        self._created_epoch = time.time()
        self._stop_file_seen = False
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
        # venue -> failure cause (http_503, ConnectTimeout, cooldown, ...) -> count
        self.poll_failures: dict[str, dict[str, int]] = {}
        self._failure_streak: dict[str, int] = {}
        self._streak_cause: dict[str, str] = {}
        self._loop_lag_max_s = 0.0
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
        self._previous_handlers: dict[int, Any] = {}
        self._signal_task: asyncio.Task[None] | None = None
        self._stop_file_task: asyncio.Task[None] | None = None
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
        started = self._clock.monotonic()
        try:
            await self._run_startup()
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
                        # The writers have drained; deleting the request tells
                        # `pmarb stop --wait` that shutdown is complete.
                        self._acknowledge_stop_request()
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
        if self._stop_file is not None:
            self._stop_file_task = asyncio.create_task(
                supervise("stop_request", self._stop_request_loop, **common),
                name="collector-stop-request",
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
            started = self._clock.monotonic()
            await self._poll_once()
            if self._stopping:
                return
            # Fixed rate: a cycle's own duration comes out of the sleep, so the
            # period is the cadence, not the cadence plus the fetch time.
            elapsed = max(0.0, self._clock.monotonic() - started)
            await self._clock.sleep(max(0.0, self.sleep_s - elapsed))

    async def _freshness_loop(self) -> None:
        while not self._stopping:
            before = self._clock.monotonic()
            await self._clock.sleep(FRESHNESS_INTERVAL_S)
            if self._stopping:
                return
            # How late the tick woke: time the event loop spent on other work.
            lag = self._clock.monotonic() - before - FRESHNESS_INTERVAL_S
            self._loop_lag_max_s = max(self._loop_lag_max_s, lag)
            await self._freshness_tick()

    async def _health_loop(self) -> None:
        while not self._stopping:
            await self._clock.sleep(HEALTH_INTERVAL_S)
            if self._stopping:
                return
            self._health_tick()

    async def _stop_request_loop(self) -> None:
        """Check the stop-request file now and then once per interval."""
        while not self._stopping:
            if self._check_stop_request():
                return
            await self._clock.sleep(self._stop_poll_interval_s)

    def _check_stop_request(self) -> bool:
        """Start a clean stop for a fresh request. Delete a stale one."""
        path = self._stop_file
        if path is None:
            return False
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return False
        except OSError as exc:
            _log.warning("could not check stop request %s: %s", path, type(exc).__name__)
            return False
        if mtime < self._created_epoch - STOP_REQUEST_GRACE_S:
            _log.warning("ignoring stale stop request %s from before this collector started", path)
            self._emit(None, STOP_REQUEST_STALE, {"path": str(path)})
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("could not remove stale stop request: %s", type(exc).__name__)
            return False
        _log.info("stop requested through %s", path)
        self._stop_file_seen = True
        self._emit(None, STOP_REQUESTED, {"source": "stop_file"})
        self._signal_stop()
        return True

    def _acknowledge_stop_request(self) -> None:
        if not self._stop_file_seen or self._stop_file is None:
            return
        try:
            self._stop_file.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("could not remove stop request after shutdown: %s", type(exc).__name__)

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
        hot = tuple(item for item in selected if item.group_id in self._polled_ids)
        cold = tuple(item for item in selected if item.group_id in self._discovery_ids)
        batches = [batch for batch in (hot, cold) if batch]
        if not batches and selected:
            batches = [tuple(selected)]
        touched: set[str] = set()
        for batch in batches:
            kalshi_ids = market_ids(batch, Venue.KALSHI)
            poly_ids = market_ids(batch, Venue.POLYMARKET_US)
            budget_s = self.fetch_budget_s(batch)
            # Both venues start together (low skew). Each accepts its own books
            # when its fetch returns, and each is bounded by ``budget_s``.
            kalshi_result, poly_result = await asyncio.gather(
                self._timed_fetch(self._kalshi, kalshi_ids, budget_s),
                self._timed_fetch(self._polyus, poly_ids, budget_s),
            )
            for venue, ids, (returned, duration) in (
                (Venue.KALSHI, kalshi_ids, kalshi_result),
                (Venue.POLYMARKET_US, poly_ids, poly_result),
            ):
                self._timing(
                    "poll_fetch",
                    duration,
                    venue=str(venue),
                    requested=len(ids),
                    returned=returned,
                    budget_s=budget_s,
                )
            checked_mono = self._clock.monotonic()
            checked_wall = self._clock.wall()
            await self._sync_backpressure()
            if self._backpressure:
                self.store.invalidate(None, RECORDING_BACKPRESSURE)
            self._apply_book_freshness(checked_mono, checked_wall)
            await self._sync_maintenance(checked_wall)
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
        await self._sync_maintenance(now_wall)
        changed |= self._apply_book_freshness(now_mono, now_wall)
        await self._notify_books(set(self._polled_ids) | changed)

    def _health_tick(self) -> None:
        self._check_disk()
        alarm = "disk_low" if self._disk_low else None
        self._emit(None, HEALTH, self._health_detail(alarm=alarm))
        self._loop_lag_max_s = 0.0

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

    def fetch_budget_s(self, batch: Sequence[ScheduledGroup]) -> float:
        """Time one venue's fetch of ``batch`` may take, retries included.

        The batch's own cadence is ``max(min cadence, n_poly / polyus rps)``,
        which also covers the books bucket's pacing of its Polymarket US calls.
        The budget is ``poll_fetch_budget_fraction`` of that cadence's freshness
        window, capped at ``poll_fetch_budget_max_s``.
        """
        settings = self._settings
        n_poly = len(market_ids(batch, Venue.POLYMARKET_US))
        cadence = max(settings.min_poll_cadence_s, n_poly / settings.polyus_books_rps)
        window = max_age_for(
            "poll",
            cadence,
            stream_max_age_s=settings.stream_max_age_s,
            poll_multiple=settings.poll_max_age_cadence_multiple,
        )
        return min(settings.poll_fetch_budget_fraction * window, settings.poll_fetch_budget_max_s)

    async def _fetch(
        self, adapter: VenueAdapter, ids: Sequence[str], budget_s: float
    ) -> list[Book]:
        if not ids:
            return []
        started = self._clock.monotonic()
        deadline = Deadline(started + budget_s, self._clock.monotonic)
        try:
            # The task copies this context, so every request it makes sees the deadline.
            with request_deadline(deadline, budget_s=budget_s):
                task = asyncio.ensure_future(adapter.get_books(list(ids)))
            books = await self._within(task, budget_s, adapter.venue, len(ids))
        except asyncio.CancelledError:
            raise
        except PartialBooksError as exc:
            self._record_adapter_error(adapter.venue, exc, ids, started, budget_s)
            books = exc.books
        except Exception as exc:  # noqa: BLE001 - any adapter failure invalidates that venue
            self._record_adapter_error(adapter.venue, exc, ids, started, budget_s)
            return []
        else:
            if not isinstance(books, list):
                self._record_adapter_error(
                    adapter.venue, TypeError("get_books must return a list"), ids, started, budget_s
                )
                return []
            self._record_success(adapter.venue)
        return books

    async def _within(
        self, task: asyncio.Future[list[Book]], budget_s: float, venue: Venue, requested: int
    ) -> list[Book]:
        """Await ``task``; cancel it if it outlives the budget (on the collector clock)."""
        timer = asyncio.ensure_future(self._clock.sleep(budget_s + FETCH_BACKSTOP_GRACE_S))
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            timer.cancel()
            await asyncio.gather(task, timer, return_exceptions=True)
            raise
        if task.done():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise FetchBudgetExceededError(venue, budget_s, requested)

    async def _timed_fetch(
        self, adapter: VenueAdapter, ids: Sequence[str], budget_s: float
    ) -> tuple[int, float]:
        """Fetch, then accept the books at once, stamped when this venue's fetch returned."""
        started = self._clock.monotonic()
        books = await self._fetch(adapter, ids, budget_s)
        accepted_mono = self._clock.monotonic()
        accepted_wall = self._clock.wall()
        for book in books:
            self._accept_book(book, accepted_mono, accepted_wall)
        return len(books), max(0.0, accepted_mono - started)

    def _record_adapter_error(
        self,
        venue: Venue,
        exc: Exception,
        requested: Sequence[str] = (),
        started_mono: float | None = None,
        budget_s: float | None = None,
    ) -> None:
        """Count, invalidate, log, and record one failed venue fetch.

        Only the markets that failed are invalidated: books that did arrive (a
        partial result) and other batches' books stay as they are.
        """
        self.error_count += 1
        if isinstance(exc, PartialBooksError):
            errors: list[BaseException] = exc.errors or [exc]
            failed_ids = list(exc.failures)
        else:
            errors = [exc]
            failed_ids = list(requested)
        primary = errors[0]
        causes: dict[str, int] = {}
        for err in errors:
            cause = failure_cause(err)
            causes[cause] = causes.get(cause, 0) + 1
        totals = self.poll_failures.setdefault(str(venue), {})
        for cause, count in causes.items():
            totals[cause] = totals.get(cause, 0) + count
        if "http_429" in causes or any("HTTP 429" in str(err) for err in errors):
            self.http_429_count += 1
        if failed_ids:
            for market_id in failed_ids:
                self.store.invalidate(market_key(venue, market_id), ADAPTER_ERROR)
        else:
            self.store.invalidate_venue(venue, ADAPTER_ERROR)

        elapsed = None if started_mono is None else self._clock.monotonic() - started_mono
        detail = failure_detail(primary)
        detail.update(
            {
                "causes": causes,
                "requested": len(requested),
                "failed": len(failed_ids),
                "fetch_elapsed_s": None if elapsed is None else round(elapsed, 4),
                "fetch_budget_s": budget_s,
            }
        )
        if isinstance(exc, PartialBooksError):
            detail["partial"] = True
            detail["summary"] = str(exc)[:500]
        key = str(venue)
        streak = self._failure_streak.get(key, 0) + 1
        self._failure_streak[key] = streak
        label = ",".join(sorted(causes))
        changed = self._streak_cause.get(key) != label
        self._streak_cause[key] = label
        detail["streak"] = streak
        loud = streak == 1 or changed or streak % ERROR_LOG_REPEAT_EVERY == 0
        _log.log(
            logging.WARNING if loud else logging.DEBUG,
            "book poll failed for %s [cause=%s failed=%d/%d streak=%d elapsed=%s budget=%s]: %s: %s",
            venue,
            label,
            len(failed_ids),
            len(requested),
            streak,
            "?" if elapsed is None else f"{elapsed:.2f}s",
            "?" if budget_s is None else f"{budget_s:.2f}s",
            type(primary).__name__,
            primary,
        )
        self._emit(key, ADAPTER_ERROR, detail)

    def _record_success(self, venue: Venue) -> None:
        key = str(venue)
        streak = self._failure_streak.pop(key, 0)
        last = self._streak_cause.pop(key, None)
        if streak:
            _log.info("book poll recovered for %s after %d failed polls (%s)", venue, streak, last)
            self._emit(key, ADAPTER_RECOVERED, {"failed_polls": streak, "last_causes": last})

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
            "poll_failures_by_cause": {
                venue: dict(causes) for venue, causes in sorted(self.poll_failures.items())
            },
            "poll_failure_streak": dict(self._failure_streak),
            "http": self._http_metrics(),
            "loop_lag_max_s": round(self._loop_lag_max_s, 4),
            "disk_free_bytes": self._disk_free_bytes,
            "raw_logging": self._raw_logging,
            "backpressure": self._backpressure,
            "cadence_s": self.cadence_s,
            "alarm": alarm,
        }

    def _http_metrics(self) -> dict[str, Any]:
        """Per-bucket request counters from the adapters' HTTP clients, if they expose them."""
        out: dict[str, Any] = {}
        seen: set[int] = set()
        for adapter in (self._kalshi, self._polyus):
            http = getattr(adapter, "http", None)
            metrics = getattr(http, "metrics", None)
            if http is None or id(http) in seen or not callable(metrics):
                continue
            seen.add(id(http))
            snapshot = metrics()
            if isinstance(snapshot, dict):
                out.update(snapshot)
        return out

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
        return [
            task
            for task in (
                self._poll_task,
                self._fresh_task,
                self._health_task,
                self._discovery_task,
                self._stop_file_task,
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
        if is_windows():
            self._install_win32_signals(loop)
            return
        for sig in _SIGNALS:
            try:
                loop.add_signal_handler(sig, self._signal_stop)
            except (NotImplementedError, RuntimeError):
                return
            self._signals.append(sig)

    def _install_win32_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        """Install ``signal.signal`` handlers that hand the stop to ``loop``.

        Python runs signal handlers in the main thread between bytecodes, so
        the handler only schedules :meth:`_signal_stop` with
        ``call_soon_threadsafe``, which also wakes a loop that is blocked in
        ``select``. The handlers this replaces are restored on exit.
        """
        if threading.current_thread() is not threading.main_thread():
            return  # signal.signal only works in the main thread

        def handler(signum: int, frame: FrameType | None) -> None:
            del frame
            try:
                loop.call_soon_threadsafe(self._signal_stop)
            except RuntimeError:  # the loop already closed
                return

        for name in _WIN32_SIGNAL_NAMES:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                previous = signal.signal(sig, handler)
            except (OSError, RuntimeError, ValueError):
                continue
            self._previous_handlers[int(sig)] = previous
            self._signals.append(sig)

    def _remove_signals(self) -> None:
        if not self._signals:
            return
        if self._previous_handlers:
            for sig in self._signals:
                previous = self._previous_handlers.pop(int(sig), None)
                try:
                    signal.signal(sig, previous if previous is not None else signal.SIG_DFL)
                except (OSError, RuntimeError, ValueError):
                    _log.warning("could not restore the handler for signal %s", int(sig))
            self._signals.clear()
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
