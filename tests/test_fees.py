"""Per-level venue fees and rounding."""

from decimal import Decimal

import pytest

from pmarb.fees import KALSHI_TAKER_COEFFICIENT, kalshi_taker_fee, polyus_taker_fee
from pmarb.models import Level


def _level(price: str, size: str) -> Level:
    return Level(price=Decimal(price), size=Decimal(size))


def test_fees_use_each_level_not_vwap() -> None:
    levels = [_level("0.10", "50"), _level("0.90", "50")]
    per_level = kalshi_taker_fee(levels, Decimal(1), "direct_member")
    vwap = kalshi_taker_fee([_level("0.50", "100")], Decimal(1), "direct_member")
    assert per_level == Decimal("0.63")
    assert vwap == Decimal("1.75")
    assert per_level != vwap


def test_kalshi_direct_member_and_round_up_cent_accumulator() -> None:
    assert KALSHI_TAKER_COEFFICIENT == Decimal("0.07")
    # Exact 1.7325 is already on the centicent grid, so ceiling leaves it.
    worked = [_level("0.45", "100")]
    assert kalshi_taker_fee(worked, Decimal(1), "direct_member") == Decimal("1.7325")
    assert kalshi_taker_fee(worked, Decimal(1), "round_up_cent") == Decimal("1.74")

    # Exact 0.00363825 ceilings to the next centicent, 0.0037.
    # 0.07 * 0.2079 * 0.5 * 0.5 = 0.00363825.
    sub_centicent = kalshi_taker_fee([_level("0.50", "0.2079")], Decimal(1), "direct_member")
    assert sub_centicent == Decimal("0.0037")

    # Exact 0.12341 ceilings to 0.1235. 0.07 * 7.052 * 0.5 * 0.5 = 0.12341.
    off_grid = kalshi_taker_fee([_level("0.50", "7.052")], Decimal(1), "direct_member")
    assert off_grid == Decimal("0.1235")

    # Exact 0.046431 ceilings to 0.0465 (half-even would have been 0.0464).
    sub_cent = kalshi_taker_fee([_level("0.33", "3")], Decimal(1), "direct_member")
    assert sub_cent == Decimal("0.0465")

    # 0.0063 ceils to 0.01 and 0.0112 ceils to 0.02 (sum 0.03) if rounded
    # per fill. The accumulator ceils the exact 0.0175 total once, to 0.02.
    fills = [_level("0.10", "1"), _level("0.20", "1")]
    assert kalshi_taker_fee(fills, Decimal(1), "round_up_cent") == Decimal("0.02")


def test_polyus_half_even_rounds_the_order_total_once() -> None:
    half_up = polyus_taker_fee([_level("0.50", "100")], Decimal("0.095"))
    assert half_up == Decimal("2.38")

    also_even = polyus_taker_fee([_level("0.50", "1")], Decimal("9.54"))
    assert also_even == Decimal("2.38")

    # Each fill is exact 0.015 (0.06 * 1 * 0.5 * 0.5). Half-even per fill
    # would be 0.02 + 0.02 = 0.04. The order total 0.03 is rounded once.
    fills = [_level("0.50", "1"), _level("0.50", "1")]
    assert polyus_taker_fee(fills, Decimal("0.06")) == Decimal("0.03")


def test_polyus_no_fee_matches_native_yes_bid_because_shape_is_symmetric() -> None:
    """NO fee price is the native YES bid, and p*(1-p) equals (1-p)*p."""
    theta = Decimal("0.095")
    acquisition = polyus_taker_fee([_level("0.40", "10")], theta)
    native_yes_bid = polyus_taker_fee([_level("0.60", "10")], theta)
    assert acquisition == native_yes_bid
    assert acquisition == Decimal("0.23")


def test_fee_coefficients_must_be_finite() -> None:
    with pytest.raises(ValueError):
        kalshi_taker_fee([_level("0.40", "1")], Decimal("NaN"))
    with pytest.raises(ValueError):
        polyus_taker_fee([_level("0.40", "1")], Decimal("NaN"))


def test_fee_inputs_reject_floats() -> None:
    with pytest.raises(TypeError):
        kalshi_taker_fee([_level("0.40", "1")], 1.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        polyus_taker_fee([_level("0.40", "1")], 0.095)  # type: ignore[arg-type]
