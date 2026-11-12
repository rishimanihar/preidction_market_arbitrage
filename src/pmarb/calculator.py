"""Two-direction complementary arbitrage assessment.

Direction A buys Kalshi YES and Polymarket US NO.
Direction B buys Polymarket US YES and Kalshi NO.

Capital is both acquisition costs, both taker fees, and ``extra_costs``.
Net profit is the $1 terminal value of ``qty`` contracts minus capital.
Return on capital is net profit divided by capital.
"""

import math
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pmarb.fees import FeeConfig, kalshi_taker_fee, polyus_taker_fee
from pmarb.ladder import InsufficientDepth, InvalidLadder, available_size, walk
from pmarb.models import Assessment, Book, LegFill, Level, Side, Venue
from pmarb.safety import assert_no_order_client

Direction = Literal["A", "B"]

_REASONS = (
    "book_invalid_kalshi",
    "book_invalid_polymarket_us",
    "book_stale_kalshi",
    "book_stale_polymarket_us",
    "book_skew",
    "insufficient_depth_kalshi",
    "insufficient_depth_polymarket_us",
    "fee_unknown",
    "invalid_capital",
    "not_profitable",
    "below_min_profit",
    "below_min_roc",
)


def assess(
    pair_id: str,
    direction: Direction,
    qty: Decimal,
    kalshi_book: Book,
    poly_book: Book,
    fees: FeeConfig,
    extra_costs: Decimal = Decimal(0),
    *,
    now_monotonic: float,
    max_book_age: Decimal = Decimal(2),
    max_skew: Decimal = Decimal(1),
    min_profit: Decimal = Decimal("0.50"),
    min_roc: Decimal = Decimal("0.005"),
    scan_limit: Decimal = Decimal(1000),
) -> Assessment:
    """Assess one direction at ``qty``.

    ``now_monotonic`` is keyword-only because it is required and follows the
    defaulted ``extra_costs`` argument. Book age and skew use monotonic
    receipt times, not ``source_ts``. A book exactly ``max_book_age`` seconds
    old, and books exactly ``max_skew`` apart, are still fresh.

    Insufficient depth at ``qty`` fails the assessment even when a smaller
    quantity would be profitable. ``max_profitable_qty`` is that smaller
    integer size, or ``None`` when no positive-profit integer size exists
    within the depth and scan limit. An unknown Polymarket US fee leaves
    ``net_profit`` as ``None`` and sets ``max_profitable_qty`` to ``None``.
    """
    _require_decimal(qty, "qty")
    _require_decimal(extra_costs, "extra_costs")
    _require_decimal(max_book_age, "max_book_age")
    _require_decimal(max_skew, "max_skew")
    _require_decimal(min_profit, "min_profit")
    _require_decimal(min_roc, "min_roc")
    _require_decimal(scan_limit, "scan_limit")
    if isinstance(now_monotonic, bool) or not isinstance(now_monotonic, (int, float)):
        raise TypeError("now_monotonic must be a real number")
    if not math.isfinite(now_monotonic):
        raise ValueError("now_monotonic must be finite")
    if direction not in ("A", "B"):
        raise ValueError("direction must be 'A' or 'B'")
    if qty <= 0:
        raise ValueError("qty must be positive")
    if extra_costs < 0:
        raise ValueError("extra_costs must be non-negative")
    if max_book_age < 0 or max_skew < 0 or scan_limit < 0:
        raise ValueError("age, skew, and scan_limit must be non-negative")
    if kalshi_book.venue != Venue.KALSHI:
        raise ValueError("kalshi_book must be a Kalshi book")
    if poly_book.venue != Venue.POLYMARKET_US:
        raise ValueError("poly_book must be a Polymarket US book")

    if direction == "A":
        kalshi_levels = kalshi_book.yes_asks
        kalshi_side = Side.YES
        poly_levels = poly_book.no_asks
        poly_side = Side.NO
    else:
        kalshi_levels = kalshi_book.no_asks
        kalshi_side = Side.NO
        poly_levels = poly_book.yes_asks
        poly_side = Side.YES

    reasons: set[str] = set()
    now = float(now_monotonic)
    kalshi_clock_ok = _receipt_ok(kalshi_book.received_monotonic, now)
    poly_clock_ok = _receipt_ok(poly_book.received_monotonic, now)
    if not kalshi_book.valid or not kalshi_clock_ok:
        reasons.add(f"book_invalid_{kalshi_book.venue.value}")
    if not poly_book.valid or not poly_clock_ok:
        reasons.add(f"book_invalid_{poly_book.venue.value}")
    if kalshi_clock_ok and _age(now, kalshi_book.received_monotonic) > max_book_age:
        reasons.add(f"book_stale_{kalshi_book.venue.value}")
    if poly_clock_ok and _age(now, poly_book.received_monotonic) > max_book_age:
        reasons.add(f"book_stale_{poly_book.venue.value}")
    if kalshi_clock_ok and poly_clock_ok:
        skew = abs(
            _as_decimal_clock(kalshi_book.received_monotonic)
            - _as_decimal_clock(poly_book.received_monotonic)
        )
        if skew > max_skew:
            reasons.add("book_skew")
    if fees.polyus_theta is None:
        reasons.add("fee_unknown")

    kalshi_walk = _walk_or_reject(kalshi_levels, qty, kalshi_book.venue.value, reasons)
    poly_walk = _walk_or_reject(poly_levels, qty, poly_book.venue.value, reasons)

    legs: tuple[LegFill, LegFill] | None = None
    capital: Decimal | None = None
    net_profit: Decimal | None = None
    roc: Decimal | None = None
    if kalshi_walk is not None and poly_walk is not None:
        k_used, k_cost = kalshi_walk
        p_used, p_cost = poly_walk
        k_fee = kalshi_taker_fee(k_used, fees.kalshi_multiplier, fees.kalshi_rounding)
        p_fee: Decimal | None = None
        if fees.polyus_theta is not None:
            # NO acquisition uses the complement of the native YES bid.
            # p*(1-p) matches the native YES price, so the acquisition level
            # is the right input for both YES and NO.
            p_fee = polyus_taker_fee(p_used, fees.polyus_theta)
            capital = k_cost + p_cost + k_fee + p_fee + extra_costs
            net_profit = qty - capital
            if capital <= 0:
                reasons.add("invalid_capital")
                capital = None
                net_profit = None
                roc = None
            else:
                roc = net_profit / capital
                if net_profit <= 0:
                    reasons.add("not_profitable")
                if net_profit < min_profit:
                    reasons.add("below_min_profit")
                if roc < min_roc:
                    reasons.add("below_min_roc")
        if direction == "A":
            legs = (
                _leg(Venue.KALSHI, kalshi_side, k_used, k_cost, k_fee),
                _leg(Venue.POLYMARKET_US, poly_side, p_used, p_cost, p_fee),
            )
        else:
            legs = (
                _leg(Venue.POLYMARKET_US, poly_side, p_used, p_cost, p_fee),
                _leg(Venue.KALSHI, kalshi_side, k_used, k_cost, k_fee),
            )

    return Assessment(
        pair_id=pair_id,
        direction=direction,
        requested_qty=qty,
        legs=legs,
        capital=capital,
        net_profit=net_profit,
        roc=roc,
        extra_costs=extra_costs,
        qualified=not reasons,
        rejection_reasons=_in_order(reasons),
        max_profitable_qty=_max_profitable_qty(
            kalshi_levels,
            poly_levels,
            fees,
            extra_costs,
            scan_limit,
        ),
        fee_model_version=fees.version,
        computed_utc=datetime.now(UTC),
    )


