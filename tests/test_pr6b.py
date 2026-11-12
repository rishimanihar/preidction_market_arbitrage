"""Evaluator, discovery, review mode, episodes, and ``pmarb scan --once``.

No network. Clocks are injected. The scan test replaces the factory's adapter
classes with fakes that never open a socket.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pmarb import cli
from pmarb.app import queries
from pmarb.collector.books import BookMeta, BookStore
from pmarb.collector.clock import FakeClock
from pmarb.collector.discovery import Discovery
from pmarb.collector.episodes import EpisodeTracker
from pmarb.collector.evaluator import (
    AssessmentSnapshot,
    Evaluator,
    downgrade_auto_qualified,
    should_persist_assessment,
)
from pmarb.collector.factory import build_collector
from pmarb.collector.registry import Registry
from pmarb.collector.review_mode import AUTO_REVIEWER, ReviewMode
from pmarb.collector.runtime import Collector
from pmarb.config import Settings
from pmarb.economics import assess_group
from pmarb.matching.candidates import generate_candidates
from pmarb.matching.rules import rules_hash
from pmarb.models import (
    AssessmentClass,
    Book,
    Episode,
    EquivalenceGroup,
    GroupMember,
    GroupStatus,
    Leg,
    Level,
    Market,
    Side,
    Venue,
)
from pmarb.review.spool import ReviewDecision, write_decision
from pmarb.storage.db import open_writer, upsert_episode
from pmarb.venues.kalshi.rest import KalshiAdapter
from pmarb.venues.polymarket_us.rest import PolymarketUSAdapter
from tests.test_matching import kalshi, poly

WALL = datetime(2026, 6, 3, 18, tzinfo=UTC)
MONO = 1000.0
PLENTY = 10**12


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("PMARB_", "KALSHI_", "POLYUS_")):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr("pmarb.collector.runtime.in_maintenance", lambda _now: False)
    monkeypatch.setattr("pmarb.collector.freshness.in_maintenance", lambda _now: False)


class FakeVenue:
    """Async adapter double. ``markets is None`` builds one live contract."""

    venue = Venue.KALSHI

    def __init__(
        self,
        http: object = None,
        *,
        settings: Settings | None = None,
        markets: list[Market] | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self.http = http
        self._settings = settings
        self._clock = clock
        self.fail = False
        self.omit = False
        self.price = Decimal("0.20")
        if markets is None:
            wall = datetime.now(UTC) if clock is None else clock.wall()
            markets = [_listed_market(self.venue, wall)]
        self.markets = markets

    async def list_markets(self, horizon_start: datetime, horizon_end: datetime):
        del horizon_start, horizon_end
        if self.fail:
            raise RuntimeError("discovery failed")
        if self.omit:
            return
        for market in self.markets:
            yield market

    async def get_market(self, market_id: str) -> Market:
        for market in self.markets:
            if market.market_id == market_id:
                return market
        raise KeyError(market_id)

    async def get_books(self, market_ids: list[str]) -> list[Book]:
        wall = self._now_wall()
        mono = self._now_mono()
        return [_book(self.venue, market_id, self.price, mono, wall) for market_id in market_ids]

    def book_stream(self, market_ids: list[str]):
        del market_ids

    def fee_model(self, market: Market):
        return _fee_model(self.venue, self._settings, market)

    def _now_wall(self) -> datetime:
        if self._clock is None:
            return datetime.now(UTC)
        return self._clock.wall()

    def _now_mono(self) -> float:
        if self._clock is None:
            return time.monotonic()
        return self._clock.monotonic()


class FakeKalshi(FakeVenue):
    venue = Venue.KALSHI


class FakePoly(FakeVenue):
    venue = Venue.POLYMARKET_US


class Pipe:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: FakeClock,
        collector: Collector,
        discovery: Discovery,
        evaluator: Evaluator,
        registry: Registry,
        kalshi: FakeVenue,
        polyus: FakeVenue,
        settings: Settings,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.collector = collector
        self.discovery = discovery
        self.evaluator = evaluator
        self.registry = registry
        self.kalshi = kalshi
        self.polyus = polyus
        self.settings = settings

    async def discover(self) -> None:
        await self.discovery.run_once()

    async def poll(self) -> None:
        await self.collector._poll_once()

    def assessments(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            'SELECT "class", reasons_json, net_profit, books_atomic FROM assessments ORDER BY id'
        ).fetchall()

    def episodes(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            'SELECT "class", termination_reason FROM episodes ORDER BY id'
        ).fetchall()

    def review_log(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT action, reviewer, result FROM review_log ORDER BY id"
        ).fetchall()


def _settings(
    tmp_path,
    *,
    review_mode: str = "auto",
    precision: str | None = "0.0001",
    theta: Decimal | None = Decimal("0.075"),
    fee_scenario: str = "conservative",
) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        review_mode=review_mode,  # type: ignore[arg-type]
        kalshi_member_precision=precision,  # type: ignore[arg-type]
        polyus_theta=theta,
        fee_scenario=fee_scenario,  # type: ignore[arg-type]
    )


def _pipe(
    tmp_path,
    *,
    review_mode: str = "auto",
    precision: str | None = "0.0001",
    theta: Decimal | None = Decimal("0.075"),
    fee_scenario: str = "conservative",
) -> Pipe:
    clock = FakeClock(mono=MONO, wall=WALL)
    settings = _settings(
        tmp_path,
        review_mode=review_mode,
        precision=precision,
        theta=theta,
        fee_scenario=fee_scenario,
    )
    conn = open_writer(tmp_path / "pmarb.sqlite")
    listed, other = _pair(clock.wall())
    kalshi_venue = FakeKalshi(settings=settings, markets=[listed], clock=clock)
    poly_venue = FakePoly(settings=settings, markets=[other], clock=clock)
    registry = Registry()
    store = BookStore()
    holder: list[Collector] = []

    def enqueue(fn) -> None:
        fn(conn)

    tracker = EpisodeTracker(enqueue=enqueue, cadence_s=lambda: holder[0].cadence_s, now=clock.wall)
    review = ReviewMode(
        settings=settings, registry=registry, enqueue=enqueue, clock=clock, episodes=tracker
    )
    discovery = Discovery(
        kalshi=kalshi_venue,
        polyus=poly_venue,
        registry=registry,
        review=review,
        clock=clock,
        interval_s=lambda: settings.discovery_interval_s,
        enqueue=enqueue,
        episodes=tracker,
    )
    evaluator = Evaluator(
        registry=registry,
        store=store,
        kalshi=kalshi_venue,
        polyus=poly_venue,
        settings=settings,
        episodes=tracker,
        enqueue=enqueue,
        clock=clock,
    )
    collector = Collector(
        kalshi=kalshi_venue,
        polyus=poly_venue,
        clock=clock,
        on_books_updated=evaluator.on_books_updated,
        on_gap=tracker.on_gap,
        settings=settings,
        store=store,
        groups=(),
        markets=(),
        install_signals=False,
        disk_free=lambda _path: PLENTY,
        discovery=discovery,
    )
    holder.append(collector)
    discovery.bind(collector)
    evaluator.bind(collector)
    return Pipe(
        conn,
        clock,
        collector,
        discovery,
        evaluator,
        registry,
        kalshi_venue,
        poly_venue,
        settings,
    )


def _pair(wall: datetime) -> tuple[Market, Market]:
    listed = kalshi("NYG", "New York Giants").model_copy(
        update={"event_start_utc": wall, "fee_meta": {"fee_multiplier": "1"}}
    )
    other = poly().model_copy(update={"event_start_utc": wall})
    return listed, other


def _listed_market(venue: Venue, wall: datetime) -> Market:
    listed, other = _pair(wall)
    return listed if venue is Venue.KALSHI else other


def _book(venue: Venue, market_id: str, price: Decimal, mono: float, wall: datetime) -> Book:
    level = Level(price=price, size=Decimal(200))
    return Book(
        venue=venue,
        market_id=market_id,
        yes_asks=[level],
        no_asks=[level],
        received_utc=wall,
        received_monotonic=mono,
        connection_generation=0,
        valid=True,
    )


def _fee_model(venue: Venue, settings: Settings | None, market: Market):
    if venue is Venue.KALSHI:
        adapter = KalshiAdapter.__new__(KalshiAdapter)
        adapter._settings = settings
        return adapter.fee_model(market)
    adapter = PolymarketUSAdapter.__new__(PolymarketUSAdapter)
    adapter._settings = settings
    return adapter.fee_model(market)


def _candidate(market_k: Market, market_p: Market) -> EquivalenceGroup:
    return EquivalenceGroup(
        group_id="fee-group",
        outcome="fee",
        members=(
            GroupMember(
                venue=market_k.venue,
                market_id=market_k.market_id,
                native_side_for_outcome=Side.YES,
            ),
            GroupMember(
                venue=market_p.venue,
                market_id=market_p.market_id,
                native_side_for_outcome=Side.YES,
            ),
        ),
        status=GroupStatus.CANDIDATE,
    )


def _quote(settings: Settings, market_k: Market, market_p: Market) -> list:
    books = {
        market_k.market_key: _book(market_k.venue, market_k.market_id, Decimal("0.20"), MONO, WALL),
        market_p.market_key: _book(market_p.venue, market_p.market_id, Decimal("0.20"), MONO, WALL),
    }
    models = {
        market_k.market_key: _fee_model(Venue.KALSHI, settings, market_k),
        market_p.market_key: _fee_model(Venue.POLYMARKET_US, settings, market_p),
    }
    return assess_group(
        _candidate(market_k, market_p),
        books,
        models,
        Decimal(1),
        now_monotonic=MONO,
        now_wall=WALL,
        books_atomic=True,
        model_version="m1-1",
    )


async def _discover_and_poll(pipe: Pipe) -> None:
    await pipe.discover()
    await pipe.poll()


def test_unknown_fees_reject_with_venue_reasons(tmp_path) -> None:
    """``fee_scenario="none"`` leaves an unset account value unknown."""
    market_k, market_p = _pair(WALL)
    known = Settings(
        _env_file=None, kalshi_member_precision="0.0001", polyus_theta=Decimal("0.075")
    )
    missing_precision = Settings(_env_file=None, polyus_theta=Decimal("0.075"), fee_scenario="none")
    missing_theta = Settings(_env_file=None, kalshi_member_precision="0.0001", fee_scenario="none")
    missing_both = Settings(_env_file=None, fee_scenario="none")

    only_kalshi = _quote(missing_precision, market_k, market_p)[0]
    assert "fee_unknown_kalshi" in only_kalshi.reasons
    assert "fee_unknown_polymarket_us" not in only_kalshi.reasons
    assert only_kalshi.net_profit is None
    assert only_kalshi.result_class is AssessmentClass.REJECTED

    only_poly = _quote(missing_theta, market_k, market_p)[0]
    assert "fee_unknown_polymarket_us" in only_poly.reasons
    assert "fee_unknown_kalshi" not in only_poly.reasons
    assert only_poly.net_profit is None

    both = _quote(missing_both, market_k, market_p)[0]
    assert "fee_unknown_kalshi" in both.reasons
    assert "fee_unknown_polymarket_us" in both.reasons
    assert both.net_profit is None

    sample = _quote(known, market_k, market_p)[0]
    assert sample.result_class is AssessmentClass.QUALIFIED
    assert sample.net_profit is not None
    for result in (sample.yes_result, sample.no_result):
        assert result is not None
        assert not result.fee.provenance.startswith("scenario:")
    texts = tuple(result.fee.provenance for result in (sample.yes_result, sample.no_result))
    assert any("precision=0.0001" in text for text in texts)
    assert any("theta=0.075" in text for text in texts)

    pipe = _pipe(tmp_path, precision=None, theta=Decimal("0.075"), fee_scenario="none")
    try:
        asyncio.run(_discover_and_poll(pipe))
        assert pipe.assessments()
        assert "fee_unknown_kalshi" in str(pipe.assessments()[0][1])
        assert pipe.episodes() == []
    finally:
        pipe.conn.close()


async def test_conservative_scenario_prices_and_opens_episodes(tmp_path) -> None:
    pipe = _pipe(tmp_path, precision=None, theta=None)
    try:
        await pipe.discover()
        await pipe.poll()
        rows = pipe.assessments()
        assert rows
        assert {row[0] for row in rows} == {"indicative"}
        assert all(row[2] is not None for row in rows)
        assert all("fee_unknown" not in str(row[1]) for row in rows)
        stored = pipe.conn.execute("SELECT fee_provenance_json FROM assessments").fetchall()
        for (raw,) in stored:
            provenance = json.loads(raw)
            for side in ("yes", "no"):
                details = provenance[side]
                assert details["provenance"].startswith("scenario:conservative; ")
                assert details["conservative_bound"] is True
        assert pipe.episodes()
        assert {row[0] for row in pipe.episodes()} == {"indicative"}
    finally:
        pipe.conn.close()


def test_scenario_fee_never_qualifies(tmp_path) -> None:
    both = tmp_path / "both"
    one = tmp_path / "kalshi"
    both.mkdir()
    one.mkdir()
    _assert_scenario_stays_indicative(both, precision=None, theta=None, prefixed_sides=2)
    _assert_scenario_stays_indicative(one, precision=None, theta=Decimal("0.075"), prefixed_sides=1)


def test_missing_multiplier_stays_unknown_in_every_fee_scenario() -> None:
    market_k, market_p = _pair(WALL)
    bare = market_k.model_copy(update={"fee_meta": {}})
    levels = [Level(price=Decimal("0.40"), size=Decimal(1))]
    for scenario in ("conservative", "none"):
        settings = Settings(
            _env_file=None,
            fee_scenario=scenario,  # type: ignore[arg-type]
            polyus_theta=Decimal("0.075"),
        )
        quote = _fee_model(Venue.KALSHI, settings, bare).taker_fee(levels, Side.YES)
        assert quote.amount is None
        assert not quote.provenance.startswith("scenario:")
        priced = _quote(settings, bare, market_p)
        assert priced
        assert all("fee_unknown_kalshi" in item.reasons for item in priced)
        assert all("fee_unknown_polymarket_us" not in item.reasons for item in priced)
        assert all(item.result_class is AssessmentClass.REJECTED for item in priced)


def test_conservative_scenario_bounds_the_account_schedules() -> None:
    market_k, market_p = _pair(WALL)
    unset = Settings(_env_file=None)
    ladders = (
        [Level(price=Decimal("0.45"), size=Decimal(100))],
        [
            Level(price=Decimal("0.10"), size=Decimal(1)),
            Level(price=Decimal("0.20"), size=Decimal(1)),
        ],
        [Level(price=Decimal("0.50"), size=Decimal("0.2079"))],
        [Level(price=Decimal("0.33"), size=Decimal(3))],
        [Level(price=Decimal("0.20"), size=Decimal(1))],
        [Level(price=Decimal("0.20"), size=Decimal(10))],
        [Level(price=Decimal("0.20"), size=Decimal(100))],
        [
            Level(price=Decimal("0.40"), size=Decimal(10)),
            Level(price=Decimal("0.55"), size=Decimal(3)),
        ],
    )
    scenario_k = _fee_model(Venue.KALSHI, unset, market_k)
    direct_k = _fee_model(
        Venue.KALSHI,
        Settings(_env_file=None, kalshi_member_precision="0.0001"),
        market_k,
    )
    configured_coarse = _fee_model(
        Venue.KALSHI,
        Settings(_env_file=None, kalshi_member_precision="0.01"),
        market_k,
    )
    for levels in ladders:
        coarse = scenario_k.taker_fee(levels, Side.YES)
        fine = direct_k.taker_fee(levels, Side.YES)
        real_coarse = configured_coarse.taker_fee(levels, Side.YES)
        assert coarse.amount is not None
        assert fine.amount is not None
        assert real_coarse.amount is not None
        assert coarse.amount >= fine.amount
        assert coarse.amount == real_coarse.amount
        assert coarse.provenance.startswith("scenario:conservative; ")
        assert "precision=0.01" in coarse.provenance
        assert coarse.conservative_bound is True
        assert not fine.provenance.startswith("scenario:")
        assert not real_coarse.provenance.startswith("scenario:")

    scenario_p = _fee_model(Venue.POLYMARKET_US, unset, market_p)
    by_theta = {
        theta: _fee_model(
            Venue.POLYMARKET_US,
            Settings(_env_file=None, polyus_theta=theta),
            market_p,
        )
        for theta in (
            Decimal("0.095"),
            Decimal("0.085"),
            Decimal("0.075"),
            Decimal("0.0695"),
        )
    }
    for levels in ladders:
        high = scenario_p.taker_fee(levels, Side.NO)
        assert high.amount is not None
        assert high.provenance.startswith("scenario:conservative; ")
        assert "theta=0.095" in high.provenance
        assert high.conservative_bound is True
        for theta, model in by_theta.items():
            other = model.taker_fee(levels, Side.NO)
            assert other.amount is not None
            assert high.amount >= other.amount
            assert not other.provenance.startswith("scenario:")
            if theta == Decimal("0.095"):
                assert other.amount == high.amount


def _assert_scenario_stays_indicative(
    tmp_path,
    *,
    precision: str | None,
    theta: Decimal | None,
    prefixed_sides: int,
) -> None:
    pipe = _pipe(tmp_path, precision=precision, theta=theta)
    try:
        _, _, human_k, human_p = _stream_markets()
        group = _approved("human-scenario", "ada", human_k, human_p)
        _prime_stream(pipe, group, (human_k, human_p))
        asyncio.run(pipe.evaluator.on_books_updated({group.group_id}))
        rows = pipe.conn.execute(
            'SELECT "class", books_atomic, fee_provenance_json, net_profit FROM assessments'
        ).fetchall()
        assert rows
        assert {row[0] for row in rows} == {"indicative"}
        assert all(row[1] == 1 and row[3] is not None for row in rows)
        for row in rows:
            provenance = json.loads(row[2])
            texts = [provenance[side]["provenance"] for side in ("yes", "no")]
            prefixed = [text for text in texts if text.startswith("scenario:conservative; ")]
            assert len(prefixed) == prefixed_sides
            for side in ("yes", "no"):
                if provenance[side]["provenance"].startswith("scenario:"):
                    assert provenance[side]["conservative_bound"] is True
            if prefixed_sides == 2:
                assert any("precision=0.01" in text for text in texts)
                assert any("theta=0.095" in text for text in texts)
            else:
                assert any(
                    text.startswith("scenario:conservative; ") and "precision=0.01" in text
                    for text in texts
                )
                assert any(
                    "theta=0.075" in text and not text.startswith("scenario:") for text in texts
                )
        assert pipe.episodes()
        assert {row[0] for row in pipe.episodes()} == {"indicative"}
    finally:
        pipe.conn.close()


def _prime_stream(pipe: Pipe, group: EquivalenceGroup, markets: tuple[Market, Market]) -> None:
    pipe.registry.groups[group.group_id] = group
    for market in markets:
        pipe.registry.markets[market.market_key] = market
        book = _book(market.venue, market.market_id, Decimal("0.20"), MONO, WALL)
        meta = BookMeta(
            received_mono=MONO,
            received_wall=WALL,
            connection_generation=0,
            source="stream",
            cached=False,
        )
        assert pipe.collector.store.put(book, meta)
    pipe.evaluator._opens = lambda _group_id: True


async def test_edge_opens_an_indicative_episode_then_disqualifies(tmp_path) -> None:
    pipe = _pipe(tmp_path)
    try:
        await pipe.discover()
        assert pipe.registry.groups
        assert all(group.status is GroupStatus.APPROVED for group in pipe.registry.groups.values())
        assert all(group.reviewer == AUTO_REVIEWER for group in pipe.registry.groups.values())
        await pipe.poll()
        assert pipe.collector.polled_group_ids
        assert pipe.assessments()
        assert {row[0] for row in pipe.assessments()} == {"indicative"}
        assert pipe.episodes()
        assert all(row[1] is None for row in pipe.episodes())
        pipe.kalshi.price = Decimal("0.70")
        pipe.polyus.price = Decimal("0.70")
        await pipe.poll()
        assert pipe.episodes()
        assert all(
            isinstance(row[1], str) and row[1].startswith("disqualified:")
            for row in pipe.episodes()
        )
    finally:
        pipe.conn.close()


async def test_rules_change_revokes_then_auto_reapproves(tmp_path) -> None:
    pipe = _pipe(tmp_path)
    try:
        await pipe.discover()
        await pipe.poll()
        revised = "The game is scheduled for January 6, 2030. Includes overtime."
        pipe.kalshi.markets[0] = pipe.kalshi.markets[0].model_copy(update={"rules_text": revised})
        await pipe.discover()
        assert [row[0] for row in pipe.review_log()] == ["auto_approve", "revoke", "auto_approve"]
        group = next(iter(pipe.registry.groups.values()))
        assert group.status is GroupStatus.APPROVED
        assert group.reviewer == AUTO_REVIEWER
        member = next(item for item in group.members if item.venue is Venue.KALSHI)
        assert member.approved_rules_hash == rules_hash(pipe.kalshi.markets[0])
        assert pipe.episodes()
        assert {row[1] for row in pipe.episodes()} == {"rules_changed"}
    finally:
        pipe.conn.close()


async def test_manual_reject_sticks_in_auto_mode(tmp_path) -> None:
    pipe = _pipe(tmp_path)
    try:
        markets = [pipe.kalshi.markets[0], pipe.polyus.markets[0]]
        candidate = generate_candidates(markets)
        assert len(candidate) == 1
        group_id = candidate[0].group.group_id
        write_decision(
            pipe.settings.data_dir / "spool" / "reviews",
            ReviewDecision(
                group_id=group_id,
                action="reject",
                reviewer="ada",
                shown_hashes={market.market_key: rules_hash(market) for market in markets},
                acknowledged_exceptions=False,
                notes="no",
                created_utc=WALL,
            ),
        )
        await pipe.discover()
        assert pipe.registry.groups[group_id].status is GroupStatus.REJECTED
        assert "auto_approve" not in [row[0] for row in pipe.review_log()]
        assert ("reject", "ada", "applied") in pipe.review_log()
        await pipe.discover()
        assert pipe.registry.groups[group_id].status is GroupStatus.REJECTED
        assert [row[0] for row in pipe.review_log()].count("auto_approve") == 0
    finally:
        pipe.conn.close()


async def test_manual_mode_approves_nothing(tmp_path) -> None:
    pipe = _pipe(tmp_path, review_mode="manual")
    try:
        await pipe.discover()
        assert pipe.registry.groups
        assert all(group.status is GroupStatus.CANDIDATE for group in pipe.registry.groups.values())
        assert pipe.review_log() == []
    finally:
        pipe.conn.close()


def test_auto_group_never_qualifies_even_when_books_are_atomic(tmp_path) -> None:
    market_k, market_p = _pair(WALL)
    settings = Settings(
        _env_file=None, kalshi_member_precision="0.0001", polyus_theta=Decimal("0.075")
    )
    priced = _quote(settings, market_k, market_p)
    qualified = [item for item in priced if item.result_class is AssessmentClass.QUALIFIED]
    assert qualified
    downgraded = downgrade_auto_qualified(qualified[0])
    assert downgraded.result_class is AssessmentClass.INDICATIVE
    assert downgraded.books_atomic is True
    assert downgraded.net_profit == qualified[0].net_profit
    assert downgrade_auto_qualified(downgraded) is downgraded

    pipe = _pipe(tmp_path)
    try:
        auto, human = _stream_groups()
        for group in (auto, human):
            pipe.registry.groups[group.group_id] = group
        for market in _stream_markets():
            pipe.registry.markets[market.market_key] = market
            book = _book(market.venue, market.market_id, Decimal("0.20"), MONO, WALL)
            meta = BookMeta(
                received_mono=MONO,
                received_wall=WALL,
                connection_generation=0,
                source="stream",
                cached=False,
            )
            assert pipe.collector.store.put(book, meta)
        pipe.evaluator._opens = lambda _group_id: True
        asyncio.run(pipe.evaluator.on_books_updated({auto.group_id, human.group_id}))
        rows = pipe.conn.execute(
            'SELECT group_id, "class", books_atomic FROM assessments'
        ).fetchall()
        auto_rows = [row for row in rows if row[0] == auto.group_id]
        human_rows = [row for row in rows if row[0] == human.group_id]
        assert auto_rows and human_rows
        assert {row[1] for row in auto_rows} == {"indicative"}
        assert any(row[2] == 1 for row in auto_rows)
        assert any(row[1] == "qualified" for row in human_rows)
    finally:
        pipe.conn.close()


def test_dedupe_band_and_heartbeat(tmp_path) -> None:
    market_k, market_p = _pair(WALL)
    settings = Settings(
        _env_file=None, kalshi_member_precision="0.0001", polyus_theta=Decimal("0.075")
    )
    sample = _quote(settings, market_k, market_p)[0]
    assert sample.net_profit is not None
    previous = AssessmentSnapshot(
        result_class=sample.result_class,
        reasons=frozenset(sample.reasons),
        net_profit=sample.net_profit,
        written_utc=sample.computed_utc,
    )
    assert should_persist_assessment(None, sample, now=sample.computed_utc) is True
    assert should_persist_assessment(previous, sample, now=sample.computed_utc) is False
    inside = sample.model_copy(update={"net_profit": sample.net_profit + Decimal("0.05")})
    assert should_persist_assessment(previous, inside, now=sample.computed_utc) is False
    outside = sample.model_copy(update={"net_profit": sample.net_profit + Decimal("0.06")})
    assert should_persist_assessment(previous, outside, now=sample.computed_utc) is True
    indicative = downgrade_auto_qualified(sample)
    reasoned = indicative.model_copy(update={"reasons": ("book_stale_kalshi",)})
    indicative_snap = AssessmentSnapshot(
        result_class=indicative.result_class,
        reasons=frozenset(indicative.reasons),
        net_profit=indicative.net_profit,
        written_utc=indicative.computed_utc,
    )
    assert should_persist_assessment(indicative_snap, reasoned, now=indicative.computed_utc) is True
    rejected = sample.model_copy(
        update={"result_class": AssessmentClass.REJECTED, "reasons": ("not_profitable",)}
    )
    assert should_persist_assessment(previous, rejected, now=sample.computed_utc) is True
    blank = AssessmentSnapshot(
        result_class=sample.result_class,
        reasons=frozenset(sample.reasons),
        net_profit=None,
        written_utc=sample.computed_utc,
    )
    assert should_persist_assessment(blank, sample, now=sample.computed_utc) is True
    later = sample.computed_utc + timedelta(seconds=60)
    assert should_persist_assessment(previous, sample, now=later) is True
    almost = sample.computed_utc + timedelta(seconds=59)
    assert should_persist_assessment(previous, sample, now=almost) is False

    pipe = _pipe(tmp_path)
    try:
        asyncio.run(_discover_and_poll(pipe))
        count = len(pipe.assessments())
        assert count >= 1
        asyncio.run(pipe.evaluator.on_books_updated(set(pipe.collector.polled_group_ids)))
        assert len(pipe.assessments()) == count
        pipe.clock.advance(mono=60, wall=timedelta(seconds=60))
        asyncio.run(pipe.poll())
        assert len(pipe.assessments()) == count * 2
        pipe.kalshi.price = Decimal("0.25")
        pipe.polyus.price = Decimal("0.25")
        asyncio.run(pipe.poll())
        assert len(pipe.assessments()) > count * 2
    finally:
        pipe.conn.close()


async def test_on_gap_closes_every_open_episode(tmp_path) -> None:
    pipe = _pipe(tmp_path)
    try:
        await pipe.discover()
        await pipe.poll()
        await pipe.collector._notify_gap("clock_jump")
        assert pipe.episodes()
        assert {row[1] for row in pipe.episodes()} == {"clock_jump"}
        await pipe.poll()
        await pipe.collector._notify_gap("recording_backpressure")
        assert "data_gap" in {row[1] for row in pipe.episodes()}
        await pipe.poll()
        await pipe.collector._notify_gap("collector_shutdown")
        assert "collector_shutdown" in {row[1] for row in pipe.episodes()}
    finally:
        pipe.conn.close()


async def test_gap_longer_than_two_cadences_closes_with_data_gap(tmp_path) -> None:
    pipe = _pipe(tmp_path)
    try:
        await pipe.discover()
        await pipe.poll()
        opened = len(pipe.episodes())
        assert opened >= 1
        assert all(row[1] is None for row in pipe.episodes())
        pipe.clock.advance(mono=3, wall=timedelta(seconds=3))
        await pipe.poll()
        reasons = [row[1] for row in pipe.episodes()]
        assert reasons.count("data_gap") == opened
        assert reasons.count(None) == opened
    finally:
        pipe.conn.close()


async def test_two_missed_listings_close_the_market(tmp_path) -> None:
    pipe = _pipe(tmp_path)
    try:
        await pipe.discover()
        await pipe.poll()
        group_id = next(iter(pipe.registry.groups))
        assert any(row[1] is None for row in pipe.episodes())
        pipe.kalshi.omit = True
        await pipe.discover()
        assert pipe.registry.groups[group_id].status is GroupStatus.APPROVED
        assert any(row[1] is None for row in pipe.episodes())
        await pipe.discover()
        closed = pipe.registry.groups[group_id]
        assert closed.status is GroupStatus.REVOKED
        assert "market closed:" in closed.notes
        assert {row[1] for row in pipe.episodes()} == {"market_closed"}
        pipe.kalshi.omit = False
        await pipe.discover()
        restored = pipe.registry.groups[group_id]
        assert restored.status is GroupStatus.APPROVED
        assert "market closed:" not in restored.notes
    finally:
        pipe.conn.close()


async def test_adapter_error_is_an_event_and_not_fatal(tmp_path) -> None:
    pipe = _pipe(tmp_path)
    try:
        pipe.kalshi.fail = True
        await pipe.discover()
        kinds = [(event.venue, event.kind) for event in pipe.collector.events]
        assert ("kalshi", "discovery_error") in kinds
        assert any(market.venue is Venue.POLYMARKET_US for market in pipe.registry.markets.values())
        assert pipe.registry.groups == {}
    finally:
        pipe.conn.close()


def test_startup_closes_episodes_left_open(tmp_path) -> None:
    settings = _settings(tmp_path)
    db_path = tmp_path / "pmarb.sqlite"
    conn = open_writer(db_path)
    upsert_episode(
        conn,
        Episode(
            id=7,
            group_id="stale",
            yes_leg=Leg(venue=Venue.KALSHI, market_id="k", side=Side.YES),
            no_leg=Leg(venue=Venue.POLYMARKET_US, market_id="p", side=Side.NO),
            qty=Decimal(1),
            result_class=AssessmentClass.INDICATIVE,
            model_version="m1-1",
            first_seen_utc=WALL,
            last_seen_utc=WALL,
            observations=3,
            peak_net=Decimal("1.25"),
            min_size=Decimal(1),
            max_size=Decimal(10),
        ),
    )
    conn.close()
    clock = FakeClock(mono=MONO, wall=WALL)
    collector = build_collector(
        settings,
        clock=clock,
        kalshi=FakeKalshi(markets=[], clock=clock, settings=settings),
        polyus=FakePoly(markets=[], clock=clock, settings=settings),
        install_signals=False,
    )
    asyncio.run(collector.run_once())
    reader = open_writer(db_path)
    try:
        row = reader.execute("SELECT termination_reason FROM episodes WHERE id = 7").fetchone()
        assert row is not None
        assert row[0] == "collector_restart"
    finally:
        reader.close()


def test_scan_once_writes_rows_the_review_queries_can_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pmarb.collector.factory.KalshiAdapter", FakeKalshi)
    monkeypatch.setattr("pmarb.collector.factory.PolymarketUSAdapter", FakePoly)
    monkeypatch.setenv("PMARB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PMARB_KALSHI_MEMBER_PRECISION", "0.0001")
    monkeypatch.setenv("PMARB_POLYUS_THETA", "0.075")
    cli.main(["scan", "--once"])
    conn = queries.open_reader(tmp_path / "pmarb.sqlite")
    try:
        assessments = queries.fetch_assessments(conn, "indicative")
        episodes = queries.fetch_episodes(conn)
        assert assessments
        assert episodes
        review = queries.fetch_group_review(conn, str(episodes[0]["group_id"]))
        assert review is not None
    finally:
        conn.close()


def _stream_markets() -> tuple[Market, Market, Market, Market]:
    auto_k, auto_p = _pair(WALL)
    auto_k = auto_k.model_copy(update={"market_id": "k-auto"})
    auto_p = auto_p.model_copy(update={"market_id": "p-auto"})
    human_k = auto_k.model_copy(update={"market_id": "k-human"})
    human_p = auto_p.model_copy(update={"market_id": "p-human"})
    return auto_k, auto_p, human_k, human_p


def _stream_groups() -> tuple[EquivalenceGroup, EquivalenceGroup]:
    auto_k, auto_p, human_k, human_p = _stream_markets()
    return (
        _approved("auto-group", AUTO_REVIEWER, auto_k, auto_p),
        _approved("human-group", "ada", human_k, human_p),
    )


def _approved(group_id: str, reviewer: str, market_k: Market, market_p: Market) -> EquivalenceGroup:
    return EquivalenceGroup(
        group_id=group_id,
        outcome=group_id,
        members=(
            GroupMember(
                venue=Venue.KALSHI,
                market_id=market_k.market_id,
                native_side_for_outcome=Side.YES,
                approved_rules_hash="v1:k",
            ),
            GroupMember(
                venue=Venue.POLYMARKET_US,
                market_id=market_p.market_id,
                native_side_for_outcome=Side.YES,
                approved_rules_hash="v1:p",
            ),
        ),
        status=GroupStatus.APPROVED,
        reviewer=reviewer,
        decided_utc=WALL,
        exception_risk=False,
    )
