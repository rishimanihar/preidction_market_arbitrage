"""Decimal preservation and model shape."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pmarb.models import Assessment, Book, LegFill, Level, Market, PairReview, Side, Venue


def test_level_keeps_decimal_and_rejects_float() -> None:
    level = Level(price=Decimal("0.45"), size=Decimal(10))
    dumped = level.model_dump()
    assert dumped["price"] == Decimal("0.45")
    assert type(dumped["price"]) is Decimal
    assert type(dumped["size"]) is Decimal

    with pytest.raises(TypeError, match="float is not permitted"):
        Level(price=0.45, size=Decimal(10))
    with pytest.raises(ValidationError):
        Level(price=Decimal(0), size=Decimal(1))
    with pytest.raises(ValidationError):
        Level(price=Decimal(1), size=Decimal(1))
    with pytest.raises(ValidationError):
        Level(price=Decimal("0.40"), size=Decimal(0))


def test_level_is_frozen() -> None:
    level = Level(price=Decimal("0.45"), size=Decimal(10))
    with pytest.raises(ValidationError):
        level.price = Decimal("0.40")


def test_market_and_pair_review_store_rules_and_mapping() -> None:
    market = Market(
        venue=Venue.KALSHI,
        market_id="KX-1",
        title="Example",
        market_type="winner",
        status="open",
        rules_text="Rules text",
        rules_hash="abc",
        url="https://kalshi.com/markets/KX-1",
        fee_meta={"multiplier": "1"},
        raw={"ticker": "KX-1"},
    )
    assert market.expected_resolution_utc is None
    assert market.expected_payout_utc is None
    assert market.fee_meta["multiplier"] == "1"
    assert market.market_key == "kalshi:KX-1"

    approved_at = datetime(2026, 11, 11, tzinfo=UTC)
    review = PairReview(
        pair_id="pair-1",
        kalshi_market_id="KX-1",
        polymarket_market_id="pm-1",
        outcome_mapping={"YES": "YES", "NO": "NO"},
        approved=True,
        reviewer="ada",
        notes="Binary $1/$0, same resolution source.",
        kalshi_rules_hash="abc",
        polymarket_rules_hash="def",
        approved_utc=approved_at,
    )
    assert review.outcome_mapping == {"YES": "YES", "NO": "NO"}
    assert review.approved_utc == approved_at


def test_book_accepts_monotonic_clock_float() -> None:
    book = Book(
        venue=Venue.POLYMARKET_US,
        market_id="pm-1",
        yes_asks=[Level(price=Decimal("0.40"), size=Decimal(1))],
        no_asks=[],
        raw_native={"bids": []},
        source_ts=None,
        received_utc=datetime(2026, 11, 11, tzinfo=UTC),
        received_monotonic=12.5,
        connection_generation=3,
        valid=True,
        invalid_reason=None,
    )
    assert book.received_monotonic == 12.5
    assert isinstance(book.received_monotonic, float)


def test_qualified_assessment_requires_priced_legs() -> None:
    leg = LegFill(
        venue=Venue.KALSHI,
        side=Side.YES,
        levels_consumed=[Level(price=Decimal("0.45"), size=Decimal(1))],
        quantity=Decimal(1),
        cost=Decimal("0.45"),
        avg_price=Decimal("0.45"),
        fee=Decimal("0.0173"),
    )
    other = LegFill(
        venue=Venue.POLYMARKET_US,
        side=Side.NO,
        levels_consumed=[Level(price=Decimal("0.50"), size=Decimal(1))],
        quantity=Decimal(1),
        cost=Decimal("0.50"),
        avg_price=Decimal("0.50"),
        fee=Decimal("0.02"),
    )
    with pytest.raises(ValidationError):
        Assessment(
            pair_id="pair-1",
            direction="A",
            requested_qty=Decimal(1),
            legs=None,
            capital=None,
            net_profit=None,
            roc=None,
            extra_costs=Decimal(0),
            qualified=True,
            rejection_reasons=[],
            max_profitable_qty=None,
            fee_model_version="v",
            computed_utc=datetime(2026, 11, 11, tzinfo=UTC),
        )
    assessment = Assessment(
        pair_id="pair-1",
        direction="A",
        requested_qty=Decimal(1),
        legs=(leg, other),
        capital=Decimal(1),
        net_profit=Decimal(0),
        roc=Decimal(0),
        extra_costs=Decimal(0),
        qualified=False,
        rejection_reasons=["not_profitable"],
        max_profitable_qty=None,
        fee_model_version="v",
        computed_utc=datetime(2026, 11, 11, tzinfo=UTC),
    )
    assert type(assessment.net_profit) is Decimal