def assess_both(
    pair_id: str,
    qty: Decimal,
    kalshi_book: Book,
    poly_book: Book,
    fees: FeeConfig,
    extra_costs: Decimal = Decimal(0),
    *,
    now_monotonic: float,
    max_book_age: Decimal = Decimal(2),
    max_skew: Decimal = Decimal(1),
    min_profit: Decimal = Decimal("0.50"),
    min_roc: Decimal = Decimal("0.005"),
    scan_limit: Decimal = Decimal(1000),
) -> tuple[Assessment, Assessment]:
    """Return direction A and direction B, each assessed on its own ladders."""
    shared = {
        "pair_id": pair_id,
        "qty": qty,
        "kalshi_book": kalshi_book,
        "poly_book": poly_book,
        "fees": fees,
        "extra_costs": extra_costs,
        "now_monotonic": now_monotonic,
        "max_book_age": max_book_age,
        "max_skew": max_skew,
        "min_profit": min_profit,
        "min_roc": min_roc,
        "scan_limit": scan_limit,
    }
    return assess(direction="A", **shared), assess(direction="B", **shared)


def _walk_or_reject(
    levels: list[Level],
    qty: Decimal,
    venue_value: str,
    reasons: set[str],
) -> tuple[list[Level], Decimal] | None:
    try:
        return walk(levels, qty)
    except InsufficientDepth:
        reasons.add(f"insufficient_depth_{venue_value}")
    except InvalidLadder:
        reasons.add(f"book_invalid_{venue_value}")
    return None


