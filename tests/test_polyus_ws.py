"""Offline protocol tests for the Polymarket US market-data WebSocket."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from pmarb.collector.books import BookMeta, BookState
from pmarb.collector.evaluator import books_are_atomic, depth_unconfirmed
from pmarb.economics import LegInput, assess
from pmarb.http import Backoff
from pmarb.models import AssessmentClass, FeeQuote, Leg, Level, Side, Venue
from pmarb.secrets import SecretPolicyError
from pmarb.venues.polymarket_us.ws import WS_URL, PolyUSMarketStream, _NoRedirectConnect
from tests.conftest import make_book

SLUG = "synthetic-market"


class ZeroFee:
    """Known zero fee, so only book validity decides the assessment."""

    def taker_fee(self, levels, side):
        del levels, side
        return FeeQuote(amount=Decimal(0), provenance="test:zero")


def frame(*, bid="0.40", offer="0.60", slug=SLUG, state="MARKET_STATE_OPEN"):
    return {
        "requestId": "md-sub-1",
        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
        "marketData": {
            "marketSlug": slug,
            "bids": [{"px": {"value": bid, "currency": "USD"}, "qty": "2"}],
            "offers": [{"px": {"value": offer, "currency": "USD"}, "qty": "3"}],
            "state": state,
            "transactTime": "2030-01-06T18:00:00Z",
        },
    }


class FakeClock:
    def __init__(self, now=datetime(2030, 1, 6, 18, tzinfo=UTC), *, advance_sleeps=True):
        self.now = now
        self.mono = 100.0
        self.advance_sleeps = advance_sleeps
        self.sleeps: list[float] = []

    def utcnow(self):
        return self.now

    def monotonic(self):
        return self.mono

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.advance_sleeps:
            self.mono += seconds
            self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class FakeSocket:
    def __init__(self, incoming, sent=None):
        self.incoming = incoming
        self.sent = sent if sent is not None else []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        async def iterate():
            for item in self.incoming:
                yield json.dumps(item) if isinstance(item, dict) else item

        return iterate()


class WaitingSocket(FakeSocket):
    def __aiter__(self):
        async def iterate():
            for item in self.incoming:
                yield json.dumps(item)
            await asyncio.Future()

        return iterate()


class AdvancingSocket(FakeSocket):
    def __init__(self, incoming, clock, advance_s):
        super().__init__(incoming)
        self.clock = clock
        self.advance_s = advance_s

    def __aiter__(self):
        async def iterate():
            for item in self.incoming:
                self.clock.now += timedelta(seconds=self.advance_s)
                self.clock.mono += self.advance_s
                yield json.dumps(item)

        return iterate()


def connector(*sockets):
    opened = []
    pending = list(sockets)

    def connect(url, *, additional_headers, **kwargs):
        opened.append((url, additional_headers, kwargs))
        if not pending:
            raise AssertionError("unexpected reconnect")
        return pending.pop(0)

    return connect, opened


def stream_with(connect, clock=None, **kwargs):
    return PolyUSMarketStream(
        "synthetic-key-id",
        ed25519.Ed25519PrivateKey.generate(),
        [SLUG],
        connect=connect,
        clock=clock or FakeClock(),
        **kwargs,
    )


def test_subscribes_with_exact_full_market_data_schema_and_auth_headers():
    socket = FakeSocket([frame()])
    connect, opened = connector(socket)
    stream = stream_with(connect)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert opened[0][0] == WS_URL == "wss://api.polymarket.us/v1/ws/markets"
    headers = opened[0][1]
    assert set(headers) == {
        "X-PM-Access-Key",
        "X-PM-Timestamp",
        "X-PM-Signature",
    }
    assert headers["X-PM-Access-Key"] == "synthetic-key-id"
    assert opened[0][2].get("user_agent_header")
    assert len(socket.sent) == 1
    assert set(socket.sent[0]) == {"subscribe"}
    subscribe = socket.sent[0]["subscribe"]
    assert set(subscribe) == {"requestId", "subscriptionType", "marketSlugs"}
    assert subscribe["requestId"]
    assert subscribe["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
    assert subscribe["marketSlugs"] == [SLUG]
    assert event.kind == "snapshot"
    assert event.venue is Venue.POLYMARKET_US


def test_key_file_constructor_uses_external_owner_only_file_and_rejects_open_mode(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    secret = base64.b64encode(key.private_bytes_raw())
    key_path = tmp_path / "polyus.key"
    key_path.touch(mode=0o600)
    key_path.write_bytes(secret)
    key_path.chmod(0o600)
    connect, _ = connector(FakeSocket([frame()]))
    PolyUSMarketStream("synthetic-key-id", key_path, [SLUG], connect=connect, clock=FakeClock())
    assert key_path.stat().st_mode & 0o777 == 0o600

    open_path = tmp_path / "open.key"
    open_path.touch(mode=0o600)
    open_path.write_bytes(secret)
    open_path.chmod(0o644)
    with pytest.raises(SecretPolicyError) as error:
        PolyUSMarketStream(
            "synthetic-key-id", open_path, [SLUG], connect=connect, clock=FakeClock()
        )
    assert secret.decode() not in str(error.value)


def test_each_market_data_message_replaces_the_complete_book():
    first = frame(bid="0.40", offer="0.60")
    second = frame(bid="0.30", offer="0.70")
    socket = FakeSocket([first, second])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        one = await anext(stream)
        two = await anext(stream)
        await stream.aclose()
        return one, two

    one, two = asyncio.run(run())
    assert one.book is not None and two.book is not None
    assert [level.price for level in one.book.yes_asks] == [Decimal("0.60")]
    assert [level.price for level in two.book.yes_asks] == [Decimal("0.70")]
    assert [level.price for level in two.book.no_asks] == [Decimal("0.70")]
    assert len(two.book.yes_asks) == len(two.book.no_asks) == 1


@pytest.mark.parametrize(
    "bad_frame",
    [
        {"request_id": "md-sub-1", "subscription_type": 1, "market_data": {"market_slug": SLUG}},
        {"requestId": "md-sub-1", "subscriptionType": 1, "marketData": frame()["marketData"]},
        {
            "requestId": "md-sub-1",
            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
            "marketData": frame()["marketData"],
        },
    ],
)
def test_unsupported_schema_is_counted_and_never_merged(bad_frame):
    socket = FakeSocket([bad_frame])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert event.kind == "invalidate"
    assert event.market_id == SLUG
    assert event.reason == "schema_failure"
    assert stream.metrics["schema_failure"] == 1


def test_heartbeat_does_not_refresh_book_receipt_age():
    clock = FakeClock(advance_sleeps=False)
    socket = FakeSocket([frame(), {"heartbeat": {}}, frame(bid="0.35", offer="0.65")])

    class AdvancingSocket(FakeSocket):
        def __aiter__(self):
            async def iterate():
                for item in self.incoming:
                    if item == {"heartbeat": {}}:
                        clock.mono += 17
                        clock.now += timedelta(seconds=17)
                    yield json.dumps(item)

            return iterate()

    socket = AdvancingSocket(socket.incoming)
    connect, _ = connector(socket)
    stream = stream_with(connect, clock)

    async def run():
        first = await anext(stream)
        second = await anext(stream)  # Heartbeat is consumed internally.
        await stream.aclose()
        return first, second

    first, second = asyncio.run(run())
    assert first.book is not None and second.book is not None
    assert second.book.received_monotonic - first.book.received_monotonic == 17
    assert second.book.received_utc - first.book.received_utc == timedelta(seconds=17)


def test_disconnect_invalidates_old_book_and_reconnect_uses_new_generation():
    first_socket, next_socket = FakeSocket([frame()]), FakeSocket([frame(bid="0.25", offer="0.75")])
    connect, _ = connector(first_socket, next_socket)
    stream = stream_with(connect)

    async def run():
        events = [await anext(stream), await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    events = asyncio.run(run())
    assert [(event.kind, event.reason) for event in events] == [
        ("snapshot", None),
        ("invalidate", "disconnect"),
        ("snapshot", None),
    ]
    assert events[0].book is not None and events[2].book is not None
    assert events[0].book.connection_generation == 1
    assert events[2].book.connection_generation == 2


def test_reconnect_does_not_splice_books_across_multiple_slugs():
    second_slug = "synthetic-market-two"
    first_socket = FakeSocket([frame(slug=SLUG)])
    next_socket = FakeSocket([frame(slug=second_slug, bid="0.20", offer="0.80")])
    connect, _ = connector(first_socket, next_socket)
    stream = PolyUSMarketStream(
        "synthetic-key-id",
        ed25519.Ed25519PrivateKey.generate(),
        [SLUG, second_slug],
        connect=connect,
        clock=FakeClock(),
    )

    async def run():
        events = [await anext(stream) for _ in range(4)]
        await stream.aclose()
        return events

    events = asyncio.run(run())
    assert [(event.kind, event.market_id, event.reason) for event in events] == [
        ("snapshot", SLUG, None),
        ("invalidate", SLUG, "disconnect"),
        ("invalidate", second_slug, "disconnect"),
        ("snapshot", second_slug, None),
    ]
    assert events[0].book.connection_generation == 1
    assert events[3].book.connection_generation == 2


def test_reconnect_backoff_is_deterministic_and_caps_at_30_seconds():
    clock = FakeClock()
    attempts = 0

    def connect(_url, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("synthetic disconnect")

    stream = stream_with(connect, clock, backoff=Backoff(rng=lambda: 1.0))

    async def run():
        events = [await anext(stream) for _ in range(7)]
        await stream.aclose()
        return events

    events = asyncio.run(run())
    assert all(event.kind == "invalidate" and event.reason == "disconnect" for event in events)
    assert attempts == 7
    assert clock.sleeps == [1, 2, 4, 8, 16, 30]


def test_schema_failure_after_snapshot_invalidates_instead_of_reusing_levels():
    malformed = {"requestId": "md-sub-1", "marketData": {"marketSlug": SLUG}}
    socket = FakeSocket([frame(), malformed])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        events = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    first, failed = asyncio.run(run())
    assert first.kind == "snapshot" and first.book is not None
    assert first.book.valid and first.book.invalid_reason is None
    assert first.book.raw_native["depth_unconfirmed"] is True
    assert failed.kind == "invalidate" and failed.book is None
    assert failed.reason == "schema_failure"


@pytest.mark.parametrize("field,value", [("bids", None), ("offers", None), ("bids", "bad")])
def test_missing_or_wrong_shaped_ladders_are_schema_failures(field, value):
    malformed = frame()
    malformed["marketData"][field] = value
    socket = FakeSocket([malformed])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert event.kind == "invalidate"
    assert event.reason == "schema_failure"
    assert stream.metrics["schema_failure"] == 1


@pytest.mark.parametrize("field", ["px", "qty"])
def test_non_string_level_values_are_schema_failures(field):
    malformed = frame()
    malformed["marketData"]["offers"][0][field] = 0.6 if field == "px" else 3
    socket = FakeSocket([malformed])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert event.kind == "invalidate"
    assert event.reason == "schema_failure"
    assert stream.metrics["schema_failure"] == 1


def _stream_state(book):
    return BookState(
        book=book,
        meta=BookMeta(
            received_mono=book.received_monotonic,
            received_wall=book.received_utc,
            connection_generation=book.connection_generation,
            source="stream",
            cached=False,
        ),
    )


def _first_book(**kwargs):
    socket = FakeSocket([frame()])
    connect, _ = connector(socket)
    stream = stream_with(connect, **kwargs)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert event.kind == "snapshot" and event.book is not None
    return stream, event.book


def test_depth_is_recorded_and_tagged_unconfirmed_but_the_book_stays_valid():
    stream, book = _first_book()
    assert book.valid
    assert book.invalid_reason is None
    assert book.raw_native["depth_unconfirmed"] is True
    assert book.raw_native["depth"] == {
        "bid_levels": 1,
        "offer_levels": 1,
        "total_levels": 2,
        "depth_unconfirmed": True,
    }
    assert stream.metrics["depth_levels"] == {SLUG: 2}
    assert stream.metrics["depth_messages"] == 1
    assert depth_unconfirmed(book, depth_confirmed=True)

    # Everything else about the pair is atomic: both streamed, live, no skew.
    # Even with the config gate lifted, the tag alone keeps it non-atomic.
    kalshi = make_book(
        Venue.KALSHI,
        "synthetic-kalshi",
        [Level(price=Decimal("0.40"), size=Decimal(100))],
        [Level(price=Decimal("0.40"), size=Decimal(100))],
        received_monotonic=book.received_monotonic,
        received_utc=book.received_utc,
    )
    states = [_stream_state(kalshi), _stream_state(book)]

    def live(_venue, _generation):
        return True

    assert not books_are_atomic(states, Decimal("0.25"), stream_current=live, depth_confirmed=True)
    assessment = assess(
        "synthetic-pair",
        LegInput(
            Leg(venue=Venue.KALSHI, market_id="synthetic-kalshi", side=Side.YES), kalshi, ZeroFee()
        ),
        LegInput(Leg(venue=Venue.POLYMARKET_US, market_id=SLUG, side=Side.NO), book, ZeroFee()),
        Decimal(1),
        now_monotonic=book.received_monotonic,
        now_wall=book.received_utc,
        books_atomic=False,
        exception_risk=False,
        model_version="test",
    )
    assert assessment.result_class is not AssessmentClass.QUALIFIED
    assert "book_invalid_polymarket_us" not in assessment.reasons


def test_confirmed_depth_lifts_the_tag():
    _, book = _first_book(depth_confirmed=True)
    assert book.valid
    assert book.raw_native["depth_unconfirmed"] is False
    assert book.raw_native["depth"]["depth_unconfirmed"] is False
    assert not depth_unconfirmed(book, depth_confirmed=True)
    # The evaluator still gates Polymarket US streams on the config value.
    assert depth_unconfirmed(book, depth_confirmed=False)


def test_market_closed_state_is_invalid_and_cancellation_closes_socket():
    socket = FakeSocket([frame(state="MARKET_STATE_SUSPENDED")])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert event.kind == "snapshot"
    assert event.book is not None and not event.book.valid
    assert event.book.invalid_reason == "book_state_MARKET_STATE_SUSPENDED"
    assert socket.closed


def test_crossed_book_is_emitted_invalid_and_never_qualified():
    socket = FakeSocket([frame(bid="0.70", offer="0.60")])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert event.kind == "snapshot"
    assert event.book is not None and not event.book.valid
    assert event.book.invalid_reason == "crossed_book"


def test_maintenance_crossing_invalidates_and_closes_pending_receive():
    clock = FakeClock(datetime(2026, 11, 12, 10, 59, 59, tzinfo=UTC), advance_sleeps=False)
    socket = WaitingSocket([frame()])
    connect, _ = connector(socket)
    stream = stream_with(connect, clock, maintenance_check_interval_s=1)

    async def run():
        first = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        clock.now += timedelta(seconds=1)
        clock.mono += 1
        maintenance = await pending
        await stream.aclose()
        live_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return first, maintenance, live_tasks

    first, maintenance, live_tasks = asyncio.run(run())
    assert first.kind == "snapshot"
    assert maintenance.kind == "invalidate"
    assert maintenance.reason == "maintenance"
    assert maintenance.market_id == SLUG
    assert socket.closed
    assert clock.now.hour == 11
    assert live_tasks == []


def test_starting_during_maintenance_invalidates_without_connecting():
    clock = FakeClock(datetime(2026, 11, 12, 11, 30, tzinfo=UTC))

    def connect(*_args, **_kwargs):
        raise AssertionError("must defer connection until maintenance ends")

    stream = stream_with(connect, clock)

    async def run():
        event = await anext(stream)
        await stream.aclose()
        return event

    event = asyncio.run(run())
    assert event.kind == "invalidate" and event.reason == "maintenance"
    assert event.market_id == SLUG


def test_missing_heartbeat_times_out_without_real_sleep_or_orphan_tasks():
    clock = FakeClock()
    socket = WaitingSocket([frame()])
    connect, _ = connector(socket)
    stream = stream_with(connect, clock, heartbeat_timeout_s=2, maintenance_check_interval_s=1)

    async def run():
        first = await anext(stream)
        disconnected = await anext(stream)
        await stream.aclose()
        live_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return first, disconnected, live_tasks

    first, disconnected, live_tasks = asyncio.run(run())
    assert first.kind == "snapshot"
    assert disconnected.kind == "invalidate" and disconnected.reason == "disconnect"
    assert clock.sleeps
    assert socket.closed
    assert live_tasks == []


def test_cancelling_pending_next_closes_socket_and_cleans_receive_tasks():
    socket = WaitingSocket([frame()])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        initial = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()
        live_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return initial, live_tasks

    initial, live_tasks = asyncio.run(run())
    assert initial.kind == "snapshot"
    assert socket.closed
    assert live_tasks == []


def test_closing_stream_while_next_is_pending_cancels_consumer_cleanly():
    socket = WaitingSocket([frame()])
    connect, _ = connector(socket)
    stream = stream_with(connect)

    async def run():
        initial = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await stream.aclose()
        with pytest.raises(asyncio.CancelledError):
            await pending
        live_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return initial, live_tasks

    initial, live_tasks = asyncio.run(run())
    assert initial.kind == "snapshot"
    assert socket.closed
    assert live_tasks == []


def test_continuous_market_data_cannot_hide_missing_heartbeats():
    clock = FakeClock(advance_sleeps=False)
    socket = AdvancingSocket([frame(), frame(bid="0.35", offer="0.65")], clock, 3)
    connect, _ = connector(socket)
    stream = stream_with(
        connect,
        clock,
        heartbeat_timeout_s=5,
        maintenance_check_interval_s=1,
    )

    async def run():
        events = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return events

    snapshot, timeout = asyncio.run(run())
    assert snapshot.kind == "snapshot" and snapshot.book is not None
    assert snapshot.book.received_monotonic == 103
    assert timeout.kind == "invalidate" and timeout.reason == "disconnect"
    assert stream.metrics["heartbeats"] == 0
    assert socket.closed


def test_resuming_after_maintenance_end_reconnects_without_week_delay():
    # 05:59:00 New York on Thursday, with a controllable clock and active feed.
    clock = FakeClock(datetime(2026, 11, 12, 10, 59, tzinfo=UTC), advance_sleeps=False)
    first_socket = WaitingSocket([frame()])
    resumed_socket = FakeSocket([frame(bid="0.25", offer="0.75")])
    connect, opened = connector(first_socket, resumed_socket)
    stream = stream_with(connect, clock, maintenance_check_interval_s=1)

    async def run():
        initial = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        # Make the timer observe 06:00:01, then consume its maintenance event.
        clock.now += timedelta(seconds=61)
        clock.mono += 61
        maintenance = await pending
        clock.now = datetime(2026, 11, 12, 13, 0, 1, tzinfo=UTC)  # 08:00:01 local.
        clock.mono += 7199
        resumed = await anext(stream)
        await stream.aclose()
        return initial, maintenance, resumed

    initial, maintenance, resumed = asyncio.run(run())
    assert initial.kind == "snapshot"
    assert maintenance.kind == "invalidate" and maintenance.reason == "maintenance"
    assert resumed.kind == "snapshot" and resumed.book is not None
    assert resumed.book.connection_generation > initial.book.connection_generation
    assert len(opened) == 2
    assert all(seconds < 10 for seconds in clock.sleeps)
    assert first_socket.closed and resumed_socket.closed


def test_default_websocket_connector_rejects_redirect_with_supported_constructor():
    connector = _NoRedirectConnect(
        WS_URL,
        additional_headers={"X-PM-Access-Key": "synthetic-key-id"},
    )
    redirect = InvalidStatus(
        Response(
            302,
            "Found",
            Headers([("Location", "wss://other.example/redirect")]),
        )
    )
    assert connector.process_redirect(redirect) is redirect


def test_sustained_connect_failures_keep_backoff_counter_finite():
    clock = FakeClock(advance_sleeps=False)
    attempts = 0

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("synthetic outage")

    stream = stream_with(connect, clock, backoff=Backoff(rng=lambda: 0.5))

    async def run():
        events = [await anext(stream) for _ in range(1100)]
        await stream.aclose()
        live_tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return events, live_tasks

    events, live_tasks = asyncio.run(run())
    assert attempts == 1100
    assert all(event.kind == "invalidate" and event.reason == "disconnect" for event in events)
    assert len(clock.sleeps) == 1099
    assert max(clock.sleeps) <= 30
    assert clock.sleeps[-1] == 22.5
    assert live_tasks == []
