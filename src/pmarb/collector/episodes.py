"""Open and close episodes for one direction, size, model, and result class.

Indicative and qualified runs are different keys and are never merged.
A discovery sample does not open an episode: the caller passes
``opens_episode=False``. Gaps longer than twice the poll cadence close with
``data_gap``. A rejected assessment closes with ``disqualified:<first reason>``.
A stream invalidation closes the episodes on that market at once
(:meth:`EpisodeTracker.on_markets_invalidated`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from pmarb.collector.clock import ensure_utc, parse_utc
from pmarb.collector.persist import Op
from pmarb.models import (
    AssessmentClass,
    Episode,
    EpisodeTermination,
    Leg,
    OpportunityAssessment,
    Side,
    Venue,
)
from pmarb.storage.db import upsert_episode

_PERSIST_EVERY = timedelta(seconds=60)

_GAP_REASONS: dict[str, tuple[EpisodeTermination, str | None]] = {
    "collector_restart": (EpisodeTermination.COLLECTOR_RESTART, None),
    "collector_shutdown": (EpisodeTermination.COLLECTOR_SHUTDOWN, None),
    "clock_jump": (EpisodeTermination.CLOCK_JUMP, None),
    "maintenance": (EpisodeTermination.MAINTENANCE, None),
    "recording_backpressure": (EpisodeTermination.DATA_GAP, None),
    "data_gap": (EpisodeTermination.DATA_GAP, None),
    "rules_changed": (EpisodeTermination.RULES_CHANGED, None),
    "market_closed": (EpisodeTermination.MARKET_CLOSED, None),
}

_Direction = tuple[str, str, str, str, str, Decimal, str]


class EpisodeTracker:
    """In-memory episodes. Writes land on open, on close, and about once a minute."""

    def __init__(
        self,
        *,
        enqueue: Callable[[Op], None],
        cadence_s: Callable[[], float],
        now: Callable[[], datetime],
    ) -> None:
        self._enqueue = enqueue
        self._cadence_s = cadence_s
        self._now = now
        self._open: dict[tuple[object, ...], Episode] = {}
        self._dirty: dict[int, Episode] = {}
        self._next_id = 0
        self._staged_at: dict[int, datetime] = {}
        self._signature: dict[int, tuple[object, ...]] = {}

    def note_high_water(self, max_id: int) -> None:
        """Keep new ids above rows already stored, including ones just closed."""
        self._next_id = max(self._next_id, max_id)

    def observe(self, assessment: OpportunityAssessment, *, opens_episode: bool) -> None:
        """Fold one assessment into the open episode for its class, or close that run."""
        if not opens_episode:
            return
        if assessment.result_class is AssessmentClass.REJECTED:
            self._reject(assessment)
            return
        self._expire_other_classes(assessment)
        self._follow(assessment)

    def on_gap(self, reason: str) -> None:
        """Close every open episode. ``recording_backpressure`` is stored as ``data_gap``."""
        termination, detail = _gap_termination(reason)
        for episode in tuple(self._open.values()):
            self._finish(episode, termination, detail)
        self.flush()

    def on_rules_changed(self, group_id: str) -> None:
        """Close this group's open episodes. A later approval starts a new run."""
        for episode in tuple(self._open.values()):
            if episode.group_id == group_id:
                self._finish(episode, EpisodeTermination.RULES_CHANGED, None)
        self.flush()

    def on_markets_closed(self, market_keys: set[str]) -> None:
        """Close episodes whose YES or NO leg is one of ``market_keys``."""
        if not market_keys:
            return
        for episode in tuple(self._open.values()):
            yes_key = _leg_key(episode.yes_leg)
            no_key = _leg_key(episode.no_leg)
            if yes_key in market_keys or no_key in market_keys:
                self._finish(episode, EpisodeTermination.MARKET_CLOSED, None)
        self.flush()

    def on_markets_invalidated(self, market_keys: set[str], reason: str) -> None:
        """Close episodes with a leg in ``market_keys`` now: a stream invalidated that book.

        A stream invalidation (disconnect, sequence gap, schema failure, crossed
        book, ...) breaks the run at once; the poll-era 2x cadence grace does not
        apply. ``market_closed`` and ``maintenance`` keep their own termination;
        anything else is ``data_gap`` with the stream's reason as the detail.
        """
        if not market_keys:
            return
        termination, detail = _gap_termination(reason)
        for episode in tuple(self._open.values()):
            if _leg_key(episode.yes_leg) in market_keys or _leg_key(episode.no_leg) in market_keys:
                self._finish(episode, termination, detail)
        self.flush()

    def mark_unassessed(self, group_id: str, now: datetime, cadence_s: float) -> None:
        """Close the group's episodes when books have been missing longer than 2× cadence."""
        for episode in tuple(self._open.values()):
            if episode.group_id != group_id:
                continue
            if (now - episode.last_seen_utc).total_seconds() > 2 * cadence_s:
                self._finish(episode, EpisodeTermination.DATA_GAP, None)

    def flush(self) -> None:
        """Enqueue dirty episodes. Safe to call when nothing changed."""
        if not self._dirty:
            return
        episodes = tuple(self._dirty.values())
        self._dirty.clear()

        def apply(conn: sqlite3.Connection, episodes: tuple[Episode, ...] = episodes) -> None:
            for episode in episodes:
                upsert_episode(conn, episode)

        try:
            self._enqueue(apply)
        except Exception:
            for episode in episodes:
                if episode.id is not None:
                    self._dirty[episode.id] = episode
            raise

    def _reject(self, assessment: OpportunityAssessment) -> None:
        reason = assessment.reasons[0] if assessment.reasons else "rejected"
        limit = 2 * self._cadence_s()
        for episode in self._matching(assessment):
            if _gap_seconds(episode, assessment.computed_utc) > limit:
                self._finish(episode, EpisodeTermination.DATA_GAP, None)
            else:
                self._finish(episode, EpisodeTermination.DISQUALIFIED, reason)

    def _expire_other_classes(self, assessment: OpportunityAssessment) -> None:
        limit = 2 * self._cadence_s()
        for episode in self._matching(assessment):
            if episode.result_class is assessment.result_class:
                continue
            if _gap_seconds(episode, assessment.computed_utc) > limit:
                self._finish(episode, EpisodeTermination.DATA_GAP, None)

    def _follow(self, assessment: OpportunityAssessment) -> None:
        existing = self._open.get(_assessment_key(assessment))
        if existing is None:
            self._open_new(assessment)
            return
        if _gap_seconds(existing, assessment.computed_utc) > 2 * self._cadence_s():
            self._finish(existing, EpisodeTermination.DATA_GAP, None)
            self._open_new(assessment)
            return
        self._continue(existing, assessment)

    def _open_new(self, assessment: OpportunityAssessment) -> None:
        size = assessment.max_profitable_qty
        if size is None:
            size = assessment.requested_qty
        net = assessment.net_profit if assessment.net_profit is not None else Decimal(0)
        episode = Episode(
            id=self._allocate(),
            group_id=assessment.group_id,
            yes_leg=assessment.yes_leg,
            no_leg=assessment.no_leg,
            qty=assessment.requested_qty,
            result_class=assessment.result_class,
            model_version=assessment.model_version,
            first_seen_utc=assessment.computed_utc,
            last_seen_utc=assessment.computed_utc,
            observations=1,
            peak_net=net,
            min_size=size,
            max_size=size,
        )
        self._open[_assessment_key(assessment)] = episode
        self._stage(episode, force=True)

    def _continue(self, episode: Episode, assessment: OpportunityAssessment) -> None:
        last_seen = max(episode.last_seen_utc, assessment.computed_utc)
        peak = episode.peak_net
        if assessment.net_profit is not None and assessment.net_profit > peak:
            peak = assessment.net_profit
        min_size = episode.min_size
        max_size = episode.max_size
        size = assessment.max_profitable_qty
        if size is not None:
            min_size = min(min_size, size)
            max_size = max(max_size, size)
        updated = episode.model_copy(
            update={
                "last_seen_utc": last_seen,
                "observations": episode.observations + 1,
                "peak_net": peak,
                "min_size": min_size,
                "max_size": max_size,
            }
        )
        self._open[_episode_key(updated)] = updated
        self._stage(updated, force=False)

    def _finish(
        self,
        episode: Episode,
        termination: EpisodeTermination,
        detail: str | None,
    ) -> None:
        self._open.pop(_episode_key(episode), None)
        closed = episode.model_copy(
            update={"termination": termination, "termination_detail": detail}
        )
        self._stage(closed, force=True)

    def _matching(self, assessment: OpportunityAssessment) -> list[Episode]:
        direction = _direction_assessment(assessment)
        return [episode for episode in self._open.values() if _direction(episode) == direction]

    def _stage(self, episode: Episode, *, force: bool) -> None:
        if episode.id is None:
            raise ValueError("episode id is required before persistence")
        previous = self._signature.get(episode.id)
        signature = (
            episode.peak_net,
            episode.min_size,
            episode.max_size,
            episode.termination,
            episode.termination_detail,
            episode.observations,
        )
        staged = self._staged_at.get(episode.id)
        due = staged is None or episode.last_seen_utc - staged >= _PERSIST_EVERY
        economics_changed = previous is None or signature[:5] != previous[:5]
        self._signature[episode.id] = signature
        if not force and not economics_changed and not due:
            return
        self._dirty[episode.id] = episode
        self._staged_at[episode.id] = episode.last_seen_utc

    def _allocate(self) -> int:
        self._next_id += 1
        return self._next_id


