"""Offline tests for Kalshi's authenticated order-book WebSocket stream."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from pmarb.logging import RedactingFilter
from pmarb.venues.kalshi.parse import parse_book
from pmarb.venues.kalshi.ws import WS_PATH, WS_URL, KalshiOrderbookStream

NOW = datetime(2030, 1, 5, tzinfo=UTC)


def frame(kind, sid, seq, msg):
    return json.dumps({"type": kind, "sid": sid, "seq": seq, "msg": msg})


def subscribed(channel, sid):
    return frame("subscribed", sid, 1, {"channel": channel, "sid": sid})


def snapshot(ticker="SYNTH", *, yes=None, no=None, sid=7, seq=2):
    return frame(
        "orderbook_snapshot",
        sid,
        seq,
        {
            "market_ticker": ticker,
            "yes_dollars_fp": yes if yes is not None else [["0.3000", "5.00"]],
            "no_dollars_fp": no if no is not None else [["0.8000", "4.00"]],
        },
    )


def delta(ticker="SYNTH", *, side="yes", price="0.3000", amount="1.00", sid=7, seq=3):
    return frame(
        "orderbook_delta",
        sid,
        seq,
        {
            "market_ticker": ticker,
            "price_dollars": price,
            "delta_fp": amount,
            "side": side,
        },
    )


class ScriptedSocket:
    def __init__(self, *frames):
        self.incoming = asyncio.Queue()
        for item in frames:
            self.incoming.put_nowait(item)
        self.sent = []
        self.closed = False

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def close(self):
        self.closed = True

    def push(self, item):
        self.incoming.put_nowait(item)


def make_stream(*sockets, tickers=("SYNTH",), **kwargs):
    connects = []

    async def connect(uri, **options):
        connects.append((uri, options, key))
        return sockets[len(connects) - 1]

    key = ed25519.Ed25519PrivateKey.generate()
    kwargs.setdefault("clock", lambda: 1_700_000_000)
    kwargs.setdefault("clock_utc", lambda: NOW)
    kwargs.setdefault("monotonic", lambda: 42.5)
    stream = KalshiOrderbookStream(
        "synthetic-key-id",
        key,
        tickers,
        connect=connect,
        **kwargs,
    )
    return stream, connects


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=2))


def test_snapshot_then_deltas_update_book_and_stamp_raw_frame():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            subscribed("market_lifecycle_v2", 8),
            snapshot(),
            delta(amount="2.00", seq=3),
        )
        stream, _ = make_stream(socket)
        first = await anext(stream)
        second = await anext(stream)
        await stream.aclose()
        return first, second

    first, second = run(scenario())
    assert first.kind == second.kind == "snapshot"
    assert first.book.yes_asks == second.book.yes_asks
    assert second.book.no_asks[0].size == Decimal("7.00")
    assert second.book.connection_generation == 1
    assert second.book.received_utc == NOW and second.book.received_monotonic == 42.5
    assert second.book.raw_native["frame"]["type"] == "orderbook_delta"


def test_zero_delta_removes_level_and_snapshot_replaces_entire_book():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot(),
            delta(side="yes", amount="-5.00", seq=3),
            snapshot(yes=[["0.2500", "2.00"]], no=[], seq=4),
        )
        stream, _ = make_stream(socket)
        initial, removed, replaced = await anext(stream), await anext(stream), await anext(stream)
        await stream.aclose()
        return initial, removed, replaced

    initial, removed, replaced = run(scenario())
    assert initial.book.no_asks
    assert removed.book.no_asks == []
    assert replaced.book.no_asks[0].price == Decimal("0.7500")
    assert replaced.book.yes_asks == []


@pytest.mark.parametrize(
    "bad_delta",
    [
        delta(amount="-6.00", seq=3),
        delta(price="0.2500", amount="1.00", seq=3),
    ],
    ids=["negative-result", "unknown-level"],
)
def test_invalid_delta_invalidates_book(bad_delta):
    async def scenario():
        socket = ScriptedSocket(subscribed("orderbook_delta", 7), snapshot(), bad_delta)
        stream, _ = make_stream(socket)
        initial, invalidated = await anext(stream), await anext(stream)
        await stream.aclose()
        return initial, invalidated

    initial, invalidated = run(scenario())
    assert initial.kind == "snapshot"
    assert invalidated.kind == "invalidate"


def test_sequence_gap_invalidates_all_tickers_on_sid_requests_snapshot_and_recovers():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot("A", sid=7, seq=2),
            delta("A", amount="1.00", sid=7, seq=4),
            delta("A", amount="10.00", sid=7, seq=5),
            delta("B", amount="10.00", sid=7, seq=6),
            delta("A", amount="10.00", sid=7, seq=8),
            snapshot("A", sid=7, seq=9),
            snapshot("B", sid=7, seq=10),
            delta("A", amount="2.00", sid=7, seq=11),
            delta("B", amount="3.00", sid=7, seq=12),
        )
        stream, _ = make_stream(socket, tickers=("A", "B"))
        events = [await anext(stream) for _ in range(9)]
        sent = list(socket.sent)
        await stream.aclose()
        return events, sent

    events, sent = run(scenario())
    assert [event.market_id for event in events[:1]] == ["A"]
    assert [(event.kind, event.market_id, event.reason) for event in events[1:3]] == [
        ("invalidate", "A", "sequence_gap"),
        ("invalidate", "B", "sequence_gap"),
    ]
    assert [(event.kind, event.market_id, event.reason) for event in events[3:5]] == [
        ("invalidate", "A", "sequence_gap"),
        ("invalidate", "B", "sequence_gap"),
    ]
    assert [event.market_id for event in events[5:7]] == ["A", "B"]
    assert [event.book.no_asks[0].size for event in events[5:7]] == [
        Decimal("5.00"),
        Decimal("5.00"),
    ]
    assert [event.book.no_asks[0].size for event in events[7:]] == [
        Decimal("7.00"),
        Decimal("8.00"),
    ]
    recoveries = [command for command in sent if command["cmd"] == "update_subscription"]
    assert len(recoveries) == 2
    assert (
        recoveries[0]["params"]
        == recoveries[1]["params"]
        == {"sids": [7], "action": "get_snapshot", "market_tickers": ["A", "B"]}
    )


def test_ok_update_acknowledgment_bridges_gap_and_does_not_refresh_book_age():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot(sid=7, seq=2),
            delta(amount="1.00", sid=7, seq=4),
            json.dumps(
                {
                    "type": "ok",
                    "id": 3,
                    "sid": 7,
                    "seq": 5,
                    "msg": {"market_tickers": ["SYNTH"]},
                }
            ),
            snapshot(sid=7, seq=6),
            delta(amount="2.00", sid=7, seq=7),
        )
        utc_times = iter(range(1, 10))
        monotonic_times = iter(range(1, 10))
        stream, _ = make_stream(
            socket,
            clock_utc=lambda: NOW + timedelta(seconds=next(utc_times)),
            monotonic=lambda: float(next(monotonic_times) * 10),
        )
        events = [await anext(stream) for _ in range(4)]
        commands = list(socket.sent)
        malformed_count = stream.malformed_frames
        await stream.aclose()
        return events, commands, malformed_count

    events, commands, malformed_count = run(scenario())
    assert [event.kind for event in events] == ["snapshot", "invalidate", "snapshot", "snapshot"]
    books = [event.book for event in events if event.book is not None]
    assert [book.received_utc for book in books] == [
        NOW + timedelta(seconds=1),
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=3),
    ]
    assert [book.received_monotonic for book in books] == [10.0, 20.0, 30.0]
    recoveries = [command for command in commands if command["cmd"] == "update_subscription"]
    assert len(recoveries) == 1
    assert malformed_count == 0


def test_ok_without_optional_sid_or_seq_is_ignored_without_resync():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot(sid=7, seq=2),
            json.dumps({"type": "ok", "id": 99, "msg": {"market_tickers": ["SYNTH"]}}),
            delta(amount="1.00", sid=7, seq=3),
        )
        stream, _ = make_stream(socket)
        events = [await anext(stream) for _ in range(2)]
        commands = list(socket.sent)
        malformed_count = stream.malformed_frames
        await stream.aclose()
        return events, commands, malformed_count

    events, commands, malformed_count = run(scenario())
    assert [event.kind for event in events] == ["snapshot", "snapshot"]
    assert not any(command["cmd"] == "update_subscription" for command in commands)
    assert malformed_count == 0


def test_sequence_is_per_sid_and_lifecycle_sequence_does_not_interrupt_books():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            subscribed("market_lifecycle_v2", 8),
            snapshot("A", sid=7, seq=2),
            frame("event_lifecycle", 8, 2, {"event_ticker": "EVT", "event_type": "updated"}),
            frame("event_fee_update", 8, 3, {"event_ticker": "EVT"}),
            frame(
                "market_lifecycle_v2",
                8,
                4,
                {
                    "market_ticker": "A",
                    "event_type": "close_date_updated",
                    "close_ts": 1_900_000_000,
                },
            ),
            delta("A", amount="1.00", sid=7, seq=3),
        )
        stream, _ = make_stream(socket, tickers=("A",))
        first, second = await anext(stream), await anext(stream)
        malformed_count = stream.malformed_frames
        await stream.aclose()
        return first, second, malformed_count

    first, second, malformed_count = run(scenario())
    assert first.kind == "snapshot"
    assert second.kind == "snapshot"
    assert second.book.no_asks[0].size == Decimal("6.00")
    assert malformed_count == 0


def test_disconnect_invalidates_bumps_generation_resubscribes_and_waits_for_snapshot():
    async def scenario():
        first_socket = ScriptedSocket(
            subscribed("orderbook_delta", 7), snapshot(), OSError("synthetic disconnect")
        )
        second_socket = ScriptedSocket(subscribed("orderbook_delta", 9), snapshot(seq=2))
        sleeps = []

        async def no_sleep(delay):
            sleeps.append(delay)

        stream, connects = make_stream(first_socket, second_socket, sleep=no_sleep, rng=lambda: 0.5)
        initial = await anext(stream)
        disconnected = await anext(stream)
        recovered = await anext(stream)
        generation = stream.connection_generation
        await stream.aclose()
        return initial, disconnected, recovered, generation, connects, sleeps, first_socket

    initial, disconnected, recovered, generation, connects, sleeps, first_socket = run(scenario())
    assert initial.kind == "snapshot"
    assert (disconnected.kind, disconnected.reason) == ("invalidate", "disconnected")
    assert recovered.kind == "snapshot" and recovered.book.connection_generation == 2
    assert generation == 2 and len(connects) == 2 and sleeps == [0.5]
    assert all(command["cmd"] == "subscribe" for command in first_socket.sent)


def test_lifecycle_settled_invalidates_and_close_date_updated_at_past_time_closes():
    async def scenario(event_type, close_ts=None):
        msg = {"market_ticker": "SYNTH", "event_type": event_type}
        if close_ts is not None:
            msg["close_ts"] = close_ts
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            subscribed("market_lifecycle_v2", 8),
            snapshot(),
            frame("market_lifecycle_v2", 8, 2, msg),
        )
        stream, _ = make_stream(socket)
        await anext(stream)
        event = await anext(stream)
        await stream.aclose()
        return event

    settled = run(scenario("settled"))
    closed_by_date = run(scenario("close_date_updated", "2029-12-01T00:00:00Z"))
    assert (settled.kind, settled.reason) == ("invalidate", "market_closed")
    assert (closed_by_date.kind, closed_by_date.reason) == ("invalidate", "market_closed")


def test_closed_ticker_does_not_block_recovery_for_open_ticker():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            subscribed("market_lifecycle_v2", 8),
            snapshot("A", sid=7, seq=2),
            snapshot("B", sid=7, seq=3),
            frame("market_lifecycle_v2", 8, 2, {"market_ticker": "A", "event_type": "settled"}),
            delta("B", sid=7, seq=5),
            snapshot("A", sid=7, seq=6),
            snapshot("B", sid=7, seq=7),
            delta("B", amount="2.00", sid=7, seq=8),
        )
        stream, _ = make_stream(socket, tickers=("A", "B"))
        events = [await anext(stream) for _ in range(7)]
        commands = list(socket.sent)
        await stream.aclose()
        return events, commands

    events, commands = run(scenario())
    assert [(event.kind, event.market_id) for event in events] == [
        ("snapshot", "A"),
        ("snapshot", "B"),
        ("invalidate", "A"),
        ("invalidate", "A"),
        ("invalidate", "B"),
        ("snapshot", "B"),
        ("snapshot", "B"),
    ]
    recovery = next(command for command in commands if command["cmd"] == "update_subscription")
    assert recovery["params"]["market_tickers"] == ["B"]


@pytest.mark.parametrize("control_type", ["error", "unsubscribed"])
def test_server_error_and_unsubscribe_control_frames_reconnect(control_type):
    async def scenario():
        first_socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot(),
            json.dumps({"type": control_type, "msg": {"sid": 7}}),
        )
        second_socket = ScriptedSocket(subscribed("orderbook_delta", 9), snapshot(seq=2))
        stream, connects = make_stream(first_socket, second_socket)
        initial = await anext(stream)
        disconnected = await anext(stream)
        recovered = await anext(stream)
        await stream.aclose()
        return initial, disconnected, recovered, connects

    initial, disconnected, recovered, connects = run(scenario())
    assert initial.kind == "snapshot"
    assert (disconnected.kind, disconnected.reason) == ("invalidate", "disconnected")
    assert recovered.kind == "snapshot" and recovered.book.connection_generation == 2
    assert len(connects) == 2


def test_crossed_book_invalidates_instead_of_emitting_snapshot():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot(yes=[["0.8000", "1.00"]], no=[["0.3000", "1.00"]]),
        )
        stream, _ = make_stream(socket)
        event = await anext(stream)
        await stream.aclose()
        return event

    event = run(scenario())
    assert event.kind == "invalidate" and event.reason == "crossed_book"


def test_malformed_json_and_invalid_frame_envelope_are_counted_and_invalidate():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot(),
            "{bad json",
            json.dumps({"type": "orderbook_delta"}),
        )
        stream, _ = make_stream(socket)
        first = await anext(stream)
        malformed = await anext(stream)
        malformed_envelope = await anext(stream)
        count = stream.malformed_frames
        await stream.aclose()
        return first, malformed, malformed_envelope, count

    first, malformed, envelope, count = run(scenario())
    assert first.kind == "snapshot"
    assert (malformed.kind, malformed.reason) == ("invalidate", "malformed_frame")
    assert (envelope.kind, envelope.reason) == ("invalidate", "malformed_frame")
    assert count == 2


def test_use_yes_price_matches_rest_acquisition_ladders_without_second_complement():
    async def scenario():
        socket = ScriptedSocket(
            subscribed("orderbook_delta", 7),
            snapshot(yes=[["0.3000", "5.00"]], no=[["0.8000", "4.00"]]),
        )
        stream, _ = make_stream(socket)
        event = await anext(stream)
        await stream.aclose()
        return event

    ws_book = run(scenario()).book
    rest_book = parse_book(
        {"yes_dollars": [["0.3000", "5.00"]], "no_dollars": [["0.2000", "4.00"]]},
        ticker="SYNTH",
        received_utc=NOW,
        received_monotonic=42.5,
        connection_generation=1,
        cache_headers={},
    )
    assert ws_book.yes_asks == rest_book.yes_asks
    assert ws_book.no_asks == rest_book.no_asks
    assert ws_book.yes_asks[0].price == Decimal("0.8000")
    assert ws_book.no_asks[0].price == Decimal("0.7000")


def test_handshake_signs_exact_websocket_path_and_subscribes_with_documented_fields():
    async def scenario():
        socket = ScriptedSocket(subscribed("orderbook_delta", 7), snapshot())
        stream, connects = make_stream(socket)
        await anext(stream)
        commands = list(socket.sent)
        await stream.aclose()
        return connects, commands

    connects, commands = run(scenario())
    uri, options, key = connects[0]
    headers = options["additional_headers"]
    assert uri == WS_URL
    assert any(isinstance(item, RedactingFilter) for item in options["logger"].filters)
    assert headers["KALSHI-ACCESS-KEY"] == "synthetic-key-id"
    signature_message = f"1700000000000GET{WS_PATH}".encode()
    key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), signature_message)
    orderbook = next(
        command["params"] for command in commands if command["params"].get("market_tickers")
    )
    assert orderbook == {
        "channels": ["orderbook_delta"],
        "market_tickers": ["SYNTH"],
        "use_yes_price": True,
        "update_interval_ms": 0,
    }
    assert any(command["params"].get("channels") == ["market_lifecycle_v2"] for command in commands)


def test_backoff_uses_injected_rng_and_cancel_closes_socket_and_propagates():
    async def scenario():
        first = ScriptedSocket(subscribed("orderbook_delta", 7), snapshot(), OSError("drop"))
        second = ScriptedSocket()
        sleeps = []

        async def no_sleep(delay):
            sleeps.append(delay)

        stream, _ = make_stream(first, second, sleep=no_sleep, rng=lambda: 0.25)
        await anext(stream)
        invalidated = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        return invalidated, sleeps, first, second

    invalidated, sleeps, first, second = run(scenario())
    assert invalidated.reason == "disconnected"
    assert sleeps == [0.25]
    assert first.closed and second.closed


def test_backoff_caps_at_thirty_seconds_with_full_jitter():
    async def scenario():
        socket = ScriptedSocket(subscribed("orderbook_delta", 7), snapshot())
        attempts = 0
        sleeps = []

        async def connect(uri, **options):
            nonlocal attempts
            attempts += 1
            if attempts <= 6:
                raise OSError("synthetic handshake failure")
            return socket

        async def no_sleep(delay):
            sleeps.append(delay)

        stream = KalshiOrderbookStream(
            "synthetic-key-id",
            ed25519.Ed25519PrivateKey.generate(),
            ["SYNTH"],
            connect=connect,
            sleep=no_sleep,
            rng=lambda: 0.5,
            clock=lambda: 1_700_000_000,
            clock_utc=lambda: NOW,
        )
        invalidations = [await anext(stream) for _ in range(6)]
        book = await anext(stream)
        await stream.aclose()
        return invalidations, book, sleeps

    invalidations, book, sleeps = run(scenario())
    assert all(event.kind == "invalidate" for event in invalidations)
    assert book.kind == "snapshot"
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 15.0]


def test_cancellation_during_backoff_sleep_propagates_cleanly():
    async def scenario():
        socket = ScriptedSocket(subscribed("orderbook_delta", 7), snapshot(), OSError("drop"))
        sleep_started = asyncio.Event()

        async def blocked_sleep(_delay):
            sleep_started.set()
            await asyncio.Event().wait()

        stream, _ = make_stream(socket, sleep=blocked_sleep)
        await anext(stream)
        invalidated = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await sleep_started.wait()
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            return invalidated, socket.closed
        raise AssertionError("cancellation should propagate")

    invalidated, closed = run(scenario())
    assert invalidated.reason == "disconnected"
    assert closed
