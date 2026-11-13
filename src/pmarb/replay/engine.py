"""Replay a raw log through the collector's parsers, books, and evaluator.

Inputs are opened read-only. Assessments and episodes are written to a new
SQLite file. A decision at time T uses only envelopes with ``recv_mono_ns``
at or before T, and only review rows whose ``applied_utc`` is at or before T.

Two replays of the same input write the same assessment rows, ignoring row
ids. ``verify_replay`` compares those rows with the source database. A live
row and a replay row can also pair as ``dedupe_timing`` when they share group,
legs, quantity, class, and reasons, their net profit is within $0.05, and
``computed_utc`` differs by at most 60 seconds (the assessment heartbeat).
Episode observation counts can be higher than a live run when one poll was
logged as several envelopes at different receipt times. Envelopes that share
``recv_mono_ns`` are applied together and evaluated once.

Torn gzip tails are skipped and recorded as a gap. They are not truncated.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pmarb.collector.books import GENERATION, BookMeta, BookState, BookStore, market_key
from pmarb.collector.clock import ensure_utc, parse_utc
from pmarb.collector.episodes import EpisodeTracker
from pmarb.collector.evaluator import HEARTBEAT, PROFIT_BAND, Evaluator
from pmarb.collector.freshness import (
    CLOCK_JUMP,
    STALE,
    ClockJumpGuard,
    book_is_cached,
    is_fresh,
    max_age_for,
)
from pmarb.collector.persist import WriteSink
from pmarb.collector.registry import Registry, read_registry
from pmarb.collector.runtime import ADAPTER_ERROR, FRESHNESS_INTERVAL_S, RECORDING_BACKPRESSURE
from pmarb.collector.scheduler import build_poll_plan
from pmarb.config import Settings
from pmarb.models import Book, EquivalenceGroup, GroupStatus, Market, OpportunityAssessment, Venue
from pmarb.replay.clock import LookaheadError, ReplayClock
from pmarb.storage.db import Write, insert_collector_event, open_reader, open_writer
from pmarb.storage.rawlog import (
    SUFFIX,
    CorruptLogError,
    Envelope,
    inspect_tail,
    iter_records,
    list_log_files,
)
from pmarb.venues.kalshi.parse import parse_book as parse_kalshi_book
from pmarb.venues.kalshi.rest import KalshiAdapter
from pmarb.venues.polymarket_us.maintenance import in_maintenance
from pmarb.venues.polymarket_us.parse import parse_book as parse_poly_book
from pmarb.venues.polymarket_us.rest import PolymarketUSAdapter

# Same 250 ms step as ``Collector._freshness_loop``. Integer nanoseconds avoid
# drift when the clock advances between envelopes.
_INTERVAL_NS = 250_000_000
if round(FRESHNESS_INTERVAL_S * 1_000_000_000) != _INTERVAL_NS:
    raise RuntimeError("replay freshness step drifted from the collector")

_PROCESS_REASONS = frozenset({CLOCK_JUMP, RECORDING_BACKPRESSURE, GENERATION, ADAPTER_ERROR})
_APPROVE = frozenset({"approve", "auto_approve"})
_COMPARE_FIELDS = (
    "group_id",
    "yes_market_key",
    "no_market_key",
    "qty",
    "qualified",
    "reasons_json",
    "gross_edge",
    "net_profit",
    "roc",
    "capital",
    "fee_yes",
    "fee_no",
    "fee_provenance_json",
    "avg_yes",
    "avg_no",
    "max_profitable_qty",
    "age_yes_ms",
    "age_no_ms",
    "skew_ms",
    "books_atomic",
    "class",
    "exception_risk",
    "model_version",
    "computed_utc",
)
_KEY_FIELDS = ("group_id", "yes_market_key", "no_market_key", "qty", "computed_utc")

VERIFY_NOTE = (
    "Row ids are ignored. dedupe_timing pairs a live row with a replay row that share "
    "group, legs, quantity, class, and reasons, with net profit within 0.05 and "
    "computed_utc within 60s (the assessment heartbeat). Episode observation counts can "
    "differ when one live poll was logged as several envelopes."
)


class ReplayError(RuntimeError):
    """The recording or the output path cannot be replayed."""


@dataclass(frozen=True, slots=True)
class RawGap:
    """One raw file that was not fully readable. The file itself is unchanged."""

    path: str
    kind: str
    detail: str
    records_kept: int
    skipped_bytes: int


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What one run wrote, plus the receipt order it applied."""

    out_db: Path
    envelopes: int
    receipts: tuple[tuple[int, str], ...]
    gaps: tuple[RawGap, ...]
    assessments: int
    episodes: int


@dataclass(frozen=True, slots=True)
class FieldMismatch:
    """One column that differs for the same group, legs, quantity, and time."""

    group_id: str
    yes_market_key: str
    no_market_key: str
    qty: str
    computed_utc: str
    field: str
    live: str
    replay: str


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """How replay assessments compare with rows already stored in the source DB."""

    matched: int
    compared: int
    dedupe_timing: int
    mismatches: tuple[FieldMismatch, ...]
    unmatched: tuple[str, ...]

    @property
    def match_rate(self) -> float:
        if self.compared == 0:
            return 1.0
        return self.matched / self.compared

    @property
    def ok(self) -> bool:
        """True when every row matched exactly or as documented dedupe timing."""
        return not self.mismatches and not self.unmatched


