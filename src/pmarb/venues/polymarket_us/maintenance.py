"""Polymarket US weekly maintenance schedule."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")
_START = time(6, 0)
_END = time(8, 0)


def _utc_window(thursday: date) -> tuple[datetime, datetime]:
    start = datetime.combine(thursday, _START, tzinfo=_EASTERN)
    end = datetime.combine(thursday, _END, tzinfo=_EASTERN)
    return start.astimezone(UTC), end.astimezone(UTC)


def _require_aware(now_utc: datetime) -> datetime:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return now_utc.astimezone(UTC)


def in_maintenance(now_utc: datetime) -> bool:
    """Return whether an aware instant falls in Thu 06:00–08:00 New York time."""
    now = _require_aware(now_utc)
    local = now.astimezone(_EASTERN)
    return local.weekday() == 3 and _START <= local.time() < _END


def next_window(now_utc: datetime) -> tuple[datetime, datetime]:
    """Return the current maintenance window if inside it, else the next one.

    The returned ``(start, end)`` pair is timezone-aware UTC. The window is
    half-open: it includes its 06:00 local start and excludes its 08:00 end.
    """
    now = _require_aware(now_utc)
    local_date = now.astimezone(_EASTERN).date()
    days_until_thursday = (3 - local_date.weekday()) % 7
    candidate_date = local_date + timedelta(days=days_until_thursday)
    start, end = _utc_window(candidate_date)
    if now >= end:
        start, end = _utc_window(candidate_date + timedelta(days=7))
    return start, end
