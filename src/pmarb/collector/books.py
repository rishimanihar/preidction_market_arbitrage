"""Latest book per market, plus the generation that produced it.

Heartbeats do not belong here. Age moves only when :meth:`BookStore.put`
replaces a book. A generation bump drops that venue's books from the same
source; an older generation cannot put itself back.

Generations are counted per ``(venue, source)``. REST polls and a WebSocket
stream are separate sequences: a polled fallback book never advances, resets,
or invalidates a stream generation, and a new stream connection never drops
the polled books that are covering for it.
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
    """Per-market latest book and the current connection generation per venue and source.

    Generations start at 0, matching the REST adapters. :meth:`put` rejects a
    book whose generation is older than the current one for its venue and
    source. :meth:`bump_generation` advances that counter and invalidates the
    venue's books from that source.
    """

    def __init__(self) -> None:
        self._books: dict[str, BookState] = {}
        self._generation: dict[tuple[Venue, BookSource], int] = {}

    def generation(self, venue: Venue, source: BookSource = "poll") -> int:
        return self._generation.get((venue, source), 0)

    def get(self, key: str) -> BookState | None:
        return self._books.get(key)

    def items(self) -> tuple[BookState, ...]:
        """A snapshot. Safe to invalidate while iterating."""
        return tuple(self._books.values())

    def put(self, book: Book, meta: BookMeta) -> bool:
        """Store ``book``. Return ``False`` when its generation is too old.

        A generation newer than the current one for this venue and source
        becomes current, and that source's earlier books for the venue are
        invalidated first. The book being stored is then written on top.
        """
        if meta.connection_generation != book.connection_generation:
            return False
        current = self.generation(book.venue, meta.source)
        if meta.connection_generation < current:
            return False
        self.advance_generation(book.venue, meta.connection_generation, meta.source)
        self._books[market_key(book.venue, book.market_id)] = BookState(book, meta)
        return True

    def invalidate(self, key: str | None, reason: str) -> None:
        """Invalidate one market, or every market when ``key`` is ``None``."""
        if reason == "":
            raise ValueError("invalidation reason must be non-empty")
        keys = tuple(self._books) if key is None else (key,)
        for item in keys:
            self._invalidate_key(item, reason)

    def invalidate_venue(
        self, venue: Venue, reason: str, *, source: BookSource | None = None
    ) -> None:
        """Invalidate ``venue``'s stored books, or only those from ``source``."""
        if reason == "":
            raise ValueError("invalidation reason must be non-empty")
        prefix = f"{venue}:"
        for key, state in tuple(self._books.items()):
            if not key.startswith(prefix):
                continue
            if source is not None and state.meta.source != source:
                continue
            self._invalidate_key(key, reason)

    def advance_generation(
        self, venue: Venue, generation: int, source: BookSource = "poll"
    ) -> bool:
        """Make ``generation`` current for ``venue`` and ``source`` if it is newer.

        That source's earlier books for the venue are invalidated. The collector
        calls this when a stream reconnects while its books are not being
        stored (REST fallback), so a late book from the dead connection is
        still rejected after the switch back.
        """
        if generation <= self.generation(venue, source):
            return False
        self._generation[(venue, source)] = generation
        self.invalidate_venue(venue, GENERATION, source=source)
        return True

    def bump_generation(self, venue: Venue, source: BookSource = "poll") -> int:
        """Advance ``venue``'s generation for ``source`` and invalidate those books."""
        nxt = self.generation(venue, source) + 1
        self._generation[(venue, source)] = nxt
        self.invalidate_venue(venue, GENERATION, source=source)
        return nxt

    def _invalidate_key(self, key: str, reason: str) -> None:
        state = self._books.get(key)
        if state is None:
            return
        self._books[key] = BookState(state.book, state.meta, invalid_reason=reason)