def close_open_episodes(conn: sqlite3.Connection, now: datetime) -> int:
    """Close rows with no termination. ``now`` is the restart instant.

    The row keeps its last observation time. The termination is ``collector_restart``.
    """
    ensure_utc(now)
    rows = conn.execute(
        """
        SELECT id, group_id, yes_market_key, no_market_key, qty, "class", model_version,
               first_seen_utc, last_seen_utc, observations, peak_net, min_size, max_size
        FROM episodes
        WHERE termination_reason IS NULL
        """
    ).fetchall()
    closed = 0
    for row in rows:
        episode = _episode_from_row(row)
        if episode is None:
            continue
        upsert_episode(
            conn,
            episode.model_copy(update={"termination": EpisodeTermination.COLLECTOR_RESTART}),
        )
        closed += 1
    return closed


def max_episode_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM episodes").fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _gap_termination(reason: str) -> tuple[EpisodeTermination, str | None]:
    mapped = _GAP_REASONS.get(reason)
    if mapped is not None:
        return mapped
    if reason == "":
        return EpisodeTermination.DATA_GAP, None
    return EpisodeTermination.DATA_GAP, reason


def _gap_seconds(episode: Episode, now: datetime) -> float:
    return (now - episode.last_seen_utc).total_seconds()


def _leg_key(leg: Leg) -> str:
    return f"{leg.venue.value}:{leg.market_id}"


