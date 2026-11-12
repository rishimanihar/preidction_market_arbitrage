"""Venue taker-fee models.

Fees are summed from each fill's own price and size. They are not computed
from the volume-weighted average price.
"""

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from pmarb.models import Level

KalshiRounding = Literal["direct_member", "round_up_cent"]

_CENT = Decimal("0.01")
_TEN_THOUSANDTH = Decimal("0.0001")

# Kalshi taker fee = round_up(M x 0.07 x C x P x (1 - P)).
# "round up" rounds so fee + position cost lands on a centicent (0.0001).
# https://kalshi.com/docs/kalshi-fee-schedule.pdf
# Last updated and effective: July 7, 2026.
KALSHI_TAKER_COEFFICIENT = Decimal("0.07")


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise TypeError("float is not permitted; use Decimal")
    return value


class FeeConfig(BaseModel):
    """Account fee assumptions stored with each assessment.

    ``polyus_theta is None`` means the Polymarket US coefficient is unknown.
    Callers must not substitute zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kalshi_multiplier: Decimal = Decimal(1)
    kalshi_rounding: KalshiRounding = "direct_member"
    polyus_theta: Decimal | None = None
    version: str

    @field_validator("kalshi_multiplier", "polyus_theta", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("kalshi_multiplier")
    @classmethod
    def _multiplier_non_negative(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError("kalshi_multiplier must be finite and non-negative")
        return value

    @field_validator("polyus_theta")
    @classmethod
    def _theta_non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("polyus_theta must be finite and non-negative")
        return value


def kalshi_taker_fee(
    levels_consumed: Sequence[Level],
    multiplier: Decimal = Decimal(1),
    rounding: KalshiRounding = "direct_member",
) -> Decimal:
    """Kalshi taker fee ``0.07 * multiplier * q * p * (1 - p)`` per fill.

    ``direct_member`` quantizes the exact order total up to 0.0001
    (``ROUND_CEILING``), the centicent grid from the July 7, 2026 schedule.
    ``round_up_cent`` ceils that same exact total to 0.01.
    """
    multiplier = _require_finite_decimal(multiplier, "multiplier")
    if multiplier < 0:
        raise ValueError("multiplier must be non-negative")
    exact = _shape_sum(levels_consumed, KALSHI_TAKER_COEFFICIENT * multiplier)
    if rounding == "direct_member":
        return exact.quantize(_TEN_THOUSANDTH, rounding=ROUND_CEILING)
    if rounding == "round_up_cent":
        return exact.quantize(_CENT, rounding=ROUND_CEILING)
    raise ValueError(f"unknown kalshi rounding: {rounding}")


def polyus_taker_fee(
    levels_consumed: Sequence[Level],
    theta: Decimal,
) -> Decimal:
    """Polymarket US taker fee for one order.

    The order's total commission is
    ``round_half_even_to_cent(sum(theta * C_i * p_i * (1 - p_i)))``.
    That sum-then-round is a reconciliation rule, not a maximum cap:
    https://docs.polymarket.us/fees.md

    For a NO leg the venue fee uses the native YES bid, which is
    ``1 - acquisition_price``. ``p * (1 - p)`` is symmetric, so that fee
    equals the fee computed from the NO acquisition price. Callers may pass
    NO acquisition levels directly.
    """
    theta = _require_finite_decimal(theta, "theta")
    if theta < 0:
        raise ValueError("theta must be non-negative")
    exact = _shape_sum(levels_consumed, theta)
    return exact.quantize(_CENT, rounding=ROUND_HALF_EVEN)


def _shape_sum(levels: Sequence[Level], coefficient: Decimal) -> Decimal:
    total = Decimal(0)
    for level in levels:
        if not isinstance(level, Level):
            raise TypeError("levels_consumed must contain Level values")
        price = level.price
        size = level.size
        if not isinstance(price, Decimal) or not isinstance(size, Decimal):
            raise TypeError("price and size must be Decimal")
        if not price.is_finite() or not size.is_finite():
            raise ValueError("price and size must be finite")
        total += coefficient * size * price * (Decimal(1) - price)
    return total


def _require_finite_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value
