import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pmarb.models import Venue
from pmarb.venues.polymarket_us.parse import is_eligible, parse_book, parse_market

FIXTURES = Path(__file__).parent / "fixtures" / "polymarket_us"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_market_fixture_and_ignore_deprecated_prices():
    raw = fixture("market_by_slug.json")["market"]
    market = parse_market(raw)
    assert market.venue is Venue.POLYMARKET_US
    assert market.market_id == raw["slug"]
    assert market.yes_participant == "Team Alpha"
    assert market.rules_text == raw["description"]
    assert market.rules_hash == ""
    assert market.expected_payout_utc is None
    assert market.event_start_utc is None  # market.startTime/endDate are not event start
    assert market.fee_meta["order_price_min_tick_size"] == "0.01"
    altered = {**raw, "outcomePrices": '["0.9999","0.0001"]'}
    assert parse_market(altered).yes_participant == market.yes_participant


def test_market_event_context_precedes_game_start_time_and_keeps_tick_decimal():
    raw = fixture("market_by_slug.json")["market"]
    raw["gameStartTime"] = "2030-01-05T00:00:00Z"
    raw["_eventStartTime"] = "2030-01-06T00:00:00Z"
    raw["_eventLeagueSlug"] = "nfl"
    m = parse_market(raw)
    assert m.event_start_utc == datetime(2030, 1, 6, tzinfo=UTC)
    assert m.league == "nfl"
    raw["orderPriceMinTickSize"] = 0.0025
    assert parse_market(raw).fee_meta["order_price_min_tick_size"] == "0.0025"


def test_game_start_fallback_and_v2_only_eligibility():
    raw = fixture("market_by_slug.json")["market"]
    raw.pop("startTime", None)
    raw["gameStartTime"] = "2030-01-05T00:00:00Z"
    raw["sportsMarketTypeV2"] = "SPORTS_MARKET_TYPE_MONEYLINE"
    raw.pop("sportsMarketType", None)
    assert parse_market(raw).event_start_utc == datetime(2030, 1, 5, tzinfo=UTC)
    assert is_eligible(raw)[0]


def test_market_event_time_is_normalized_to_utc():
    raw = fixture("market_by_slug.json")["market"]
    raw["_eventStartTime"] = "2030-01-06T06:00:00+02:00"
    assert parse_market(raw).event_start_utc == datetime(2030, 1, 6, 4, tzinfo=UTC)


def test_market_type_disagreement_is_preserved_in_metadata():
    raw = fixture("market_by_slug.json")["market"]
    raw["sportsMarketTypeV2"] = "SPORTS_MARKET_TYPE_MONEYLINE"
    raw["sportsMarketType"] = "futures"
    market = parse_market(raw)
    assert market.market_type == "winner"
    assert "market_type_disagreement" in market.fee_meta


def test_every_market_fixture_parses():
    for raw in fixture("markets_list.json")["markets"]:
        parse_market(raw)


def test_eligibility_rejects_resolved_and_malformed_sides_but_not_combo_enabled():
    raw = fixture("market_by_slug.json")["market"]
    assert is_eligible(raw)[0]
    resolved = {**raw, "status": "MARKET_STATUS_RESOLVED", "active": True}
    assert "market_not_open" in is_eligible(resolved)[1]
    # comboEnabled means "usable as a combo leg", not "is a combo" (live
    # game-winner markets all have it set); it must not disqualify.
    for combo_flag in (True, False):
        assert is_eligible({**raw, "comboEnabled": combo_flag})[0]
    malformed = {**raw, "marketSides": raw["marketSides"][:1]}
    assert "not_single_instrument" in is_eligible(malformed)[1]
    for sides in (
        [None, raw["marketSides"][1]],
        [dict(raw["marketSides"][0], long=False), raw["marketSides"][1]],
    ):
        assert "not_single_instrument" in is_eligible({**raw, "marketSides": sides})[1]
    assert "market_closed" in is_eligible({k: v for k, v in raw.items() if k != "closed"})[1]


