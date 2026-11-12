"""Tests for ``pmarb.kalshi_fee_rounding``.

Every worked example in https://docs.kalshi.com/getting_started/fee_rounding is encoded here with
the doc text quoted. Numbers that are NOT in the doc are labelled "derived".
"""

import random
from decimal import Decimal as D

import pytest

from pmarb.kalshi_fee_rounding import (
    DIRECT_PRECISION,
    NON_DIRECT_PRECISION,
    SIX_DP,
    OrderFeeAccumulator,
    ceil_6dp,
    ceil_precision,
    floor_precision,
    model_fee,
    rebate_amount,
    rounding_step,
    trade_fee,
)

# ---------------------------------------------------------------------------
# Official example 1: "FCM-cleared fill"
#
# Doc: "For a non-direct member with `$0.01` balance precision, suppose a buy has `-$0.055000`
# of signed revenue and a model fee of `$0.00363825`:
#   trade fee      = ceil_6dp($0.00363825)           =  $0.003639
#   aligned change = floor_cent(-$0.055 - $0.003639) = -$0.060000
#   rounding fee   = (-$0.055 - $0.003639) - -$0.06  =  $0.001361
# The balance changes by `-$0.06`; the trade fee plus rounding fee is exactly `$0.005`."
# ---------------------------------------------------------------------------


def test_official_fcm_cleared_fill_steps() -> None:
    assert ceil_6dp(D("0.00363825")) == D("0.003639")
    assert trade_fee(D("0.00363825")) == D("0.003639")
    assert floor_precision(D("-0.055") - D("0.003639"), NON_DIRECT_PRECISION) == D("-0.060000")

    step = rounding_step(D("-0.055000"), D("0.00363825"), NON_DIRECT_PRECISION)
    assert step.trade_fee == D("0.003639")
    assert step.aligned_change == D("-0.060000")
    assert step.rounding_fee == D("0.001361")
    # "the trade fee plus rounding fee is exactly $0.005"
    assert step.trade_fee + step.rounding_fee == D("0.005")


def test_official_fcm_cleared_fill_through_accumulator() -> None:
    acc = OrderFeeAccumulator(NON_DIRECT_PRECISION)
    charge = acc.apply_revenue(D("-0.055000"), D("0.00363825"))
    assert charge.aligned_change == D("-0.06")
    assert charge.balance_change == D("-0.06")  # "The balance changes by -$0.06"
    assert charge.net_fee == D("0.005")
    assert charge.rebate == 0
    assert charge.accumulator_after == D("0.001361")


# ---------------------------------------------------------------------------
# Official example 2: "Accumulator across fills"
#
# Doc: "If three fills each add `$0.004` of rounding overpayment, the accumulator carries it
# forward. Assuming the third fill has enough total fee to cover a `$0.01` rebate:
#   Fill 1: added 0.004, before rebate 0.004, rebate -,     carried forward 0.004
#   Fill 2: added 0.004, before rebate 0.008, rebate -,     carried forward 0.008
#   Fill 3: added 0.004, before rebate 0.012, rebate 0.010, carried forward 0.002
# This table uses a non-direct member's `$0.01` precision."
#
# The doc gives only the rounding added, so each fill is built (derived) as revenue -$0.05 with
# model fee $0.006: raw = -0.056, floor_cent = -0.06, rounding fee = $0.004, and the fill's own
# fee (0.006 + 0.004 = 0.010) covers a $0.01 rebate.
# ---------------------------------------------------------------------------


