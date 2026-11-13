"""Taker fee models: Kalshi accumulator plus one step, Polymarket US order cap."""

from decimal import Decimal

import pytest

from pmarb.fees import KalshiFeeModel, PolymarketUSFeeModel
from pmarb.fees.kalshi import DEFAULT_PROVENANCE
from pmarb.fees.kalshi_rounding import (
    DIRECT_PRECISION,
    NON_DIRECT_PRECISION,
    TAKER_COEFFICIENT,
    OrderFeeAccumulator,
)
from pmarb.fees.polymarket_us import FEE_URL
from pmarb.models import Level, Side

DIRECT = DIRECT_PRECISION


def _level(price: str, size: str) -> Level:
    return Level(price=Decimal(price), size=Decimal(size))


def _charged(levels: list[Level], precision: Decimal = DIRECT) -> Decimal:
    """Documented per-order accumulator, then the one-step margin."""
    accumulator = OrderFeeAccumulator(precision)
    for level in levels:
        accumulator.apply_fill(level.price, level.size, True, Decimal(1))
    return accumulator.total_net_fee + precision


def test_default_coefficient_matches_the_fee_schedule() -> None:
    assert KalshiFeeModel().coefficient == Decimal("0.07")
    assert KalshiFeeModel().coefficient == TAKER_COEFFICIENT
    assert KalshiFeeModel().provenance_base == DEFAULT_PROVENANCE


def test_worked_fill_is_the_accumulator_plus_one_precision_step() -> None:
    # 0.07 * 100 * 0.45 * 0.55 = 1.7325 exactly.
    # Revenue -45 is on the 0.0001 grid, so the rounding fee is 0 and the
    # accumulator's net fee stays 1.7325. The conservative margin adds one step.
    levels = [_level("0.45", "100")]
    model = KalshiFeeModel(multiplier=Decimal(1), precision=DIRECT)
    yes = model.taker_fee(levels, Side.YES)
    no = model.taker_fee(levels, Side.NO)
    assert yes.amount == Decimal("1.7326")
    assert yes.amount == _charged(levels)
    assert no.amount == yes.amount
    assert yes.conservative_bound is True
    assert "coefficient=0.07" in yes.provenance
    assert "multiplier=1" in yes.provenance
    assert "precision=0.0001" in yes.provenance
    assert "per-level fills + 1 step margin" in yes.provenance


def test_each_level_is_one_fill_rather_than_the_vwap() -> None:
    # 0.10 x 50 and 0.90 x 50: each exact fee is 0.315, both revenues sit on
    # the 0.0001 grid, total net fee 0.630, plus one step = 0.6301.
    # One fill at 0.50 for the same 100 contracts is 1.75 plus one step.
    per_level = [_level("0.10", "50"), _level("0.90", "50")]
    vwap = [_level("0.50", "100")]
    model = KalshiFeeModel(multiplier=Decimal(1), precision=DIRECT)
    assert model.taker_fee(per_level, Side.YES).amount == Decimal("0.6301")
    assert model.taker_fee(per_level, Side.YES).amount == _charged(per_level)
    assert model.taker_fee(vwap, Side.YES).amount == Decimal("1.7501")


def test_no_side_uses_the_level_price() -> None:
    # p*(1-p) at 0.40 equals p*(1-p) at 0.60. YES and NO at the same price
    # match because the NO ladder is already the NO acquisition price.
    model = KalshiFeeModel(multiplier=Decimal(1), precision=DIRECT)
    at_forty = _level("0.40", "10")
    at_sixty = _level("0.60", "10")
    yes = model.taker_fee([at_forty], Side.YES).amount
    no = model.taker_fee([at_forty], Side.NO).amount
    complement = model.taker_fee([at_sixty], Side.YES).amount
    assert yes == no == complement
    # 0.07 * 10 * 0.40 * 0.60 = 0.168. Revenue -4 is on the grid, plus 0.0001.
    assert yes == Decimal("0.1681")


def test_direct_member_examples_include_the_margin() -> None:
    model = KalshiFeeModel(multiplier=Decimal(1), precision=DIRECT)
    # 0.07 * 0.2079 * 0.5 * 0.5 = 0.00363825. After alignment the net fee is
    # 0.00365, plus one 0.0001 step.
    assert model.taker_fee([_level("0.50", "0.2079")], Side.YES).amount == Decimal("0.00375")
    # 0.07 * 7.052 * 0.25 = 0.12341. Net fee after alignment is 0.12350, plus one step.
    assert model.taker_fee([_level("0.50", "7.052")], Side.YES).amount == Decimal("0.1236")
    # 0.07 * 3 * 0.33 * 0.67 = 0.046431. Net fee 0.046500, plus one step.
    assert model.taker_fee([_level("0.33", "3")], Side.YES).amount == Decimal("0.0466")


