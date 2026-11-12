"""Kalshi taker fees from the fee schedule and the documented rounding algorithm.

Schedule: https://kalshi.com/docs/kalshi-fee-schedule.pdf (effective 2026-07-07).
Rounding: https://docs.kalshi.com/getting_started/fee_rounding

Each consumed level is one taker buy fill on a single per-order accumulator.
The returned amount is that order's net fee plus one precision step. The extra
step is the conservative margin from the fee-rounding review: the documented
algorithm can exceed a naive ceil of the exact fee by one grid step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from pmarb.fees.kalshi_rounding import OrderFeeAccumulator
from pmarb.models import FeeQuote, Level, Side

DEFAULT_COEFFICIENT = Decimal("0.07")
DEFAULT_PROVENANCE = "kalshi-fee-schedule-pdf effective 2026-07-07 (read 2026-11-11)"

_ONE = Decimal(1)


def _optional_decimal(
    value: Decimal | None,
    name: str,
    *,
    positive: bool = False,
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal or None")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive:
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    elif value < 0:
        raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class KalshiFeeModel:
    """Taker fee for one Kalshi series.

    ``coefficient`` defaults to the July 2026 schedule rate. ``multiplier``
    comes from the series. ``precision`` is ``0.0001`` for a direct member and
    ``0.01`` for a non-direct member. Any of the three being ``None`` means
    that input is unknown and the quote amount is ``None``.
    """

    coefficient: Decimal | None = DEFAULT_COEFFICIENT
    multiplier: Decimal | None = None
    precision: Decimal | None = None
    provenance_base: str = DEFAULT_PROVENANCE

    def __post_init__(self) -> None:
        _optional_decimal(self.coefficient, "coefficient")
        _optional_decimal(self.multiplier, "multiplier")
        _optional_decimal(self.precision, "precision", positive=True)
        if not isinstance(self.provenance_base, str) or self.provenance_base == "":
            raise ValueError("provenance_base must be a non-empty string")

    def taker_fee(self, levels: Sequence[Level], side: Side) -> FeeQuote:
        """Fee for buying the consumed levels.

        ``side`` does not change the price. NO levels are already NO
        acquisition prices, and Kalshi charges the contract that was bought.
        ``p * (1 - p)`` is symmetric, so complementing a NO price would be a
        different contract, not a fee identity.
        """
        if not isinstance(side, Side):
            raise TypeError("side must be Side")
        coefficient = self.coefficient
        multiplier = self.multiplier
        precision = self.precision
        unknown = [
            name
            for name, value in (
                ("coefficient", coefficient),
                ("multiplier", multiplier),
                ("precision", precision),
            )
            if value is None
        ]
        if unknown or coefficient is None or multiplier is None or precision is None:
            names = ", ".join(unknown)
            return FeeQuote(
                amount=None,
                provenance=f"{self.provenance_base}; unknown: {names}",
            )

        accumulator = OrderFeeAccumulator(precision)
        for level in levels:
            if not isinstance(level, Level):
                raise TypeError("levels must contain Level values")
            exact = coefficient * multiplier * level.size * level.price * (_ONE - level.price)
            revenue = -(level.price * level.size)
            accumulator.apply_revenue(revenue, exact)
        amount = accumulator.total_net_fee + precision
        return FeeQuote(
            amount=amount,
            provenance=(
                f"{self.provenance_base}; coefficient={coefficient}; "
                f"multiplier={multiplier}; precision={precision}; "
                "per-level fills + 1 step margin"
            ),
            conservative_bound=True,
        )
