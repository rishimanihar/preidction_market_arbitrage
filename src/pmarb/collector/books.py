"""Latest book per market, plus the generation that produced it.

Heartbeats do not belong here. Age moves only when :meth:`BookStore.put`
replaces a book. A generation bump drops every book from that venue; an older
generation cannot put itself back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pmarb.collector.clock import ensure_utc
from pmarb.models import Book, Venue

BookSource = Literal["poll", "stream"]
GENERATION = "generation"


def market_key(venue: Venue | str, market_id: str) -> str:
    """``venue:market_id``, the same key :class:`~pmarb.models.Market` uses."""
    return f"{venue}:{market_id}"


@dataclass(frozen=True, slots=True)
class BookMeta:
    """How a stored book was received.

    ``received_mono`` / ``received_wall`` are the collector clock at accept
    time. ``source`` is ``poll`` or ``stream``. ``cached`` is true for a
    cache hit (never fresh). ``books_atomic_group_id`` is set only when the
    caller knows both legs are one snapshot; the poller leaves it ``None``.
    ``discovery_sample`` marks a 60 s sample. Those must not open episodes.
    """

    received_mono: float
    received_wall: datetime
    connection_generation: int
    source: BookSource
    cached: bool
    books_atomic_group_id: str | None = None
    discovery_sample: bool = False

    def __post_init__(self) -> None:
        if self.source not in ("poll", "stream"):
            raise ValueError("source must be 'poll' or 'stream'")
        if self.connection_generation < 0:
            raise ValueError("connection_generation must be >= 0")
        object.__setattr__(self, "received_wall", ensure_utc(self.received_wall))


@dataclass(frozen=True, slots=True)
class BookState:
    """One stored book. ``invalid_reason`` is set when it must not be used."""

    book: Book
    meta: BookMeta
    invalid_reason: str | None = None

    @property
    def market_key(self) -> str:
        return market_key(self.book.venue, self.book.market_id)


class BookStore:
    """Per-market latest book and the current connection generation per venue.

    Generations start at 0, matching the adapters. :meth:`put` rejects a book
    whose generation is older than the venue's current one. :meth:`bump_generation`
    advances that counter and invalidates the venue's books.
    """

    def __init__(self) -> None:
        self._books: dict[str, BookState] = {}
        self._generation: dict[Venue, int] = {}

    def generation(self, venue: Venue) -> int:
        return self._generation.get(venue, 0)

    def get(self, key: str) -> BookState | None:
        return self._books.get(key)

    def items(self) -> tuple[BookState, ...]:
        """A snapshot. Safe to invalidate while iterating."""
        return tuple(self._books.values())

    def put(self, book: Book, meta: BookMeta) -> bool:
        """Store ``book``. Return ``False`` when its generation is too old.

        A generation newer than the store's current one becomes the current
        generation, and any earlier book for that venue is invalidated first.
        The book being stored is then written on top of that.
        """
        if meta.connection_generation != book.connection_generation:
            return False
        current = self.generation(book.venue)
        if meta.connection_generation < current:
            return False
        if meta.connection_generation > current:
            self._generation[book.venue] = meta.connection_generation
            self.invalidate_venue(book.venue, GENERATION)
        self._books[market_key(book.venue, book.market_id)] = BookState(book, meta)
        return True

    def invalidate(self, key: str | None, reason: str) -> None:
        """Invalidate one market, or every market when ``key`` is ``None``."""
        if reason == "":
            raise ValueError("invalidation reason must be non-empty")
        keys = tuple(self._books) if key is None else (key,)
        for item in keys:
            self._invalidate_key(item, reason)

    def invalidate_venue(self, venue: Venue, reason: str) -> None:
        """Invalidate every stored book for ``venue``."""
        if reason == "":
            raise ValueError("invalidation reason must be non-empty")
        prefix = f"{venue}:"
        for key in tuple(self._books):
            if key.startswith(prefix):
                self._invalidate_key(key, reason)

    def bump_generation(self, venue: Venue) -> int:
        """Advance ``venue``'s generation and invalidate its current books."""
        nxt = self.generation(venue) + 1
        self._generation[venue] = nxt
        self.invalidate_venue(venue, GENERATION)
        return nxt

    def _invalidate_key(self, key: str, reason: str) -> None:
        state = self._books.get(key)
        if state is None:
            return
        self._books[key] = BookState(state.book, state.meta, invalid_reason=reason)
