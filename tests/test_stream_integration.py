"""M2-D: WebSocket streams wired into the collector.

No network and no real keys. Streams are fake async iterators the test feeds
(:class:`FakeStream`); adapters are fakes that record REST calls. Key files are
generated at runtime in ``tmp_path`` (outside the checkout) with owner-only
permissions. Clocks are :class:`FakeClock`; mono and wall always advance
together unless a test is about the two clocks disagreeing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from pmarb.collector.books import BookStore, market_key
from pmarb.collector.clock import FakeClock
from pmarb.collector.episodes import EpisodeTracker
from pmarb.collector.evaluator import Evaluator
from pmarb.collector.factory import build_collector
from pmarb.collector.registry import Registry
from pmarb.collector.review_mode import AUTO_REVIEWER
from pmarb.collector.runtime import Collector
from pmarb.collector.streams import (
    KEY_INVALID,
    KEY_POLICY,
    KEYS_CONFIGURED,
    KEYS_MISSING,
    StreamHealth,
    poll_mode,
    select_venue_modes,
    stream_mode,
)
from pmarb.config import Settings
from pmarb.models import Book, Level, Market, Venue
from pmarb.protocols import BookEvent
from pmarb.storage.db import _apply_batch, open_writer
from pmarb.venues.kalshi.ws import KalshiOrderbookStream
from pmarb.venues.polymarket_us.ws import PolyUSMarketStream
from tests.test_kalshi_ws import ScriptedSocket, subscribed
from tests.test_kalshi_ws import snapshot as kalshi_snapshot_frame
from tests.test_pr6b import WALL, _approved, _fee_model, _pair

MONO = 1000.0
PLENTY = 10**12
REPO_ROOT = Path(__file__).resolve().parents[1]
K_ID = "k-1"
P_ID = "p-1"
GROUP = "g-1"
# Named so the strict secret-scanner rules (key id = value) never see a literal.
FAKE_KALSHI_ID = "synthetic-kalshi-id"
FAKE_POLY_ID = "synthetic-polyus-id"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("PMARB_", "KALSHI_", "POLYUS_")):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr("pmarb.collector.runtime.in_maintenance", lambda _now: False)
    monkeypatch.setattr("pmarb.collector.freshness.in_maintenance", lambda _now: False)


# --- fakes -------------------------------------------------------------------


_END = object()


class FakeStream:
    """A venue stream the test drives. ``connection_generation`` behaves like the real ones.

    It starts at 0, becomes 1 on :meth:`connect`, and advances on
    :meth:`disconnect` before the per-market invalidations, as both venue
    streams do.
    """

    def __init__(self, venue: Venue, market_ids, clock: FakeClock) -> None:
        self.venue = venue
        self.market_ids = tuple(market_ids)
        self.clock = clock
        self.connection_generation = 0
        self.closed = False
        self._queue: asyncio.Queue[object] = asyncio.Queue()

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> BookEvent:
        item = await self._queue.get()
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, BookEvent)
        return item

    async def aclose(self) -> None:
        self.closed = True

    def connect(self) -> None:
        self.connection_generation += 1

    def snapshot(
        self,
        market_id: str,
        *,
        price: str = "0.20",
        raw: dict | None = None,
        generation: int | None = None,
    ) -> None:
        level = Level(price=Decimal(price), size=Decimal(200))
        book = Book(
            venue=self.venue,
            market_id=market_id,
            yes_asks=[level],
            no_asks=[level],
            raw_native=raw,
            received_utc=self.clock.wall(),
            received_monotonic=self.clock.monotonic(),
            connection_generation=(
                self.connection_generation if generation is None else generation
            ),
            valid=True,
        )
        self._queue.put_nowait(BookEvent("snapshot", book, market_id, self.venue, None))

    def invalidate(self, market_id: str, reason: str) -> None:
        self._queue.put_nowait(BookEvent("invalidate", None, market_id, self.venue, reason))

    def disconnect(self, reason: str = "disconnected") -> None:
        self.connection_generation += 1
        for market_id in self.market_ids:
            self.invalidate(market_id, reason)

    def end(self) -> None:
        self._queue.put_nowait(_END)


class StreamFactory:
    """Builds :class:`FakeStream` objects and keeps them for the test."""

    def __init__(self, venue: Venue, clock: FakeClock) -> None:
        self.venue = venue
        self.clock = clock
        self.streams: list[FakeStream] = []

    def __call__(self, market_ids) -> FakeStream:
        stream = FakeStream(self.venue, market_ids, self.clock)
        self.streams.append(stream)
        return stream

    @property
    def last(self) -> FakeStream:
        assert self.streams, f"no {self.venue} stream was started"
        return self.streams[-1]


class FakeAdapter:
    """REST adapter double: records ``get_books`` calls and answers at the fake clock."""

    def __init__(self, venue: Venue, settings: Settings, clock: FakeClock) -> None:
        self.venue = venue
        self._settings = settings
        self._clock = clock
        self.calls: list[list[str]] = []
        self.price = Decimal("0.20")

    async def get_books(self, market_ids) -> list[Book]:
        self.calls.append(list(market_ids))
        level = Level(price=self.price, size=Decimal(200))
        return [
            Book(
                venue=self.venue,
                market_id=market_id,
                yes_asks=[level],
                no_asks=[level],
                received_utc=self._clock.wall(),
                received_monotonic=self._clock.monotonic(),
                connection_generation=0,
                valid=True,
            )
            for market_id in market_ids
        ]

    def book_stream(self, market_ids):
        del market_ids

    def fee_model(self, market: Market):
        return _fee_model(self.venue, self._settings, market)


class ZeroBackoff:
    def delay(self, attempt: int) -> float:
        del attempt
        return 0.0


class Rig:
    """Collector + evaluator + episode tracker on one in-memory database."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        kalshi_streams: bool = True,
        poly_streams: bool = True,
        reviewer: str = "ada",
        precision: str | None = "0.0001",
        theta: Decimal | None = Decimal("0.075"),
        depth_confirmed: bool = True,
        **settings_kw,
    ) -> None:
        self.clock = FakeClock(mono=MONO, wall=WALL)
        self.settings = Settings(
            _env_file=None,
            data_dir=tmp_path,
            kalshi_member_precision=precision,  # type: ignore[arg-type]
            polyus_theta=theta,
            polyus_ws_depth_confirmed=depth_confirmed,
            **settings_kw,
        )
        self.conn = open_writer(tmp_path / "pmarb.sqlite")
        market_k, market_p = _pair(WALL)
        self.market_k = market_k.model_copy(update={"market_id": K_ID})
        self.market_p = market_p.model_copy(update={"market_id": P_ID})
        self.group = _approved(GROUP, reviewer, self.market_k, self.market_p)
        self.kalshi = FakeAdapter(Venue.KALSHI, self.settings, self.clock)
        self.polyus = FakeAdapter(Venue.POLYMARKET_US, self.settings, self.clock)
        self.factories = {
            Venue.KALSHI: StreamFactory(Venue.KALSHI, self.clock),
            Venue.POLYMARKET_US: StreamFactory(Venue.POLYMARKET_US, self.clock),
        }
        modes = {
            Venue.KALSHI: (
                stream_mode(Venue.KALSHI, self.factories[Venue.KALSHI])
                if kalshi_streams
                else poll_mode(Venue.KALSHI, KEYS_MISSING, "key id not configured")
            ),
            Venue.POLYMARKET_US: (
                stream_mode(Venue.POLYMARKET_US, self.factories[Venue.POLYMARKET_US])
                if poly_streams
                else poll_mode(Venue.POLYMARKET_US, KEYS_MISSING, "key id not configured")
            ),
        }
        self.registry = Registry()
        self.registry.groups[self.group.group_id] = self.group
        for market in (self.market_k, self.market_p):
            self.registry.markets[market.market_key] = market
        self.store = BookStore()
        self.gaps: list[str] = []
        self.db_queue: asyncio.Queue[object] = asyncio.Queue()
        holder: list[Collector] = []
        self.tracker = EpisodeTracker(
            enqueue=self._write, cadence_s=lambda: holder[0].cadence_s, now=self.clock.wall
        )
        self.evaluator = Evaluator(
            registry=self.registry,
            store=self.store,
            kalshi=self.kalshi,
            polyus=self.polyus,
            settings=self.settings,
            episodes=self.tracker,
            enqueue=self._write,
            clock=self.clock,
        )

        def on_gap(reason: str) -> None:
            self.gaps.append(reason)
            self.tracker.on_gap(reason)

        self.collector = Collector(
            kalshi=self.kalshi,
            polyus=self.polyus,
            clock=self.clock,
            on_books_updated=self.evaluator.on_books_updated,
            on_gap=on_gap,
            settings=self.settings,
            store=self.store,
            db_queue=self.db_queue,
            groups=(self.group,),
            markets=(self.market_k, self.market_p),
            install_signals=False,
            disk_free=lambda _path: PLENTY,
            backoff=ZeroBackoff(),
            venue_modes=modes,
            on_invalidated=self.tracker.on_markets_invalidated,
        )
        holder.append(self.collector)
        self.evaluator.bind(self.collector)

    def _write(self, fn) -> None:
        fn(self.conn)

    def stream(self, venue: Venue) -> FakeStream:
        return self.factories[venue].last

    @property
    def k(self) -> FakeStream:
        return self.stream(Venue.KALSHI)

    @property
    def p(self) -> FakeStream:
        return self.stream(Venue.POLYMARKET_US)

    async def poll(self) -> None:
        await self.collector._poll_once()
        await settle()

    async def tick(self, seconds: float = 0.0) -> None:
        """Advance both clocks together, then run one freshness pass."""
        if seconds:
            self.clock.advance(mono=seconds, wall=timedelta(seconds=seconds))
        await self.collector._freshness_tick()
        await settle()

    def advance(self, seconds: float) -> None:
        self.clock.advance(mono=seconds, wall=timedelta(seconds=seconds))

    def state(self, venue: Venue, market_id: str):
        return self.store.get(market_key(venue, market_id))

    def assessments(self) -> list[tuple]:
        return self.conn.execute(
            'SELECT "class", books_atomic, reasons_json FROM assessments ORDER BY id'
        ).fetchall()

    def episodes(self) -> list[tuple]:
        return self.conn.execute(
            'SELECT "class", termination_reason FROM episodes ORDER BY id'
        ).fetchall()

    def kinds(self, venue: str | None = None) -> list[str]:
        return [
            event.kind
            for event in self.collector.events
            if event.kind != "timing" and (venue is None or event.venue == venue)
        ]

    def stored_events(self) -> list[tuple[str | None, str, dict]]:
        ops = []
        while not self.db_queue.empty():
            ops.append(self.db_queue.get_nowait())
        _apply_batch(self.conn, ops)
        return [
            (venue, kind, json.loads(detail))
            for venue, kind, detail in self.conn.execute(
                "SELECT venue, kind, detail_json FROM collector_events ORDER BY id"
            )
        ]

    async def close(self) -> None:
        await self.collector.stop()
        self.conn.close()


