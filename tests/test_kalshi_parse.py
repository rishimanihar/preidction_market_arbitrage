import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pmarb.models import Venue
from pmarb.venues.kalshi.parse import is_eligible, parse_book, parse_market

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"
NOW = datetime(2030, 1, 5, tzinfo=UTC)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def parse_book_fixture(name):
    return parse_book(
        fixture(name)["orderbook_fp"],
        ticker="SYNTHETIC",
        received_utc=NOW,
        received_monotonic=123.5,
        connection_generation=7,
        cache_headers={},
    )


def test_every_market_fixture_parses_and_retains_expected_metadata():
    series = fixture("series_KXNFLGAME.json")["series"]
    markets = fixture("markets_page_1.json")["markets"] + fixture("markets_page_2.json")["markets"]
    parsed = [parse_market(raw, series, None) for raw in markets]
    assert len(parsed) == len(markets)
    assert all(m.venue is Venue.KALSHI and m.league == "nfl" for m in parsed)
    assert all(
        m.rules_text == raw["rules_primary"] + raw["rules_secondary"]
        for m, raw in zip(parsed, markets)
    )


def test_market_event_and_expected_resolution_fields():
    raw = fixture("markets_page_1.json")["markets"][0]
    series = fixture("series_KXNFLGAME.json")["series"]
    market = parse_market(raw, series, None)
    assert market.event_start_utc == datetime(2030, 1, 7, tzinfo=UTC)
    assert market.expected_resolution_utc == datetime(2030, 1, 7, tzinfo=UTC)
    assert market.expected_resolution_src == "kalshi_expected_expiration_time"
    assert market.expected_payout_utc is None
    assert market.participants == ()
    assert market.rules_hash == ""
    assert market.fee_meta["fee_type"] == series["fee_type"]
    assert market.fee_meta["fee_multiplier"] == str(series["fee_multiplier"])
    assert market.fee_meta["contract_terms_url"] == series["contract_terms_url"]
    assert json.loads(market.fee_meta["settlement_sources"]) == series["settlement_sources"]
    assert market.raw["yes_sub_title"] == raw["yes_sub_title"]
    event_market = parse_market(raw, series, {"occurrence_datetime": "2030-01-08T00:00:00Z"})
    assert event_market.event_start_utc == datetime(2030, 1, 8, tzinfo=UTC)
    no_occurrence = {k: v for k, v in raw.items() if k != "occurrence_datetime"}
    market = parse_market(no_occurrence, series, None)
    assert market.event_start_utc is None
    assert market.raw["expected_expiration_time"] == raw["expected_expiration_time"]
    assert "close_time" not in market.raw or market.raw["close_time"] == raw["close_time"]


@pytest.mark.parametrize(
    ("ticker", "league"),
    [
        ("KXNFLGAME", "nfl"),
        ("KXNBAGAME", "nba"),
        ("KXNHLGAME", "nhl"),
        ("KXNCAAFGAME", "ncaaf"),
        ("KXNCAAMBGAME", "ncaamb"),
        ("KXNCAAWBGAME", "ncaawb"),
    ],
)
def test_supported_series_map_to_league(ticker, league):
    raw = fixture("markets_page_1.json")["markets"][0]
    assert parse_market(raw, {"ticker": ticker}).league == league


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market_type", "scalar"),
        ("notional_value_dollars", "2.0000"),
        ("settlement_bounds_type", "custom"),
        ("mve_collection_ticker", "SYNTHETIC-COMBO"),
        ("status", "closed"),
    ],
)
def test_eligibility_rejects_out_of_scope_market(field, value):
    raw = fixture("markets_page_1.json")["markets"][0]
    series = fixture("series_KXNFLGAME.json")["series"]
    assert is_eligible(raw, series)[0]
    assert not is_eligible({**raw, field: value}, series)[0]