def test_official_accumulator_table_non_direct() -> None:
    acc = OrderFeeAccumulator(NON_DIRECT_PRECISION)
    c1 = acc.apply_revenue(D("-0.05"), D("0.006"))
    assert c1.rounding_fee == D("0.004")
    assert (c1.accumulator_before_rebate, c1.rebate, c1.accumulator_after) == (
        D("0.004"),
        D(0),
        D("0.004"),
    )
    c2 = acc.apply_revenue(D("-0.05"), D("0.006"))
    assert (c2.accumulator_before_rebate, c2.rebate, c2.accumulator_after) == (
        D("0.008"),
        D(0),
        D("0.008"),
    )
    c3 = acc.apply_revenue(D("-0.05"), D("0.006"))
    assert c3.rounding_fee == D("0.004")
    assert (c3.accumulator_before_rebate, c3.rebate, c3.accumulator_after) == (
        D("0.012"),
        D("0.010"),
        D("0.002"),
    )
    # Net fee is never negative; here the rebate exactly cancels the fill's fees.
    assert c3.net_fee == D(0)
    assert acc.total_net_fee == D("0.010") + D("0.010")
    assert acc.total_rebate == D("0.010")


def test_direct_member_rebates_use_0001_increments() -> None:
    """Doc: "Direct-member rebates follow the same mechanics in `$0.0001` increments."

    Derived numbers: revenue -$0.05, model fee $0.0000405 -> trade fee $0.000041, raw
    -$0.050041, floor to $0.0001 = -$0.0501, rounding fee $0.000059 on every fill.
    """
    acc = OrderFeeAccumulator(DIRECT_PRECISION)
    c1 = acc.apply_revenue(D("-0.05"), D("0.0000405"))
    assert c1.trade_fee == D("0.000041")
    assert c1.aligned_change == D("-0.0501")
    assert c1.rounding_fee == D("0.000059")
    assert c1.rebate == 0  # 0.000059 < 0.0001
    c2 = acc.apply_revenue(D("-0.05"), D("0.0000405"))
    assert c2.accumulator_before_rebate == D("0.000118")
    assert c2.rebate == D("0.0001")
    assert c2.accumulator_after == D("0.000018")
    assert c2.net_fee == D("0.000041") + D("0.000059") - D("0.0001")  # = 0
    assert c2.net_fee >= 0


# ---------------------------------------------------------------------------
# Claims from the doc prose
# ---------------------------------------------------------------------------


def test_net_fee_is_never_negative_even_with_large_accumulator() -> None:
    """Doc: "Net fee = trade fee + rounding fee - rebate (always >= $0.00)"; rebate "capped so
    the fill's net fee cannot be negative"."""
    acc = OrderFeeAccumulator(NON_DIRECT_PRECISION)
    acc.apply_revenue(D("-0.05"), D("0.006"))
    acc.apply_revenue(D("-0.05"), D("0.006"))  # accumulator 0.008
    # Tiny fee: trade 0.000001 + rounding 0.009999 = 0.01 of fee on this fill.
    c = acc.apply_revenue(D("-0.05"), D("0.0000005"))
    assert c.rounding_fee == D("0.009999")
    assert c.accumulator_before_rebate == D("0.017999")
    assert c.rebate == D("0.01")  # capped at the fill's own 0.01, not the 0.017999 available
    assert c.net_fee == 0
    assert c.accumulator_after == D("0.007999")


def test_rebate_capped_below_one_grid_step_gives_zero() -> None:
    # Fill fee 0.005 < one cent: floor to the grid is 0 even though 0.009 is accumulated.
    assert rebate_amount(D("0.009"), D("0.005"), NON_DIRECT_PRECISION) == 0
    assert rebate_amount(D("0.012"), D("0.5"), NON_DIRECT_PRECISION) == D("0.01")
    assert rebate_amount(D(0), D("0.5"), NON_DIRECT_PRECISION) == 0


def test_aligned_change_is_on_the_precision_grid() -> None:
    """Doc: "Before any rebate, the user's balance changes by `aligned_change`, which is always
    on the user's precision grid."""
    rng = random.Random(7)
    for precision in (DIRECT_PRECISION, NON_DIRECT_PRECISION):
        for _ in range(500):
            revenue = -D(rng.randint(1, 10_000_000)) * SIX_DP
            fee = D(rng.randint(0, 50_000_000)) * SIX_DP / 1000
            step = rounding_step(revenue, fee, precision)
            assert step.aligned_change % precision == 0
            assert 0 <= step.rounding_fee < precision


