"""Pure price-normalization helpers.

Adapters will call these later. Each complement is applied once: a YES ask is
``1 - NO bid`` (Kalshi) or the native YES offer (Polymarket US), and a NO ask
is ``1 - YES bid``. Results are merged by price and sorted ascending.
"""

from collections.abc import Sequence
from decimal import Decimal

from pmarb.ladder import InvalidLadder
from pmarb.models import Level


def kalshi_asks_from_bids(
    yes_bids: Sequence[Level],
    no_bids: Sequence[Level],
) -> tuple[list[Level], list[Level]]:
    """Return ``(yes_asks, no_asks)`` from Kalshi YES and NO bid ladders.

    YES ask = ``1 - NO bid`` with that NO bid's size.
    NO ask = ``1 - YES bid`` with that YES bid's size.
    """
    yes_asks = _asks_from_opposite_bids(no_bids)
    no_asks = _asks_from_opposite_bids(yes_bids)
    return yes_asks, no_asks


def polyus_asks(
    yes_offers: Sequence[Level],
    yes_bids: Sequence[Level],
) -> tuple[list[Level], list[Level]]:
    """Return ``(yes_asks, no_asks)`` for a Polymarket US YES instrument.

    YES acquisition is the native YES offer ladder.
    NO acquisition is ``1 - native YES bid`` with that bid's size.
    """
    return _sort_merge(yes_offers), _asks_from_opposite_bids(yes_bids)


def _asks_from_opposite_bids(bids: Sequence[Level]) -> list[Level]:
    complemented = [Level(price=Decimal(1) - bid.price, size=bid.size) for bid in bids]
    return _sort_merge(complemented)


def _sort_merge(levels: Sequence[Level]) -> list[Level]:
    checked: list[Level] = []
    for level in levels:
        if not isinstance(level, Level):
            raise InvalidLadder("levels must be Level values")
        if (
            not isinstance(level.price, Decimal)
            or not isinstance(level.size, Decimal)
            or level.price <= 0
            or level.price >= 1
            or level.size <= 0
        ):
            raise InvalidLadder("level price must be in (0, 1) and size must be positive")
        checked.append(level)
    ordered = sorted(checked, key=lambda level: level.price)
    merged: list[Level] = []
    for level in ordered:
        if merged and merged[-1].price == level.price:
            previous = merged[-1]
            merged[-1] = Level(price=previous.price, size=previous.size + level.size)
        else:
            merged.append(level)
    return merged
