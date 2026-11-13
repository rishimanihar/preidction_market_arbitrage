"""Walk an ascending acquisition ladder."""

from collections.abc import Sequence
from decimal import Decimal

from pmarb.models import Level


class LadderError(ValueError):
    """Base class for acquisition-ladder errors."""


class InvalidLadder(LadderError):
    """Levels are unsorted or contain a price or size that cannot be traded."""


class InsufficientDepth(LadderError):
    """Visible size cannot fill the requested quantity."""

    def __init__(self, requested: Decimal, available: Decimal) -> None:
        self.requested = requested
        self.available = available
        super().__init__(
            f"insufficient depth: requested {requested} but only {available} is available"
        )


def available_size(levels: Sequence[Level]) -> Decimal:
    """Return total size after rejecting a ladder ``walk`` would reject."""
    _validate(levels)
    return sum((level.size for level in levels), Decimal(0))


def walk(levels: Sequence[Level], qty: Decimal) -> tuple[list[Level], Decimal]:
    """Consume ``qty`` from ``levels`` from cheapest to most expensive.

    Returns the consumed levels (a partial last level when needed) and the
    total cost. Raises ``InvalidLadder`` for unsorted, non-positive size,
    non-positive quantity, or a price outside (0, 1). Raises
    ``InsufficientDepth`` when visible size is less than ``qty``.
    """
    if not isinstance(qty, Decimal):
        raise TypeError("qty must be Decimal")
    if not qty.is_finite() or qty <= 0:
        raise InvalidLadder("qty must be positive and finite")
    available = available_size(levels)
    if available < qty:
        raise InsufficientDepth(requested=qty, available=available)

    remaining = qty
    consumed: list[Level] = []
    cost = Decimal(0)
    for level in levels:
        if remaining == 0:
            break
        take = min(remaining, level.size)
        consumed.append(Level(price=level.price, size=take))
        cost += take * level.price
        remaining -= take
    if remaining != 0:
        raise InsufficientDepth(requested=qty, available=available)
    return consumed, cost


def _validate(levels: Sequence[Level]) -> None:
    previous: Decimal | None = None
    for level in levels:
        if not isinstance(level, Level):
            raise InvalidLadder("levels must be Level values")
        price = level.price
        size = level.size
        if not isinstance(price, Decimal) or not isinstance(size, Decimal):
            raise InvalidLadder("price and size must be Decimal")
        if not price.is_finite() or price <= 0 or price >= 1:
            raise InvalidLadder("price must be finite and in (0, 1)")
        if not size.is_finite() or size <= 0:
            raise InvalidLadder("size must be finite and positive")
        if previous is not None and price <= previous:
            raise InvalidLadder("levels must be strictly ascending by price")
        previous = price