def test_accumulator_carries_from_taker_to_maker_fills() -> None:
    """Doc: "The fee accumulator is maintained per order across all fills regardless of whether
    the fills are taker or maker ... the accumulated rounding carries over to subsequent maker
    fills."""
    acc = OrderFeeAccumulator(NON_DIRECT_PRECISION)
    taker = acc.apply_fill(D("0.50"), D(10), is_taker=True)
    maker = acc.apply_fill(D("0.50"), D(10), is_taker=False)
    # taker model fee 0.07 * 10 * .25 = 0.175, maker 0.0175 * 10 * .25 = 0.04375
    assert taker.model_fee == D("0.175")
    assert maker.model_fee == D("0.04375")
    assert maker.accumulator_before_rebate == taker.accumulator_after + maker.rounding_fee
    assert maker.accumulator_before_rebate > maker.rounding_fee  # carry-over occurred


# ---------------------------------------------------------------------------
# Fee model facts (fee schedule, not the rounding page)
# ---------------------------------------------------------------------------


def test_model_fee_taker_and_maker() -> None:
    # 0.07 * 1 * 100 * 0.5 * 0.5 = 1.75 ; maker is a quarter of that.
    assert model_fee(D("0.5"), D(100), is_taker=True) == D("1.75")
    assert model_fee(D("0.5"), D(100), is_taker=False) == D("0.4375")
    assert model_fee(D("0.5"), D(100), is_taker=True, multiplier=D("0.5")) == D("0.875")


def test_research_example_1_7325_direct_vs_non_direct() -> None:
    """docs/research/kalshi.md: "Direct-member $0.0001 alignment adds $0 of rounding on that
    input. The same input under non-direct $0.01 alignment adds $0.0075 of rounding fee."
    Revenue here is an illustrative on-grid amount, not from the doc."""
    fee = D("1.7325")
    direct = rounding_step(D("-50.0000"), fee, DIRECT_PRECISION)
    non_direct = rounding_step(D("-50.00"), fee, NON_DIRECT_PRECISION)
    assert direct.trade_fee == D("1.7325")
    assert direct.rounding_fee == 0
    assert non_direct.rounding_fee == D("0.0075")
    assert non_direct.trade_fee + non_direct.rounding_fee == D("1.74")


def test_seller_revenue_is_positive_and_floors_toward_negative_infinity() -> None:
    # Derived: revenue +0.50, fee 0.0026 -> raw 0.4974 -> floor_cent 0.49, rounding 0.0074.
    step = rounding_step(D("0.50"), D("0.0026"), NON_DIRECT_PRECISION)
    assert step.aligned_change == D("0.49")
    assert step.rounding_fee == D("0.0074")