def _max_profitable_qty(
    kalshi_levels: list[Level],
    poly_levels: list[Level],
    fees: FeeConfig,
    extra_costs: Decimal,
    scan_limit: Decimal,
) -> Decimal | None:
    if fees.polyus_theta is None:
        return None
    try:
        available = min(available_size(kalshi_levels), available_size(poly_levels))
    except InvalidLadder:
        return None
    upper = min(scan_limit, available).to_integral_value(rounding=ROUND_DOWN)
    best: Decimal | None = None
    qty = Decimal(1)
    while qty <= upper:
        net = _net_profit_at(qty, kalshi_levels, poly_levels, fees, extra_costs)
        if net is not None and net > 0:
            best = qty
        qty += 1
    return best


def _net_profit_at(
    qty: Decimal,
    kalshi_levels: list[Level],
    poly_levels: list[Level],
    fees: FeeConfig,
    extra_costs: Decimal,
) -> Decimal | None:
    if fees.polyus_theta is None:
        return None
    try:
        k_used, k_cost = walk(kalshi_levels, qty)
        p_used, p_cost = walk(poly_levels, qty)
    except (InsufficientDepth, InvalidLadder):
        return None
    k_fee = kalshi_taker_fee(k_used, fees.kalshi_multiplier, fees.kalshi_rounding)
    p_fee = polyus_taker_fee(p_used, fees.polyus_theta)
    capital = k_cost + p_cost + k_fee + p_fee + extra_costs
    return qty - capital


def _leg(
    venue: Venue,
    side: Side,
    levels: list[Level],
    cost: Decimal,
    fee: Decimal | None,
) -> LegFill:
    quantity = sum((level.size for level in levels), Decimal(0))
    return LegFill(
        venue=venue,
        side=side,
        levels_consumed=list(levels),
        quantity=quantity,
        cost=cost,
        avg_price=cost / quantity,
        fee=fee,
    )


def _age(now_monotonic: float, received_monotonic: float) -> Decimal:
    return _as_decimal_clock(now_monotonic) - _as_decimal_clock(received_monotonic)


def _as_decimal_clock(value: float) -> Decimal:
    return Decimal(str(value))


def _receipt_ok(received_monotonic: float, now_monotonic: float) -> bool:
    """A receipt is usable only when it is finite and not from the future."""
    return math.isfinite(received_monotonic) and received_monotonic <= now_monotonic


def _in_order(reasons: set[str]) -> list[str]:
    ordered = [reason for reason in _REASONS if reason in reasons]
    return ordered + sorted(reasons.difference(_REASONS))


def _require_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


assert_no_order_client()