async def settle(rounds: int = 50) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def classes(rows) -> set[str]:
    return {row[0] for row in rows}


async def connected_rig(tmp_path: Path, **kwargs) -> Rig:
    """Both venues streaming, both connected, one snapshot each at the same instant."""
    rig = Rig(tmp_path, **kwargs)
    await rig.poll()
    rig.k.connect()
    rig.p.connect()
    rig.k.snapshot(K_ID)
    rig.p.snapshot(P_ID, raw={"depth_unconfirmed": False})
    await settle()
    return rig


# --- key material, built at runtime ------------------------------------------


def _write_key(path: Path, data: bytes, mode: int) -> Path:
    path.touch(mode=0o600)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def kalshi_key_file(directory: Path, mode: int = 0o600) -> Path:
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return _write_key(directory / "kalshi.key", pem, mode)


def polyus_key_file(directory: Path, mode: int = 0o600) -> Path:
    raw = ed25519.Ed25519PrivateKey.generate().private_bytes_raw()
    return _write_key(directory / "polyus.key", base64.b64encode(raw), mode)


def key_settings(tmp_path: Path, **values) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path / "data", **values)


# --- 1. per-venue mode selection ---------------------------------------------


def test_keys_configured_select_streams_for_both_venues(tmp_path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    settings = key_settings(
        tmp_path,
        kalshi_api_key_id=FAKE_KALSHI_ID,
        kalshi_private_key_path=kalshi_key_file(keys),
        polyus_api_key_id=FAKE_POLY_ID,
        polyus_private_key_path=polyus_key_file(keys),
    )
    modes = select_venue_modes(settings, FakeClock(), repo_root=REPO_ROOT)
    for venue in (Venue.KALSHI, Venue.POLYMARKET_US):
        assert modes[venue].mode == "stream"
        assert modes[venue].reason == KEYS_CONFIGURED
        assert modes[venue].factory is not None
    kalshi_stream = modes[Venue.KALSHI].factory(["KX-1"])  # type: ignore[misc]
    poly_stream = modes[Venue.POLYMARKET_US].factory(["pm-1"])  # type: ignore[misc]
    # Built, not connected: a stream opens its socket on the first anext().
    assert isinstance(kalshi_stream, KalshiOrderbookStream)
    assert kalshi_stream.market_tickers == ("KX-1",)
    assert isinstance(poly_stream, PolyUSMarketStream)
    assert poly_stream.slugs == ("pm-1",)
    assert poly_stream._depth_confirmed is False
    assert "depth_unconfirmed" in (modes[Venue.POLYMARKET_US].detail or "")
    for mode in modes.values():
        assert "synthetic-" not in json.dumps(mode.describe())


def test_keys_missing_keep_rest_polling_per_venue(tmp_path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    settings = key_settings(
        tmp_path,
        kalshi_api_key_id=FAKE_KALSHI_ID,
        kalshi_private_key_path=kalshi_key_file(keys),
        # Polymarket US has a path but no key id.
        polyus_private_key_path=polyus_key_file(keys),
    )
    modes = select_venue_modes(settings, FakeClock(), repo_root=REPO_ROOT)
    assert modes[Venue.KALSHI].mode == "stream"
    assert modes[Venue.POLYMARKET_US].mode == "poll"
    assert modes[Venue.POLYMARKET_US].reason == KEYS_MISSING
    assert modes[Venue.POLYMARKET_US].detail == "key id not configured"
    assert modes[Venue.POLYMARKET_US].factory is None

    bare = select_venue_modes(key_settings(tmp_path), FakeClock(), repo_root=REPO_ROOT)
    assert {mode.mode for mode in bare.values()} == {"poll"}
    assert bare[Venue.KALSHI].detail == "key id and key path not configured"


def test_key_file_failing_policy_falls_back_to_poll_with_a_clear_event(tmp_path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    open_key = kalshi_key_file(keys, mode=0o644)
    secret = open_key.read_bytes()
    settings = key_settings(
        tmp_path,
        kalshi_api_key_id=FAKE_KALSHI_ID,
        kalshi_private_key_path=open_key,
        polyus_api_key_id=FAKE_POLY_ID,
        polyus_private_key_path=polyus_key_file(keys),
    )
    clock = FakeClock(mono=MONO, wall=WALL)
    collector = build_collector(
        settings,
        clock=clock,
        kalshi=FakeAdapter(Venue.KALSHI, settings, clock),
        polyus=FakeAdapter(Venue.POLYMARKET_US, settings, clock),
        install_signals=False,
        key_repo_root=REPO_ROOT,
    )
    modes = collector.venue_modes
    assert modes[Venue.KALSHI].mode == "poll"
    assert modes[Venue.KALSHI].reason == KEY_POLICY
    assert "too open" in (modes[Venue.KALSHI].detail or "")
    assert modes[Venue.POLYMARKET_US].mode == "stream"

    asyncio.run(collector.run_once())
    events = {
        event.venue: event.detail for event in collector.events if event.kind == "stream_mode"
    }
    assert events["kalshi"]["mode"] == "poll"
    assert events["kalshi"]["reason"] == KEY_POLICY
    assert "too open" in events["kalshi"]["detail"]
    assert events["polymarket_us"]["mode"] == "stream"
    # A one-shot scan never opens a socket; it says so.
    assert "once" in events["polymarket_us"]
    conn = open_writer(settings.data_dir / "pmarb.sqlite")
    try:
        rows = conn.execute(
            "SELECT venue, detail_json FROM collector_events WHERE kind = 'stream_mode'"
        ).fetchall()
    finally:
        conn.close()
    stored = {venue: json.loads(detail) for venue, detail in rows}
    assert stored["kalshi"]["reason"] == KEY_POLICY
    assert stored["polymarket_us"]["mode"] == "stream"
    for _venue, detail in rows:
        assert secret.decode() not in detail
        assert "synthetic-" not in detail


def test_unreadable_key_content_falls_back_to_poll_without_echoing_it(tmp_path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    junk = b"synthetic-not-a-key-" + os.urandom(8).hex().encode()
    settings = key_settings(
        tmp_path,
        kalshi_api_key_id=FAKE_KALSHI_ID,
        kalshi_private_key_path=_write_key(keys / "kalshi.key", junk, 0o600),
        polyus_api_key_id=FAKE_POLY_ID,
        polyus_private_key_path=_write_key(keys / "polyus.key", junk, 0o600),
    )
    modes = select_venue_modes(settings, FakeClock(), repo_root=REPO_ROOT)
    for venue in (Venue.KALSHI, Venue.POLYMARKET_US):
        assert modes[venue].mode == "poll"
        assert modes[venue].reason == KEY_INVALID
        assert junk.decode() not in json.dumps(modes[venue].describe())


async def test_modes_are_in_health_events(tmp_path) -> None:
    rig = Rig(tmp_path, poly_streams=False)
    try:
        await rig.poll()
        rig.k.connect()
        rig.k.snapshot(K_ID)
        await settle()
        rig.collector._health_tick()
        health = [event for event in rig.collector.events if event.kind == "health"][-1]
        venues = health.detail["venues"]
        assert venues["kalshi"]["mode"] == "stream"
        assert venues["kalshi"]["active"] == "stream"
        assert venues["kalshi"]["state"] == "healthy"
        assert venues["kalshi"]["generation"] == 1
        assert venues["polymarket_us"] == {
            "mode": "poll",
            "reason": KEYS_MISSING,
            "active": "poll",
        }
    finally:
        await rig.close()


# --- 2/3. streamed books reach the evaluator; the only qualified path --------


async def test_streamed_books_reach_the_evaluator_without_rest(tmp_path) -> None:
    rig = await connected_rig(tmp_path)
    try:
        # Live markets on a streaming venue are not polled over REST.
        assert rig.kalshi.calls == []
        assert rig.polyus.calls == []
        assert rig.k.market_ids == (K_ID,)
        assert rig.p.market_ids == (P_ID,)
        for venue, market_id in ((Venue.KALSHI, K_ID), (Venue.POLYMARKET_US, P_ID)):
            state = rig.state(venue, market_id)
            assert state is not None and state.invalid_reason is None
            assert state.meta.source == "stream"
            assert state.meta.connection_generation == 1
            assert rig.store.generation(venue, "stream") == 1
            assert rig.store.generation(venue, "poll") == 0
        rows = rig.assessments()
        assert rows
        assert "qualified" in classes(rows)
        assert {row[0] for row in rig.episodes()} >= {"qualified"}
        assert all(row[1] is None for row in rig.episodes())
    finally:
        await rig.close()


@pytest.mark.parametrize(
    ("case", "kwargs", "skew_s", "qualified", "atomic"),
    [
        ("human, real fees, both streamed, skew 250 ms", {}, 0.25, True, True),
        ("skew 251 ms", {}, 0.251, False, False),
        ("auto-approved group (D27)", {"reviewer": AUTO_REVIEWER}, 0.0, False, True),
        ("scenario fees (D30)", {"precision": None, "theta": None}, 0.0, False, True),
        ("one scenario fee (D30)", {"precision": None}, 0.0, False, True),
    ],
)
async def test_qualified_needs_human_group_real_fees_streams_and_small_skew(
    tmp_path, case, kwargs, skew_s, qualified, atomic
) -> None:
    rig = Rig(tmp_path, **kwargs)
    try:
        await rig.poll()
        rig.k.connect()
        rig.p.connect()
        rig.k.snapshot(K_ID)
        await settle()
        rig.advance(skew_s)
        rig.p.snapshot(P_ID, raw={"depth_unconfirmed": False})
        await settle()
        rows = rig.assessments()
        assert rows, case
        assert ("qualified" in classes(rows)) is qualified, case
        if not qualified:
            assert classes(rows) <= {"indicative", "rejected"}, case
            assert "indicative" in classes(rows), case
        priced = [row for row in rows if row[0] != "rejected"]
        assert {row[1] for row in priced} == {1 if atomic else 0}, case
    finally:
        await rig.close()


# --- 4. depth_unconfirmed ----------------------------------------------------


@pytest.mark.parametrize(
    ("depth_confirmed", "raw"),
    [
        (True, {"depth_unconfirmed": True}),  # the tag alone
        (False, {"depth_unconfirmed": False}),  # the config gate alone
        (False, None),
    ],
)
async def test_depth_unconfirmed_forces_indicative(tmp_path, depth_confirmed, raw) -> None:
    rig = Rig(tmp_path, depth_confirmed=depth_confirmed)
    try:
        await rig.poll()
        rig.k.connect()
        rig.p.connect()
        rig.k.snapshot(K_ID)
        rig.p.snapshot(P_ID, raw=raw)
        await settle()
        state = rig.state(Venue.POLYMARKET_US, P_ID)
        assert state is not None and state.invalid_reason is None and state.book.valid
        rows = rig.assessments()
        assert rows
        assert "qualified" not in classes(rows)
        assert "indicative" in classes(rows)
        assert all(row[1] == 0 for row in rows)
        assert {row[0] for row in rig.episodes()} == {"indicative"}
    finally:
        await rig.close()


# --- 5. outage -> REST fallback -> recovery, no mixing of generations --------


async def test_outage_falls_back_to_rest_then_recovers_without_mixing_generations(
    tmp_path,
) -> None:
    rig = await connected_rig(tmp_path, stream_fallback_after_s=5.0, stream_recovery_s=10.0)
    try:
        assert "qualified" in classes(rig.assessments())
        health = rig.collector.stream_health(Venue.KALSHI)
        assert isinstance(health, StreamHealth) and health.healthy

        # The Kalshi socket drops. Its books go invalid at once, with its reason.
        rig.k.disconnect("disconnected")
        await settle()
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None
        assert k_state.meta.source == "stream"
        assert k_state.invalid_reason == "disconnected"
        assert health.state == "reconnecting" and not health.fallback
        assert "stream_disconnect" in rig.gaps
        assert {row[1] for row in rig.episodes()} <= {
            "data_gap:disconnected",
            "data_gap:stream_disconnect",
        }

        # Still inside N seconds: no REST yet.
        await rig.poll()
        assert rig.kalshi.calls == []

        # Past N seconds without a snapshot: fall back to REST for Kalshi only.
        await rig.tick(5.1)
        assert health.fallback and health.fallback_reason == "disconnected"
        assert rig.collector.active_source(Venue.KALSHI) == "poll"
        assert rig.collector.active_source(Venue.POLYMARKET_US) == "stream"
        await rig.poll()
        assert rig.kalshi.calls == [[K_ID]]
        assert rig.polyus.calls == []
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.invalid_reason is None
        assert k_state.meta.source == "poll"
        assert k_state.meta.connection_generation == 0
        # The poll sequence never touched the stream's.
        assert rig.store.generation(Venue.KALSHI, "stream") == 1
        assert rig.store.generation(Venue.KALSHI, "poll") == 0

        # A REST leg is never atomic: indicative while in fallback.
        before = len(rig.assessments())
        rig.p.snapshot(P_ID, raw={"depth_unconfirmed": False})
        await settle()
        during = rig.assessments()[before:]
        assert during
        assert "qualified" not in classes(during)
        assert "indicative" in classes(during)
        assert all(row[1] == 0 for row in during)

        # The stream reconnects on a new generation. While in fallback its
        # snapshots only count toward health; they never replace the REST book.
        rig.k.connect()
        assert rig.k.connection_generation == 3
        rig.k.snapshot(K_ID)
        await settle()
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.meta.source == "poll"
        assert health.fallback
        # The store follows the live connection, so the dead one cannot return.
        assert rig.store.generation(Venue.KALSHI, "stream") == 3
        assert rig.store.generation(Venue.KALSHI, "poll") == 0

        # Not yet stable for the recovery window.
        await rig.tick(9.0)
        assert health.fallback
        await rig.poll()
        assert rig.kalshi.calls[-1] == [K_ID]

        # Stable long enough: switch back to the stream.
        await rig.tick(1.0)
        assert not health.fallback and health.healthy
        assert rig.collector.active_source(Venue.KALSHI) == "stream"
        calls = len(rig.kalshi.calls)
        await rig.poll()
        assert len(rig.kalshi.calls) == calls

        # A late book from the dead connection cannot come back.
        rig.k.snapshot(K_ID, generation=1)
        await settle()
        assert rig.state(Venue.KALSHI, K_ID).meta.source == "poll"

        # Fresh stream books on the current generation qualify again.
        rig.k.snapshot(K_ID)
        rig.p.snapshot(P_ID, raw={"depth_unconfirmed": False})
        await settle()
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.meta.source == "stream"
        assert k_state.meta.connection_generation == 3
        assert rig.store.generation(Venue.KALSHI, "stream") == 3
        assert rig.collector.stream_is_current(Venue.KALSHI, 3)
        assert not rig.collector.stream_is_current(Venue.KALSHI, 1)
        assert rig.assessments()[-1][0] == "qualified"

        assert rig.kinds("kalshi").count("stream_fallback") == 1
        assert rig.kinds("kalshi").count("stream_recovered") == 1
        assert "stream_fallback" not in rig.kinds("polymarket_us")
        stored = rig.stored_events()
        fallback = [detail for venue, kind, detail in stored if kind == "stream_fallback"]
        recovered = [detail for venue, kind, detail in stored if kind == "stream_recovered"]
        assert fallback and fallback[0]["reason"] == "disconnected"
        assert recovered and recovered[0]["recovered_from"] == "disconnected"
        assert recovered[0]["fallback_s"] == pytest.approx(10.0)
    finally:
        await rig.close()


async def test_repeated_gaps_and_schema_failures_fall_back(tmp_path) -> None:
    rig = await connected_rig(tmp_path, stream_max_gaps=3, stream_max_schema_failures=2)
    try:
        health_k = rig.collector.stream_health(Venue.KALSHI)
        assert health_k is not None
        for _ in range(2):
            rig.k.invalidate(K_ID, "sequence_gap")
            rig.k.invalidate(K_ID, "sequence_gap")  # same incident: no snapshot between
            rig.k.snapshot(K_ID)
            await settle()
        assert not health_k.fallback
        rig.k.invalidate(K_ID, "sequence_gap")
        await settle()
        assert health_k.fallback and health_k.fallback_reason == "sequence_gaps"
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.invalid_reason is not None

        health_p = rig.collector.stream_health(Venue.POLYMARKET_US)
        assert health_p is not None
        rig.p.invalidate(P_ID, "schema_failure")
        rig.p.snapshot(P_ID, raw={"depth_unconfirmed": False})
        rig.p.invalidate(P_ID, "schema_failure")
        await settle()
        assert health_p.fallback and health_p.fallback_reason == "schema_failures"
        await rig.poll()
        assert rig.kalshi.calls == [[K_ID]]
        assert rig.polyus.calls == [[P_ID]]
        rows = rig.assessments()
        assert rows[-1][0] == "indicative" and rows[-1][1] == 0
    finally:
        await rig.close()


async def test_a_crashed_stream_restarts_on_a_new_generation(tmp_path) -> None:
    rig = await connected_rig(tmp_path)
    try:
        first = rig.k
        first.connect()  # local generation 2 on the same instance
        first.snapshot(K_ID)
        await settle()
        assert rig.state(Venue.KALSHI, K_ID).meta.connection_generation == 2
        first.end()
        await settle()
        assert first.closed
        assert "stream_ended" in rig.kinds("kalshi")
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.invalid_reason == "stream_ended"
        second = rig.k
        assert second is not first
        second.connect()  # local generation 1 again; collector generation 3
        second.snapshot(K_ID)
        await settle()
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.invalid_reason is None
        assert k_state.meta.connection_generation == 3
        assert rig.collector.stream_is_current(Venue.KALSHI, 3)
    finally:
        await rig.close()


# --- 6. an invalidate event closes the episode -------------------------------


@pytest.mark.parametrize(
    ("venue", "reason", "termination"),
    [
        (Venue.KALSHI, "sequence_gap", "data_gap:sequence_gap"),
        (Venue.KALSHI, "crossed_book", "data_gap:crossed_book"),
        (Venue.KALSHI, "market_closed", "market_closed"),
        (Venue.POLYMARKET_US, "parse_failure", "data_gap:parse_failure"),
    ],
)
async def test_an_invalidate_event_closes_the_episode(tmp_path, venue, reason, termination) -> None:
    rig = await connected_rig(tmp_path)
    try:
        open_rows = rig.episodes()
        assert open_rows and all(row[1] is None for row in open_rows)
        market_id = K_ID if venue is Venue.KALSHI else P_ID
        rig.stream(venue).invalidate(market_id, reason)
        await settle()
        state = rig.state(venue, market_id)
        assert state is not None and state.invalid_reason == reason
        closed = rig.episodes()
        assert len(closed) == len(open_rows)
        assert {row[1] for row in closed} == {termination}
        # One market invalid is not a disconnect: no collector-wide gap.
        assert "stream_disconnect" not in rig.gaps
    finally:
        await rig.close()


async def test_disconnect_leaving_no_valid_book_calls_on_gap_once(tmp_path) -> None:
    rig = await connected_rig(tmp_path)
    try:
        rig.p.disconnect("disconnect")
        rig.p.invalidate(P_ID, "disconnect")
        await settle()
        assert rig.gaps.count("stream_disconnect") == 1
        assert all(row[1] is not None for row in rig.episodes())
        assert "stream_disconnect" in rig.kinds("polymarket_us")
    finally:
        await rig.close()


# --- 7. one venue streaming, the other polling --------------------------------


async def test_one_venue_streaming_and_one_polling_is_indicative(tmp_path) -> None:
    rig = Rig(tmp_path, poly_streams=False)
    try:
        await rig.poll()
        assert rig.polyus.calls == [[P_ID]]
        assert rig.kalshi.calls == []
        assert Venue.POLYMARKET_US not in {f.venue for f in rig.factories.values() if f.streams}
        rig.k.connect()
        rig.k.snapshot(K_ID)
        await settle()
        k_state = rig.state(Venue.KALSHI, K_ID)
        p_state = rig.state(Venue.POLYMARKET_US, P_ID)
        assert k_state is not None and k_state.meta.source == "stream"
        assert p_state is not None and p_state.meta.source == "poll"
        rows = rig.assessments()
        assert rows
        assert "qualified" not in classes(rows)
        assert "indicative" in classes(rows)
        assert all(row[1] == 0 for row in rows)
        assert {row[0] for row in rig.episodes()} == {"indicative"}
    finally:
        await rig.close()


# --- rules that stay in force --------------------------------------------------


async def test_freshness_uses_both_clocks_for_stream_books(tmp_path) -> None:
    rig = await connected_rig(tmp_path)
    try:
        # Monotonic time paused (sleep) while wall time moved on; the offset
        # step stays under the 1 s clock-jump guard.
        rig.clock.advance(mono=1.5, wall=timedelta(seconds=2.4))
        await rig.tick()
        for venue, market_id in ((Venue.KALSHI, K_ID), (Venue.POLYMARKET_US, P_ID)):
            state = rig.state(venue, market_id)
            assert state is not None and state.invalid_reason == "stale"
        assert "clock_jump" not in rig.gaps
    finally:
        await rig.close()


async def test_maintenance_invalidates_polymarket_us_stream_books_without_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await connected_rig(tmp_path, stream_fallback_after_s=5.0)
    try:
        monkeypatch.setattr("pmarb.collector.runtime.in_maintenance", lambda _now: True)
        monkeypatch.setattr("pmarb.collector.freshness.in_maintenance", lambda _now: True)
        rig.p.invalidate(P_ID, "maintenance")
        await settle()
        await rig.tick(0.25)
        p_state = rig.state(Venue.POLYMARKET_US, P_ID)
        assert p_state is not None and p_state.invalid_reason == "maintenance"
        assert "maintenance" in rig.gaps
        assert {row[1] for row in rig.episodes()} == {"maintenance"}
        health = rig.collector.stream_health(Venue.POLYMARKET_US)
        assert health is not None and health.state == "maintenance"
        # REST is down for maintenance too: no fallback, however long it lasts.
        await rig.tick(30.0)
        assert not health.fallback
    finally:
        await rig.close()


async def test_resubscribe_on_a_new_live_market_starts_a_fresh_generation(tmp_path) -> None:
    rig = await connected_rig(tmp_path)
    try:
        assert len(rig.factories[Venue.KALSHI].streams) == 1
        other_k = rig.market_k.model_copy(update={"market_id": "k-2"})
        other_p = rig.market_p.model_copy(update={"market_id": "p-2"})
        other = _approved("g-2", "ada", other_k, other_p)
        rig.registry.groups[other.group_id] = other
        for market in (other_k, other_p):
            rig.registry.markets[market.market_key] = market
        rig.collector.set_universe(
            (rig.group, other), (rig.market_k, rig.market_p, other_k, other_p)
        )
        await rig.poll()
        assert len(rig.factories[Venue.KALSHI].streams) == 2
        old, new = rig.factories[Venue.KALSHI].streams
        assert old.closed
        assert set(new.market_ids) == {K_ID, "k-2"}
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.invalid_reason == "stream_resubscribe"
        assert {row[1] for row in rig.episodes()} == {"data_gap:stream_resubscribe"}
        new.connect()
        new.snapshot(K_ID)
        await settle()
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.invalid_reason is None
        assert k_state.meta.connection_generation == 2
    finally:
        await rig.close()


async def test_real_kalshi_stream_class_feeds_the_collector(tmp_path) -> None:
    """The factory's KalshiOrderbookStream, on a scripted socket, through the collector."""
    keys = tmp_path / "keys"
    keys.mkdir()
    rig = Rig(tmp_path, poly_streams=False)
    try:
        settings = key_settings(
            tmp_path,
            kalshi_api_key_id=FAKE_KALSHI_ID,
            kalshi_private_key_path=kalshi_key_file(keys),
        )
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            subscribed("market_lifecycle_v2", 8),
            kalshi_snapshot_frame(K_ID),
        )
        opened: list[str] = []

        async def connect(uri, **options):
            del options
            opened.append(uri)
            return socket

        modes = select_venue_modes(
            settings, rig.clock, repo_root=REPO_ROOT, connect={Venue.KALSHI: connect}
        )
        assert modes[Venue.KALSHI].mode == "stream"
        rig.collector._modes[Venue.KALSHI] = modes[Venue.KALSHI]
        await rig.poll()
        await settle(200)
        assert opened and opened[0].startswith("wss://")
        assert socket.sent[0]["cmd"] == "subscribe"
        assert socket.sent[0]["params"]["market_tickers"] == [K_ID]
        k_state = rig.state(Venue.KALSHI, K_ID)
        assert k_state is not None and k_state.invalid_reason is None
        assert k_state.meta.source == "stream"
        assert k_state.meta.connection_generation == 1
        assert k_state.book.received_monotonic == rig.clock.monotonic()
        # use_yes_price=true: NO bids arrive YES-scaled and are YES asks as-is.
        assert k_state.book.yes_asks[0].price == Decimal("0.8000")
        assert k_state.book.no_asks[0].price == Decimal("0.7000")
        assert rig.assessments()
        assert "qualified" not in classes(rig.assessments())
    finally:
        await rig.close()


async def test_an_invalid_snapshot_closes_the_episode(tmp_path) -> None:
    rig = await connected_rig(tmp_path)
    try:
        assert rig.episodes() and all(row[1] is None for row in rig.episodes())
        level = Level(price=Decimal("0.20"), size=Decimal(200))
        suspended = Book(
            venue=Venue.POLYMARKET_US,
            market_id=P_ID,
            yes_asks=[level],
            no_asks=[level],
            raw_native={"depth_unconfirmed": False},
            received_utc=rig.clock.wall(),
            received_monotonic=rig.clock.monotonic(),
            connection_generation=rig.p.connection_generation,
            valid=False,
            invalid_reason="book_state_MARKET_STATE_SUSPENDED",
        )
        rig.p._queue.put_nowait(BookEvent("snapshot", suspended, P_ID, Venue.POLYMARKET_US, None))
        await settle()
        assert {row[1] for row in rig.episodes()} == {"data_gap:book_state_MARKET_STATE_SUSPENDED"}
    finally:
        await rig.close()


async def test_subscribed_markets_are_not_also_sampled_over_rest(tmp_path) -> None:
    rig = Rig(tmp_path)
    try:
        other_k = rig.market_k.model_copy(update={"market_id": "k-2"})
        other_p = rig.market_p.model_copy(update={"market_id": "p-2"})
        other = _approved("g-2", "ada", other_k, other_p)
        rig.registry.groups[other.group_id] = other
        for market in (other_k, other_p):
            rig.registry.markets[market.market_key] = market
        rig.collector.set_universe(
            (rig.group, other), (rig.market_k, rig.market_p, other_k, other_p)
        )
        await rig.poll()
        assert set(rig.k.market_ids) == {K_ID, "k-2"}
        rig.k.connect()
        rig.k.snapshot(K_ID)
        await settle()

        # g-1 leaves the live set but stays subscribed (no restart for a market
        # that drops out). Its 60 s discovery sample must not alternate REST
        # books with the stream's.
        later = WALL + timedelta(hours=3)
        cold_k = rig.market_k.model_copy(update={"event_start_utc": later})
        cold_p = rig.market_p.model_copy(update={"event_start_utc": later})
        rig.collector.set_universe((rig.group, other), (cold_k, cold_p, other_k, other_p))
        rig.advance(60.0)
        await rig.poll()
        assert rig.collector.discovery_group_ids == {GROUP}
        assert rig.collector.polled_group_ids == {"g-2"}
        assert len(rig.factories[Venue.KALSHI].streams) == 1
        assert rig.kalshi.calls == []
        assert rig.polyus.calls == []
        assert rig.state(Venue.KALSHI, K_ID).meta.source == "stream"
    finally:
        await rig.close()


async def test_full_run_starts_streams_and_stops_cleanly(tmp_path) -> None:
    rig = Rig(tmp_path)
    task = asyncio.create_task(rig.collector.run())
    try:
        for _ in range(200):
            if all(factory.streams for factory in rig.factories.values()):
                break
            await asyncio.sleep(0)
        rig.k.connect()
        rig.p.connect()
        rig.k.snapshot(K_ID)
        rig.p.snapshot(P_ID, raw={"depth_unconfirmed": False})
        await settle()
        assert "qualified" in classes(rig.assessments())
        assert [kind for kind in rig.kinds() if kind == "stream_mode"] == [
            "stream_mode",
            "stream_mode",
        ]
        await rig.collector.stop()
        await asyncio.wait_for(task, 2)
        assert rig.k.closed and rig.p.closed
        assert "collector_shutdown" in rig.gaps
        assert {row[1] for row in rig.episodes()} == {"collector_shutdown"}
    finally:
        await rig.collector.stop()
        if not task.done():
            task.cancel()
        rig.conn.close()
