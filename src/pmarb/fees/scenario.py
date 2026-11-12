"""Account-parameter stand-ins for an unset venue fee setting.

``fee_scenario="conservative"`` prices a soak when the account value is
missing. The quote is an upper bound and its provenance starts with
``scenario:``, so the evaluator stores it as indicative. A configured value
is used as given. A missing Kalshi series multiplier stays unknown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from pmarb.models import FeeQuote, Level, Side
from pmarb.protocols import FeeModel

SCENARIO_PREFIX = "scenario:conservative; "
# Non-direct Kalshi grid: coarser than a direct member's 0.0001.
KALSHI_CONSERVATIVE_PRECISION = Decimal("0.01")
# Highest published Polymarket US tier.
POLYUS_CONSERVATIVE_THETA = Decimal("0.095")


def account_fee_value(
    fee_scenario: str | None,
    configured: Decimal | None,
    stand_in: Decimal,
) -> tuple[Decimal | None, bool]:
    """Return ``(value, scenario)`` for one account fee parameter.

    ``scenario`` is true only when ``configured`` is unset and the
    conservative stand-in is the value that will be quoted.
    """
    if configured is not None:
        return configured, False
    if fee_scenario == "conservative":
        return stand_in, True
    return None, False


def with_scenario(model: FeeModel, scenario: bool) -> FeeModel:
    """Prefix computed quotes when the account value was a stand-in."""
    if scenario:
        return ScenarioFeeModel(model)
    return model


@dataclass(frozen=True, slots=True)
class ScenarioFeeModel:
    """Mark a computed fee as a conservative scenario bound.

    An unknown inner quote is returned unchanged. Unknown is not a bound,
    and a missing series multiplier must stay unknown in every mode.
    """

    inner: FeeModel

    def taker_fee(self, levels: Sequence[Level], side: Side) -> FeeQuote:
        quote = self.inner.taker_fee(levels, side)
        if quote.amount is None:
            return quote
        return FeeQuote(
            amount=quote.amount,
            provenance=f"{SCENARIO_PREFIX}{quote.provenance}",
            conservative_bound=True,
        )
