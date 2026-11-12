"""Acquisition-ladder walking."""

from decimal import Decimal

import pytest

from pmarb.ladder import InsufficientDepth, InvalidLadder, walk
from pmarb.models import Level


def _level(price: str, size: str) -> Level:
    return Level(price=Decimal(price), size=Decimal(size))


def test_walks_across_multiple_levels() -> None:
    levels = [_level("0.40", "40"), _level("0.50", "60")]
    consumed, cost = walk(levels, Decimal(50))
    assert consumed == [_level("0.40", "40"), _level("0.50", "10")]
    assert cost == Decimal(21)
    assert levels[0].size == Decimal(40)


def test_exact_depth_and_fractional_quantity() -> None:
    consumed, cost = walk([_level("0.40", "1")], Decimal("0.5"))
    assert consumed == [_level("0.40", "0.5")]
    assert cost == Decimal("0.20")

    consumed, cost = walk([_level("0.25", "8")], Decimal(8))
    assert consumed == [_level("0.25", "8")]
    assert cost == Decimal(2)


def test_thin_book_raises_insufficient_depth() -> None:
    with pytest.raises(InsufficientDepth) as exc_info:
        walk([_level("0.40", "30")], Decimal(50))
    assert exc_info.value.requested == Decimal(50)
    assert exc_info.value.available == Decimal(30)


def test_rejects_unsorted_equal_and_bad_prices() -> None:
    with pytest.raises(InvalidLadder):
        walk([_level("0.50", "1"), _level("0.40", "1")], Decimal(1))
    with pytest.raises(InvalidLadder):
        walk([_level("0.40", "1"), _level("0.40", "1")], Decimal(1))

    bad_levels = (
        ("0", "1"),
        ("1", "1"),
        ("-0.1", "1"),
        ("1.1", "1"),
        ("0.4", "0"),
        ("0.4", "-1"),
    )
    for price, size in bad_levels:
        bad = Level.model_construct(price=Decimal(price), size=Decimal(size))
        with pytest.raises(InvalidLadder):
            walk([bad], Decimal(1))


def test_rejects_non_finite_quantity_and_levels() -> None:
    levels = [_level("0.40", "1")]
    with pytest.raises(InvalidLadder):
        walk(levels, Decimal("NaN"))
    with pytest.raises(InvalidLadder):
        walk(levels, Decimal("Infinity"))
    nan_price = Level.model_construct(price=Decimal("NaN"), size=Decimal(1))
    infinite_size = Level.model_construct(price=Decimal("0.40"), size=Decimal("Infinity"))
    with pytest.raises(InvalidLadder):
        walk([nan_price], Decimal(1))
    with pytest.raises(InvalidLadder):
        walk([infinite_size], Decimal(1))


def test_rejects_non_positive_or_non_decimal_quantity() -> None:
    levels = [_level("0.40", "1")]
    with pytest.raises(InvalidLadder):
        walk(levels, Decimal(0))
    with pytest.raises(InvalidLadder):
        walk(levels, Decimal(-1))
    with pytest.raises(TypeError):
        walk(levels, 1.5)  # type: ignore[arg-type]
