"""Poller, freshness, and shutdown. Clocks are injected; nothing sleeps for real."""

from __future__ import annotations

import asyncio
import os
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from pmarb.collector import (
    LIVE_WINDOW,
    BookMeta,
    BookStore,
    ClockJumpGuard,
    Collector,
    FakeClock,
    SystemClock,
    build_poll_plan,
    is_fresh,
    max_age_for,
    supervise,
)
from pmarb.collector.books import BookState, market_key
from pmarb.config import Settings
from pmarb.http import Backoff, RetriesExhaustedError
from pmarb.models import Book, EquivalenceGroup, GroupMember, GroupStatus, Market, Side, Venue
from pmarb.storage.db import DbWriterTask, open_reader
from pmarb.storage.rawlog import Envelope, RawLogTask, RawLogWriter, iter_records, list_log_files

# Wednesday 18:00 UTC, outside the Thursday Polymarket maintenance window.
T0 = datetime(2026, 6, 3, 18, tzinfo=UTC)
PLENTY = 10 * 1024**3

# Fixed instants from tests/test_polyus_maintenance.py.
EST_INSIDE = datetime(2026, 3, 5, 11, tzinfo=UTC)
EST_END = datetime(2026, 3, 5, 13, tzinfo=UTC)
EDT_INSIDE = datetime(2026, 3, 12, 10, tzinfo=UTC)
BEFORE_FALLBACK = datetime(2026, 10, 29, 10, 30, tzinfo=UTC)
AFTER_FALLBACK = datetime(2026, 11, 5, 11, 30, tzinfo=UTC)

