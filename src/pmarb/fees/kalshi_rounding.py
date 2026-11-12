"""Kalshi fee rounding, implemented step by step from the official documentation.

Primary source: https://docs.kalshi.com/getting_started/fee_rounding
(raw markdown: https://docs.kalshi.com/getting_started/fee_rounding.md).

Fee model (fee schedule PDF, effective 2026-07-07, https://kalshi.com/docs/kalshi-fee-schedule.pdf):
taker = 0.07 x M x C x P x (1 - P); maker = 0.0175 x M x C x P x (1 - P).
The fee-rounding page defines only what happens AFTER the model fee is known.

All functions are pure and use ``Decimal``; floats are rejected.

AMBIGUITIES
-----------
The page is ambiguous or silent in these places. The choice made here is stated, not hidden.

1. Rebate formula. Step 5 says "Rebate accumulated rounding in increments of the user's target
   balance precision, capped so the fill's net fee cannot be negative". Implemented as
   ``rebate = floor_precision(min(accumulator_before_rebate, trade_fee + rounding_fee))``. The
   cap is taken on the fill's own pre-rebate fee (trade + rounding), then floored to the grid.
   The docs do not give the cap formula; the second worked example only says "enough total fee".
2. Accumulator timing. The table shows "Before Rebate" = carried + added, so the fill's own
   rounding fee is eligible for rebate on that same fill. Implemented that way.
3. Trade fee on a zero model fee. ``ceil_6dp(0) = 0``; no special case is documented.
4. Maker/taker coefficients. The page does not define the model fee; 0.07 / 0.0175 come from the
   fee schedule, and ``multiplier`` is the per-series fee multiplier M. Series-specific fee
   structures (fee-free, flat) are out of scope: pass an explicit model fee to
   ``OrderFeeAccumulator.apply_revenue`` for those.
5. Position-cost / revenue alignment. The page defines no separate "position cost" step beyond
   ``aligned_change``; the claim "trade fee plus rounding fee is exactly $0.005" in the first
   example is simply ``revenue - aligned_change``. Nothing else is implemented.
6. Seller sign. Revenue is "negative for buyers"; the page gives no seller example. Sellers are
   modelled with positive revenue and the same five steps (floor is toward negative infinity).
7. Revenue granularity. The page says fees are six-decimal amounts; this module does not
   quantize or validate revenue and works on whatever Decimal is given (fractional counts work).
8. Order-level vs. fill-level model fee. The page computes ``ceil_6dp`` per fill, so the sum of
   trade fees can exceed ``ceil(sum of exact model fees)``. See the bound discussion in
   ``tests/test_kalshi_fee_rounding.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

SOURCE_URL = "https://docs.kalshi.com/getting_started/fee_rounding"

TAKER_COEFFICIENT = Decimal("0.07")
MAKER_COEFFICIENT = Decimal("0.0175")
SIX_DP = Decimal("0.000001")
DIRECT_PRECISION = Decimal("0.0001")
NON_DIRECT_PRECISION = Decimal("0.01")

_ZERO = Decimal(0)
_ONE = Decimal(1)


def _dec(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _round_to(value: Decimal, grid: Decimal, rounding: str) -> Decimal:
    """Round to a multiple of ``grid``."""
    steps = (value / grid).to_integral_value(rounding=rounding)
    return steps * grid


def ceil_6dp(value: Decimal) -> Decimal:
    """Round up to the nearest $0.000001.

    Doc: "Trade fee | Fee from the fee model, rounded up to the nearest `$0.000001`"
    https://docs.kalshi.com/getting_started/fee_rounding
    """
    return _round_to(_dec(value, "value"), SIX_DP, ROUND_CEILING)


def floor_precision(value: Decimal, precision: Decimal) -> Decimal:
    """Round toward negative infinity onto the user's balance grid.

    Doc step 2: "Compute `aligned_change = floor_precision(revenue - trade_fee)`"
    https://docs.kalshi.com/getting_started/fee_rounding
    """
    return _round_to(_dec(value, "value"), _dec(precision, "precision"), ROUND_FLOOR)


def ceil_precision(value: Decimal, precision: Decimal) -> Decimal:
    """Round up onto a precision grid (helper for the conservative bound; not in the doc)."""
    return _round_to(_dec(value, "value"), _dec(precision, "precision"), ROUND_CEILING)


def model_fee(
    price: Decimal,
    count: Decimal,
    *,
    is_taker: bool,
    multiplier: Decimal = _ONE,
) -> Decimal:
    """Exact (unrounded) fee-model amount for one fill.

    Source is the fee schedule, not the rounding page: taker ``0.07 x M x C x P x (1 - P)``,
    maker ``0.0175 x M x C x P x (1 - P)`` (https://kalshi.com/docs/kalshi-fee-schedule.pdf,
    effective 2026-07-07). The rounding page then says "Compute `trade_fee = ceil_6dp(model_fee)`"
    https://docs.kalshi.com/getting_started/fee_rounding
    """
    price = _dec(price, "price")
    count = _dec(count, "count")
    multiplier = _dec(multiplier, "multiplier")
    if not _ZERO <= price <= _ONE:
        raise ValueError("price must be in [0, 1] dollars")
    if count < 0 or multiplier < 0:
        raise ValueError("count and multiplier must be non-negative")
    coefficient = TAKER_COEFFICIENT if is_taker else MAKER_COEFFICIENT
    return coefficient * multiplier * count * price * (_ONE - price)


def trade_fee(fee: Decimal) -> Decimal:
    """Step 1: ``trade_fee = ceil_6dp(model_fee)``.

    Doc step 1: "Compute `trade_fee = ceil_6dp(model_fee)`"; table: "Trade fee | Fee from the
    fee model, rounded up to the nearest `$0.000001`".
    https://docs.kalshi.com/getting_started/fee_rounding
    """
    fee = _dec(fee, "model_fee")
    if fee < 0:
        raise ValueError("model_fee must be non-negative")
    return ceil_6dp(fee)


def fill_revenue(price: Decimal, count: Decimal, *, is_buyer: bool = True) -> Decimal:
    """Signed revenue of a fill: price x quantity, negative for buyers.

    Doc: "Given a fill's signed `revenue` (negative for buyers) and model fee"; "a fill's
    revenue (price x quantity)". https://docs.kalshi.com/getting_started/fee_rounding
    """
    amount = _dec(price, "price") * _dec(count, "count")
    return -amount if is_buyer else amount


@dataclass(frozen=True, slots=True)
class RoundingStep:
    """Steps 1-3 for a single fill, before any accumulator effect."""

    trade_fee: Decimal
    aligned_change: Decimal
    rounding_fee: Decimal


def rounding_step(revenue: Decimal, fee: Decimal, precision: Decimal) -> RoundingStep:
    """Steps 1-3 of "Rounding Mechanics".

    Doc: "1. Compute `trade_fee = ceil_6dp(model_fee)` 2. Compute
    `aligned_change = floor_precision(revenue - trade_fee)` 3. Compute
    `rounding_fee = (revenue - trade_fee) - aligned_change`"
    https://docs.kalshi.com/getting_started/fee_rounding
    """
    revenue = _dec(revenue, "revenue")
    tf = trade_fee(fee)
    raw = revenue - tf
    aligned = floor_precision(raw, precision)
    return RoundingStep(trade_fee=tf, aligned_change=aligned, rounding_fee=raw - aligned)


def rebate_amount(
    accumulator_before_rebate: Decimal,
    fill_fee_before_rebate: Decimal,
    precision: Decimal,
) -> Decimal:
    """Step 5 rebate.

    Doc: "Rebate accumulated rounding in increments of the user's target balance precision,
    capped so the fill's net fee cannot be negative"; "Rebate | Refund from accumulated rounding
    overpayment, aligned to the user's target balance precision".
    https://docs.kalshi.com/getting_started/fee_rounding
    (Cap formula is an interpretation: see AMBIGUITIES item 1.)
    """
    capped = min(
        _dec(accumulator_before_rebate, "accumulator"),
        _dec(fill_fee_before_rebate, "fee"),
    )
    if capped <= _ZERO:
        return _ZERO
    return floor_precision(capped, precision)


@dataclass(frozen=True, slots=True)
class FillCharge:
    """What one fill was charged and the accumulator state afterwards.

    ``net_fee = trade_fee + rounding_fee - rebate`` (doc: "Net fee = trade fee + rounding fee -
    rebate (always >= $0.00)"). ``balance_change = aligned_change + rebate``.
    """

    revenue: Decimal
    model_fee: Decimal
    trade_fee: Decimal
    aligned_change: Decimal
    rounding_fee: Decimal
    accumulator_before_rebate: Decimal
    rebate: Decimal
    accumulator_after: Decimal
    net_fee: Decimal
    balance_change: Decimal
    total_net_fee: Decimal
    total_rebate: Decimal


class OrderFeeAccumulator:
    """Per-order fee accumulator shared by all fills, taker or maker.

    Doc: "The fee accumulator is maintained per order across all fills regardless of whether
    the fills are taker or maker. If an order initially takes (matching resting orders) and then
    becomes a resting maker order, the accumulated rounding carries over to subsequent maker
    fills." https://docs.kalshi.com/getting_started/fee_rounding
    Direct members use $0.0001 precision, non-direct $0.01 (same page, "Overview").
    """

    def __init__(self, precision: Decimal) -> None:
        precision = _dec(precision, "precision")
        if precision <= _ZERO:
            raise ValueError("precision must be positive")
        self.precision = precision
        self.accumulator = _ZERO
        self.total_net_fee = _ZERO
        self.total_rebate = _ZERO
        self.total_trade_fee = _ZERO
        self.total_rounding_fee = _ZERO
        self.total_balance_change = _ZERO

    def apply_revenue(self, revenue: Decimal, fee: Decimal) -> FillCharge:
        """Run steps 1-5 for a fill given signed revenue and the exact model fee.

        Doc step 4: "Add the rounding fee to the order's accumulator"; step 5: rebate as in
        ``rebate_amount``. https://docs.kalshi.com/getting_started/fee_rounding
        """
        step = rounding_step(revenue, fee, self.precision)
        before = self.accumulator + step.rounding_fee
        rebate = rebate_amount(before, step.trade_fee + step.rounding_fee, self.precision)
        self.accumulator = before - rebate
        net = step.trade_fee + step.rounding_fee - rebate
        self.total_net_fee += net
        self.total_rebate += rebate
        self.total_trade_fee += step.trade_fee
        self.total_rounding_fee += step.rounding_fee
        self.total_balance_change += step.aligned_change + rebate
        return FillCharge(
            revenue=revenue,
            model_fee=fee,
            trade_fee=step.trade_fee,
            aligned_change=step.aligned_change,
            rounding_fee=step.rounding_fee,
            accumulator_before_rebate=before,
            rebate=rebate,
            accumulator_after=self.accumulator,
            net_fee=net,
            balance_change=step.aligned_change + rebate,
            total_net_fee=self.total_net_fee,
            total_rebate=self.total_rebate,
        )

    def apply_fill(
        self,
        price: Decimal,
        count: Decimal,
        is_taker: bool,
        multiplier: Decimal = _ONE,
        *,
        is_buyer: bool = True,
    ) -> FillCharge:
        """Apply one fill: model fee from the fee schedule, then the doc's steps 1-5."""
        fee = model_fee(price, count, is_taker=is_taker, multiplier=multiplier)
        return self.apply_revenue(fill_revenue(price, count, is_buyer=is_buyer), fee)
