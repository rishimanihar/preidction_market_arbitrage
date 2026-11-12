"""When a stored book is still usable.

A book is fresh only when both clocks say it is young, it is a valid
uncached snapshot, and Polymarket US is not inside its maintenance window.
Checking the wall clock as well as the monotonic clock catches a machine
sleep: monotonic time pauses, wall time does not, and the book looks recent
on only one of them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pmarb.collector.books import BookState
from pmarb.collector.clock import ensure_utc
from pmarb.models import Book, EpisodeTermination, Venue
from pmarb.venues.polymarket_us.maintenance import in_maintenance

STALE = "stale"
CACHED = "cached"
MAINTENANCE = EpisodeTermination.MAINTENANCE.value
CLOCK_JUMP = EpisodeTermination.CLOCK_JUMP.value

_CACHED_REASONS = frozenset({CACHED, "cached_response"})


def max_age_for(
    source: Literal["poll", "stream"],
    cadence_s: float,
    *,
    stream_max_age_s: float = 2.0,
    poll_multiple: float = 2.0,
) -> float:
    """Stream books live ``stream_max_age_s`` (default 2 s).

    Polled books live ``poll_multiple`` times the cadence they were fetched
    on (default ``2 × cadence``). Pass the 60 s sample interval for a
    discovery sample; pass the fast cadence for a live group.
    """
    if source == "stream":
        if stream_max_age_s < 0:
            raise ValueError("stream_max_age_s must be >= 0")
        return stream_max_age_s
    if source == "poll":
        if cadence_s < 0:
            raise ValueError("cadence_s must be >= 0")
        if poll_multiple < 0:
            raise ValueError("poll_multiple must be >= 0")
        return poll_multiple * cadence_s
    raise ValueError("source must be 'poll' or 'stream'")


def book_is_cached(book: Book) -> bool:
    """True when the adapter marked a cache hit, or the headers say so."""
    if book.invalid_reason in _CACHED_REASONS:
        return True
    raw = book.raw_native
    if not isinstance(raw, dict):
        return False
    headers = raw.get("cache_headers")
    if not isinstance(headers, dict):
        return False
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    try:
        if Decimal(lowered.get("x-kalshi-cache-hits", "0")) >= 1:
            return True
    except (InvalidOperation, ValueError):
        pass
    for directive in lowered.get("cache-control", "").split(","):
        name, separator, value = directive.strip().partition("=")
        if name.strip().lower() == "max-age" and separator:
            try:
                if Decimal(value.strip().strip('"')) > 0:
                    return True
            except (InvalidOperation, ValueError):
                continue
    return False


def is_fresh(
    state: BookState,
    now_mono: float,
    now_wall: datetime,
    max_age_s: float,
) -> tuple[bool, str | None]:
    """Return whether ``state`` may be used, and a reason when it may not.

    Both clocks have to agree the book is within ``max_age_s``. A Polymarket US
    book inside the venue maintenance window is ``maintenance`` even when it
    is young. An already-invalid book keeps its reason, except that maintenance
    replaces it while the window is open.
    """
    wall = ensure_utc(now_wall)
    if state.book.venue is Venue.POLYMARKET_US and in_maintenance(wall):
        return False, MAINTENANCE
    if state.invalid_reason:
        return False, state.invalid_reason
    if state.meta.cached or book_is_cached(state.book):
        return False, CACHED
    if not state.book.valid:
        return False, state.book.invalid_reason or "invalid"
    age_mono = now_mono - state.meta.received_mono
    age_wall = (wall - state.meta.received_wall).total_seconds()
    if age_mono > max_age_s or age_wall > max_age_s:
        return False, STALE
    return True, None


class ClockJumpGuard:
    """Detect a step in ``wall − mono`` between checks.

    The first :meth:`check` only records the baseline and returns ``False``.
    A later check returns ``True`` when the offset has moved by more than
    ``threshold_s`` (sleep/wake or an NTP step, in either direction).
    """

    def __init__(self, threshold_s: float = 1.0) -> None:
        if threshold_s < 0:
            raise ValueError("threshold_s must be >= 0")
        self.threshold_s = threshold_s
        self._offset: float | None = None

    def check(self, now_mono: float, now_wall: datetime) -> bool:
        offset = ensure_utc(now_wall).timestamp() - now_mono
        previous = self._offset
        self._offset = offset
        if previous is None:
            return False
        return abs(offset - previous) > self.threshold_s
