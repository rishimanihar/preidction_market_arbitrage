"""Structural contracts for venue adapters and fee models.

Protocols, the book-event carrier, and the partial ``get_books`` result.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
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


class PartialBooksError(Exception):
    """``get_books`` fetched some books but not all of them.

    ``books`` holds the books that arrived. ``failures`` maps each market id
    that did not arrive to the exception from its request, so several ids can
    share one exception (one failed Kalshi batch). The collector keeps
    ``books`` and invalidates only the failed ids.
    """

    def __init__(
        self,
        venue: Venue,
        books: Sequence[Book],
        failures: Mapping[str, BaseException],
    ) -> None:
        self.venue = venue
        self.books = list(books)
        self.failures = dict(failures)
        errors = self.errors
        first = errors[0] if errors else None
        detail = f"; first: {type(first).__name__}: {first}" if first is not None else ""
        super().__init__(
            f"{len(self.failures)} of {len(self.failures) + len(self.books)} {venue} books "
            f"failed in {len(errors)} request(s){detail}"
        )

    @property
    def errors(self) -> list[BaseException]:
        """Each distinct request failure once, in the order first seen."""
        seen: dict[int, BaseException] = {}
        for exc in self.failures.values():
            seen.setdefault(id(exc), exc)
        return list(seen.values())


def collect_books(
    venue: Venue,
    outcomes: Sequence[tuple[Sequence[str], list[Book] | BaseException]],
) -> list[Book]:
    """Merge per-request outcomes from ``asyncio.gather(..., return_exceptions=True)``.

    Returns the books when every request succeeded. When exactly one request
    was made and it failed, that exception is raised unchanged. Otherwise any
    failure raises :class:`PartialBooksError` carrying the books that did
    arrive. Cancellation and other non-``Exception`` errors are re-raised.
    """
    books: list[Book] = []
    failures: dict[str, BaseException] = {}
    for ids, outcome in outcomes:
        if isinstance(outcome, BaseException):
            if not isinstance(outcome, Exception):
                raise outcome
            for market_id in ids:
                failures[market_id] = outcome
        else:
            books.extend(outcome)
    if not failures:
        return books
    if len(outcomes) == 1:
        only = outcomes[0][1]
        assert isinstance(only, BaseException)
        raise only
    raise PartialBooksError(venue, books, failures)