_ENV_KEYS = {
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "POLYUS_API_KEY_ID",
    "POLYUS_PRIVATE_KEY_PATH",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("PMARB_") or name in _ENV_KEYS:
            monkeypatch.delenv(name, raising=False)


def make_market(venue: Venue, market_id: str, start: datetime | None) -> Market:
    return Market(
        venue=venue,
        market_id=market_id,
        title=market_id,
        market_type="moneyline",
        event_start_utc=start,
        status="open",
        rules_text="rules",
        rules_hash="h",
    )


def make_group(
    gid: str,
    kalshi_id: str,
    poly_id: str,
    *,
    status: GroupStatus = GroupStatus.APPROVED,
) -> EquivalenceGroup:
    approved = status is GroupStatus.APPROVED
    return EquivalenceGroup(
        group_id=gid,
        outcome=f"{gid} wins",
        members=(
            GroupMember(
                venue=Venue.KALSHI,
                market_id=kalshi_id,
                native_side_for_outcome=Side.YES,
                approved_rules_hash="hk" if approved else None,
            ),
            GroupMember(
                venue=Venue.POLYMARKET_US,
                market_id=poly_id,
                native_side_for_outcome=Side.YES,
                approved_rules_hash="hp" if approved else None,
            ),
        ),
        status=status,
        reviewer="ada" if approved else None,
        decided_utc=T0 if approved else None,
    )


def make_book(
    venue: Venue,
    market_id: str,
    *,
    generation: int = 0,
    raw: dict | None = None,
    valid: bool = True,
    reason: str | None = None,
) -> Book:
    return Book(
        venue=venue,
        market_id=market_id,
        yes_asks=[],
        no_asks=[],
        raw_native=raw,
        received_utc=T0,
        received_monotonic=0.0,
        connection_generation=generation,
        valid=valid,
        invalid_reason=reason,
    )


def meta(generation: int, *, discovery: bool = False) -> BookMeta:
    return BookMeta(
        received_mono=0.0,
        received_wall=T0,
        connection_generation=generation,
        source="poll",
        cached=False,
        discovery_sample=discovery,
    )


def state_for(
    venue: Venue,
    wall: datetime,
    *,
    mono: float = 0.0,
    cached: bool = False,
    valid: bool = True,
    reason: str | None = None,
) -> BookState:
    stamped = BookMeta(
        received_mono=mono,
        received_wall=wall,
        connection_generation=0,
        source="poll",
        cached=cached,
    )
    return BookState(make_book(venue, "m", valid=valid, reason=reason), stamped)


class FakeVenue:
    """Records call order and yields once so the other venue can start."""

    def __init__(
        self,
        venue: Venue,
        log: list[tuple[str, str]],
        *,
        generation: int = 0,
        cache_headers: dict[str, str] | None = None,
    ) -> None:
        self.venue = venue
        self.log = log
        self.generation = generation
        self.cache_headers = cache_headers
        self.calls: list[list[str]] = []
        self.error: BaseException | None = None

    async def get_books(self, market_ids: list[str]) -> list[Book]:
        ids = list(market_ids)
        self.calls.append(ids)
        self.log.append((str(self.venue), "start"))
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        self.log.append((str(self.venue), "end"))
        books = []
        for market_id in ids:
            raw: dict = {"market": market_id}
            if self.cache_headers is not None:
                raw["cache_headers"] = dict(self.cache_headers)
            books.append(make_book(self.venue, market_id, generation=self.generation, raw=raw))
        return books


class BlockingVenue:
    def __init__(self, venue: Venue) -> None:
        self.venue = venue
        self.started = asyncio.Event()

    async def get_books(self, market_ids: list[str]) -> list[Book]:
        del market_ids
        self.started.set()
        await asyncio.Event().wait()
        return []


class Harness:
    def __init__(self, collector: Collector) -> None:
        self.collector = collector
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> asyncio.Task[None]:
        self.task = asyncio.create_task(self.collector.run())
        return self.task

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc, tb
        await self.collector.stop()
        task = self.task
        assert task is not None
        if not task.done():
            try:
                await task
            except asyncio.CancelledError:
                return False
            return False
        if task.cancelled():
            return False
        task_exc = task.exception()
        if task_exc is None or isinstance(task_exc, asyncio.CancelledError):
            return False
        if exc_type is None:
            raise task_exc
        return False


def pair(gid: str, kalshi_id: str, poly_id: str, start: datetime | None):
    group = make_group(gid, kalshi_id, poly_id)
    markets = [
        make_market(Venue.KALSHI, kalshi_id, start),
        make_market(Venue.POLYMARKET_US, poly_id, start),
    ]
    return group, markets


def build(clock: FakeClock, kalshi, poly, *, groups=(), markets=(), **kwargs):
    gaps: list[str] = []
    updates: list[set[str]] = []
    kwargs.setdefault("disk_free", lambda _path: PLENTY)
    kwargs.setdefault("install_signals", False)
    kwargs.setdefault("settings", Settings(_env_file=None))
    collector = Collector(
        kalshi=kalshi,
        polyus=poly,
        clock=clock,
        on_books_updated=updates.append,
        on_gap=gaps.append,
        groups=groups,
        markets=markets,
        **kwargs,
    )
    return collector, gaps, updates


async def until(pred, *, rounds: int = 500, detail: str = "") -> None:
    for _ in range(rounds):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError(detail or "condition not met")


async def park(collector: Collector, clock: FakeClock, cycles: int) -> None:
    await until(
        lambda: collector.cycles >= cycles and clock.pending >= 3,
        detail=f"cycles={collector.cycles} pending={clock.pending}",
    )


def assert_overlapped(log: list[tuple[str, str]]) -> None:
    starts = [i for i, item in enumerate(log) if item[1] == "start"]
    ends = [i for i, item in enumerate(log) if item[1] == "end"]
    assert len(starts) == 2 and len(ends) == 2
    assert max(starts) < min(ends)
    assert {item[0] for item in log if item[1] == "start"} == {"kalshi", "polymarket_us"}


def seed(collector: Collector, clock: FakeClock, venue: Venue, market_id: str) -> None:
    stamped = BookMeta(
        received_mono=clock.monotonic(),
        received_wall=clock.wall(),
        connection_generation=collector.store.generation(venue),
        source="poll",
        cached=False,
    )
    assert collector.store.put(
        make_book(venue, market_id, generation=stamped.connection_generation), stamped
    )


def dummy_env(i: int) -> Envelope:
    return Envelope(
        recv_utc="2026-06-03T18:00:00+00:00",
        recv_mono_ns=i,
        venue="kalshi",
        kind="rest_book",
        conn_gen=0,
        endpoint="/markets/orderbooks",
        status=200,
        body={"i": i},
    )


def plan_at(start: datetime):
    group, markets = pair("g", "KX", "pm", start)
    return build_poll_plan([group], markets, T0)


# --- clock -----------------------------------------------------------------


async def test_system_clock_wall_is_aware_utc() -> None:
    clock = SystemClock()
    wall = clock.wall()
    assert wall.tzinfo is not None and wall.utcoffset() is not None
    before = clock.monotonic()
    await clock.sleep(0)
    assert clock.monotonic() >= before


async def test_fake_clock_advances_mono_and_wall_independently() -> None:
    clock = FakeClock(mono=10, wall=T0)
    clock.advance(mono=5)
    assert clock.monotonic() == 15
    assert clock.wall() == T0
    clock.advance(wall=timedelta(seconds=3))
    assert clock.monotonic() == 15
    assert clock.wall() == T0 + timedelta(seconds=3)

    task = asyncio.create_task(clock.sleep(5))
    await asyncio.sleep(0)
    clock.advance(wall=timedelta(seconds=5))
    await asyncio.sleep(0)
    assert not task.done()
    clock.advance(mono=5)
    await task


# --- book store ------------------------------------------------------------


def test_generation_rejection_and_bump() -> None:
    store = BookStore()
    key = market_key(Venue.KALSHI, "KX")
    assert store.put(make_book(Venue.KALSHI, "KX", generation=0), meta(0))
    assert store.put(make_book(Venue.KALSHI, "KX", generation=0), meta(1)) is False
    assert store.bump_generation(Venue.KALSHI) == 1
    held = store.get(key)
    assert held is not None and held.invalid_reason == "generation"
    assert store.put(make_book(Venue.KALSHI, "KX", generation=0), meta(0)) is False
    assert held.book.connection_generation == 0
    assert store.put(make_book(Venue.KALSHI, "KX", generation=1), meta(1))
    current = store.get(key)
    assert current is not None and current.invalid_reason is None
    assert store.bump_generation(Venue.KALSHI) == 2
    bumped = store.get(key)
    assert bumped is not None and bumped.invalid_reason == "generation"
    assert store.put(make_book(Venue.KALSHI, "KX", generation=1), meta(1)) is False
    assert bumped.book.connection_generation == 1

    poly = market_key(Venue.POLYMARKET_US, "pm")
    assert store.put(make_book(Venue.POLYMARKET_US, "pm", generation=0), meta(0))
    store.invalidate(None, "recording_backpressure")
    assert store.get(key) is not None
    assert store.get(poly) is not None
    assert store.get(key).invalid_reason == "recording_backpressure"  # type: ignore[union-attr]
    assert store.get(poly).invalid_reason == "recording_backpressure"  # type: ignore[union-attr]
    store.invalidate(poly, "stale")
    assert store.get(poly).invalid_reason == "stale"  # type: ignore[union-attr]
    assert store.get(key).invalid_reason == "recording_backpressure"  # type: ignore[union-attr]


# --- freshness -------------------------------------------------------------


def test_dual_clock_freshness_including_wall_sleep() -> None:
    state = state_for(Venue.KALSHI, T0, mono=100.0)
    assert is_fresh(state, 102.0, T0 + timedelta(seconds=2), 4.0) == (True, None)
    # Monotonic time paused; the wall clock alone is past max age.
    slept = is_fresh(state, 100.0, T0 + timedelta(seconds=30), 4.0)
    assert slept == (False, "stale")
    assert is_fresh(state, 105.0, T0 + timedelta(seconds=1), 4.0) == (False, "stale")
    assert is_fresh(state, 104.0, T0 + timedelta(seconds=4), 4.0) == (True, None)
    just_over = is_fresh(state, 104.0, T0 + timedelta(seconds=4.01), 4.0)
    assert just_over == (False, "stale")


def test_cached_and_invalid_books_are_not_fresh() -> None:
    cached = state_for(Venue.KALSHI, T0, cached=True)
    assert is_fresh(cached, 0.0, T0, 10.0) == (False, "cached")
    crossed = state_for(Venue.KALSHI, T0, valid=False, reason="crossed_book")
    assert is_fresh(crossed, 0.0, T0, 10.0) == (False, "crossed_book")


def test_max_age_for_stream_and_poll() -> None:
    assert max_age_for("stream", 30.0) == 2.0
    assert max_age_for("poll", 1.5) == 3.0
    assert max_age_for("poll", 4.0, poll_multiple=2.0) == 8.0


def test_clock_jump_guard_threshold() -> None:
    guard = ClockJumpGuard(threshold_s=1.0)
    assert guard.check(0.0, T0) is False
    assert guard.check(10.0, T0 + timedelta(seconds=10)) is False
    # A step of exactly one second is not more than the threshold.
    assert guard.check(10.0, T0 + timedelta(seconds=11)) is False
    assert guard.check(10.0, T0 + timedelta(seconds=12.5)) is True
    backwards = ClockJumpGuard(threshold_s=1.0)
    assert backwards.check(0.0, T0) is False
    assert backwards.check(5.0, T0 + timedelta(seconds=3)) is True


@pytest.mark.parametrize(
    ("instant", "in_window"),
    [
        (EST_INSIDE, True),
        (EST_END, False),
        (EDT_INSIDE, True),
        (BEFORE_FALLBACK, True),
        (AFTER_FALLBACK, True),
        (T0, False),
    ],
)
def test_maintenance_freshness_across_dst(instant: datetime, in_window: bool) -> None:
    fresh, reason = is_fresh(state_for(Venue.POLYMARKET_US, instant), 0.0, instant, 10.0)
    if in_window:
        assert (fresh, reason) == (False, "maintenance")
    else:
        assert (fresh, reason) == (True, None)
    kalshi = is_fresh(state_for(Venue.KALSHI, instant), 0.0, instant, 10.0)
    assert kalshi == (True, None)


# --- scheduler -------------------------------------------------------------


def test_scheduler_selection_cap_and_order() -> None:
    groups = []
    markets = []
    for i in range(9):
        gid = f"g{i:02d}"
        groups.append(make_group(gid, f"KX-{i}", f"pm-{i}"))
        start = T0 + timedelta(minutes=i)
        markets.append(make_market(Venue.KALSHI, f"KX-{i}", start))
        markets.append(make_market(Venue.POLYMARKET_US, f"pm-{i}", start))
    groups.append(make_group("nope", "KX-no", "pm-no", status=GroupStatus.CANDIDATE))
    markets.append(make_market(Venue.KALSHI, "KX-no", T0))
    markets.append(make_market(Venue.POLYMARKET_US, "pm-no", T0))
    groups.append(make_group("later", "KX-later", "pm-later"))
    later = T0 + timedelta(hours=2)
    markets.append(make_market(Venue.KALSHI, "KX-later", later))
    markets.append(make_market(Venue.POLYMARKET_US, "pm-later", later))
    groups.append(make_group("nostart", "KX-none", "pm-none"))
    markets.append(make_market(Venue.KALSHI, "KX-none", None))
    markets.append(make_market(Venue.POLYMARKET_US, "pm-none", None))

    plan = build_poll_plan(groups, markets, T0)
    assert [item.group_id for item in plan.polled] == [f"g{i:02d}" for i in range(8)]
    assert all(not item.discovery_sample for item in plan.polled)
    discovery_ids = [item.group_id for item in plan.discovery]
    assert discovery_ids == ["g08", "later", "nostart"]
    assert all(item.discovery_sample for item in plan.discovery)
    assert "nope" not in discovery_ids
    assert plan.cadence_s == 2.0
    assert plan.sample_interval_s == 60.0

    same = T0 + timedelta(minutes=1)
    tied = build_poll_plan(
        [make_group("b", "KX-b", "pm-b"), make_group("a", "KX-a", "pm-a")],
        [
            make_market(Venue.KALSHI, "KX-b", same),
            make_market(Venue.POLYMARKET_US, "pm-b", same),
            make_market(Venue.KALSHI, "KX-a", same),
            make_market(Venue.POLYMARKET_US, "pm-a", same),
        ],
        T0,
    )
    assert [item.group_id for item in tied.polled] == ["a", "b"]

    assert [item.group_id for item in plan_at(T0).polled] == ["g"]
    assert [item.group_id for item in plan_at(T0 + timedelta(minutes=30)).polled] == ["g"]
    boundary = plan_at(T0 + timedelta(minutes=30, seconds=1))
    assert boundary.polled == ()
    assert boundary.discovery[0].discovery_sample
    assert [item.group_id for item in plan_at(T0 - LIVE_WINDOW).polled] == ["g"]
    ended = plan_at(T0 - LIVE_WINDOW - timedelta(seconds=1))
    assert ended.polled == ()
    assert ended.discovery[0].discovery_sample


def test_scheduler_cadence() -> None:
    assert build_poll_plan([], [], T0).cadence_s == 1.0

    def cadence(n: int, **kwargs) -> float:
        groups = [make_group(f"g{i}", f"KX-{i}", f"pm-{i}") for i in range(n)]
        markets = []
        for i in range(n):
            markets.append(make_market(Venue.KALSHI, f"KX-{i}", T0))
            markets.append(make_market(Venue.POLYMARKET_US, f"pm-{i}", T0))
        return build_poll_plan(groups, markets, T0, **kwargs).cadence_s

    assert cadence(3) == 1.0
    assert cadence(8) == 2.0
    assert cadence(5, polyus_books_rps=2.0) == 2.5
    assert cadence(1, min_cadence_s=2.0) == 2.0


# --- supervisor ------------------------------------------------------------


def test_backoff_delay_includes_jitter() -> None:
    assert Backoff(rng=lambda: 0.0).delay(0) == 0.5
    assert Backoff(rng=lambda: 1.0).delay(0) == 1.0


async def test_supervisor_restarts_after_backoff_and_propagates_cancel() -> None:
    clock = FakeClock()
    calls = 0
    crashes: list[tuple[str, BaseException]] = []

    async def loop() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        await clock.sleep(100)

    task = asyncio.create_task(
        supervise(
            "poll",
            loop,
            backoff=Backoff(rng=lambda: 0.0),
            clock=clock,
            on_crash=lambda name, exc: crashes.append((name, exc)),
        )
    )
    await until(lambda: calls == 1 and clock.pending >= 1)
    assert crashes and crashes[0][0] == "poll"
    assert isinstance(crashes[0][1], RuntimeError)
    clock.advance(mono=0.49)
    await asyncio.sleep(0)
    assert calls == 1
    clock.advance(mono=0.01)
    await until(lambda: calls == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 2
    assert len(crashes) == 1


async def test_supervisor_does_not_swallow_cancelled_error() -> None:
    clock = FakeClock()
    calls = 0
    crashes: list[BaseException] = []

    async def loop() -> None:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    task = asyncio.create_task(
        supervise(
            "poll",
            loop,
            backoff=Backoff(rng=lambda: 0.0),
            clock=clock,
            on_crash=lambda _name, exc: crashes.append(exc),
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1
    assert crashes == []


# --- runtime ---------------------------------------------------------------


async def test_concurrent_fetch_one_batch_per_venue() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    kalshi = FakeVenue(Venue.KALSHI, log)
    poly = FakeVenue(Venue.POLYMARKET_US, log)
    group_a, markets_a = pair("a", "KX-a", "pm-a", T0)
    group_b, markets_b = pair("b", "KX-b", "pm-b", T0 + timedelta(minutes=1))
    collector, _gaps, updates = build(
        clock,
        kalshi,
        poly,
        groups=(group_a, group_b),
        markets=(*markets_a, *markets_b),
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        assert_overlapped(log)
        assert kalshi.calls == [["KX-a", "KX-b"]]
        assert poly.calls == [["pm-a", "pm-b"]]
        assert {"a", "b"} in updates
        for venue, market_id in (
            (Venue.KALSHI, "KX-a"),
            (Venue.KALSHI, "KX-b"),
            (Venue.POLYMARKET_US, "pm-a"),
            (Venue.POLYMARKET_US, "pm-b"),
        ):
            state = collector.store.get(market_key(venue, market_id))
            assert state is not None
            assert state.meta.source == "poll"
            assert state.meta.books_atomic_group_id is None
            assert state.meta.discovery_sample is False
            assert state.invalid_reason is None
        assert collector.opens_episodes("a")
        assert collector.opens_episodes("b")


async def test_poll_rejects_older_generation() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log, generation=0),
        FakeVenue(Venue.POLYMARKET_US, log, generation=0),
        groups=(group,),
        markets=markets,
    )
    collector.store.bump_generation(Venue.KALSHI)
    async with Harness(collector):
        await park(collector, clock, 1)
        assert collector.store.get(market_key(Venue.KALSHI, "KX")) is None
        poly_state = collector.store.get(market_key(Venue.POLYMARKET_US, "pm"))
        assert poly_state is not None and poly_state.invalid_reason is None


async def test_discovery_samples_are_flagged_and_not_on_the_fast_cadence() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    kalshi = FakeVenue(Venue.KALSHI, log)
    poly = FakeVenue(Venue.POLYMARKET_US, log)
    live, live_markets = pair("live", "KX-live", "pm-live", T0)
    later, later_markets = pair("later", "KX-later", "pm-later", T0 + timedelta(hours=2))
    collector, _gaps, _updates = build(
        clock,
        kalshi,
        poly,
        groups=(live, later),
        markets=(*live_markets, *later_markets),
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        assert collector.opens_episodes("live")
        assert not collector.opens_episodes("later")
        live_state = collector.store.get(market_key(Venue.POLYMARKET_US, "pm-live"))
        later_state = collector.store.get(market_key(Venue.POLYMARKET_US, "pm-later"))
        assert live_state is not None and later_state is not None
        assert live_state.meta.discovery_sample is False
        assert later_state.meta.discovery_sample is True
        assert kalshi.calls[0] == ["KX-live"]
        assert kalshi.calls[1] == ["KX-later"]
        assert poly.calls[0] == ["pm-live"]
        assert poly.calls[1] == ["pm-later"]

        clock.advance(mono=3, wall=timedelta(seconds=3))
        await park(collector, clock, 2)
        assert kalshi.calls[2] == ["KX-live"]
        assert poly.calls[2] == ["pm-live"]
        held = collector.store.get(market_key(Venue.POLYMARKET_US, "pm-later"))
        assert held is not None
        assert held.meta.received_mono == 0.0
        assert held.invalid_reason is None
        refreshed = collector.store.get(market_key(Venue.KALSHI, "KX-live"))
        assert refreshed is not None and refreshed.meta.received_mono == 3.0

        clock.advance(mono=57, wall=timedelta(seconds=57))
        await park(collector, clock, 3)
        assert kalshi.calls[3] == ["KX-live"]
        assert kalshi.calls[4] == ["KX-later"]


async def test_once_falls_back_to_soonest_capped_discovery_samples() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    groups = []
    markets = []
    for index, minutes in enumerate((90, 60, 120, 45, -420)):
        group, pair_markets = pair(
            f"g{index}", f"KX{index}", f"pm{index}", T0 + timedelta(minutes=minutes)
        )
        groups.append(group)
        markets.extend(pair_markets)
    kalshi = FakeVenue(Venue.KALSHI, log)
    poly = FakeVenue(Venue.POLYMARKET_US, log)
    collector, _gaps, _updates = build(
        clock,
        kalshi,
        poly,
        groups=groups,
        markets=markets,
        settings=Settings(_env_file=None, max_polled_groups=2),
    )
    collector._bind()
    await collector._poll_once(once=True)
    assert kalshi.calls == [["KX3", "KX1"]]
    assert poly.calls == [["pm3", "pm1"]]
    assert collector.opens_episodes("g3") is False
    assert collector.opens_episodes("g1") is False
    assert collector.store.get(market_key(Venue.KALSHI, "KX3")).meta.discovery_sample


async def test_once_does_not_notify_unselected_group_sharing_a_market() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    selected, selected_markets = pair(
        "a-selected", "KX-shared", "pm-selected", T0 + timedelta(minutes=60)
    )
    later, later_markets = pair("z-later", "KX-unused", "pm-later", T0 + timedelta(minutes=90))
    later = later.model_copy(update={"members": (selected.members[0], later.members[1])})
    markets = [selected_markets[0], selected_markets[1], later_markets[1]]
    collector, _gaps, updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(selected, later),
        markets=markets,
        settings=Settings(_env_file=None, max_polled_groups=1),
    )
    collector._bind()
    await collector._poll_once(once=True)
    assert updates == [{"a-selected"}]


@pytest.mark.parametrize(
    ("instant", "in_window"),
    [
        (EST_INSIDE, True),
        (EDT_INSIDE, True),
        (BEFORE_FALLBACK, True),
        (AFTER_FALLBACK, True),
        (EST_END, False),
    ],
)
async def test_maintenance_invalidation_across_dst(instant: datetime, in_window: bool) -> None:
    clock = FakeClock(wall=instant)
    log: list[tuple[str, str]] = []
    collector, gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, log), FakeVenue(Venue.POLYMARKET_US, log)
    )
    seed(collector, clock, Venue.POLYMARKET_US, "pm")
    seed(collector, clock, Venue.KALSHI, "kx")
    async with Harness(collector):
        await park(collector, clock, 1)
        poly = collector.store.get(market_key(Venue.POLYMARKET_US, "pm"))
        kalshi = collector.store.get(market_key(Venue.KALSHI, "kx"))
        assert poly is not None and kalshi is not None
        assert kalshi.invalid_reason is None
        if in_window:
            assert poly.invalid_reason == "maintenance"
            assert gaps == ["maintenance"]
        else:
            assert poly.invalid_reason is None
            assert gaps == []


async def test_clock_jump_invalidates_all() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    collector, gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, log), FakeVenue(Venue.POLYMARKET_US, log)
    )
    seed(collector, clock, Venue.KALSHI, "kx")
    seed(collector, clock, Venue.POLYMARKET_US, "pm")
    async with Harness(collector):
        await park(collector, clock, 1)
        clock.advance(mono=0.25, wall=timedelta(seconds=0.25))
        await until(lambda: collector.freshness_ticks >= 1 and clock.pending >= 3)
        assert gaps == []
        clock.advance(mono=0.25, wall=timedelta(seconds=5.25))
        await until(lambda: any(event.kind == "clock_jump" for event in collector.events))
        assert "clock_jump" in gaps
        for venue, market_id in ((Venue.KALSHI, "kx"), (Venue.POLYMARKET_US, "pm")):
            state = collector.store.get(market_key(venue, market_id))
            assert state is not None and state.invalid_reason == "clock_jump"


