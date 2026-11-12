"""Catalog refresh: list both venues, store markets, and maintain groups.

Adapter failures are collector events. They do not leave the loop. A market
inside the horizon that is missing from two successful listings in a row is
treated as closed: its episodes end with ``market_closed`` and the group is
revoked until every member is listed again.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from pmarb.collector.clock import Clock, ensure_utc
from pmarb.collector.episodes import EpisodeTracker
from pmarb.collector.persist import Op
from pmarb.collector.registry import Registry
from pmarb.collector.review_mode import ReviewMode, market_closed
from pmarb.http import failure_cause, failure_detail
from pmarb.matching.candidates import generate_candidates
from pmarb.matching.rules import rules_hash
from pmarb.models import EquivalenceGroup, GroupStatus, Market, Venue
from pmarb.protocols import VenueAdapter
from pmarb.storage.db import insert_rules_version, upsert_group, upsert_market

_log = logging.getLogger(__name__)

LOOKBACK = timedelta(hours=12)
HORIZON = timedelta(days=7)
CLOSE_AFTER_MISSES = 2


@dataclass(frozen=True, slots=True)
class _Listing:
    """One adapter pass. ``ids is None`` means the listing failed."""

    venue: Venue
    ids: set[str] | None
    markets: tuple[Market, ...]


class Discovery:
    """One catalog pass, or a supervised loop on the discovery interval."""

    def __init__(
        self,
        *,
        kalshi: VenueAdapter,
        polyus: VenueAdapter,
        registry: Registry,
        review: ReviewMode,
        clock: Clock,
        interval_s: Callable[[], float],
        enqueue: Callable[[Op], None],
        episodes: EpisodeTracker,
        publish: Callable[[], None] | None = None,
        emit: Callable[[str | None, str, Mapping[str, object]], None] | None = None,
        stopping: Callable[[], bool] | None = None,
    ) -> None:
        self._kalshi = kalshi
        self._polyus = polyus
        self._registry = registry
        self._review = review
        self._clock = clock
        self._interval_s = interval_s
        self._enqueue = enqueue
        self._episodes = episodes
        self._publish = publish if publish is not None else (lambda: None)
        self._emit = emit if emit is not None else (lambda _venue, _kind, _detail: None)
        self._stopping = stopping if stopping is not None else (lambda: False)
        self._misses: dict[str, int] = {}
        self._hash_seconds = 0.0

    def bind(self, collector: object) -> None:
        """Publish into the collector and record events on it."""
        self._publish = lambda: collector.set_universe(  # type: ignore[attr-defined]
            tuple(self._registry.groups.values()),
            tuple(self._registry.markets.values()),
        )
        self._emit = collector.record_event  # type: ignore[attr-defined]
        self._stopping = lambda: bool(collector.stopping)  # type: ignore[attr-defined]

    async def run(self) -> None:
        """Refresh, then sleep. Cancellation propagates. Adapter errors do not."""
        while not self._stopping():
            await self.run_once()
            if self._stopping():
                return
            await self._clock.sleep(self._interval_s())

    async def run_once(self) -> None:
        """List both venues, then match, review, and publish as one write."""
        now = self._clock.wall()
        start = now - LOOKBACK
        end = now + HORIZON
        listings = [
            await self._collect(adapter, start, end) for adapter in (self._kalshi, self._polyus)
        ]
        self._commit(listings, start, end)
        self._emit(None, "timing", {"phase": "rules_hash", "duration_s": self._hash_seconds})
        self._hash_seconds = 0.0

    async def _collect(self, adapter: VenueAdapter, start: datetime, end: datetime) -> _Listing:
        started = self._clock.monotonic()
        metrics = getattr(getattr(adapter, "http", None), "request_metrics", None)
        http_venue = "polyus" if adapter.venue is Venue.POLYMARKET_US else str(adapter.venue)
        before = metrics(http_venue, "discovery") if callable(metrics) else {}
        pages_before = getattr(adapter, "discovery_pages", 0)
        found: list[Market] = []
        try:
            async for market in adapter.list_markets(start, end):
                found.append(market)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a venue outage must not stop discovery
            _log.warning(
                "discovery failed for %s [cause=%s]: %s: %s",
                adapter.venue,
                failure_cause(exc),
                type(exc).__name__,
                exc,
            )
            self._emit(str(adapter.venue), "discovery_error", failure_detail(exc))
            listing = _Listing(adapter.venue, None, ())
        else:
            listing = _Listing(adapter.venue, {market.market_id for market in found}, tuple(found))
        after = metrics(http_venue, "discovery") if callable(metrics) else {}
        requests = after.get("requests", 0) - before.get("requests", 0)
        attempts = after.get("attempts", 0) - before.get("attempts", 0)
        self._emit(
            str(adapter.venue),
            "timing",
            {
                "phase": "discovery_venue",
                "duration_s": max(0.0, self._clock.monotonic() - started),
                "markets": len(listing.markets),
                "requests": requests,
                "retries": max(0, attempts - requests),
                "pages": max(0, getattr(adapter, "discovery_pages", 0) - pages_before),
            },
        )
        return listing

    def _commit(self, listings: list[_Listing], start: datetime, end: datetime) -> None:
        def run() -> None:
            self._apply(listings, start, end)

        batch = getattr(self._enqueue, "batch", None)
        if callable(batch):
            with batch():
                run()
            return
        run()

    def _apply(self, listings: list[_Listing], start: datetime, end: datetime) -> None:
        refreshed: dict[str, Market] = {}
        errors = 0
        for listing in listings:
            if listing.ids is None:
                errors += 1
                continue
            for market in listing.markets:
                stored = self._ingest(market)
                if stored is not None:
                    refreshed[stored.market_key] = stored
            self._mark_closed(self._absences(listing.venue, listing.ids, start, end))
        self._recover(refreshed)
        new_groups = self._candidates()
        stats = self._review.apply_cycle(refreshed)
        self._publish()
        self._emit(
            None,
            "discovery",
            {
                "markets": len(refreshed),
                "new_groups": new_groups,
                "revoked": stats["revoked"],
                "auto_approved": stats["auto_approved"],
                "adapter_errors": errors,
            },
        )
        _log.info(
            "discovery markets=%s new_groups=%s revoked=%s auto_approved=%s errors=%s",
            len(refreshed),
            new_groups,
            stats["revoked"],
            stats["auto_approved"],
            errors,
        )

    def _ingest(self, market: Market) -> Market | None:
        now = self._clock.wall()
        try:
            hash_started = self._clock.monotonic()
            stored = market.model_copy(update={"rules_hash": rules_hash(market)})
            self._hash_seconds += max(0.0, self._clock.monotonic() - hash_started)
        except (TypeError, ValueError) as exc:
            self._emit(
                str(market.venue),
                "discovery_error",
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                    "market_id": market.market_id,
                },
            )
            return None
        self._registry.markets[stored.market_key] = stored

        def apply(conn: sqlite3.Connection, stored: Market = stored, now: datetime = now) -> None:
            upsert_market(conn, stored, now)
            insert_rules_version(
                conn, stored.market_key, stored.rules_hash, stored.rules_text, "", now
            )

        self._enqueue(apply)
        return stored

    def _candidates(self) -> int:
        added = 0
        for candidate in generate_candidates(self._registry.markets.values()):
            group = candidate.group
            if group.group_id in self._registry.groups:
                continue
            if group.group_id in self._review.human_rejected:
                continue
            self._registry.groups[group.group_id] = group
            self._enqueue(_group_op(group))
            added += 1
        return added

    def _absences(
        self, venue: Venue, fetched_ids: set[str], start: datetime, end: datetime
    ) -> set[str]:
        closed: set[str] = set()
        window_start = ensure_utc(start)
        window_end = ensure_utc(end)
        for market in self._registry.markets.values():
            if market.venue is not venue:
                continue
            if market.market_id in fetched_ids:
                self._misses.pop(market.market_key, None)
                continue
            event = market.event_start_utc
            if event is None:
                continue
            event = ensure_utc(event)
            if event < window_start or event > window_end:
                continue
            count = self._misses.get(market.market_key, 0) + 1
            self._misses[market.market_key] = count
            if count >= CLOSE_AFTER_MISSES:
                closed.add(market.market_key)
        return closed

    def _mark_closed(self, keys: set[str]) -> None:
        if not keys:
            return
        for group in tuple(self._registry.groups.values()):
            hit = _member_keys(group) & keys
            if not hit:
                continue
            protected = (
                group.status is GroupStatus.REJECTED
                or group.group_id in self._review.human_rejected
            )
            if protected:
                self._episodes.on_markets_closed(hit)
                continue
            note = "market closed: " + ", ".join(sorted(hit))
            if note in group.notes.splitlines() and group.status is GroupStatus.REVOKED:
                self._episodes.on_markets_closed(hit)
                continue
            notes = note if group.notes == "" else f"{group.notes.rstrip()}\n{note}"
            updated = group.model_copy(update={"status": GroupStatus.REVOKED, "notes": notes})
            self._registry.groups[group.group_id] = updated
            self._enqueue(_group_op(updated))
            self._episodes.on_markets_closed(hit)

    def _recover(self, refreshed: Mapping[str, Market]) -> None:
        """A closed group whose members are all listed again can be reviewed once more."""
        for group in tuple(self._registry.groups.values()):
            if not market_closed(group) or group.group_id in self._review.human_rejected:
                continue
            keys = [f"{member.venue.value}:{member.market_id}" for member in group.members]
            if any(key not in refreshed for key in keys):
                continue
            kept = [
                line for line in group.notes.splitlines() if not line.startswith("market closed:")
            ]
            status = GroupStatus.CANDIDATE if group.status is GroupStatus.REVOKED else group.status
            reviewer = None if status is GroupStatus.CANDIDATE else group.reviewer
            decided = None if status is GroupStatus.CANDIDATE else group.decided_utc
            updated = group.model_copy(
                update={
                    "notes": "\n".join(kept),
                    "status": status,
                    "reviewer": reviewer,
                    "decided_utc": decided,
                }
            )
            self._registry.groups[group.group_id] = updated
            self._enqueue(_group_op(updated))
            for key in keys:
                self._misses.pop(key, None)


def _group_op(group: EquivalenceGroup) -> Op:
    def apply(conn: sqlite3.Connection, group: EquivalenceGroup = group) -> None:
        upsert_group(conn, group)

    return apply


def _member_keys(group: EquivalenceGroup) -> set[str]:
    return {f"{member.venue.value}:{member.market_id}" for member in group.members}