def _direction(episode: Episode) -> _Direction:
    return (
        episode.group_id,
        episode.yes_leg.venue.value,
        episode.yes_leg.market_id,
        episode.no_leg.venue.value,
        episode.no_leg.market_id,
        episode.qty,
        episode.model_version,
    )


def _direction_assessment(assessment: OpportunityAssessment) -> _Direction:
    return (
        assessment.group_id,
        assessment.yes_leg.venue.value,
        assessment.yes_leg.market_id,
        assessment.no_leg.venue.value,
        assessment.no_leg.market_id,
        assessment.requested_qty,
        assessment.model_version,
    )


def _assessment_key(assessment: OpportunityAssessment) -> tuple[object, ...]:
    return (*_direction_assessment(assessment), assessment.result_class.value)


def _episode_key(episode: Episode) -> tuple[object, ...]:
    return (*_direction(episode), episode.result_class.value)


def _episode_from_row(row: tuple[object, ...]) -> Episode | None:
    try:
        result_class = AssessmentClass(str(row[5]))
        if result_class is AssessmentClass.REJECTED:
            return None
        first_seen = parse_utc(row[7] if isinstance(row[7], str) else None)
        last_seen = parse_utc(row[8] if isinstance(row[8], str) else None)
        if first_seen is None or last_seen is None:
            return None
        return Episode(
            id=int(row[0]) if row[0] is not None else None,
            group_id=str(row[1]),
            yes_leg=_leg(str(row[2]), Side.YES),
            no_leg=_leg(str(row[3]), Side.NO),
            qty=Decimal(str(row[4])),
            result_class=result_class,
            model_version=str(row[6]),
            first_seen_utc=first_seen,
            last_seen_utc=last_seen,
            observations=int(row[9]),
            peak_net=Decimal(str(row[10])),
            min_size=Decimal(str(row[11])),
            max_size=Decimal(str(row[12])),
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def _leg(key: str, side: Side) -> Leg:
    venue_text, market_id = key.split(":", 1)
    return Leg(venue=Venue(venue_text), market_id=market_id, side=side)