async def test_adapter_error_invalidates_only_that_venue() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    kalshi = FakeVenue(Venue.KALSHI, log)
    poly = FakeVenue(Venue.POLYMARKET_US, log)
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(clock, kalshi, poly, groups=(group,), markets=markets)
    async with Harness(collector):
        await park(collector, clock, 1)
        kalshi.error = RuntimeError("kalshi down")
        step = collector.sleep_s
        clock.advance(mono=step, wall=timedelta(seconds=step))
        await park(collector, clock, 2)
        kalshi_state = collector.store.get(market_key(Venue.KALSHI, "KX"))
        poly_state = collector.store.get(market_key(Venue.POLYMARKET_US, "pm"))
        assert kalshi_state is not None and kalshi_state.invalid_reason == "adapter_error"
        assert kalshi_state.meta.received_mono == 0.0
        assert poly_state is not None and poly_state.invalid_reason is None
        assert poly_state.meta.received_mono == 1.0
        assert collector.error_count == 1
        assert collector.http_429_count == 0
        assert "adapter_error" not in gaps
        assert any(
            event.kind == "adapter_error" and event.venue == "kalshi" for event in collector.events
        )


async def test_http_429_is_counted() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    poly = FakeVenue(Venue.POLYMARKET_US, log)
    poly.error = RetriesExhaustedError("HTTP 429 after 5 attempts", status_code=429)
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, log), poly, groups=(group,), markets=markets
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        assert collector.http_429_count == 1
        assert collector.error_count == 1
        assert collector.store.get(market_key(Venue.POLYMARKET_US, "pm")) is None
        kalshi_state = collector.store.get(market_key(Venue.KALSHI, "KX"))
        assert kalshi_state is not None and kalshi_state.invalid_reason is None