def test_no_asks_are_complemented_from_bids_and_inputs_sorted():
    raw = fixture("book.json")
    data = raw["marketData"]
    data["offers"] = list(reversed(data["offers"]))
    data["bids"] = list(reversed(data["bids"]))
    book = parse_book(raw, datetime(2030, 1, 6, tzinfo=UTC), 1.0, 3)
    assert book.valid
    assert [x.price for x in book.yes_asks] == [
        Decimal("0.4600"),
        Decimal("0.4700"),
        Decimal("0.4800"),
    ]
    assert [x.price for x in book.no_asks] == [
        Decimal("0.5500"),
        Decimal("0.5600"),
        Decimal("0.5700"),
    ]
    assert book.no_asks[0].size == Decimal("100.0000")


def test_quiet_book_old_source_time_remains_valid_and_raw_nanoseconds_retained():
    raw = fixture("book_quiet_old_transacttime.json")
    book = parse_book(raw, datetime(2030, 1, 6, tzinfo=UTC), 2.0, 4)
    assert book.valid
    assert book.source_ts.microsecond == 0
    assert book.raw_native["marketData"]["transactTime"].endswith("000000001Z")


def test_expired_and_crossed_books_fail_closed():
    now = datetime(2030, 1, 6, tzinfo=UTC)
    expired = parse_book(fixture("book_expired_state.json"), now, 2.0, 0)
    assert not expired.valid
    assert expired.invalid_reason == "book_state_MARKET_STATE_EXPIRED"
    crossed_raw = fixture("book.json")
    crossed_raw["marketData"]["offers"][0]["px"]["value"] = "0.4400"
    crossed = parse_book(crossed_raw, now, 3.0, 0)
    assert not crossed.valid
    assert crossed.invalid_reason == "crossed_book"


def test_malformed_book_levels_fail_closed():
    raw = {
        "marketData": {
            "marketSlug": "bad",
            "state": "MARKET_STATE_OPEN",
            "offers": [{"px": {"value": "NaN"}, "qty": "1"}],
            "bids": [],
        }
    }
    book = parse_book(raw, datetime(2030, 1, 6, tzinfo=UTC), 0.0, 0)
    assert not book.valid
    assert book.yes_asks == []


def test_malformed_missing_arrays_and_out_of_range_values_fail_closed():
    now = datetime(2030, 1, 6, tzinfo=UTC)
    incomplete = {"marketData": {"state": "MARKET_STATE_OPEN", "offers": []}}
    assert not parse_book(incomplete, now, 0.0, 0).valid
    for px, qty in [("0", "1"), ("1", "1"), ("0.5", "0"), ("NaN", "1")]:
        malformed = {
            "marketData": {
                "marketSlug": "bad",
                "state": "MARKET_STATE_OPEN",
                "offers": [{"px": {"value": px}, "qty": qty}],
                "bids": [],
            }
        }
        assert not parse_book(malformed, now, 0.0, 0).valid


def test_empty_open_book_is_valid_and_malformed_wrappers_or_timestamps_fail_closed():
    now = datetime(2030, 1, 6, tzinfo=UTC)
    empty = {
        "marketData": {
            "marketSlug": "empty",
            "state": "MARKET_STATE_OPEN",
            "offers": [],
            "bids": [],
        }
    }
    parsed_empty = parse_book(empty, now, 0.0, 0)
    assert parsed_empty.valid
    for malformed in (
        {"marketData": None},
        {
            "marketData": {
                "marketSlug": "bad",
                "state": "MARKET_STATE_OPEN",
                "offers": [],
                "bids": [],
                "transactTime": "bad",
            }
        },
    ):
        parsed = parse_book(malformed, now, 0.0, 0)
        assert not parsed.valid
        assert parsed.source_ts is None
        assert parsed.raw_native == malformed
