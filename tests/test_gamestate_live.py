import asyncio
from datetime import UTC, datetime

import httpx

from pmarb.gamestate.kalshi_live import KalshiLiveClient, parse_live
from pmarb.http import ObservationHttpClient, TokenBucket

NOW = datetime(2030, 1, 5, tzinfo=UTC)


def make_http(handler):
    return ObservationHttpClient(
        transport=httpx.MockTransport(handler),
        buckets={("kalshi", "gamestate"): TokenBucket(1000, 100)},
        max_retries=0,
    )


def test_parse_nba_live_data_and_ignores_lagging_milestone_status():
    state = parse_live(
        {
            "live_data": {
                "milestone_id": "m1",
                "type": "basketball_game",
                "details": {
                    "home_points": "91",
                    "away_points": 88,
                    "period": 4,
                    "period_remaining_time": "02:15",
                    "status": "live",
                    "possession": "home",
                    "last_play": "Made 3PT",
                    "result_pending": False,
                    "live_data_delayed": True,
                },
            },
            "milestone": {"status": "final"},
        },
        "NBA",
        event_ticker="KXNBA-30JAN05",
        received_utc=NOW,
        received_mono=12.0,
    )
    assert (state.source, state.status, state.period, state.clock) == (
        "kalshi_live_data",
        "live",
        4,
        "02:15",
    )
    assert (state.home_points, state.away_points, state.delayed_flag) == (91, 88, True)
    assert state.event_ticker == "KXNBA-30JAN05"
    assert state.raw["live_data"]["details"]["status"] == "live"
    assert state.received_mono == 12.0


def test_parse_nfl_live_data_and_missing_fields_are_safe():
    nfl = parse_live(
        {
            "type": "football_game",
            "milestone_id": "n1",
            "details": {
                "home_points": 7,
                "away_points": 3,
                "quarter": "2",
                "clock": "8:43",
                "status": "live",
                "situation": "3rd & 4",
                "live_data_delayed": False,
            },
        },
        "NFL",
    )
    empty = parse_live({}, "NBA")
    assert (nfl.period, nfl.clock, nfl.home_points, nfl.away_points) == ("2", "8:43", 7, 3)
    assert nfl.delayed_flag is False
    assert empty.status is empty.period is empty.clock is None
    assert empty.home_points is empty.result_pending is empty.delayed_flag is None


def test_resolve_milestones_caches_event_mapping():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.params.get("cursor") == "next":
            return httpx.Response(
                200,
                json={
                    "milestones": [
                        {
                            "id": "mil-1",
                            "related_event_tickers": ["EV-1"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "milestones": [
                    {
                        "id": "other",
                        "related_event_tickers": ["EV-2"],
                    }
                ],
                "cursor": "next",
            },
        )

    async def run():
        async with make_http(handler) as http:
            client = KalshiLiveClient(http)
            assert await client.resolve_milestones(["EV-1"]) == {"EV-1": "mil-1"}
            assert await client.resolve_milestones(["EV-1"]) == {"EV-1": "mil-1"}

    asyncio.run(run())
    assert len(requests) == 2
    assert requests[0].url.params["related_event_ticker"] == "EV-1"
    assert requests[0].url.params["limit"] == "500"
    assert requests[1].url.params["cursor"] == "next"


def test_batch_request_encoding_and_raw_callback_preserves_http_bytes():
    content = (
        b'{"live_datas":[{"type":"football_game","milestone_id":"m1","details":{"status":"live"}}]}'
    )
    requests, logged = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, content=content, headers={"content-type": "application/json", "ETag": "abc"}
        )

    async def run():
        async with make_http(handler) as http:
            client = KalshiLiveClient(http, clock_utc=lambda: NOW, monotonic=lambda: 3.5)
            client.raw_log = lambda *args: logged.append(args)
            states = await client.fetch_live(["m1", "m2"])
            return states

    states = asyncio.run(run())
    assert len(requests) == 1
    assert requests[0].url.params.get_list("milestone_ids") == ["m1", "m2"]
    assert states[0].milestone_id == "m1" and states[0].league == "UNKNOWN"
    assert len(logged) == 1 and logged[0][0] == content
    assert logged[0][2:4] == (NOW, 3.5)
    assert logged[0][4:6] == ("/live_data/batch", 200)
    assert logged[0][6] == {"etag": "abc"}


def test_fetch_live_chunks_at_documented_batch_limit():
    requested = []

    def handler(request):
        ids = request.url.params.get_list("milestone_ids")
        requested.append(ids)
        return httpx.Response(
            200,
            json={
                "live_datas": [
                    {"type": "basketball_game", "milestone_id": mid, "details": {"status": "live"}}
                    for mid in ids
                ]
            },
        )

    async def run():
        async with make_http(handler) as http:
            return await KalshiLiveClient(http).fetch_live([f"m{i}" for i in range(205)])

    states = asyncio.run(run())
    assert [len(batch) for batch in requested] == [100, 100, 5]
    assert len(states) == 205