async def test_backpressure_invalidates_and_slows_until_drained() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    raw_q: asyncio.Queue[Envelope] = asyncio.Queue()
    for i in range(11):
        raw_q.put_nowait(dummy_env(i))
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
        raw_queue=raw_q,
        queue_limit=10,
    )
    seed(collector, clock, Venue.KALSHI, "extra")
    async with Harness(collector):
        await park(collector, clock, 1)
        assert collector.backpressure is False
        assert gaps == []
        while raw_q.qsize() > 5:
            raw_q.get_nowait()
        clock.advance(mono=6, wall=timedelta(seconds=6))
        await park(collector, clock, 2)
        assert collector.backpressure is False
        for i in range(11, 22):
            raw_q.put_nowait(dummy_env(i))
        clock.advance(mono=1, wall=timedelta(seconds=1))
        await park(collector, clock, 3)
        assert collector.backpressure is False
        clock.advance(mono=5, wall=timedelta(seconds=5))
        await park(collector, clock, 4)
        assert collector.backpressure is False
        clock.advance(mono=0.1, wall=timedelta(seconds=0.1))
        await collector._sync_backpressure()
        assert collector.backpressure is True
        assert collector.plan is not None
        collector._set_sleep(collector.plan)
        assert collector.sleep_s == 60.0
        assert gaps == ["recording_backpressure"]
        for market_id in ("KX", "pm", "extra"):
            venue = Venue.POLYMARKET_US if market_id == "pm" else Venue.KALSHI
            state = collector.store.get(market_key(venue, market_id))
            assert state is not None and state.invalid_reason == "recording_backpressure"
        assert raw_q.qsize() >= 11
        while raw_q.qsize() > 5:
            raw_q.get_nowait()
        clock.advance(mono=60, wall=timedelta(seconds=60))
        await park(collector, clock, 5)
        assert collector.backpressure is False
        assert collector.sleep_s == 1.0
        assert gaps == ["recording_backpressure"]
        kx = collector.store.get(market_key(Venue.KALSHI, "KX"))
        pm = collector.store.get(market_key(Venue.POLYMARKET_US, "pm"))
        extra = collector.store.get(market_key(Venue.KALSHI, "extra"))
        assert kx is not None and kx.invalid_reason is None
        assert pm is not None and pm.invalid_reason is None
        assert extra is not None and extra.invalid_reason == "recording_backpressure"


