"""Which approved groups to poll, and how often.

The fast set is games that are live or about to start, capped, soonest start
first. Every other approved group is a discovery sample: fetched on a slow
cadence and flagged so it never opens an episode. The fast cadence is
``max(min_cadence, n_polymarket_markets / polyus_books_rps)`` over the fast
set only. Discovery markets are not part of that count.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pmarb.collector.clock import ensure_utc
from pmarb.models import EquivalenceGroup, GroupStatus, Market, Venue

# A game counts as live from its start through this long after. Long enough
# for a delayed finish; short enough that yesterday's games fall off the cap.
LIVE_WINDOW = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class ScheduledGroup:
    """One approved group in a plan. Discovery samples never open episodes."""

    group: EquivalenceGroup
    start: datetime | None
    discovery_sample: bool

    @property
    def group_id(self) -> str:
        return self.group.group_id


@dataclass(frozen=True, slots=True)
class PollPlan:
    """The fast poll set, the discovery samples, and the fast cadence."""

    polled: tuple[ScheduledGroup, ...]
    discovery: tuple[ScheduledGroup, ...]
    cadence_s: float
    sample_interval_s: float

    def ids_for(self, venue: Venue, *, include_discovery: bool) -> tuple[str, ...]:
        """Market ids for ``venue``, polled groups first, each id once."""
        selected = self.polled + self.discovery if include_discovery else self.polled
        return market_ids(selected, venue)


def market_ids(groups: Sequence[ScheduledGroup], venue: Venue) -> tuple[str, ...]:
    """Member ids for ``venue`` in group order, without duplicates."""
    seen: list[str] = []
    for item in groups:
        for member in item.group.members:
            if member.venue == venue and member.market_id not in seen:
                seen.append(member.market_id)
    return tuple(seen)


def build_poll_plan(
    groups: Sequence[EquivalenceGroup],
    markets: Sequence[Market],
    now: datetime,
    *,
    max_polled_groups: int = 8,
    start_window: timedelta = timedelta(minutes=30),
    live_window: timedelta = LIVE_WINDOW,
    sample_interval_s: float = 60.0,
    min_cadence_s: float = 1.0,
    polyus_books_rps: float = 4.0,
) -> PollPlan:
    """Choose the fast set and the discovery samples.

    A group is polled when its start is inside the live window
    (``start <= now <= start + live_window``) or it starts within
    ``start_window``. The fast set is the soonest ``max_polled_groups`` of
    those, ordered by start then ``group_id``. Approved groups that do not
    make the fast set, including ones past the cap, are discovery samples.
    Groups that are not approved are ignored. A group whose start is unknown
    is a discovery sample.
    """
    if max_polled_groups < 1:
        raise ValueError("max_polled_groups must be >= 1")
    if polyus_books_rps <= 0:
        raise ValueError("polyus_books_rps must be > 0")
    if min_cadence_s <= 0:
        raise ValueError("min_cadence_s must be > 0")
    if sample_interval_s <= 0:
        raise ValueError("sample_interval_s must be > 0")
    if live_window < timedelta(0) or start_window < timedelta(0):
        raise ValueError("windows must be >= 0")
    instant = ensure_utc(now)
    by_key = {market.market_key: market for market in markets}

    hot: list[tuple[datetime, str, EquivalenceGroup]] = []
    cold: list[tuple[datetime | None, str, EquivalenceGroup]] = []
    seen: set[str] = set()
    for group in groups:
        if group.status is not GroupStatus.APPROVED or group.group_id in seen:
            continue
        seen.add(group.group_id)
        start = _group_start(group, by_key)
        if start is not None and _is_hot(start, instant, start_window, live_window):
            hot.append((start, group.group_id, group))
        else:
            cold.append((start, group.group_id, group))

    hot.sort(key=lambda item: (item[0], item[1]))
    polled_rows = hot[:max_polled_groups]
    cold.extend(hot[max_polled_groups:])
    cold.sort(key=_cold_key)

    polled = tuple(ScheduledGroup(group, start, False) for start, _, group in polled_rows)
    discovery = tuple(ScheduledGroup(group, start, True) for start, _, group in cold)
    n_poly = len(market_ids(polled, Venue.POLYMARKET_US))
    cadence_s = max(min_cadence_s, n_poly / polyus_books_rps)
    return PollPlan(
        polled=polled,
        discovery=discovery,
        cadence_s=cadence_s,
        sample_interval_s=sample_interval_s,
    )


def _group_start(group: EquivalenceGroup, by_key: dict[str, Market]) -> datetime | None:
    starts: list[datetime] = []
    for member in group.members:
        market = by_key.get(f"{member.venue}:{member.market_id}")
        if market is not None and market.event_start_utc is not None:
            starts.append(ensure_utc(market.event_start_utc))
    if not starts:
        return None
    return min(starts)


def _is_hot(
    start: datetime,
    now: datetime,
    start_window: timedelta,
    live_window: timedelta,
) -> bool:
    if start <= now <= start + live_window:
        return True
    return now <= start <= now + start_window


def _cold_key(item: tuple[datetime | None, str, EquivalenceGroup]) -> tuple[bool, float, str]:
    start, group_id, _ = item
    if start is None:
        return (True, 0.0, group_id)
    return (False, start.timestamp(), group_id)