def test_non_direct_two_fills_rebate_then_add_one_cent() -> None:
    # Fills at 0.10 and 0.20. The accumulator's total net fee is 0.02
    # (a 0.01 rebate on the second fill). Non-direct precision then adds 0.01.
    levels = [_level("0.10", "1"), _level("0.20", "1")]
    model = KalshiFeeModel(multiplier=Decimal(1), precision=NON_DIRECT_PRECISION)
    assert model.taker_fee(levels, Side.YES).amount == Decimal("0.03")
    assert model.taker_fee(levels, Side.YES).amount == _charged(levels, NON_DIRECT_PRECISION)


def test_zero_coefficient_on_grid_is_only_the_margin() -> None:
    model = KalshiFeeModel(coefficient=Decimal(0), multiplier=Decimal(1), precision=DIRECT)
    quote = model.taker_fee([_level("0.10", "1")], Side.YES)
    assert quote.amount == DIRECT
    assert quote.conservative_bound is True


def test_unknown_kalshi_inputs_name_what_is_missing() -> None:
    levels = [_level("0.45", "1")]
    missing_multiplier = KalshiFeeModel(multiplier=None, precision=DIRECT)
    quote = missing_multiplier.taker_fee(levels, Side.YES)
    assert quote.amount is None
    assert quote.conservative_bound is False
    assert quote.provenance == f"{DEFAULT_PROVENANCE}; unknown: multiplier"

    missing_all = KalshiFeeModel(coefficient=None)
    assert missing_all.taker_fee(levels, Side.NO).provenance.endswith(
        "unknown: coefficient, multiplier, precision"
    )


def test_kalshi_rejects_bad_numbers() -> None:
    with pytest.raises(TypeError):
        KalshiFeeModel(multiplier=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        KalshiFeeModel(multiplier=Decimal(-1), precision=DIRECT)
    with pytest.raises(ValueError):
        KalshiFeeModel(multiplier=Decimal(1), precision=Decimal(0))
    with pytest.raises(ValueError):
        KalshiFeeModel(multiplier=Decimal("NaN"), precision=DIRECT)
    with pytest.raises(TypeError):
        KalshiFeeModel(multiplier=Decimal(1), precision=DIRECT).taker_fee(
            [_level("0.40", "1")],
            "yes",  # type: ignore[arg-type]
        )


def test_polymarket_order_cap_half_even_to_the_cent() -> None:
    model = PolymarketUSFeeModel(theta=Decimal("0.095"))
    # 0.095 * 100 * 0.50 * 0.50 = 2.375, halfway from 2.37 to 2.38.
    # The hundredths digit 7 is odd, so half-even rounds to 2.38.
    quote = model.taker_fee([_level("0.50", "100")], Side.NO)
    assert quote.amount == Decimal("2.38")
    assert quote.conservative_bound is True
    assert "theta=0.095" in quote.provenance
    assert FEE_URL in quote.provenance


def test_polymarket_cap_can_bind_below_per_fill_rounding() -> None:
    # theta 0.0695, two 1-lots at 0.50. Exact per fill 0.017375 rounds to 0.02
    # (0.04 together). The exact sum 0.03475 rounds to the 0.03 order cap.
    model = PolymarketUSFeeModel(theta=Decimal("0.0695"))
    quote = model.taker_fee([_level("0.50", "1"), _level("0.50", "1")], Side.YES)
    assert quote.amount == Decimal("0.03")


def test_polymarket_shape_is_symmetric_for_yes_and_no() -> None:
    model = PolymarketUSFeeModel(theta=Decimal("0.095"))
    # 0.095 * 10 * 0.40 * 0.60 = 0.228, which rounds to 0.23.
    acquisition = model.taker_fee([_level("0.40", "10")], Side.NO).amount
    native = model.taker_fee([_level("0.60", "10")], Side.YES).amount
    same_price = model.taker_fee([_level("0.40", "10")], Side.YES).amount
    assert acquisition == native == same_price == Decimal("0.23")


def test_polymarket_theta_allow_list_and_unknown() -> None:
    for raw in ("0.095", "0.085", "0.075", "0.0695"):
        PolymarketUSFeeModel(theta=Decimal(raw))
    with pytest.raises(ValueError, match="0.0695"):
        PolymarketUSFeeModel(theta=Decimal("0.07"))
    with pytest.raises(TypeError):
        PolymarketUSFeeModel(theta=0.095)  # type: ignore[arg-type]
    unknown = PolymarketUSFeeModel(theta=None).taker_fee([_level("0.50", "1")], Side.YES)
    assert unknown.amount is None
    assert unknown.conservative_bound is False
    assert unknown.provenance == f"polymarket-us theta unknown; {FEE_URL}"
