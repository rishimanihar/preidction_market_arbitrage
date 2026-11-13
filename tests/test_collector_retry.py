"""Book-poll failures: per-fetch budget, venue isolation, enriched errors, health counters.

Clocks are injected. The HTTP client tests use ``httpx.MockTransport``; nothing
touches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from pmarb.collector import FakeClock, build_poll_plan
from pmarb.collector.books import market_key
from pmarb.collector.runtime import FETCH_BACKSTOP_GRACE_S, Collector
from pmarb.config import Settings
from pmarb.http import Backoff, ObservationHttpClient, RetriesExhaustedError, TokenBucket
from pmarb.models import Book, Venue
from pmarb.venues.kalshi.rest import KalshiAdapter
from pmarb.venues.polymarket_us.rest import PolymarketUSAdapter
from tests.test_collector import T0, FakeVenue, Harness, build, pair, park, seed, until

FIXTURES = Path(__file__).parent / "fixtures"
KALSHI_BATCH = json.loads((FIXTURES / "kalshi" / "orderbooks_batch.json").read_text())
KX = KALSHI_BATCH["orderbooks"][0]["ticker"]
POLY_BOOK = json.loads((FIXTURES / "polymarket_us" / "book.json").read_text())


class SlowVenue:
    """A venue whose book calls never finish on their own."""

    def __init__(self, venue: Venue, clock: FakeClock) -> None:
        self.venue = venue
        self.clock = clock
        self.calls = 0
        self.cancelled = 0

    async def get_books(self, market_ids: list[str]) -> list[Book]:
        self.calls += 1
        try:
            await self.clock.sleep(1000)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return []


def http_client(handler: Any, clock: FakeClock) -> ObservationHttpClient:
    """The real client on the collector's fake clock. Its sleeps move that clock."""

    async def sleep(seconds: float) -> None:
        clock.advance(mono=seconds, wall=timedelta(seconds=seconds))
        await asyncio.sleep(0)

    buckets = {
        (venue, purpose): TokenBucket(1000, 100, clock=clock.monotonic, sleep=sleep)
        for venue in ("kalshi", "polyus")
        for purpose in ("books", "discovery")
    }
    return ObservationHttpClient(
        Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        buckets=buckets,
        backoff=Backoff(rng=lambda: 0.0),
        sleep=sleep,
        clock=clock.monotonic,
    )


def kalshi_ok(request: httpx.Request) -> httpx.Response:
    wanted = request.url.params.get_list("tickers")
    rows = [row for row in KALSHI_BATCH["orderbooks"] if row["ticker"] in wanted]
    return httpx.Response(200, json={"orderbooks": rows})


def poly_ok(slug: str) -> httpx.Response:
    body = json.loads(json.dumps(POLY_BOOK))
    body["marketData"]["marketSlug"] = slug
    return httpx.Response(200, json=body)


def events(collector: Collector, kind: str, venue: str | None = None) -> list[dict[str, Any]]:
    return [
        event.detail
        for event in collector.events
        if event.kind == kind and (venue is None or event.venue == venue)
    ]


async def settle(rounds: int = 60) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


# --- budget --------------------------------------------------------------------


def test_fetch_budget_stays_below_the_freshness_window() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    collector, _gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, log), FakeVenue(Venue.POLYMARKET_US, log)
    )

    def budget(n_poly: int) -> float:
        groups, markets = [], []
        for n in range(n_poly):
            group, members = pair(f"g{n}", f"KX{n}", f"pm{n}", T0)
            groups.append(group)
            markets.extend(members)
        plan = build_poll_plan(groups, markets, T0, max_polled_groups=1000)
        return collector.fetch_budget_s(plan.polled)

    # 2 × cadence windows of 2 s, 4 s, and 40 s; the last is capped.
    assert budget(1) == pytest.approx(1.5)
    assert budget(8) == pytest.approx(3.0)
    assert budget(80) == pytest.approx(10.0)
    for n in (1, 4, 8, 16):
        cadence = max(1.0, n / 4)
        assert budget(n) < 2 * cadence


# --- isolation -------------------------------------------------------------------


async def test_slow_venue_is_cut_at_its_budget_and_the_other_is_accepted_first() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    slow = SlowVenue(Venue.POLYMARKET_US, clock)
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, log), slow, groups=(group,), markets=markets
    )
    task = asyncio.create_task(collector.run_once())
    kalshi_key = market_key(Venue.KALSHI, "KX")
    await until(lambda: collector.store.get(kalshi_key) is not None)
    # Kalshi's book is in the store while Polymarket US is still hanging.
    assert not task.done() and slow.calls == 1
    state = collector.store.get(kalshi_key)
    assert state is not None and state.invalid_reason is None
    assert state.meta.received_mono == 0.0

    clock.advance(mono=1.5 + FETCH_BACKSTOP_GRACE_S, wall=timedelta(seconds=1.75))
    await asyncio.wait_for(task, 2)
    assert slow.cancelled == 1
    (error,) = events(collector, "adapter_error", "polymarket_us")
    assert error["cause"] == "budget_exceeded"
    assert error["error_type"] == "FetchBudgetExceededError"
    assert error["fetch_budget_s"] == pytest.approx(1.5)
    assert (error["requested"], error["failed"]) == (1, 1)
    fetches = {
        detail["venue"]: detail["duration_s"]
        for detail in events(collector, "timing")
        if detail["phase"] == "poll_fetch"
    }
    assert fetches == {"kalshi": 0.0, "polymarket_us": pytest.approx(1.75)}