async def test_discovery_sized_raw_burst_does_not_invalidate_books() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    raw_q: asyncio.Queue[Envelope] = asyncio.Queue()
    for i in range(1001):
        raw_q.put_nowait(dummy_env(i))
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
        raw_queue=raw_q,
    )
    seed(collector, clock, Venue.KALSHI, "KX")
    seed(collector, clock, Venue.POLYMARKET_US, "pm")
    await collector._sync_backpressure()
    assert collector.backpressure is False
    assert gaps == []
    for venue, market_id in (
        (Venue.KALSHI, "KX"),
        (Venue.POLYMARKET_US, "pm"),
    ):
        state = collector.store.get(market_key(venue, market_id))
        assert state is not None and state.invalid_reason is None


async def test_disk_guard_stops_raw_logging_and_keeps_summaries() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    raw_q: asyncio.Queue[Envelope] = asyncio.Queue()
    db_q: asyncio.Queue[object] = asyncio.Queue()
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log, cache_headers={"X-Kalshi-Cache-Hits": "0"}),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
        raw_queue=raw_q,
        db_queue=db_q,
        disk_free=lambda _path: 1_000_000_000,
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        assert raw_q.empty()
        assert collector.raw_logging is False
        assert db_q.qsize() >= 1
        alarms = [event for event in collector.events if event.detail.get("alarm") == "disk_low"]
        assert len(alarms) == 1
        assert alarms[0].kind == "health"
        assert alarms[0].detail["raw_logging"] is False
        assert alarms[0].detail["disk_free_bytes"] == 1_000_000_000
        state = collector.store.get(market_key(Venue.KALSHI, "KX"))
        assert state is not None and state.invalid_reason is None