@dataclass(frozen=True, slots=True)
class _Recorded:
    mono_ns: int
    venue: str
    path: str
    index: int
    envelope: Envelope
    wall: datetime


@dataclass(frozen=True, slots=True)
class _Review:
    row_id: int
    applied_utc: datetime
    action: str
    reviewer: str


class _GroupHistory:
    """Status of one group as of a wall time.

    Membership, rules hashes, and ``exception_risk`` come from the groups row,
    which is the stored snapshot rather than a full history of edits. Status,
    reviewer, and ``decided_utc`` follow applied ``review_log`` rows. With no
    applied log, an approved row is visible only once ``decided_utc`` has been
    reached.
    """

    def __init__(self, final: EquivalenceGroup, events: tuple[_Review, ...]) -> None:
        self.final = final
        self.events = events
        self.candidate = final.model_copy(
            update={"status": GroupStatus.CANDIDATE, "reviewer": None, "decided_utc": None}
        )

    def at(self, when: datetime) -> EquivalenceGroup:
        when = ensure_utc(when)
        if not self.events:
            group = self._from_row(when)
        else:
            group = self.candidate
            for event in self.events:
                if event.applied_utc > when:
                    break
                group = _apply_review(self.final, group, event)
        if group.status is GroupStatus.APPROVED and (
            group.decided_utc is None or group.decided_utc > when
        ):
            raise LookaheadError(
                f"group {group.group_id} approved at {group.decided_utc} is used at "
                f"{when.isoformat()}"
            )
        return group

    def _from_row(self, when: datetime) -> EquivalenceGroup:
        decided = self.final.decided_utc
        if decided is not None and decided > when:
            return self.candidate
        if self.final.status is GroupStatus.APPROVED and (decided is None or decided > when):
            return self.candidate
        return self.final


