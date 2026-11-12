import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from pmarb.http import ObservationHttpClient, TokenBucket
from pmarb.venues.kalshi.rest import BOOK_BATCH_SIZE, KalshiAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"
SERIES = json.loads((FIXTURES / "series_KXNFLGAME.json").read_text())["series"]
MARKETS = json.loads((FIXTURES / "markets_page_1.json").read_text())["markets"]
BATCH = json.loads((FIXTURES / "orderbooks_batch.json").read_text())
NOW = datetime(2030, 1, 5, tzinfo=UTC)


def client(handler):
    buckets = {
        ("kalshi", "books"): TokenBucket(1000, 100),
        ("kalshi", "discovery"): TokenBucket(1000, 100),
    }
    return ObservationHttpClient(
        transport=httpx.MockTransport(handler), buckets=buckets, max_retries=0
    )


def response(body, headers=None):
    return httpx.Response(200, json=body, headers=headers)


@pytest.mark.parametrize("final_cursor", ["", None])
def test_market_pagination_stops_for_empty_and_null_cursor(final_cursor):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/series/KXNFLGAME"):
            return response({"series": SERIES})
        if request.url.path.endswith("/events/" + MARKETS[0]["event_ticker"]):
            return response({"event": {"series_ticker": "KXNFLGAME"}})
        if request.url.path.endswith("/markets"):
            cursor = request.url.params.get("cursor")
            if cursor is None:
                return response({"markets": [MARKETS[0]], "cursor": "next"})
            return response({"markets": [], "cursor": final_cursor})
        raise AssertionError(request.url)

    async def run():
        async with client(handler) as http:
            adapter = KalshiAdapter(http, series_tickers=["KXNFLGAME"], clock_utc=lambda: NOW)
            return [
                market
                async for market in adapter.list_markets(
                    NOW - timedelta(days=1), NOW + timedelta(days=3)
                )
            ]

    result = asyncio.run(run())
    market_requests = [r for r in requests if r.url.path.endswith("/markets")]
    assert len(market_requests) == 2
    assert market_requests[0].url.params["status"] == "open"
    assert all(r.url.params.get("status") != "all" for r in requests)
    assert all(r.method == "GET" for r in requests)
    assert len(result) == 1


def test_active_market_with_expired_expected_time_remains_in_discovery():
    row = {
        **MARKETS[0],
        "occurrence_datetime": "2030-01-05T12:00:00Z",
        "expected_expiration_time": "2029-12-01T00:00:00Z",
    }

    def handler(request):
        if request.url.path.endswith("/series/KXNFLGAME"):
            return response({"series": SERIES})
        if request.url.path.endswith("/markets"):
            return response({"markets": [row]})
        if "/events/" in request.url.path:
            return response({"event": {"series_ticker": "KXNFLGAME"}})
        raise AssertionError(request.url)

    async def run():
        async with client(handler) as http:
            adapter = KalshiAdapter(http, series_tickers=["KXNFLGAME"], clock_utc=lambda: NOW)
            return [m async for m in adapter.list_markets(NOW, NOW + timedelta(days=1))]

    assert len(asyncio.run(run())) == 1


def test_active_expired_market_still_obeys_supplied_horizon():
    row = {
        **MARKETS[0],
        "occurrence_datetime": "2029-12-01T00:00:00Z",
        "expected_expiration_time": "2029-11-30T00:00:00Z",
    }

    def handler(request):
        if request.url.path.endswith("/series/KXNFLGAME"):
            return response({"series": SERIES})
        if request.url.path.endswith("/markets"):
            return response({"markets": [row]})
        if "/events/" in request.url.path:
            return response({"event": {"series_ticker": "KXNFLGAME"}})
        raise AssertionError(request.url)

    async def run():
        async with client(handler) as http:
            adapter = KalshiAdapter(http, series_tickers=["KXNFLGAME"], clock_utc=lambda: NOW)
            return [m async for m in adapter.list_markets(NOW, NOW + timedelta(days=1))]

    assert asyncio.run(run()) == []


def test_expected_expiration_minus_three_hours_is_horizon_fallback():
    row = {**MARKETS[0], "expected_expiration_time": "2030-01-05T03:00:00Z"}
    row.pop("occurrence_datetime", None)

    def handler(request):
        if request.url.path.endswith("/series/KXNFLGAME"):
            return response({"series": SERIES})
        if request.url.path.endswith("/markets"):
            return response({"markets": [row]})
        if "/events/" in request.url.path:
            return response({"event": {"series_ticker": "KXNFLGAME"}})
        raise AssertionError(request.url)

    async def run():
        async with client(handler) as http:
            adapter = KalshiAdapter(http, series_tickers=["KXNFLGAME"], clock_utc=lambda: NOW)
            return [m async for m in adapter.list_markets(NOW, NOW)]

    result = asyncio.run(run())
    assert len(result) == 1
    assert result[0].event_start_utc is None
    assert result[0].raw["close_time"] != row["expected_expiration_time"]