async def test_health_event_emitted() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        clock.advance(mono=10, wall=timedelta(seconds=10))
        await until(lambda: any(event.kind == "health" for event in collector.events))
        health = next(event for event in collector.events if event.kind == "health")
        assert health.detail["alarm"] is None
        assert health.detail["error_count"] == 0
        assert health.detail["http_429_count"] == 0
        assert health.detail["disk_free_bytes"] == PLENTY
        assert health.detail["raw_logging"] is True
        assert health.detail["queue_depth_raw"] == 0
        assert health.detail["queue_depth_db"] == 0
        assert health.detail["cadence_s"] == 1.0
        ages = health.detail["last_book_age_s"]
        assert isinstance(ages["kalshi"], float)
        assert isinstance(ages["polymarket_us"], float)


async def test_raw_envelopes_written_per_book() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    raw_q: asyncio.Queue[Envelope] = asyncio.Queue()
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log, generation=4, cache_headers={"X-Kalshi-Cache-Hits": "0"}),
        FakeVenue(Venue.POLYMARKET_US, log, generation=4),
        groups=(group,),
        markets=markets,
        raw_queue=raw_q,
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        envs = []
        while not raw_q.empty():
            envs.append(raw_q.get_nowait())
    assert len(envs) == 2
    by_venue = {env.venue: env for env in envs}
    kalshi = by_venue["kalshi"]
    poly = by_venue["polymarket_us"]
    assert kalshi.kind == "rest_book" and poly.kind == "rest_book"
    assert kalshi.conn_gen == 4 and poly.conn_gen == 4
    assert kalshi.endpoint == "/markets/orderbooks"
    assert poly.endpoint == "/v1/markets/pm/book"
    assert kalshi.status == 200 and poly.status == 200
    assert kalshi.cache_headers == {"X-Kalshi-Cache-Hits": "0"}
    assert "cache_headers" not in kalshi.body
    assert kalshi.body == {"market": "KX"}
    assert poly.cache_headers == {}
    assert poly.body == {"market": "pm"}
    assert kalshi.recv_utc.startswith("2026-06-03T18:00:00")
    assert kalshi.recv_mono_ns == 0


