"""Price polled groups and store an assessment when the result actually moves.

``books_atomic`` is true only when every leg is a stream book and the skew
between those stamps is within the qualified limit. A poll is never atomic.
Two results stay indicative even when the books are atomic: a reviewer whose
name starts with ``auto:`` (D27), and a fee quote whose provenance starts
with ``scenario:``. Both are stored by rebuilding a qualified assessment as
indicative. The fee quote itself is unchanged, so the scenario provenance
is what the assessment row records.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from pmarb.collector.books import BookState, BookStore
from pmarb.collector.clock import Clock
from pmarb.collector.episodes import EpisodeTracker
from pmarb.collector.persist import Op
from pmarb.collector.registry import Registry
from pmarb.config import Settings
from pmarb.economics import Limits, assess_group
from pmarb.models import (
    AssessmentClass,
    Book,
    EquivalenceGroup,
    GroupStatus,
    OpportunityAssessment,
    Venue,
)
from pmarb.protocols import FeeModel, VenueAdapter
from pmarb.storage.db import insert_assessment

MODEL_VERSION = "m1-1"
SIZE_VIEWS = (Decimal(1), Decimal(10), Decimal(100))
PROFIT_BAND = Decimal("0.05")
HEARTBEAT = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class AssessmentSnapshot:
    """The last assessment row written for one direction and size."""

    result_class: AssessmentClass
    reasons: frozenset[str]
    net_profit: Decimal | None
    written_utc: datetime


def downgrade_auto_qualified(assessment: OpportunityAssessment) -> OpportunityAssessment:
    """Rebuild a qualified assessment as indicative.

    The copy keeps books, prices, fee quotes, and ``books_atomic``. Only the
    class changes. An ``auto:`` reviewer (D27) and a ``scenario:`` fee quote
    both go through here, so the scenario provenance stays on the stored row.
    """
    if assessment.result_class is not AssessmentClass.QUALIFIED:
        return assessment
    data = assessment.model_dump()
    data["result_class"] = AssessmentClass.INDICATIVE
    return OpportunityAssessment.model_validate(data)


def _scenario_fee(assessment: OpportunityAssessment) -> bool:
    """True when either leg was priced with a scenario stand-in."""
    for result in (assessment.yes_result, assessment.no_result):
        if result is not None and result.fee.provenance.startswith("scenario:"):
            return True
    return False


def books_are_atomic(states: Sequence[BookState], skew_limit_s: float) -> bool:
    """True when every leg is a stream snapshot and their skew is within the limit."""
    if len(states) < 2:
        return False
    if any(state.meta.source != "stream" for state in states):
        return False
    monos = [state.book.received_monotonic for state in states]
    return max(monos) - min(monos) <= skew_limit_s


def should_persist_assessment(
    previous: AssessmentSnapshot | None,
    assessment: OpportunityAssessment,
    *,
    now: datetime,
    band: Decimal = PROFIT_BAND,
    heartbeat: timedelta = HEARTBEAT,
) -> bool:
    """Write on a new class, a new reason set, a net move outside the band, or the minute heartbeat."""
    if previous is None:
        return True
    if assessment.result_class is not previous.result_class:
        return True
    if frozenset(assessment.reasons) != previous.reasons:
        return True
    if _net_moved(previous.net_profit, assessment.net_profit, band):
        return True
    return now - previous.written_utc >= heartbeat


def limits_for(settings: Settings, states: Sequence[BookState], cadence_s: float) -> Limits:
    """Stream books use the stream age. Polled books use the cadence multiple."""
    if states and all(state.meta.source == "stream" for state in states):
        max_age = settings.stream_max_age_s
    else:
        max_age = cadence_s * settings.poll_max_age_cadence_multiple
    return Limits(
        max_book_age_s=Decimal(str(max_age)),
        max_skew_indicative_s=Decimal(str(settings.skew_indicative_s)),
        max_skew_qualified_s=Decimal(str(settings.skew_qualified_s)),
        min_touch_qty=Decimal(settings.min_touch_qty),
    )


class Evaluator:
    """Evaluate every fresh book update; discovery samples never open episodes."""

    def __init__(
        self,
        *,
        registry: Registry,
        store: BookStore,
        kalshi: VenueAdapter,
        polyus: VenueAdapter,
        settings: Settings,
        episodes: EpisodeTracker,
        enqueue: Callable[[Op], None],
        clock: Clock,
        opens_episodes: Callable[[str], bool] | None = None,
        cadence_s: Callable[[], float] | None = None,
        record_event: Callable[[str, dict[str, object]], None] | None = None,
        on_assessment: Callable[[OpportunityAssessment], None] | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._kalshi = kalshi
        self._polyus = polyus
        self._settings = settings
        self._episodes = episodes
        self._enqueue = enqueue
        self._clock = clock
        self._opens = opens_episodes if opens_episodes is not None else (lambda _group_id: True)
        self._cadence = cadence_s if cadence_s is not None else (lambda: 1.0)
        self._record_event = (
            record_event if record_event is not None else (lambda _kind, _detail: None)
        )
        self._on_assessment = on_assessment
        self._written: dict[tuple[object, ...], AssessmentSnapshot] = {}

    def bind(self, collector: object) -> None:
        """Read the fast-set and the cadence from a running collector."""
        opens = getattr(collector, "opens_episodes", None)
        cadence = getattr(collector, "cadence_s", None)
        if not callable(opens) or not isinstance(cadence, int | float):
            raise TypeError("collector must provide opens_episodes and cadence_s")
        self._opens = opens
        self._cadence = lambda: collector.cadence_s  # type: ignore[attr-defined]
        record = getattr(collector, "record_event", None)
        if callable(record):
            self._record_event = lambda kind, detail: record(None, kind, detail)

    async def on_books_updated(self, group_ids: set[str]) -> None:
        for group_id in sorted(group_ids):
            self._evaluate(group_id)
        self._episodes.flush()

    def _evaluate(self, group_id: str) -> None:
        group = self._registry.groups.get(group_id)
        if group is None or group.status is not GroupStatus.APPROVED:
            return
        loaded = self._books(group)
        if loaded is None:
            if self._opens(group_id):
                self._episodes.mark_unassessed(group_id, self._clock.wall(), self._cadence())
            return
        states, books = loaded
        cadence = self._cadence()
        if states and all(state.meta.discovery_sample for state in states):
            cadence = self._settings.sample_interval_s
        limits = limits_for(self._settings, states, cadence)
        max_age = float(limits.max_book_age_s)
        now_mono = self._clock.monotonic()
        now_wall = self._clock.wall()
        mono_ages = [now_mono - state.book.received_monotonic for state in states]
        wall_ages = [(now_wall - state.book.received_utc).total_seconds() for state in states]
        if any(
            state.invalid_reason is not None
            or not state.book.valid
            or mono_age < 0
            or mono_age > max_age
            or wall_age < 0
            or wall_age > max_age
            for state, mono_age, wall_age in zip(states, mono_ages, wall_ages, strict=True)
        ):
            self._record_event(
                "evaluation_error",
                {
                    "group_id": group_id,
                    "reason": "books_not_fresh",
                    "max_age_s": max_age,
                    "book_ages_monotonic_s": mono_ages,
                    "book_ages_wall_s": wall_ages,
                },
            )
            if self._opens(group_id):
                self._episodes.mark_unassessed(group_id, now_wall, cadence)
            return
        atomic = books_are_atomic(states, self._settings.skew_qualified_s)
        fee_models = self._fee_models(group)
        auto = bool(group.reviewer) and group.reviewer.startswith("auto:")
        for qty in SIZE_VIEWS:
            priced = assess_group(
                group,
                books,
                fee_models,
                qty,
                now_monotonic=now_mono,
                now_wall=now_wall,
                books_atomic=atomic,
                model_version=MODEL_VERSION,
                limits=limits,
            )
            for assessment in priced:
                if auto or _scenario_fee(assessment):
                    assessment = downgrade_auto_qualified(assessment)
                persisted = self._persist(assessment)
                if persisted and self._on_assessment is not None:
                    self._on_assessment(assessment)
                if self._opens(group_id):
                    self._episodes.observe(assessment, opens_episode=True)

    def _books(self, group: EquivalenceGroup) -> tuple[list[BookState], dict[str, Book]] | None:
        states: list[BookState] = []
        books: dict[str, Book] = {}
        for member in group.members:
            key = f"{member.venue.value}:{member.market_id}"
            state = self._store.get(key)
            if state is None:
                return None
            book = state.book
            if state.invalid_reason and book.valid:
                book = book.model_copy(
                    update={"valid": False, "invalid_reason": state.invalid_reason}
                )
            states.append(state)
            books[key] = book
        return states, books

    def _fee_models(self, group: EquivalenceGroup) -> dict[str, FeeModel]:
        models: dict[str, FeeModel] = {}
        for member in group.members:
            market = self._registry.market(member.venue, member.market_id)
            if market is None:
                continue
            if member.venue is Venue.KALSHI:
                adapter = self._kalshi
            elif member.venue is Venue.POLYMARKET_US:
                adapter = self._polyus
            else:
                continue
            models[market.market_key] = adapter.fee_model(market)
        return models

    def _persist(self, assessment: OpportunityAssessment) -> bool:
        key = (
            assessment.group_id,
            assessment.yes_leg.venue.value,
            assessment.yes_leg.market_id,
            assessment.no_leg.venue.value,
            assessment.no_leg.market_id,
            assessment.requested_qty,
        )
        previous = self._written.get(key)
        if not should_persist_assessment(previous, assessment, now=assessment.computed_utc):
            return False

        def apply(conn: sqlite3.Connection, assessment: OpportunityAssessment = assessment) -> None:
            insert_assessment(conn, assessment)

        self._enqueue(apply)
        self._written[key] = AssessmentSnapshot(
            result_class=assessment.result_class,
            reasons=frozenset(assessment.reasons),
            net_profit=assessment.net_profit,
            written_utc=assessment.computed_utc,
        )
        return True


def _net_moved(previous: Decimal | None, current: Decimal | None, band: Decimal) -> bool:
    if previous is None or current is None:
        return previous is not current
    return abs(current - previous) > band
