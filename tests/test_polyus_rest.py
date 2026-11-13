from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from pmarb.collector.clock import FakeClock
from pmarb.collector.discovery import Discovery
from pmarb.http import ObservationHttpClient, TokenBucket
from pmarb.models import Venue
from pmarb.venues.polymarket_us.rest import PAGE_SIZE, PolymarketUSAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "polymarket_us"


def _client(handler):
    transport = httpx.MockTransport(handler)
    buckets = {
        ("polyus", "books"): TokenBucket(1000, 50),
        ("polyus", "discovery"): TokenBucket(1000, 50),
    }
    return ObservationHttpClient(transport=transport, buckets=buckets, max_retries=0)


def _json_response(body):
    return httpx.Response(200, json=body)


def test_league_and_event_pagination_and_horizon_filter():
    requests = []
    market = json.loads((FIXTURES / "market_by_slug.json").read_text())["market"]
    market["gameStartTime"] = "2030-01-06T00:00:00Z"
    now = datetime(2030, 1, 5, tzinfo=UTC)

    async def handler(request):
        requests.append(request)
        path, params = request.url.path, dict(request.url.params)
        offset = int(params["offset"])
        if path == "/v2/leagues":
            rows = (
                [{"slug": f"other-{i}"} for i in range(PAGE_SIZE)]
                if offset == 0
                else [{"slug": "nfl"}]
            )
            return _json_response({"leagues": rows})
        if path == "/v2/leagues/nfl/events":
            if offset == 0:
                rows = [
                    {"startTime": "2029-12-01T00:00:00Z", "markets": []} for _ in range(PAGE_SIZE)
                ]
            else:
                recent = json.loads(json.dumps(market))
                recent["slug"] = "recent-event-market"
                rows = [
                    {"startTime": "2030-01-04T13:00:00Z", "markets": [recent]},
                    {"markets": [market]},  # fallback to market.gameStartTime
                ]
            return _json_response({"events": rows})
        raise AssertionError(path)

    async def run():
        async with _client(handler) as http:
            adapter = PolymarketUSAdapter(http, leagues=["nfl"], clock_utc=lambda: now)
            result = [
                m
                async for m in adapter.list_markets(
                    now - timedelta(hours=12), now + timedelta(days=2)
                )
            ]
            return result, adapter.discovery_pages

    result, page_count = asyncio.run(run())
    assert len(result) == 2
    assert result[0].event_start_utc == datetime(2030, 1, 4, 13, tzinfo=UTC)
    assert result[0].league == "nfl"
    assert result[1].event_start_utc == datetime(2030, 1, 6, tzinfo=UTC)
    assert page_count == 4
    assert [(r.url.path, r.url.params.get("offset")) for r in requests] == [
        ("/v2/leagues", "0"),
        ("/v2/leagues", str(PAGE_SIZE)),
        ("/v2/leagues/nfl/events", "0"),
        ("/v2/leagues/nfl/events", str(PAGE_SIZE)),
    ]
    assert all(r.method == "GET" for r in requests)
    assert all(int(r.url.params["limit"]) <= 50 for r in requests)


def test_discovery_timing_counts_real_polyus_http_requests():
    events = []
    clock = FakeClock(wall=datetime(2030, 1, 5, tzinfo=UTC))

    async def handler(request):
        if request.url.path == "/v2/leagues":
            return _json_response({"leagues": []})
        raise AssertionError(request.url)

    async def run():
        async with _client(handler) as http:
            adapter = PolymarketUSAdapter(http, clock_utc=clock.wall)
            discovery = Discovery(
                kalshi=object(),
                polyus=adapter,
                registry=object(),
                review=object(),
                clock=clock,
                interval_s=lambda: 1.0,
                enqueue=lambda _op: None,
                episodes=object(),
                emit=lambda venue, kind, detail: events.append((venue, kind, detail)),
            )
            listing = await discovery._collect(
                adapter,
                clock.wall() - timedelta(days=1),
                clock.wall() + timedelta(days=1),
            )
            assert listing.ids == set()
            assert adapter.http.request_metrics("polyus", "discovery")["requests"] == 1

    asyncio.run(run())
    timing = next(
        detail
        for venue, kind, detail in events
        if venue == str(Venue.POLYMARKET_US)
        and kind == "timing"
        and detail.get("phase") == "discovery_venue"
    )
    assert timing["requests"] == 1
    assert timing["pages"] == 1
    assert timing["markets"] == 0


def test_get_books_concurrent_and_keep_raw_body():
    active = 0
    peak = 0
    requests = []
    body = json.loads((FIXTURES / "book.json").read_text())

    async def handler(request):
        nonlocal active, peak
        requests.append(request)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        requested_slug = request.url.path.split("/")[-2]
        response_body = json.loads(json.dumps(body))
        response_body["marketData"]["marketSlug"] = requested_slug
        return _json_response(response_body)

    async def run():
        async with _client(handler) as http:
            adapter = PolymarketUSAdapter(
                http,
                clock_utc=lambda: datetime(2030, 1, 6, tzinfo=UTC),
                monotonic=lambda: 42.0,
                connection_generation=3,
            )
            return await adapter.get_books(["slug-a", "slug-b", "slug-c"])

    books = asyncio.run(run())
    assert peak > 1
    assert len(books) == 3
    assert all(book.valid and book.connection_generation == 3 for book in books)
    assert [book.market_id for book in books] == ["slug-a", "slug-b", "slug-c"]
    assert all(book.received_monotonic == 42.0 for book in books)
    assert all(r.method == "GET" and "/book" in r.url.path for r in requests)


def test_detail_uses_slug_detail_endpoint_and_only_get():
    requests = []
    body = json.loads((FIXTURES / "market_by_slug.json").read_text())

    async def handler(request):
        requests.append(request)
        return _json_response(body)

    async def run():
        async with _client(handler) as http:
            return await PolymarketUSAdapter(http).get_market("aec-nfl-aaa-bbb-2030-01-06")

    market = asyncio.run(run())
    assert market.market_id == "aec-nfl-aaa-bbb-2030-01-06"
    assert requests[0].url.path == "/v1/market/slug/aec-nfl-aaa-bbb-2030-01-06"
    assert requests[0].method == "GET"


def test_book_identity_mismatch_fails_closed():
    body = json.loads((FIXTURES / "book.json").read_text())

    async def handler(request):
        return _json_response(body)

    async def run():
        async with _client(handler) as http:
            return await PolymarketUSAdapter(http).get_books(["different-slug"])

    with pytest.raises(ValueError, match="identity mismatch"):
        asyncio.run(run())


def test_malformed_discovery_response_fails_closed():
    async def handler(request):
        return _json_response({})

    async def run():
        async with _client(handler) as http:
            adapter = PolymarketUSAdapter(http, leagues=["nfl"])
            return [
                m
                async for m in adapter.list_markets(
                    datetime(2030, 1, 1, tzinfo=UTC), datetime(2030, 1, 2, tzinfo=UTC)
                )
            ]

    with pytest.raises(ValueError, match="missing 'leagues'"):
        asyncio.run(run())