async def test_shutdown_drains_and_reports_gap(tmp_path) -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    raw_q: asyncio.Queue[Envelope] = asyncio.Queue()
    writer = RawLogWriter(tmp_path / "raw", clock=lambda: T0)
    raw_task = RawLogTask(writer, raw_q, flush_interval=3600)
    db_path = tmp_path / "pmarb.sqlite"
    db_q: asyncio.Queue[object] = asyncio.Queue()
    db_task = DbWriterTask(
        db_path,
        db_q,
        commit_interval_s=3600,
        batch_size=100,
        checkpoint_interval_s=3600,
    )
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log, cache_headers={"Age": "0"}),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
        raw_task=raw_task,
        db_task=db_task,
        settings=Settings(_env_file=None, data_dir=tmp_path),
    )
    async with Harness(collector):
        await asyncio.wait_for(db_task.ready.wait(), 2)
        await park(collector, clock, 1)
        await until(
            lambda: raw_q.empty() and writer.pending >= 2,
            detail=f"qsize={raw_q.qsize()} pending={writer.pending}",
        )
        assert "collector_shutdown" not in gaps
    assert "collector_shutdown" in gaps
    files = list_log_files(tmp_path / "raw", "kalshi") + list_log_files(
        tmp_path / "raw", "polymarket_us"
    )
    records = list(iter_records(files))
    assert len(records) == 2
    assert {record.kind for record in records} == {"rest_book"}
    assert writer.pending == 0
    with closing(open_reader(db_path)) as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM collector_events ORDER BY id")]
    assert [kind for kind in kinds if kind != "timing"] == ["collector_shutdown"]
    assert "timing" in kinds


async def test_signal_stop_shuts_down() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
        install_signals=True,
    )
    async with Harness(collector) as task:
        await park(collector, clock, 1)
        assert collector._signals
        collector._signal_stop()
        await task
        if collector._signal_task is not None:
            await collector._signal_task
    assert collector._signals == []
    assert "collector_shutdown" in gaps


async def test_cancel_during_fetch_is_not_an_adapter_error() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    kalshi = BlockingVenue(Venue.KALSHI)
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(
        clock,
        kalshi,
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
    )
    async with Harness(collector) as task:
        await asyncio.wait_for(kalshi.started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert collector.error_count == 0
        assert "collector_shutdown" in gaps


async def test_empty_universe_does_not_fetch() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    kalshi = FakeVenue(Venue.KALSHI, log)
    poly = FakeVenue(Venue.POLYMARKET_US, log)
    collector, _gaps, _updates = build(clock, kalshi, poly)
    async with Harness(collector):
        await park(collector, clock, 1)
        assert kalshi.calls == []
        assert poly.calls == []