def test_series_metadata_is_cached_across_discovery_calls():
    series_calls = 0

    def handler(request):
        nonlocal series_calls
        if request.url.path.endswith("/series/KXNFLGAME"):
            series_calls += 1
            return response({"series": SERIES})
        if request.url.path.endswith("/markets"):
            return response({"markets": [], "cursor": ""})
        raise AssertionError(request.url)

    async def run():
        async with client(handler) as http:
            adapter = KalshiAdapter(http, series_tickers=["KXNFLGAME"])
            for _ in range(2):
                [m async for m in adapter.list_markets(NOW, NOW + timedelta(days=1))]

    asyncio.run(run())
    assert series_calls == 1


def test_get_market_joins_series_through_event_without_market_series_field():
    raw = {**MARKETS[0]}
    raw.pop("series_ticker", None)
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/markets/" + raw["ticker"]):
            return response({"market": raw})
        if request.url.path.endswith("/events/" + raw["event_ticker"]):
            return response({"event": {"series_ticker": "KXNFLGAME"}})
        if request.url.path.endswith("/series/KXNFLGAME"):
            return response({"series": SERIES})
        raise AssertionError(request.url)

    async def run():
        async with client(handler) as http:
            return await KalshiAdapter(http).get_market(raw["ticker"])

    market = asyncio.run(run())
    assert market.market_id == raw["ticker"]
    assert market.league == "nfl"
    assert paths[-1].endswith("/series/KXNFLGAME")


def test_batched_books_use_repeated_query_params_and_preserve_full_raw_body():
    seen = []

    def handler(request):
        seen.append(request)
        return response(BATCH)

    async def run():
        async with client(handler) as http:
            return await KalshiAdapter(
                http, clock_utc=lambda: NOW, monotonic=lambda: 55.0, connection_generation=4
            ).get_books([row["ticker"] for row in BATCH["orderbooks"]])

    books = asyncio.run(run())
    req = seen[0]
    assert req.method == "GET"
    assert req.url.path == "/trade-api/v2/markets/orderbooks"
    assert req.url.params.get_list("tickers") == [row["ticker"] for row in BATCH["orderbooks"]]
    assert all(book.raw_native["orderbooks"] == BATCH["orderbooks"] for book in books)
    assert "cache_headers" in books[0].raw_native
    assert all(book.connection_generation == 4 for book in books)


def test_one_receipt_timestamp_is_shared_by_books_in_a_batch():
    def handler(request):
        return response(BATCH)

    utc_calls = iter([NOW, NOW + timedelta(seconds=1)])
    mono_calls = iter([10.0, 11.0])

    async def run():
        async with client(handler) as http:
            adapter = KalshiAdapter(
                http,
                clock_utc=lambda: next(utc_calls),
                monotonic=lambda: next(mono_calls),
            )
            return await adapter.get_books([row["ticker"] for row in BATCH["orderbooks"]])

    books = asyncio.run(run())
    assert books[0].received_utc == books[1].received_utc == NOW
    assert books[0].received_monotonic == books[1].received_monotonic == 10.0


def test_comma_joined_response_ticker_mismatch_raises():
    def handler(request):
        return response({"orderbooks": [{"ticker": "A,B", "orderbook_fp": {}}]})

    async def run():
        async with client(handler) as http:
            return await KalshiAdapter(http).get_books(["A", "B"])

    with pytest.raises(ValueError, match="ticker mismatch"):
        asyncio.run(run())


def test_books_are_chunked_at_100():
    requests = []
    tickers = [f"SYN-{n}" for n in range(BOOK_BATCH_SIZE + 1)]

    def handler(request):
        requests.append(request)
        batch = request.url.params.get_list("tickers")
        return response({"orderbooks": [{"ticker": t, "orderbook_fp": {}} for t in batch]})

    async def run():
        async with client(handler) as http:
            return await KalshiAdapter(http).get_books(tickers)

    books = asyncio.run(run())
    assert len(books) == len(tickers)
    assert [len(r.url.params.get_list("tickers")) for r in requests] == [100, 1]


def test_book_cache_headers_invalidate_returned_books():
    def handler(request):
        requested = request.url.params.get_list("tickers")
        payload = {"orderbooks": [row for row in BATCH["orderbooks"] if row["ticker"] in requested]}
        return response(payload, {"Cache-Control": 'public, max-age="15"'})

    async def run():
        async with client(handler) as http:
            return await KalshiAdapter(http).get_books([BATCH["orderbooks"][0]["ticker"]])

    assert not asyncio.run(run())[0].valid


def test_adapter_emits_only_get_requests_and_never_status_all():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/series/KXNFLGAME"):
            return response({"series": SERIES})
        if request.url.path.endswith("/markets"):
            return response({"markets": [], "cursor": None})
        raise AssertionError(request.url)

    async def run():
        async with client(handler) as http:
            adapter = KalshiAdapter(http, series_tickers=["KXNFLGAME"])
            [m async for m in adapter.list_markets(NOW, NOW + timedelta(days=1))]
            assert adapter.book_stream([]) is None

    asyncio.run(run())
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.params.get("status") != "all" for request in requests)