def test_rejects_floats_and_negative_fee() -> None:
    with pytest.raises(TypeError):
        ceil_6dp(0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        trade_fee(D("-0.1"))
    with pytest.raises(ValueError):
        OrderFeeAccumulator(D(0))


# ---------------------------------------------------------------------------
# Multi-fill orders
# ---------------------------------------------------------------------------


def test_multi_fill_order_totals_are_consistent() -> None:
    acc = OrderFeeAccumulator(NON_DIRECT_PRECISION)
    fills = [
        (D("0.55"), D(1), True),
        (D("0.55"), D(2), True),
        (D("0.60"), D(3), True),
        (D("0.60"), D(4), False),
    ]
    revenue = D(0)
    for price, count, taker in fills:
        c = acc.apply_fill(price, count, taker)
        revenue += c.revenue
        assert c.net_fee >= 0
        assert c.rebate % NON_DIRECT_PRECISION == 0
    # Balance change over the order = revenue - net fee, and sits on the cent grid.
    assert acc.total_balance_change == revenue - acc.total_net_fee
    assert acc.total_balance_change % NON_DIRECT_PRECISION == 0
    assert acc.total_net_fee == acc.total_trade_fee + acc.accumulator


def test_multi_fill_converges_to_single_fill_for_whole_cent_amounts() -> None:
    """Doc: "so the total fee converges to what a single equivalent fill would cost"."""
    price, counts = D("0.50"), [D(1)] * 8
    split = OrderFeeAccumulator(NON_DIRECT_PRECISION)
    for count in counts:
        split.apply_fill(price, count, is_taker=True)
    whole = OrderFeeAccumulator(NON_DIRECT_PRECISION)
    whole.apply_fill(price, sum(counts), is_taker=True)
    # Within one grid step: leftover accumulator can remain unrebated at the end of the order.
    assert abs(split.total_net_fee - whole.total_net_fee) < NON_DIRECT_PRECISION


# ---------------------------------------------------------------------------
# Property tests: which total-fee bound is safe?
#
# Findings (20,000 random orders each, seeded):
#  * Naive bound ceil(sum exact model fee, precision) is NOT safe for direct members: the
#    algorithm can exceed it by one grid step ($0.0001) because each fill's trade fee is rounded
#    up to $0.000001 individually (see AMBIGUITIES item 8). It IS safe for non-direct members when
#    revenue is already on the cent grid.
#  * Safe bound when revenue is on the member's grid:
#        ceil_precision(sum exact model fee + n_fills * $0.000001, precision)
#  * If revenue is NOT on the member's grid (e.g. fractional counts for a non-direct member), the
#    balance alignment itself costs up to one grid step per fill and cannot be rebated beyond the
#    fill's fee; that is not fee-model cost, and the bound is loosened by n_fills * precision.
# ---------------------------------------------------------------------------


def _random_order(
    rng: random.Random, precision: D, *, whole_counts: bool
) -> tuple[OrderFeeAccumulator, D, int]:
    acc = OrderFeeAccumulator(precision)
    exact = D(0)
    n = rng.randint(1, 8)
    for _ in range(n):
        price = D(rng.randint(1, 99)) / 100
        count = D(rng.randint(1, 300))
        if not whole_counts:
            count /= 100
        taker = rng.random() < 0.5
        multiplier = rng.choice([D(1), D("0.5"), D("0.25")])
        exact += model_fee(price, count, is_taker=taker, multiplier=multiplier)
        charge = acc.apply_fill(price, count, taker, multiplier)
        assert charge.net_fee >= 0
        assert charge.rebate % precision == 0
        assert charge.accumulator_after >= 0
    return acc, exact, n


@pytest.mark.parametrize("precision", [DIRECT_PRECISION, NON_DIRECT_PRECISION])
def test_property_safe_bound_when_revenue_on_grid(precision: D) -> None:
    rng = random.Random(20260711)
    # Direct: cent prices x 2dp counts -> revenue on 0.0001 grid. Non-direct: whole counts.
    whole = precision == NON_DIRECT_PRECISION
    for _ in range(3000):
        acc, exact, n = _random_order(rng, precision, whole_counts=whole)
        safe = ceil_precision(exact + n * SIX_DP, precision)
        assert acc.total_net_fee <= safe
        # Never undercharges the exact model fee by more than one grid step either.
        assert acc.total_net_fee > exact - precision


def test_property_naive_ceil_bound_can_be_exceeded_for_direct_members() -> None:
    """Documents that ceil(sum exact fee, 0.0001) is NOT a safe upper bound (<= 1 grid step off)."""
    rng = random.Random(1)
    worst = D(0)
    for _ in range(20000):
        acc, exact, _ = _random_order(rng, DIRECT_PRECISION, whole_counts=False)
        worst = max(worst, acc.total_net_fee - ceil_precision(exact, DIRECT_PRECISION))
    assert worst > 0
    assert worst <= DIRECT_PRECISION


def test_property_loose_bound_when_revenue_off_grid_non_direct() -> None:
    rng = random.Random(99)
    for _ in range(3000):
        acc, exact, n = _random_order(rng, NON_DIRECT_PRECISION, whole_counts=False)
        loose = ceil_precision(exact + n * SIX_DP, NON_DIRECT_PRECISION) + n * NON_DIRECT_PRECISION
        assert acc.total_net_fee <= loose
