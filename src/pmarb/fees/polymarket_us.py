"""Polymarket US taker fee, using the order cap as a conservative bound.

https://docs.polymarket.us/fees.md

Each fill is rounded half-even to the cent, and the order's cumulative charge
is capped at ``round_half_even(sum of the exact fill fees)``. An aggregated
book does not say how an order would split, so the assessment uses that cap.
Actual per-fill charges are at most the cap. ``theta`` is the account
coefficient, not the market's ``feeCoefficient``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from pmarb.models import FeeQuote, Level, Side

FEE_URL = "https://docs.polymarket.us/fees.md"
ALLOWED_THETA = frozenset(
    {
        Decimal("0.095"),
        Decimal("0.085"),
        Decimal("0.075"),
        Decimal("0.0695"),
    }
)

_CENT = Decimal("0.01")
_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class PolymarketUSFeeModel:
    """Account taker coefficient and the documented order cap.

    ``theta is None`` means the account tier is unknown. Any other value
    outside ``ALLOWED_THETA`` is rejected.
    """

    theta: Decimal | None

    def __post_init__(self) -> None:
        if self.theta is None:
            return
        if isinstance(self.theta, bool) or not isinstance(self.theta, Decimal):
            raise TypeError("theta must be Decimal or None")
        if self.theta not in ALLOWED_THETA:
            allowed = ", ".join(str(value) for value in sorted(ALLOWED_THETA))
            raise ValueError(f"theta must be one of {allowed}")

    def taker_fee(self, levels: Sequence[Level], side: Side) -> FeeQuote:
        """Cap for buying the consumed levels.

        YES and NO both use the level price. NO ladders are already NO
        acquisition prices, and ``p * (1 - p)`` equals ``(1 - p) * p``.
        """
        if not isinstance(side, Side):
            raise TypeError("side must be Side")
        if self.theta is None:
            return FeeQuote(
                amount=None,
                provenance=f"polymarket-us theta unknown; {FEE_URL}",
            )
        exact = _ZERO
        for level in levels:
            if not isinstance(level, Level):
                raise TypeError("levels must contain Level values")
            exact += self.theta * level.size * level.price * (_ONE - level.price)
        amount = exact.quantize(_CENT, rounding=ROUND_HALF_EVEN)
        return FeeQuote(
            amount=amount,
            provenance=(
                "polymarket-us order cap round_half_even(sum theta*q*p*(1-p)); "
                f"theta={self.theta}; {FEE_URL}"
            ),
            conservative_bound=True,
        )