async def test_failing_venue_never_lets_the_healthy_venues_books_go_stale() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    slow = SlowVenue(Venue.POLYMARKET_US, clock)
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, log), slow, groups=(group,), markets=markets
    )
    kalshi_key = market_key(Venue.KALSHI, "KX")
    async with Harness(collector):
        await until(lambda: collector.store.get(kalshi_key) is not None)
        for _ in range(40):  # 10 s in freshness-tick steps
            clock.advance(mono=0.25, wall=timedelta(seconds=0.25))
            await settle()
            state = collector.store.get(kalshi_key)
            assert state is not None and state.invalid_reason is None, clock.monotonic()
    assert slow.calls >= 5
    assert slow.cancelled >= 5
    errors = events(collector, "adapter_error", "polymarket_us")
    assert errors and all(error["cause"] == "budget_exceeded" for error in errors)
    assert "kalshi" not in collector.poll_failures


async def test_cycles_run_at_a_fixed_rate() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []

    class Latent(FakeVenue):
        async def get_books(self, market_ids: list[str]) -> list[Book]:
            clock.advance(mono=0.4, wall=timedelta(seconds=0.4))
            return await super().get_books(market_ids)

    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock,
        Latent(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        # The 0.4 s fetch comes out of the 1 s cadence.
        assert any(abs(s - 0.6) < 1e-9 for s in clock.sleeps)
        assert 1.0 not in clock.sleeps


# --- the real client inside a poll ---------------------------------------------------


async def test_retry_budget_bounds_a_failing_venue_and_the_error_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    http = http_client(handler, clock)
    group, markets = pair("g", KX, "pm", T0)
    collector, _gaps, _updates = build(
        clock,
        KalshiAdapter(http),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
    )
    with caplog.at_level(logging.WARNING, logger="pmarb.collector.runtime"):
        await collector.run_once()
    await http.aclose()

    # Budget 1.5 s: attempts at 0 s and 0.5 s; the next 1 s backoff would reach the deadline.
    assert len(calls) == 2
    assert clock.monotonic() == pytest.approx(0.5)
    (error,) = events(collector, "adapter_error", "kalshi")
    assert error["error_type"] == "RetriesExhaustedError"
    assert (error["cause"], error["status_code"]) == ("http_503", 503)
    assert (error["attempts"], error["elapsed_s"]) == (2, pytest.approx(0.5))
    assert (error["http_venue"], error["purpose"]) == ("kalshi", "books")
    assert error["endpoint"] == "/markets/orderbooks"
    assert error["path"] == "/trade-api/v2/markets/orderbooks"
    assert (error["stop"], error["waiting_for"]) == ("deadline", "cooldown")
    assert error["history"] == ["http_503", "http_503"]
    assert error["fetch_budget_s"] == pytest.approx(1.5)
    assert error["fetch_elapsed_s"] == pytest.approx(0.5)
    assert error["fetch_elapsed_s"] < error["fetch_budget_s"]
    assert error["causes"] == {"http_503": 1}

    (line,) = [r.getMessage() for r in caplog.records if "book poll failed" in r.getMessage()]
    for piece in (
        "book poll failed for kalshi",
        "cause=http_503",
        "failed=1/1",
        "elapsed=0.50s",
        "budget=1.50s",
        (
            "RetriesExhaustedError: kalshi/books GET /markets/orderbooks failed: "
            "HTTP 503 after 2 attempts in 0.50s"
        ),
    ):
        assert piece in line

    poly_state = collector.store.get(market_key(Venue.POLYMARKET_US, "pm"))
    assert poly_state is not None and poly_state.invalid_reason is None


async def test_retry_after_is_honoured_across_poll_cycles() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "30"})
        return kalshi_ok(request)

    http = http_client(handler, clock)
    group, markets = pair("g", KX, "pm", T0)
    collector, _gaps, _updates = build(
        clock,
        KalshiAdapter(http),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
    )
    collector._bind()
    kalshi_key = market_key(Venue.KALSHI, KX)

    await collector._poll_once()  # 429 with Retry-After 30: give up at once, no sleep
    first = events(collector, "adapter_error", "kalshi")[-1]
    assert (first["cause"], first["retry_after_s"], first["attempts"]) == ("http_429", 30.0, 1)
    assert collector.http_429_count == 1 and clock.monotonic() == 0.0

    clock.advance(mono=1.0, wall=timedelta(seconds=1))
    await collector._poll_once()  # still cooling down: nothing is sent
    second = events(collector, "adapter_error", "kalshi")[-1]
    assert len(calls) == 1
    assert (second["cause"], second["attempts"], second["last_cause"]) == (
        "cooldown",
        0,
        "http_429",
    )
    assert second["streak"] == 2

    clock.advance(mono=30.0, wall=timedelta(seconds=30))
    await collector._poll_once()  # cooldown over: one request, and it succeeds
    assert len(calls) == 2
    state = collector.store.get(kalshi_key)
    assert state is not None and state.invalid_reason is None
    (recovered,) = events(collector, "adapter_recovered", "kalshi")
    assert recovered == {"failed_polls": 2, "last_causes": "cooldown"}
    await http.aclose()


