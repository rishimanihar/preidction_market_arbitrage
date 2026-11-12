"""Kalshi and Polymarket US acquisition-ladder normalization."""

from decimal import Decimal

from pmarb.ladder import walk
from pmarb.models import Level
from pmarb.normalize import kalshi_asks_from_bids, polyus_asks


def _level(price: str, size: str) -> Level:
    return Level(price=Decimal(price), size=Decimal(size))


def test_kalshi_bids_become_asks_once_and_ascending() -> None:
    yes_bids = [_level("0.40", "10"), _level("0.20", "7")]
    no_bids = [_level("0.70", "3"), _level("0.10", "4")]
    yes_asks, no_asks = kalshi_asks_from_bids(yes_bids, no_bids)

    assert yes_asks == [_level("0.30", "3"), _level("0.90", "4")]
    assert no_asks == [_level("0.60", "10"), _level("0.80", "7")]
    # Complementing the ask again would recover the bid. The helper stops once.
    assert yes_asks[0].price == Decimal(1) - Decimal("0.70")
    assert yes_asks[0].price != no_bids[0].price

    consumed, cost = walk(yes_asks, Decimal(3))
    assert consumed == [_level("0.30", "3")]
    assert cost == Decimal("0.90")


def test_kalshi_merges_equal_complemented_prices() -> None:
    yes_asks, no_asks = kalshi_asks_from_bids(
        [],
        [_level("0.25", "4"), _level("0.25", "6")],
    )
    assert yes_asks == [_level("0.75", "10")]
    assert no_asks == []


def test_polyus_yes_offers_and_no_from_yes_bids() -> None:
    yes_asks, no_asks = polyus_asks(
        [_level("0.62", "8"), _level("0.41", "2")],
        [_level("0.55", "9")],
    )
    assert yes_asks == [_level("0.41", "2"), _level("0.62", "8")]
    assert no_asks == [_level("0.45", "9")]
    assert yes_asks[0].price == Decimal("0.41")