def test_eligibility_rejects_unknown_settlement_bounds_and_unknown_series():
    raw = fixture("markets_page_1.json")["markets"][0]
    series = fixture("series_KXNFLGAME.json")["series"]
    unknown_bounds = {k: v for k, v in raw.items() if k != "settlement_bounds_type"}
    assert not is_eligible(unknown_bounds, series)[0]
    assert not is_eligible(raw, {**series, "ticker": "KXOTHER"})[0]


def test_all_orderbook_fixtures_parse():
    for name in (
        "orderbook_ascending.json",
        "orderbook_descending.json",
        "orderbook_empty_side.json",
    ):
        parse_book_fixture(name)


def test_bid_complements_sorting_and_fractional_sizes():
    ascending = parse_book_fixture("orderbook_ascending.json")
    descending = parse_book_fixture("orderbook_descending.json")
    assert ascending.valid and descending.valid
    assert ascending.yes_asks == descending.yes_asks
    assert ascending.no_asks == descending.no_asks
    assert [(x.price, x.size) for x in ascending.yes_asks] == [
        (Decimal("0.48"), Decimal("80.00")),
        (Decimal("0.50"), Decimal("60.25")),
        (Decimal("0.60"), Decimal("15.00")),
    ]
    assert [(x.price, x.size) for x in ascending.no_asks] == [
        (Decimal("0.55"), Decimal("120.00")),
        (Decimal("0.60"), Decimal("25.50")),
        (Decimal("0.70"), Decimal("10.00")),
    ]


def test_empty_side_is_valid_and_remains_empty():
    book = parse_book_fixture("orderbook_empty_side.json")
    assert book.valid
    assert book.yes_asks == []
    assert book.no_asks


def test_missing_and_null_sides_are_empty_ladders():
    for payload in ({"yes_dollars": []}, {"yes_dollars": None, "no_dollars": None}):
        book = parse_book(
            payload,
            ticker="T",
            received_utc=NOW,
            received_monotonic=1.0,
            connection_generation=0,
            cache_headers={},
        )
        assert book.valid
        assert book.yes_asks == [] and book.no_asks == []


def test_cached_and_crossed_books_are_invalid():
    raw = fixture("orderbook_ascending.json")["orderbook_fp"]
    cached = parse_book(
        raw,
        ticker="T",
        received_utc=NOW,
        received_monotonic=1.0,
        connection_generation=0,
        cache_headers={"x-kalshi-cache-hits": "1"},
    )
    assert not cached.valid and cached.invalid_reason == "cached_response"
    quoted_case_mixed = parse_book(
        raw,
        ticker="T",
        received_utc=NOW,
        received_monotonic=1.0,
        connection_generation=0,
        cache_headers={"X-Kalshi-Cache-Hits": "0", "cAcHe-CoNtRoL": 'public, max-age="15"'},
    )
    assert not quoted_case_mixed.valid and quoted_case_mixed.invalid_reason == "cached_response"
    max_age = parse_book(
        raw,
        ticker="T",
        received_utc=NOW,
        received_monotonic=1.0,
        connection_generation=0,
        cache_headers={"Cache-Control": "public, max-age=15"},
    )
    assert not max_age.valid
    crossed = parse_book(
        {"yes_dollars": [["0.6000", "1.00"]], "no_dollars": [["0.5000", "1.00"]]},
        ticker="T",
        received_utc=NOW,
        received_monotonic=1.0,
        connection_generation=0,
        cache_headers={},
    )
    assert not crossed.valid


@pytest.mark.parametrize(
    "level",
    [
        ["bad", "1.00"],
        ["0.2000", "bad"],
        ["-0.1000", "1.00"],
        ["0.2000", "-1.00"],
        ["NaN", "1.00"],
        ["0.2000", "Infinity"],
        ["1.0000", "1.00"],
        ["0.2000"],
    ],
)
def test_malformed_negative_and_nonfinite_levels_invalidate_book(level):
    book = parse_book(
        {"yes_dollars": [level], "no_dollars": []},
        ticker="T",
        received_utc=NOW,
        received_monotonic=1.0,
        connection_generation=0,
        cache_headers={},
    )
    assert not book.valid