async def test_partial_failure_keeps_good_books_and_counts_causes_in_health() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.split("/")[-2]
        return httpx.Response(503) if slug == "pm-bad" else poly_ok(slug)

    http = http_client(handler, clock)
    good, good_markets = pair("g1", "KX1", "pm-a", T0)
    bad, bad_markets = pair("g2", "KX2", "pm-bad", T0)
    collector, _gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        PolymarketUSAdapter(http),
        groups=(good, bad),
        markets=good_markets + bad_markets,
    )
    await collector.run_once()
    await http.aclose()

    good_state = collector.store.get(market_key(Venue.POLYMARKET_US, "pm-a"))
    assert good_state is not None and good_state.invalid_reason is None
    assert collector.store.get(market_key(Venue.POLYMARKET_US, "pm-bad")) is None
    (error,) = events(collector, "adapter_error", "polymarket_us")
    assert error["partial"] is True
    assert (error["requested"], error["failed"]) == (2, 1)
    assert error["causes"] == {"http_503": 1}
    assert error["endpoint"] == "/v1/markets/{slug}/book"
    assert error["path"] == "/v1/markets/pm-bad/book"

    health = collector._health_detail(alarm=None)
    assert health["poll_failures_by_cause"] == {"polymarket_us": {"http_503": 1}}
    assert health["poll_failure_streak"] == {"polymarket_us": 1}
    books = health["http"]["polyus/books"]
    assert books["requests"] == 2 and books["failures"] == 1
    assert books["http_5xx"] == books["attempt_failures_by_cause"]["http_503"] >= 1


# --- counters ---------------------------------------------------------------------


async def test_health_counts_failures_by_cause_and_clears_the_streak_on_recovery() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    kalshi = FakeVenue(Venue.KALSHI, log)
    group, markets = pair("g", "KX", "pm", T0)
    collector, _gaps, _updates = build(
        clock, kalshi, FakeVenue(Venue.POLYMARKET_US, log), groups=(group,), markets=markets
    )
    collector._bind()
    kalshi.error = RetriesExhaustedError(
        status_code=503, venue="kalshi", purpose="books", attempts=2, history=("http_503",) * 2
    )
    await collector._poll_once()
    kalshi.error = RetriesExhaustedError(
        venue="kalshi", purpose="books", attempts=1, error_type="ConnectTimeout", error_message="t"
    )
    await collector._poll_once()
    collector._health_tick()
    health = events(collector, "health")[-1]
    assert health["poll_failures_by_cause"] == {"kalshi": {"http_503": 1, "ConnectTimeout": 1}}
    assert health["poll_failure_streak"] == {"kalshi": 2}
    assert health["error_count"] == 2
    assert "loop_lag_max_s" in health

    kalshi.error = None
    await collector._poll_once()
    collector._health_tick()
    health = events(collector, "health")[-1]
    assert health["poll_failure_streak"] == {}
    assert health["poll_failures_by_cause"] == {"kalshi": {"http_503": 1, "ConnectTimeout": 1}}
    assert events(collector, "adapter_recovered", "kalshi") == [
        {"failed_polls": 2, "last_causes": "ConnectTimeout"}
    ]


def test_a_failure_invalidates_only_the_markets_that_failed() -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    collector, _gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, log), FakeVenue(Venue.POLYMARKET_US, log)
    )
    seed(collector, clock, Venue.KALSHI, "KX-HOT")
    seed(collector, clock, Venue.KALSHI, "KX-COLD")
    collector._record_adapter_error(Venue.KALSHI, RuntimeError("cold batch failed"), ["KX-COLD"])
    hot = collector.store.get(market_key(Venue.KALSHI, "KX-HOT"))
    cold = collector.store.get(market_key(Venue.KALSHI, "KX-COLD"))
    assert hot is not None and hot.invalid_reason is None
    assert cold is not None and cold.invalid_reason == "adapter_error"
    assert collector.poll_failures == {"kalshi": {"RuntimeError": 1}}
