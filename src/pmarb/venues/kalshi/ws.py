"""Authenticated, observation-only Kalshi order-book WebSocket stream."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

import websockets

from pmarb.logging import RedactingFilter
from pmarb.models import Book, Level, Venue
from pmarb.protocols import BookEvent
from pmarb.venues.kalshi.auth import KalshiKey, kalshi_auth_headers
from pmarb.venues.kalshi.parse import normalize_orderbook_levels

logger = logging.getLogger(__name__)
WEBSOCKETS_LOGGER = logging.getLogger("websockets.client")
WEBSOCKETS_LOGGER.addFilter(RedactingFilter())
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"


class KalshiOrderbookStream(AsyncIterator[BookEvent]):
    """Maintain normalized books from Kalshi snapshots and ordered deltas.

    ``clock`` is a Unix-seconds clock used for signing. ``clock_utc`` and
    ``monotonic`` stamp emitted books; ``sleep`` and ``rng`` make reconnects
    deterministic for callers and tests.
    """

    def __init__(
        self,
        key_id: str,
        key: KalshiKey,
        market_tickers: Sequence[str],
        *,
        connect: Callable[..., Any] = websockets.connect,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
        clock_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.key_id = key_id
        self.key = key
        self.market_tickers = tuple(dict.fromkeys(market_tickers))
        if not self.market_tickers:
            raise ValueError("at least one market ticker is required")
        self._connect = connect
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._monotonic = monotonic
        self._clock_utc = clock_utc
        self.connection_generation = 0
        self.malformed_frames = 0
        self._socket: Any = None
        self._closed = False
        self._events: deque[BookEvent] = deque()
        self._commands: deque[dict[str, Any]] = deque()
        self._seq: dict[int, int] = {}
        self._channel_sids: dict[str, int] = {}
        self._books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
        self._awaiting_snapshot: set[str] = set(self.market_tickers)
        self._pending_recovery: set[str] = set()
        self._closed_tickers: set[str] = set()
        self._backoff_level = 0
        self._ever_connected = False
        self._sid_channels: dict[int, str] = {}
        self._force_reconnect = False
        self._command_id = 0

    def __aiter__(self) -> KalshiOrderbookStream:
        return self

    async def __anext__(self) -> BookEvent:
        while True:
            if self._events:
                return self._events.popleft()
            if self._closed:
                raise StopAsyncIteration
            if self._socket is None:
                await self._connect_and_subscribe()
                if self._events:
                    continue
            if self._commands:
                try:
                    await self._flush_commands()
                except asyncio.CancelledError:
                    await self.aclose()
                    raise
                except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
                    await self._connection_lost()
                    continue
            try:
                frame = await self._socket.recv()
            except asyncio.CancelledError:
                await self.aclose()
                raise
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
                await self._connection_lost()
                continue
            self._process_frame(frame)
            if self._force_reconnect:
                self._force_reconnect = False
                await self._connection_lost()
                continue
            if self._commands:
                try:
                    await self._flush_commands()
                except asyncio.CancelledError:
                    await self.aclose()
                    raise
                except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
                    await self._connection_lost()

    async def _send(self, command: dict[str, Any]) -> None:
        # websockets automatically answers server Ping frames with Pong frames;
        # control frames don't enter recv() or refresh book age.
        assert self._socket is not None
        await self._socket.send(json.dumps(command, separators=(",", ":")))

    async def _flush_commands(self) -> None:
        while self._commands:
            await self._send(self._commands.popleft())

    async def _connect_and_subscribe(self) -> None:
        try:
            if self._backoff_level:
                cap = min(30.0, 2.0 ** min(self._backoff_level - 1, 5))
                await self._sleep(cap * min(1.0, max(0.0, self._rng())))
            headers = kalshi_auth_headers(self.key_id, self.key, "GET", WS_PATH, self._clock)
            self._socket = await self._connect(
                WS_URL, additional_headers=headers, logger=WEBSOCKETS_LOGGER
            )
            if not self._ever_connected:
                self.connection_generation += 1
                self._ever_connected = True
            self._seq.clear()
            self._channel_sids.clear()
            self._sid_channels.clear()
            self._books.clear()
            self._awaiting_snapshot = set(self.market_tickers) - self._closed_tickers
            self._pending_recovery.clear()
            # Per https://docs.kalshi.com/websockets/orderbook-updates,
            # `channels`, `market_tickers`, `use_yes_price`, and
            # `update_interval_ms` select YES-scale, real-time books.
            await self._send(
                {
                    "id": self._next_id(),
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": list(self.market_tickers),
                        "use_yes_price": True,
                        "update_interval_ms": 0,
                    },
                }
            )
            await self._send(
                {
                    "id": self._next_id(),
                    "cmd": "subscribe",
                    "params": {"channels": ["market_lifecycle_v2"]},
                }
            )
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
            await self._connection_lost()

    def _next_id(self) -> int:
        self._command_id += 1
        return self._command_id

    def _process_frame(self, frame: Any) -> None:
        try:
            data = json.loads(frame) if isinstance(frame, (str, bytes, bytearray)) else frame
            if not isinstance(data, dict):
                raise TypeError("frame must be an object")
            frame_type = data.get("type")
            msg = data.get("msg", {})
            if msg is None and data.get("type") == "ok":
                msg = {}
            if not isinstance(msg, dict):
                raise TypeError("frame msg must be an object")
            sid = data.get("sid")
            if frame_type == "subscribed":
                self._handle_subscribed(msg, data)
                return
            if frame_type in {"error", "unsubscribed"}:
                self._force_reconnect = True
                return
            if frame_type == "ok":
                if "sid" in data and (not isinstance(sid, int) or isinstance(sid, bool)):
                    raise ValueError("ok response has invalid sid")
                if "seq" in data:
                    if "sid" not in data:
                        raise ValueError("ok response has seq without sid")
                    self._sequence_gap(sid, data, frame_type)
                return
            if frame_type in {
                "orderbook_snapshot",
                "orderbook_delta",
                "market_lifecycle_v2",
                "event_lifecycle",
                "event_fee_update",
            }:
                if not isinstance(sid, int) or isinstance(sid, bool):
                    raise ValueError("subscription sid missing")
                if self._sequence_gap(sid, data, frame_type):
                    return
                if frame_type == "orderbook_snapshot":
                    self._snapshot(msg, data)
                elif frame_type == "orderbook_delta":
                    self._delta(msg, data)
                else:
                    self._lifecycle(msg, data)
            else:
                raise ValueError("unknown WebSocket frame type")
        except (TypeError, ValueError, KeyError, ArithmeticError, json.JSONDecodeError) as exc:
            self.malformed_frames += 1
            logger.warning("Malformed Kalshi WebSocket market-data frame: %s", type(exc).__name__)
            ticker = None
            if isinstance(locals().get("msg"), dict):
                ticker = msg.get("market_ticker")
            self._invalidate(ticker if ticker in self.market_tickers else None, "malformed_frame")
            self._request_snapshot()

    def _handle_subscribed(self, msg: dict[str, Any], data: dict[str, Any]) -> None:
        channel = msg.get("channel")
        sid = msg.get("sid")
        if (
            not isinstance(channel, str)
            or not isinstance(sid, int)
            or isinstance(sid, bool)
            or channel not in {"orderbook_delta", "market_lifecycle_v2"}
        ):
            raise ValueError("subscribed response has invalid channel or sid")
        self._channel_sids[channel] = sid
        self._sid_channels[sid] = channel
        if "seq" in data:
            if "sid" in data and data["sid"] != sid:
                raise ValueError("subscribed response envelope sid does not match msg sid")
            marker = "ok" if channel == "orderbook_delta" else "event_lifecycle"
            self._sequence_gap(sid, data, marker)
        elif "sid" in data and data["sid"] != sid:
            raise ValueError("subscribed response envelope sid does not match msg sid")
        if channel == "orderbook_delta" and self._pending_recovery:
            self._request_snapshot()

    def _sequence_gap(self, sid: int, data: dict[str, Any], frame_type: str) -> bool:
        seq = data.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("invalid sequence number")
        previous = self._seq.get(sid)
        self._seq[sid] = seq
        channel = self._sid_channels.get(sid)
        if frame_type == "ok":
            if channel is None:
                raise ValueError("ok response references unknown subscription sid")
            expected_channel = channel
        else:
            expected_channel = (
                "market_lifecycle_v2"
                if frame_type in {"market_lifecycle_v2", "event_lifecycle", "event_fee_update"}
                else "orderbook_delta"
            )
        if channel is None:
            self._sid_channels[sid] = expected_channel
            self._channel_sids.setdefault(expected_channel, sid)
        elif channel != expected_channel:
            raise ValueError("frame arrived on the wrong subscription")
        if previous is not None and seq != previous + 1:
            self._invalidate(None, "sequence_gap")
            if channel == "market_lifecycle_v2":
                self._force_reconnect = True
            else:
                self._request_snapshot()
            return True
        return False

    def _snapshot(self, msg: dict[str, Any], data: dict[str, Any]) -> None:
        ticker = msg.get("market_ticker")
        if ticker not in self.market_tickers:
            raise ValueError("snapshot ticker is not subscribed")
        if ticker in self._closed_tickers:
            return
        yes = self._parse_wire_levels(msg.get("yes_dollars_fp", []))
        no = self._parse_wire_levels(msg.get("no_dollars_fp", []))
        self._books[ticker] = {"yes": yes, "no": no}
        self._awaiting_snapshot.discard(ticker)
        self._pending_recovery.discard(ticker)
        if not self._awaiting_snapshot:
            self._backoff_level = 0
        self._emit_book(ticker, data, msg)

    def _delta(self, msg: dict[str, Any], data: dict[str, Any]) -> None:
        ticker = msg.get("market_ticker")
        if ticker not in self.market_tickers:
            raise ValueError("delta ticker is not subscribed")
        if (
            ticker in self._closed_tickers
            or ticker in self._awaiting_snapshot
            or ticker not in self._books
        ):
            return
        side = msg.get("side")
        if side not in {"yes", "no"}:
            raise ValueError("delta side must be yes or no")
        price = self._decimal(msg.get("price_dollars"))
        delta = self._decimal(msg.get("delta_fp"))
        if not Decimal(0) <= price <= Decimal(1):
            raise ValueError("delta price is outside the binary price range")
        levels = self._books[ticker][side]
        if price not in levels:
            raise ValueError("delta references unknown price level")
        size = levels[price] + delta
        if size < 0:
            raise ValueError("delta produces negative level size")
        if size == 0:
            del levels[price]
        else:
            levels[price] = size
        self._emit_book(ticker, data, msg)

    def _emit_book(self, ticker: str, data: dict[str, Any], msg: dict[str, Any]) -> None:
        stored = self._books[ticker]
        yes_levels = [Level(price=p, size=s) for p, s in stored["yes"].items()]
        no_levels = [Level(price=p, size=s) for p, s in stored["no"].items()]
        yes_asks, no_asks = normalize_orderbook_levels(
            [[str(level.price), str(level.size)] for level in yes_levels],
            [[str(level.price), str(level.size)] for level in no_levels],
            no_prices_are_yes=True,
        )
        crossed = bool(
            yes_asks
            and no_asks
            and min(x.price for x in yes_asks) + min(x.price for x in no_asks) < 1
        )
        received_utc = self._aware_utc(self._clock_utc())
        book = Book(
            venue=Venue.KALSHI,
            market_id=ticker,
            yes_asks=yes_asks,
            no_asks=no_asks,
            raw_native={"frame": data, "msg": msg},
            source_ts=None,
            received_utc=received_utc,
            received_monotonic=self._monotonic(),
            connection_generation=self.connection_generation,
            valid=not crossed,
            invalid_reason="crossed_book" if crossed else None,
        )
        if crossed:
            self._events.append(self._event("invalidate", ticker, "crossed_book"))
            self._awaiting_snapshot.add(ticker)
            self._request_snapshot()
        else:
            if not self._awaiting_snapshot:
                self._backoff_level = 0
            self._events.append(BookEvent("snapshot", book, ticker, Venue.KALSHI, None))

    def _lifecycle(self, msg: dict[str, Any], data: dict[str, Any]) -> None:
        ticker = msg.get("market_ticker")
        if ticker not in self.market_tickers:
            return
        event_type = msg.get("event_type")
        close_ts = msg.get("close_ts")
        close_due = False
        if isinstance(close_ts, int | float):
            close_due = datetime.fromtimestamp(close_ts, UTC) <= self._aware_utc(self._clock_utc())
        elif isinstance(close_ts, str):
            parsed = datetime.fromisoformat(close_ts)
            close_due = parsed.tzinfo is not None and parsed <= self._aware_utc(self._clock_utc())
        if event_type in {"deactivated", "determined", "settled"} or (
            event_type == "close_date_updated" and close_due
        ):
            self._closed_tickers.add(ticker)
            self._awaiting_snapshot.discard(ticker)
            self._pending_recovery.discard(ticker)
            self._invalidate(ticker, "market_closed")
        elif event_type == "activated":
            self._closed_tickers.discard(ticker)
            self._invalidate(ticker, "market_reactivated")
            self._request_snapshot()

    def _parse_wire_levels(self, rows: Any) -> dict[Decimal, Decimal]:
        if not isinstance(rows, list):
            raise TypeError("book ladder must be an array")
        levels: dict[Decimal, Decimal] = {}
        for row in rows:
            if not isinstance(row, list | tuple) or len(row) != 2:
                raise ValueError("book level must contain price and size")
            price, size = self._decimal(row[0]), self._decimal(row[1])
            if price < 0 or price > 1 or size < 0 or price in levels:
                raise ValueError("invalid or duplicate snapshot level")
            if size > 0:
                levels[price] = size
        return levels

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        if not isinstance(value, str):
            raise TypeError("wire price and size must be strings")
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("wire price and size must be finite")
        return number

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC clock must return an aware datetime")
        return value.astimezone(UTC)

    def _request_snapshot(self) -> None:
        active = set(self.market_tickers) - self._closed_tickers
        if not active:
            return
        self._pending_recovery.update(active)
        if self._socket is None:
            return
        sid = self._channel_sids.get("orderbook_delta")
        if sid is None:
            return
        self._commands.append(
            {
                "id": self._next_id(),
                "cmd": "update_subscription",
                "params": {
                    "sids": [sid],
                    "action": "get_snapshot",
                    "market_tickers": [
                        ticker for ticker in self.market_tickers if ticker in active
                    ],
                },
            }
        )

    def _invalidate(self, ticker: str | None, reason: str) -> None:
        targets = (ticker,) if ticker else self.market_tickers
        for target in targets:
            if target not in self._closed_tickers:
                self._awaiting_snapshot.add(target)
            self._events.append(self._event("invalidate", target, reason))

    @staticmethod
    def _event(kind: str, ticker: str, reason: str) -> BookEvent:
        return BookEvent(kind, None, ticker, Venue.KALSHI, reason)

    async def _connection_lost(self) -> None:
        had_socket = self._socket is not None
        await self._close_socket()
        self._books.clear()
        self._awaiting_snapshot = set(self.market_tickers) - self._closed_tickers
        self._pending_recovery.clear()
        self._seq.clear()
        self._channel_sids.clear()
        self._sid_channels.clear()
        self._commands.clear()
        self.connection_generation += int(had_socket and self._ever_connected)
        self._backoff_level += 1
        for ticker in self.market_tickers:
            self._events.append(self._event("invalidate", ticker, "disconnected"))

    async def _close_socket(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await socket.close()
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
                logger.debug("Kalshi WebSocket close failed")

    async def aclose(self) -> None:
        self._closed = True
        await self._close_socket()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
