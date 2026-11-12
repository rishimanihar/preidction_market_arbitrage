"""Structural contracts for venue adapters and fee models.

Protocols and the book-event carrier only. Adapters land in later PRs.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pmarb.models import Book, FeeQuote, Level, Market, Side, Venue


class FeeModel(Protocol):
    """Per-leg taker fee. ``FeeQuote.amount is None`` means unknown, not zero."""

    def taker_fee(self, levels: Sequence[Level], side: Side) -> FeeQuote: ...


@dataclass(frozen=True, slots=True)
class BookEvent:
    """A normalized book snapshot, or an invalidation of one market's book."""

    kind: Literal["snapshot", "invalidate"]
    book: Book | None
    market_id: str
    venue: Venue
    reason: str | None


class VenueAdapter(Protocol):
    """Venue I/O used by the collector.

    ``list_markets`` yields markets whose event starts in
    ``[horizon_start, horizon_end]``. ``get_books`` batches when the venue
    supports it. ``book_stream`` is ``None`` when the process has no keys.
    """

    venue: Venue

    async def list_markets(
        self, horizon_start: datetime, horizon_end: datetime
    ) -> AsyncIterator[Market]: ...

    async def get_market(self, market_id: str) -> Market: ...

    async def get_books(self, market_ids: Sequence[str]) -> list[Book]: ...

    def book_stream(self, market_ids: Sequence[str]) -> AsyncIterator[BookEvent] | None: ...

    def fee_model(self, market: Market) -> FeeModel: ...