class _ApplyNow:
    """Run each queued write on this connection, in call order."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def put_nowait(self, item: object) -> None:
        if isinstance(item, Write):
            item.apply(self._conn)
            return
        apply = getattr(item, "apply", None)
        if callable(apply):
            apply(self._conn)
            return
        if callable(item):
            item(self._conn)
            return
        raise TypeError(f"unsupported replay write: {type(item).__name__}")

    def qsize(self) -> int:
        return 0


def parse_rest_books(env: Envelope) -> list[Book]:
    """Re-parse one ``rest_book`` body with the venue's ``parse_book``.

    Kalshi bodies are the batch ``{"orderbooks": [...]}`` response, or one
    ``{"ticker", "orderbook_fp"}`` object. Polymarket US bodies are the single
    book response ``parse_book`` already accepts. ``conn_gen`` is the book's
    connection generation.
    """
    wall = parse_utc(env.recv_utc)
    if wall is None:
        raise ReplayError(f"envelope has no recv_utc: {env.recv_utc!r}")
    mono = env.recv_mono_ns / 1_000_000_000
    try:
        venue = Venue(env.venue)
    except ValueError as exc:
        raise ReplayError(f"unknown venue {env.venue!r}") from exc
    if venue is Venue.KALSHI:
        return _parse_kalshi(env, wall, mono)
    if venue is Venue.POLYMARKET_US:
        return [_parse_poly(env, wall, mono)]
    raise ReplayError(f"no book parser for {venue.value}")


def verify_replay(
    raw_root: Path | str,
    source_db: Path | str,
    *,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    settings: Settings | None = None,
) -> VerifyReport:
    """Replay ``raw_root`` against ``source_db`` and compare assessment rows.

    The replay database is temporary and removed before this returns. The
    source database and the raw log are not modified.
    """
    with tempfile.TemporaryDirectory(prefix="pmarb-replay-") as tmp:
        out = Path(tmp) / "replay.sqlite"
        ReplayEngine(
            raw_root,
            source_db,
            out,
            since=since,
            until=until,
            settings=settings,
        ).run()
        return _compare_assessments(Path(source_db), out)


def format_verify_report(report: VerifyReport) -> str:
    """Plain-text verify output: rate, mismatched fields, then the note."""
    lines = [
        f"match_rate={report.match_rate:.6f}",
        f"matched={report.matched}",
        f"compared={report.compared}",
        f"dedupe_timing={report.dedupe_timing}",
    ]
    for item in report.mismatches:
        lines.append(
            "mismatch "
            f"group={item.group_id} yes={item.yes_market_key} no={item.no_market_key} "
            f"qty={item.qty} computed_utc={item.computed_utc} field={item.field} "
            f"live={item.live} replay={item.replay}"
        )
    lines.extend(report.unmatched)
    lines.append(f"note: {VERIFY_NOTE}")
    return "\n".join(lines) + "\n"


class ReplayEngine:
    """Feed a raw log through the same book, freshness, and episode code as live.

    ``groups_from`` is ``source_db``: group membership is the groups table, and
    approval time comes from ``review_log`` (or ``groups.decided_utc`` when that
    group has no applied log). ``since`` and ``until`` are inclusive bounds on
    ``recv_utc``. The source database and the raw log are opened read-only.
    """

    def __init__(
        self,
        raw_root: Path | str,
        source_db: Path | str,
        out_db: Path | str,
        *,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        groups_from: str = "source_db",
        settings: Settings | None = None,
        on_assessment: Callable[[int, OpportunityAssessment], None] | None = None,
    ) -> None:
        if groups_from != "source_db":
            raise ReplayError(f"unsupported groups_from: {groups_from!r}")
        self.raw_root = Path(raw_root)
        self.source_db = Path(source_db)
        self.out_db = Path(out_db)
        self.since = _bound(since, "since")
        self.until = _bound(until, "until")
        self.groups_from = groups_from
        self.settings = settings if settings is not None else Settings(_env_file=None)
        self.on_assessment = on_assessment
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ReplayError("since is after until")
        self._ran = False

    def run(self) -> ReplayResult:
        """Replay once. Replaces ``out_db`` when that path is not the source DB."""
        if self._ran:
            raise ReplayError("replay already ran")
        self._ran = True
        _refuse_same_file(self.out_db, self.source_db)
        if not self.raw_root.is_dir():
            raise ReplayError(f"raw log directory does not exist: {self.raw_root}")
        inputs = self._input_paths()
        before = _stamp(inputs)
        out_conn: sqlite3.Connection | None = None
        error: BaseException | None = None
        result: ReplayResult | None = None
        try:
            recorded, gaps = load_recording(self.raw_root, since=self.since, until=self.until)
            histories, markets, seen = _load_source(self.source_db)
            _reset_output(self.out_db)
            out_conn = open_writer(self.out_db)
            result = _Session(self, out_conn, recorded, gaps, histories, markets, seen).run()
        except (
            ReplayError,
            LookaheadError,
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            RuntimeError,
            ArithmeticError,
            CorruptLogError,
        ) as exc:
            error = exc
        finally:
            if out_conn is not None:
                out_conn.close()
        if _stamp(inputs) != before:
            raise ReplayError("replay changed the source database or the raw log") from error
        if error is not None:
            raise error
        if result is None:
            raise ReplayError("replay produced no result")
        return result

    def _input_paths(self) -> list[Path]:
        paths = [self.source_db]
        raw = self.raw_root / "raw"
        if raw.is_dir():
            paths.extend(sorted(path for path in raw.rglob("*") if path.is_file()))
        return paths


class _Session:
    """One forward pass. The source connection is already closed."""

    def __init__(
        self,
        engine: ReplayEngine,
        out: sqlite3.Connection,
        recorded: tuple[_Recorded, ...],
        gaps: tuple[RawGap, ...],
        histories: dict[str, _GroupHistory],
        markets: dict[str, Market],
        seen: dict[str, datetime | None],
    ) -> None:
        self.engine = engine
        self.out = out
        self.recorded = recorded
        self.gaps = gaps
        self.histories = histories
        self._all_markets = markets
        self._market_seen = seen
        self.clock = ReplayClock()
        self.store = BookStore()
        self.registry = Registry()
        self._jump = ClockJumpGuard()
        self._in_maintenance = False
        self.cadence_s = engine.settings.min_poll_cadence_s
        self._polled_ids: frozenset[str] = frozenset()
        self._receipts: list[tuple[int, str]] = []
        sink = WriteSink(_ApplyNow(out))
        # ``__new__`` skips the HTTP client. Replay only calls ``fee_model``.
        kalshi = KalshiAdapter.__new__(KalshiAdapter)
        kalshi._settings = engine.settings
        poly = PolymarketUSAdapter.__new__(PolymarketUSAdapter)
        poly._settings = engine.settings
        self._episodes = EpisodeTracker(
            enqueue=sink,
            cadence_s=lambda: self.cadence_s,
            now=self.clock.wall,
        )
        self._evaluator = Evaluator(
            registry=self.registry,
            store=self.store,
            kalshi=kalshi,
            polyus=poly,
            settings=engine.settings,
            episodes=self._episodes,
            enqueue=sink,
            clock=self.clock,
            opens_episodes=lambda group_id: group_id in self._polled_ids,
            cadence_s=lambda: self.cadence_s,
            on_assessment=(
                (lambda assessment: engine.on_assessment(self.clock.mono_ns, assessment))
                if engine.on_assessment is not None
                else None
            ),
        )

    def run(self) -> ReplayResult:
        for gap in self.gaps:
            self._event(
                None,
                "raw_gap",
                {
                    "path": gap.path,
                    "kind": gap.kind,
                    "detail": gap.detail,
                    "records_kept": gap.records_kept,
                    "skipped_bytes": gap.skipped_bytes,
                    "action": "skipped",
                },
                utc=_gap_time(gap.path),
            )
        if self.recorded:
            self._play()
        self._episodes.flush()
        self.out.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assessments = int(self.out.execute("SELECT COUNT(*) FROM assessments").fetchone()[0])
        episodes = int(self.out.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])
        return ReplayResult(
            out_db=self.engine.out_db,
            envelopes=len(self.recorded),
            receipts=tuple(self._receipts),
            gaps=self.gaps,
            assessments=assessments,
            episodes=episodes,
        )

    def _play(self) -> None:
        """Apply receipts in order, and tick freshness every 250 ms of replay time.

        The first tick is one interval after the first envelope. When an
        envelope falls on a tick, the envelope is applied first. After the
        last envelope, ticks continue long enough for a book to go stale and
        for a data gap to close an open episode.
        """
        events = self.recorded
        index = 0
        next_tick = events[0].mono_ns + _INTERVAL_NS
        tail_end: int | None = None
        while True:
            if index < len(events) and events[index].mono_ns <= next_tick:
                mono = events[index].mono_ns
                batch: list[_Recorded] = []
                while index < len(events) and events[index].mono_ns == mono:
                    batch.append(events[index])
                    index += 1
                self._apply_batch(batch)
                continue
            if index >= len(events) and tail_end is None:
                self._refresh_plan()
                tail_end = self.clock.mono_ns + self._tail_ns()
            if tail_end is not None and next_tick > tail_end:
                break
            if self._until_blocks(next_tick):
                break
            if self.clock.started and self.clock.mono_ns > next_tick:
                raise LookaheadError(
                    f"freshness tick {next_tick} is behind applied time {self.clock.mono_ns}"
                )
            self.clock.advance_to(next_tick)
            self._freshness_tick()
            next_tick += _INTERVAL_NS

    def _apply_batch(self, batch: Sequence[_Recorded]) -> None:
        touched: set[str] = set()
        for item in batch:
            env = item.envelope
            self.clock.seek(item.mono_ns, item.wall)
            if env.recv_mono_ns > self.clock.mono_ns or item.wall > self.clock.wall():
                raise LookaheadError(
                    f"envelope {env.recv_mono_ns} at {item.wall.isoformat()} "
                    "is after the decision clock"
                )
            self._receipts.append((env.recv_mono_ns, env.venue))
            if env.kind != "rest_book":
                continue
            if env.status not in (None, 200):
                self._event(
                    env.venue, "replay_skip", {"status": env.status, "endpoint": env.endpoint}
                )
                continue
            self._refresh_plan()
            touched |= self._ingest(env)
        self._refresh_plan()
        touched |= self._expire(self.clock.monotonic(), self.clock.wall())
        self._evaluate(touched)

    def _freshness_tick(self) -> None:
        self._refresh_plan()
        now_mono = self.clock.monotonic()
        now_wall = self.clock.wall()
        changed: set[str] = set()
        if self._jump.check(now_mono, now_wall):
            self._assert_no_lookahead("clock_jump")
            self.store.invalidate(None, CLOCK_JUMP)
            self._episodes.on_gap(CLOCK_JUMP)
            self._event(None, CLOCK_JUMP, {"mono": now_mono, "wall": now_wall.isoformat()})
            changed |= self._approved_ids()
        if self._enter_maintenance(now_wall):
            changed |= self._approved_ids()
        changed |= self._expire(now_mono, now_wall)
        self._evaluate(set(self._polled_ids) | changed)

    def _ingest(self, env: Envelope) -> set[str]:
        before = {
            venue: self.store.generation(venue) for venue in (Venue.KALSHI, Venue.POLYMARKET_US)
        }
        books = parse_rest_books(env)
        touched: set[str] = set()
        for book in books:
            if book.received_monotonic > self.clock.monotonic() + 1e-9:
                raise LookaheadError(
                    f"{book.venue.value}:{book.market_id} received at "
                    f"{book.received_monotonic} after {self.clock.monotonic()}"
                )
            if book.received_utc > self.clock.wall():
                raise LookaheadError(
                    f"{book.venue.value}:{book.market_id} wall time "
                    f"{book.received_utc.isoformat()} is after the decision"
                )
            meta = BookMeta(
                received_mono=book.received_monotonic,
                received_wall=book.received_utc,
                connection_generation=book.connection_generation,
                source="poll",
                cached=book_is_cached(book),
                books_atomic_group_id=None,
                discovery_sample=self._is_discovery(book.venue, book.market_id),
            )
            if not self.store.put(book, meta):
                continue
            touched |= self._groups_touching(book.venue, book.market_id)
            if self.store.generation(book.venue) > before[book.venue]:
                touched |= self._groups_for_venue(book.venue)
        return touched

    def _expire(self, now_mono: float, now_wall: datetime) -> set[str]:
        changed: set[str] = set()
        for state in self.store.items():
            if state.invalid_reason in _PROCESS_REASONS:
                continue
            fresh, reason = is_fresh(state, now_mono, now_wall, self._max_age(state))
            if fresh or reason == state.invalid_reason:
                continue
            why = reason or STALE
            self.store.invalidate(state.market_key, why)
            if why == STALE:
                self._event(str(state.book.venue), "stale", {"market_key": state.market_key})
            changed |= self._groups_for_key(state.market_key)
        return changed

    def _evaluate(self, group_ids: set[str]) -> None:
        self._refresh_plan()
        self._assert_no_lookahead("evaluate")
        # ``on_books_updated`` is async because the collector hook is async.
        # The body does not await; driving it keeps the live call path.
        coro = self._evaluator.on_books_updated(set(group_ids))
        try:
            coro.send(None)
        except StopIteration:
            return
        coro.close()
        raise RuntimeError("evaluator.on_books_updated awaited; replay cannot follow that")

    def _refresh_plan(self) -> None:
        now = self.clock.wall()
        visible: dict[str, Market] = {}
        for key, market in self._all_markets.items():
            seen = self._market_seen.get(key)
            if seen is not None and seen > now:
                continue
            visible[key] = market
        self.registry.markets.clear()
        self.registry.markets.update(visible)
        current = {group_id: history.at(now) for group_id, history in self.histories.items()}
        self.registry.groups.clear()
        self.registry.groups.update(current)
        plan = build_poll_plan(
            tuple(current.values()),
            tuple(visible.values()),
            now,
            max_polled_groups=self.engine.settings.max_polled_groups,
            start_window=timedelta(minutes=self.engine.settings.polled_group_start_window_min),
            sample_interval_s=self.engine.settings.sample_interval_s,
            min_cadence_s=self.engine.settings.min_poll_cadence_s,
            polyus_books_rps=self.engine.settings.polyus_books_rps,
        )
        self.cadence_s = plan.cadence_s
        self._polled_ids = frozenset(item.group_id for item in plan.polled)

    def _assert_no_lookahead(self, what: str) -> None:
        now_mono = self.clock.monotonic()
        now_wall = self.clock.wall()
        for state in self.store.items():
            if state.meta.received_mono > now_mono + 1e-9:
                raise LookaheadError(f"{what} saw {state.market_key} received after the clock")
            if state.meta.received_wall > now_wall:
                raise LookaheadError(f"{what} saw {state.market_key} with a future wall time")
        for key in self.registry.markets:
            seen = self._market_seen.get(key)
            if seen is not None and seen > now_wall:
                raise LookaheadError(f"{what} used market {key} first seen at {seen.isoformat()}")
        for group in self.registry.groups.values():
            if group.status is not GroupStatus.APPROVED:
                continue
            if group.decided_utc is None or group.decided_utc > now_wall:
                raise LookaheadError(
                    f"{what} used {group.group_id} approved at {group.decided_utc}"
                )
        for mono_ns, _venue in self._receipts:
            if mono_ns > self.clock.mono_ns:
                raise LookaheadError(f"{what} applied envelope {mono_ns} after the clock")

    def _enter_maintenance(self, now_wall: datetime) -> bool:
        active = in_maintenance(now_wall)
        if active and not self._in_maintenance:
            self._in_maintenance = True
            self._episodes.on_gap("maintenance")
            self._event(str(Venue.POLYMARKET_US), "maintenance", {})
            return True
        if not active:
            self._in_maintenance = False
        return False

    def _max_age(self, state: BookState) -> float:
        settings = self.engine.settings
        if state.meta.source == "stream":
            return max_age_for(
                "stream",
                self.cadence_s,
                stream_max_age_s=settings.stream_max_age_s,
                poll_multiple=settings.poll_max_age_cadence_multiple,
            )
        cadence = settings.sample_interval_s if state.meta.discovery_sample else self.cadence_s
        return max_age_for(
            "poll",
            cadence,
            stream_max_age_s=settings.stream_max_age_s,
            poll_multiple=settings.poll_max_age_cadence_multiple,
        )

    def _tail_ns(self) -> int:
        """Replay time after the last envelope for staleness and a data gap.

        A book stays fresh for about one max-age after receipt. Closing an
        episode then needs another two cadences, plus a second so the closing
        tick falls inside the horizon. The span uses books actually stored.
        """
        span = 0.0
        for state in self.store.items():
            cadence = (
                self.engine.settings.sample_interval_s
                if state.meta.discovery_sample
                else self.cadence_s
            )
            span = max(span, self._max_age(state) + 2 * cadence)
        if span <= 0:
            return 0
        return round((span + 1.0) * 1_000_000_000)

    def _until_blocks(self, mono_ns: int) -> bool:
        until = self.engine.until
        if until is None or not self.clock.started:
            return False
        delta = mono_ns - self.clock.mono_ns
        wall = self.clock.wall() + timedelta(microseconds=max(delta, 0) // 1000)
        return wall > until

    def _is_discovery(self, venue: Venue, market_id: str) -> bool:
        ids = self._groups_touching(venue, market_id)
        if not ids:
            return False
        return all(group_id not in self._polled_ids for group_id in ids)

    def _groups_touching(self, venue: Venue, market_id: str) -> set[str]:
        return self._groups_for_key(market_key(venue, market_id))

    def _groups_for_key(self, key: str) -> set[str]:
        found: set[str] = set()
        for group in self.registry.groups.values():
            if group.status is not GroupStatus.APPROVED:
                continue
            for member in group.members:
                if market_key(member.venue, member.market_id) == key:
                    found.add(group.group_id)
        return found

    def _groups_for_venue(self, venue: Venue) -> set[str]:
        prefix = f"{venue.value}:"
        found: set[str] = set()
        for group in self.registry.groups.values():
            if group.status is not GroupStatus.APPROVED:
                continue
            if any(
                market_key(member.venue, member.market_id).startswith(prefix)
                for member in group.members
            ):
                found.add(group.group_id)
        return found

    def _approved_ids(self) -> set[str]:
        return {
            group.group_id
            for group in self.registry.groups.values()
            if group.status is GroupStatus.APPROVED
        }

    def _event(
        self,
        venue: str | None,
        kind: str,
        detail: dict[str, Any],
        *,
        utc: datetime | None = None,
    ) -> None:
        when = self.clock.wall() if utc is None else utc
        insert_collector_event(self.out, when, venue, kind, detail)


def load_recording(
    raw_root: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[tuple[_Recorded, ...], tuple[RawGap, ...]]:
    """Read hourly files and merge them by ``recv_mono_ns``.

    Ties break by venue, then path, then position in the file. Torn tails and
    mid-file damage become gaps. Good members before the damage are kept.
    The files are not modified.
    """
    recorded: list[_Recorded] = []
    gaps: list[RawGap] = []
    for path in _raw_files(raw_root):
        records, gap = _read_file(path)
        if gap is not None:
            gaps.append(gap)
        for index, env in enumerate(records):
            wall = parse_utc(env.recv_utc)
            if wall is None:
                raise ReplayError(f"{path}: envelope {index} has no recv_utc")
            if since is not None and wall < since:
                continue
            if until is not None and wall > until:
                continue
            recorded.append(
                _Recorded(
                    mono_ns=env.recv_mono_ns,
                    venue=env.venue,
                    path=str(path),
                    index=index,
                    envelope=env,
                    wall=wall,
                )
            )
    recorded.sort(key=lambda item: (item.mono_ns, item.venue, item.path, item.index))
    return tuple(recorded), tuple(gaps)


def _raw_files(root: Path) -> list[Path]:
    raw = Path(root) / "raw"
    if not raw.is_dir():
        return []
    files: list[Path] = []
    for venue_dir in sorted(path for path in raw.iterdir() if path.is_dir()):
        files.extend(list_log_files(root, venue_dir.name))
    return files


def _read_file(path: Path) -> tuple[list[Envelope], RawGap | None]:
    try:
        scan = inspect_tail(path)
    except CorruptLogError as exc:
        kept = _consume_until_error(path)
        return kept, RawGap(
            path=str(path),
            kind="corrupt",
            detail=str(exc),
            records_kept=len(kept),
            skipped_bytes=0,
        )
    kept = list(iter_records([path], tolerate_torn_tail=True))
    if scan.truncated_bytes:
        return kept, RawGap(
            path=str(path),
            kind="torn_tail",
            detail=f"skipped {scan.truncated_bytes} torn-tail bytes at offset {scan.good_size}",
            records_kept=len(kept),
            skipped_bytes=scan.truncated_bytes,
        )
    return kept, None


def _consume_until_error(path: Path) -> list[Envelope]:
    kept: list[Envelope] = []
    records = iter_records([path])
    while True:
        try:
            kept.append(next(records))
        except StopIteration:
            return kept
        except CorruptLogError:
            return kept


def _gap_time(path: str) -> datetime:
    hour = _file_hour(Path(path))
    if hour is None:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return hour


def _file_hour(path: Path) -> datetime | None:
    try:
        day = datetime.strptime(path.parent.name, "%Y-%m-%d").replace(tzinfo=UTC)
        hour = int(path.name.removesuffix(SUFFIX))
    except ValueError:
        return None
    if not 0 <= hour <= 23:
        return None
    return day.replace(hour=hour)


def _load_source(
    path: Path,
) -> tuple[dict[str, _GroupHistory], dict[str, Market], dict[str, datetime | None]]:
    conn = open_reader(path)
    try:
        registry = read_registry(conn)
        events: dict[str, list[_Review]] = {}
        rows = conn.execute(
            """
            SELECT id, group_id, action, reviewer, applied_utc
            FROM review_log
            WHERE result = 'applied'
            ORDER BY applied_utc, id
            """
        )
        for row_id, group_id, action, reviewer, applied in rows:
            if not isinstance(group_id, str) or group_id not in registry.groups:
                continue
            when = parse_utc(applied if isinstance(applied, str) else None)
            if when is None:
                raise ReplayError(f"review_log {row_id} has no applied_utc")
            events.setdefault(group_id, []).append(
                _Review(
                    row_id=int(row_id),
                    applied_utc=when,
                    action=str(action),
                    reviewer="" if reviewer is None else str(reviewer),
                )
            )
        histories = {
            group_id: _GroupHistory(group, tuple(events.get(group_id, ())))
            for group_id, group in registry.groups.items()
        }
        seen: dict[str, datetime | None] = {}
        for key, first_seen in conn.execute("SELECT market_key, first_seen_utc FROM markets"):
            if isinstance(key, str):
                seen[key] = parse_utc(first_seen if isinstance(first_seen, str) else None)
        return histories, dict(registry.markets), seen
    finally:
        conn.close()


def _apply_review(
    final: EquivalenceGroup, current: EquivalenceGroup, event: _Review
) -> EquivalenceGroup:
    stamp = {"reviewer": event.reviewer or None, "decided_utc": event.applied_utc}
    if event.action in _APPROVE:
        if not event.reviewer:
            raise ReplayError(f"group {final.group_id} approval has no reviewer")
        return final.model_copy(update={**stamp, "status": GroupStatus.APPROVED})
    if event.action == "reject":
        return current.model_copy(update={**stamp, "status": GroupStatus.REJECTED})
    if event.action == "revoke":
        return current.model_copy(update={**stamp, "status": GroupStatus.REVOKED})
    if event.action == "mark_exception_risk":
        return current.model_copy(update={**stamp, "exception_risk": True})
    return current


def _parse_kalshi(env: Envelope, wall: datetime, mono: float) -> list[Book]:
    body = env.body
    if not isinstance(body, dict):
        raise ReplayError("Kalshi book body must be an object")
    rows = body.get("orderbooks")
    if isinstance(rows, list):
        books: list[Book] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ReplayError(f"Kalshi batch row {index} is not an object")
            ticker = row.get("ticker")
            orderbook = row.get("orderbook_fp")
            if not isinstance(ticker, str) or ticker == "" or not isinstance(orderbook, dict):
                raise ReplayError(f"Kalshi batch row {index} has no ticker and orderbook_fp")
            books.append(_kalshi_book(env, wall, mono, ticker, orderbook))
        return books
    orderbook = body.get("orderbook_fp", body)
    ticker = body.get("ticker")
    if not isinstance(ticker, str) or ticker == "":
        ticker = _ticker_from_endpoint(env.endpoint)
    if not isinstance(orderbook, dict) or ticker is None:
        raise ReplayError("Kalshi book body has no orderbook")
    return [_kalshi_book(env, wall, mono, ticker, orderbook)]


def _kalshi_book(
    env: Envelope,
    wall: datetime,
    mono: float,
    ticker: str,
    orderbook: dict[str, Any],
) -> Book:
    return parse_kalshi_book(
        orderbook,
        ticker=ticker,
        received_utc=wall,
        received_monotonic=mono,
        connection_generation=env.conn_gen,
        cache_headers=dict(env.cache_headers),
    )


def _parse_poly(env: Envelope, wall: datetime, mono: float) -> Book:
    body = env.body
    if not isinstance(body, dict):
        raise ReplayError("Polymarket US book body must be an object")
    book = parse_poly_book(
        body,
        received_utc=wall,
        received_monotonic=mono,
        connection_generation=env.conn_gen,
    )
    if env.cache_headers:
        book = book.model_copy(
            update={"raw_native": {**book.raw_native, "cache_headers": dict(env.cache_headers)}}
        )
    if book.market_id:
        return book
    slug = _slug_from_endpoint(env.endpoint)
    if slug is None:
        return book
    return book.model_copy(update={"market_id": slug})


def _ticker_from_endpoint(endpoint: str) -> str | None:
    if "tickers=" not in endpoint:
        return None
    raw = endpoint.split("tickers=", 1)[1].split("&", 1)[0].strip()
    return raw or None


def _slug_from_endpoint(endpoint: str) -> str | None:
    marker = "/markets/"
    if marker not in endpoint:
        return None
    slug = endpoint.split(marker, 1)[1].split("/", 1)[0].strip()
    return slug or None


def _bound(value: str | datetime | None, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if not isinstance(value, str):
        raise ReplayError(f"{name} must be an ISO timestamp")
    parsed = parse_utc(value)
    if parsed is None:
        raise ReplayError(f"invalid {name}: {value!r}")
    return parsed


def _refuse_same_file(out_db: Path, source_db: Path) -> None:
    if _same_file(out_db, source_db):
        raise ReplayError("replay output cannot overwrite the source database")


def _same_file(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    if left.exists() and right.exists():
        try:
            return left.samefile(right)
        except OSError:
            return False
    return False


def _reset_output(path: Path) -> None:
    if path.exists() and path.is_dir():
        raise ReplayError(f"replay output is a directory: {path}")
    for extra in ("", "-wal", "-shm"):
        candidate = path if extra == "" else Path(f"{path}{extra}")
        if candidate.is_file():
            candidate.unlink()


def _stamp(paths: Sequence[Path]) -> tuple[tuple[str, int | None, str | None], ...]:
    """Content fingerprints. Sidecar mtimes are ignored because readers can touch them."""
    found: list[tuple[str, int | None, str | None]] = []
    for path in paths:
        if not path.is_file():
            found.append((str(path), None, None))
            continue
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        found.append((str(path), path.stat().st_size, digest))
    return tuple(found)


def _compare_assessments(live_db: Path, replay_db: Path) -> VerifyReport:
    live_conn = open_reader(live_db)
    replay_conn = open_reader(replay_db)
    try:
        live_rows = _assessment_rows(live_conn)
        replay_rows = _assessment_rows(replay_conn)
    finally:
        live_conn.close()
        replay_conn.close()
    return _match_rows(live_rows, replay_rows)


def _assessment_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    sql = """
        SELECT group_id, yes_market_key, no_market_key, qty, qualified, reasons_json,
               gross_edge, net_profit, roc, capital, fee_yes, fee_no, fee_provenance_json,
               avg_yes, avg_no, max_profitable_qty, age_yes_ms, age_no_ms, skew_ms,
               books_atomic, "class", exception_risk, model_version, computed_utc
        FROM assessments
        ORDER BY group_id, yes_market_key, no_market_key, qty, computed_utc, id
    """
    rows: list[dict[str, Any]] = []
    for record in conn.execute(sql):
        rows.append(dict(zip(_COMPARE_FIELDS, record, strict=True)))
    return rows


def _match_rows(
    live_rows: Sequence[Mapping[str, Any]],
    replay_rows: Sequence[Mapping[str, Any]],
) -> VerifyReport:
    live_by = _group_rows(live_rows)
    replay_by = _group_rows(replay_rows)
    exact = 0
    bad_pairs = 0
    mismatches: list[FieldMismatch] = []
    live_left: list[Mapping[str, Any]] = []
    replay_left: list[Mapping[str, Any]] = []
    for key in sorted(set(live_by) | set(replay_by)):
        left = live_by.get(key, [])
        right = replay_by.get(key, [])
        paired = min(len(left), len(right))
        for index in range(paired):
            diffs = _field_diffs(left[index], right[index])
            if diffs:
                bad_pairs += 1
                mismatches.extend(diffs)
            else:
                exact += 1
        live_left.extend(left[paired:])
        replay_left.extend(right[paired:])
    dedupe, live_only, replay_only = _pair_dedupe(live_left, replay_left)
    unmatched = tuple(_unmatched("live", row) for row in live_only) + tuple(
        _unmatched("replay", row) for row in replay_only
    )
    compared = exact + bad_pairs + dedupe + len(live_only) + len(replay_only)
    return VerifyReport(
        matched=exact + dedupe,
        compared=compared,
        dedupe_timing=dedupe,
        mismatches=tuple(mismatches),
        unmatched=unmatched,
    )


def _group_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, ...], list[Mapping[str, Any]]]:
    grouped: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_row_key(row), []).append(row)
    return grouped


def _row_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_text(row[field]) for field in _KEY_FIELDS)


def _field_diffs(live: Mapping[str, Any], replay: Mapping[str, Any]) -> list[FieldMismatch]:
    diffs: list[FieldMismatch] = []
    for field in _COMPARE_FIELDS:
        live_text = _text(live[field])
        replay_text = _text(replay[field])
        if live_text == replay_text:
            continue
        diffs.append(
            FieldMismatch(
                group_id=_text(live["group_id"]),
                yes_market_key=_text(live["yes_market_key"]),
                no_market_key=_text(live["no_market_key"]),
                qty=_text(live["qty"]),
                computed_utc=_text(live["computed_utc"]),
                field=field,
                live=live_text,
                replay=replay_text,
            )
        )
    return diffs


def _pair_dedupe(
    live_rows: Sequence[Mapping[str, Any]],
    replay_rows: Sequence[Mapping[str, Any]],
) -> tuple[int, list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    used: set[int] = set()
    pairs = 0
    live_only: list[Mapping[str, Any]] = []
    for live in live_rows:
        match = _nearest_dedupe(live, replay_rows, used)
        if match is None:
            live_only.append(live)
            continue
        used.add(match)
        pairs += 1
    replay_only = [row for index, row in enumerate(replay_rows) if index not in used]
    return pairs, live_only, replay_only


def _nearest_dedupe(
    live: Mapping[str, Any],
    replay_rows: Sequence[Mapping[str, Any]],
    used: set[int],
) -> int | None:
    best: int | None = None
    best_gap: float | None = None
    for index, replay in enumerate(replay_rows):
        if index in used or not _same_dedupe_identity(live, replay):
            continue
        gap = _time_gap_s(live, replay)
        if gap is None or gap > HEARTBEAT.total_seconds():
            continue
        if not _net_within_band(live.get("net_profit"), replay.get("net_profit")):
            continue
        if best_gap is None or gap < best_gap:
            best = index
            best_gap = gap
    return best


def _same_dedupe_identity(live: Mapping[str, Any], replay: Mapping[str, Any]) -> bool:
    fields = ("group_id", "yes_market_key", "no_market_key", "qty", "class", "reasons_json")
    return all(_text(live[field]) == _text(replay[field]) for field in fields)


def _time_gap_s(live: Mapping[str, Any], replay: Mapping[str, Any]) -> float | None:
    left = parse_utc(_text(live["computed_utc"]))
    right = parse_utc(_text(replay["computed_utc"]))
    if left is None or right is None:
        return None
    return abs((left - right).total_seconds())


def _net_within_band(live: object, replay: object) -> bool:
    left = _decimal(live)
    right = _decimal(replay)
    if left is None or right is None:
        return left is None and right is None
    return abs(left - right) <= PROFIT_BAND


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "" or value == "null":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def _unmatched(side: str, row: Mapping[str, Any]) -> str:
    return (
        f"unmatched side={side} group={_text(row['group_id'])} "
        f"yes={_text(row['yes_market_key'])} no={_text(row['no_market_key'])} "
        f"qty={_text(row['qty'])} computed_utc={_text(row['computed_utc'])}"
    )


def _text(value: object) -> str:
    if value is None:
        return "null"
    return str(value)
