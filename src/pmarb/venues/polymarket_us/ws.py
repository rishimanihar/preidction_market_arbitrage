"""Authenticated Polymarket US market-data snapshot stream.

Only the camelCase/string-enum schema from the Markets WebSocket channel page
is accepted. The overview page documents a conflicting snake_case/numeric form;
frames in that form are counted and rejected until a captured frame changes the
pin deliberately.

The docs say a snapshot carries the "top levels" but never publish N. Until a
captured frame confirms the depth, each streamed book stays valid (it is the
venue's own snapshot) but ``raw_native["depth_unconfirmed"]`` is ``True``. The
evaluator never treats such a leg as atomic, so its results are indicative and
never qualified. ``depth_confirmed=True`` (config
``polyus_ws_depth_confirmed``) lifts the tag once that capture exists.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import WebSocketException

from pmarb.http import Backoff
from pmarb.logging import RedactingFilter
from pmarb.models import Book, Venue
from pmarb.protocols import BookEvent
from pmarb.venues.polymarket_us.auth import load_polyus_key, polyus_auth_headers
from pmarb.venues.polymarket_us.maintenance import in_maintenance, next_window
from pmarb.venues.polymarket_us.parse import parse_book

WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"
SUBSCRIPTION_TYPE = "SUBSCRIPTION_TYPE_MARKET_DATA"
# One captured real frame can replace this channel-page schema pin centrally.
PINNED_SCHEMA = {
    "subscription_type_field": "subscriptionType",
    "subscription_type": SUBSCRIPTION_TYPE,
    "market_data_field": "marketData",
    "market_slug_field": "marketSlug",
    "state_field": "state",
    "enum_type": str,
}
MAX_SLUGS_PER_SUBSCRIPTION = 100
HEARTBEAT_TIMEOUT_S = 30.0
MAINTENANCE_CHECK_INTERVAL_S = 1.0
_USER_AGENT = "pmarb/0.1 market-data observer"
_Material = Ed25519PrivateKey | str | Path


class PolyUSMarketStream:
    """Reconnectable async iterator of complete Polymarket US book snapshots.

    ``connect`` follows the ``websockets.connect(url, additional_headers=...)``
    shape and may be injected with a scripted async context manager in tests.
    ``clock`` may be a callable returning aware UTC datetimes, or an object with
    ``utcnow()``, ``monotonic()``, and ``sleep(seconds)`` methods.
    """

    venue = Venue.POLYMARKET_US

    def __init__(
        self,
        key_id: str,
        key: _Material,
        slugs: Sequence[str],
        *,
        connect: Callable[..., Any] | None = None,
        clock: Any = None,
        heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
        maintenance_check_interval_s: float = MAINTENANCE_CHECK_INTERVAL_S,
        backoff: Backoff | None = None,
        depth_confirmed: bool = False,
    ) -> None:
        if not key_id:
            raise ValueError("key_id must be non-empty")
        if heartbeat_timeout_s <= 0 or maintenance_check_interval_s <= 0:
            raise ValueError("heartbeat and maintenance intervals must be positive")
        if isinstance(key, Ed25519PrivateKey):
            self._key = key
        else:
            self._key = load_polyus_key(key)
        self.key_id = key_id
        self.slugs = tuple(dict.fromkeys(str(slug) for slug in slugs))
        if not self.slugs or any(not slug for slug in self.slugs):
            raise ValueError("slugs must contain non-empty market slugs")
        self._connect = connect or _NoRedirectConnect
        self._clock = clock
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._maintenance_check_interval_s = maintenance_check_interval_s
        self._backoff = backoff or Backoff()
        self._depth_confirmed = bool(depth_confirmed)
        self._closing = False
        self._maintenance_interrupt = False
        self._iterator: AsyncIterator[BookEvent] | None = None
        self._next_task: asyncio.Task[Any] | None = None
        self._connection_generation = 0
        self._last_books: dict[str, Book] = {}
        self.metrics: dict[str, Any] = {
            "schema_failure": 0,
            "depth_levels": {},
            "depth_messages": 0,
            "disconnects": 0,
            "heartbeats": 0,
        }
        self._transport_logger = logging.getLogger("pmarb.polyus.ws.transport")
        self._transport_logger.addFilter(RedactingFilter())
        self._transport_logger.setLevel(logging.WARNING)

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def __aiter__(self) -> PolyUSMarketStream:
        return self

    async def __anext__(self) -> BookEvent:
        if self._iterator is None:
            self._iterator = self._events()
        self._next_task = asyncio.current_task()
        try:
            return await anext(self._iterator)
        finally:
            self._next_task = None

    async def aclose(self) -> None:
        """Stop reconnecting and close any active receive/backoff cleanly."""
        self._closing = True
        task = self._next_task
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._iterator is not None:
            await self._iterator.aclose()

    async def _events(self) -> AsyncIterator[BookEvent]:
        retry = 0
        while not self._closing:
            connected_at: float | None = None
            now = self._utcnow()
            if in_maintenance(now):
                for event in self._invalidate_all("maintenance"):
                    yield event
                now = self._utcnow()
                if in_maintenance(now):
                    await self._sleep(self._maintenance_delay(now))
                continue

            try:
                headers = polyus_auth_headers(
                    self.key_id, self._key, "GET", WS_PATH, clock=self._auth_clock
                )
                async with self._connection(headers) as socket:
                    if self._closing:
                        return
                    if self._connection_generation == 0:
                        self._connection_generation = 1
                    self._maintenance_interrupt = False
                    connected_at = self._monotonic()
                    for start in range(0, len(self.slugs), MAX_SLUGS_PER_SUBSCRIPTION):
                        request_id = str(uuid.uuid4())
                        payload = {
                            "subscribe": {
                                "requestId": request_id,
                                "subscriptionType": SUBSCRIPTION_TYPE,
                                "marketSlugs": list(
                                    self.slugs[start : start + MAX_SLUGS_PER_SUBSCRIPTION]
                                ),
                            }
                        }
                        await socket.send(json.dumps(payload, separators=(",", ":")))
                    async with aclosing(self._receive(socket)) as events:
                        async for event in events:
                            yield event
                    if self._closing:
                        return
                    if not self._maintenance_interrupt:
                        raise ConnectionError("market stream ended")
                if self._maintenance_interrupt:
                    now = self._utcnow()
                    if in_maintenance(now):
                        await self._sleep(self._maintenance_delay(now))
                    continue
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, TimeoutError, WebSocketException):
                if self._closing:
                    return
                if (
                    connected_at is not None
                    and self._monotonic() - connected_at >= self._heartbeat_timeout_s
                ):
                    retry = 0
                self.metrics["disconnects"] += 1
                now = self._utcnow()
                # Advance before invalidations so observers cannot treat prior
                # generation books as current during the reconnect delay.
                if self._connection_generation:
                    self._connection_generation += 1
                reason = "maintenance" if in_maintenance(now) else "disconnect"
                for event in self._invalidate_all(reason):
                    yield event
                now = self._utcnow()
                if in_maintenance(now):
                    await self._sleep(self._maintenance_delay(now))
                else:
                    await self._sleep(self._backoff.delay(retry))
                    retry = min(retry + 1, 30)

    @asynccontextmanager
    async def _connection(self, headers: dict[str, str]):
        kwargs: dict[str, Any] = {
            "additional_headers": headers,
            "user_agent_header": _USER_AGENT,
        }
        if self._connect is _NoRedirectConnect:
            kwargs["logger"] = self._transport_logger
        connection = self._connect(WS_URL, **kwargs)
        if inspect.isawaitable(connection):
            connection = await connection
        if hasattr(connection, "__aenter__"):
            async with connection as socket:
                yield socket
        else:
            try:
                yield connection
            finally:
                close = getattr(connection, "close", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result

    async def _receive(self, socket: Any) -> AsyncIterator[BookEvent]:
        iterator = socket.__aiter__()
        last_heartbeat = self._monotonic()
        receive_task: asyncio.Task[Any] | None = None
        timer_task: asyncio.Task[None] | None = None
        try:
            while not self._closing:
                if receive_task is None:
                    receive_task = asyncio.create_task(anext(iterator))
                wait_s = min(
                    self._maintenance_check_interval_s,
                    max(0.001, self._heartbeat_timeout_s - (self._monotonic() - last_heartbeat)),
                )
                timer_task = asyncio.create_task(self._sleep(wait_s))
                done, _ = await asyncio.wait(
                    (receive_task, timer_task), return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task in done:
                    timer_task.cancel()
                    await _quiet_cancel(timer_task)
                    timer_task = None
                    try:
                        frame = receive_task.result()
                    except StopAsyncIteration:
                        return
                    receive_task = None
                    if self._monotonic() - last_heartbeat >= self._heartbeat_timeout_s:
                        raise TimeoutError("market stream heartbeat timeout")
                    if in_maintenance(self._utcnow()):
                        self._connection_generation += 1
                        self._maintenance_interrupt = True
                        for event in self._invalidate_all("maintenance"):
                            yield event
                        return
                    events, heartbeat = self._parse_frame(frame)
                    if heartbeat:
                        self.metrics["heartbeats"] += 1
                        last_heartbeat = self._monotonic()
                    else:
                        for event in events:
                            yield event
                    continue

                timer_task = None
                now = self._utcnow()
                if in_maintenance(now):
                    receive_task.cancel()
                    await _quiet_cancel(receive_task)
                    receive_task = None
                    self._connection_generation += 1
                    self._maintenance_interrupt = True
                    for event in self._invalidate_all("maintenance"):
                        yield event
                    return
                if self._monotonic() - last_heartbeat >= self._heartbeat_timeout_s:
                    receive_task.cancel()
                    await _quiet_cancel(receive_task)
                    receive_task = None
                    raise TimeoutError("market stream heartbeat timeout")
        finally:
            for task in (receive_task, timer_task):
                if task is not None and not task.done():
                    task.cancel()
                    await _quiet_cancel(task)

    def _parse_frame(self, frame: Any) -> tuple[list[BookEvent], bool]:
        try:
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8")
            payload = json.loads(frame) if isinstance(frame, str) else frame
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.metrics["schema_failure"] += 1
            return self._schema_invalidation(None), False

        if not isinstance(payload, dict):
            self.metrics["schema_failure"] += 1
            return self._schema_invalidation(None), False
        if set(payload) == {"heartbeat"} and isinstance(payload["heartbeat"], dict):
            return [], True

        data = payload.get(PINNED_SCHEMA["market_data_field"])
        if (
            payload.get(PINNED_SCHEMA["subscription_type_field"])
            != PINNED_SCHEMA["subscription_type"]
            or not isinstance(data, dict)
            or not isinstance(data.get(PINNED_SCHEMA["state_field"]), PINNED_SCHEMA["enum_type"])
            or "market_data" in payload
            or "subscription_type" in payload
            or "market_slug" in data
        ):
            self.metrics["schema_failure"] += 1
            slug = self._frame_slug(payload)
            return self._schema_invalidation(slug), False

        slug = data.get(PINNED_SCHEMA["market_slug_field"])
        if not isinstance(slug, str) or slug not in self.slugs:
            self.metrics["schema_failure"] += 1
            return self._schema_invalidation(slug if isinstance(slug, str) else None), False
        if not self._book_shape_is_pinned(data):
            self.metrics["schema_failure"] += 1
            return self._schema_invalidation(slug), False
        # The parser enforces string px/qty fields, explicit open state and the
        # YES offers / complemented NO bids semantics shared with REST.
        received_utc = self._utcnow()
        received_mono = self._monotonic()
        try:
            book = parse_book(
                payload,
                received_utc=received_utc,
                received_monotonic=received_mono,
                connection_generation=self._connection_generation,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return [self._invalidation(slug, "parse_failure")], False
        if (
            book.market_id != slug
            or book.invalid_reason
            and book.invalid_reason.startswith("invalid_book")
        ):
            return [self._invalidation(slug, "parse_failure")], False

        bid_count = len(data.get("bids", [])) if isinstance(data.get("bids"), list) else 0
        offer_count = len(data.get("offers", [])) if isinstance(data.get("offers"), list) else 0
        unconfirmed = not self._depth_confirmed
        depth = {
            "bid_levels": bid_count,
            "offer_levels": offer_count,
            "total_levels": bid_count + offer_count,
            "depth_unconfirmed": unconfirmed,
        }
        self.metrics["depth_levels"][slug] = depth["total_levels"]
        self.metrics["depth_messages"] += 1
        raw_native = dict(book.raw_native or {})
        raw_native["depth"] = depth
        # The book stays valid. The tag keeps it out of atomic (qualified)
        # results until a captured frame confirms how many levels arrive.
        raw_native["depth_unconfirmed"] = unconfirmed
        book = book.model_copy(update={"raw_native": raw_native})
        self._last_books[slug] = book
        return [
            BookEvent(kind="snapshot", book=book, market_id=slug, venue=self.venue, reason=None)
        ], False

    def _schema_invalidation(self, slug: str | None) -> list[BookEvent]:
        target = slug if slug in self.slugs else None
        if target is not None:
            return [self._invalidation(target, "schema_failure")]
        # With no reliable identity, invalidate every subscribed book; a bad
        # frame must not leave stale state available for qualification.
        self._last_books.clear()
        return self._invalidate_all("schema_failure")

    def _frame_slug(self, payload: dict[str, Any]) -> str | None:
        data = payload.get("market_data") or payload.get("marketData")
        if isinstance(data, dict):
            value = data.get("marketSlug") or data.get("market_slug")
            return value if isinstance(value, str) else None
        return None

    def _invalidation(self, slug: str, reason: str) -> BookEvent:
        self._last_books.pop(slug, None)
        return BookEvent(
            kind="invalidate", book=None, market_id=slug, venue=self.venue, reason=reason
        )

    def _invalidate_all(self, reason: str) -> list[BookEvent]:
        self._last_books.clear()
        return [
            BookEvent(kind="invalidate", book=None, market_id=slug, venue=self.venue, reason=reason)
            for slug in self.slugs
        ]

    @staticmethod
    def _book_shape_is_pinned(data: dict[str, Any]) -> bool:
        """Check wire field casing and primitive types before semantic parsing."""
        for side in ("bids", "offers"):
            rows = data.get(side)
            if not isinstance(rows, list):
                return False
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("px"), dict):
                    return False
                if not isinstance(row["px"].get("value"), str) or not isinstance(
                    row.get("qty"), str
                ):
                    return False
        return True

    def _utcnow(self) -> datetime:
        if self._clock is None:
            return datetime.now(UTC)
        if hasattr(self._clock, "utcnow"):
            value = self._clock.utcnow()
        else:
            value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(UTC)

    def _monotonic(self) -> float:
        if self._clock is not None and hasattr(self._clock, "monotonic"):
            return float(self._clock.monotonic())
        return time.monotonic()

    def _auth_clock(self) -> float:
        return self._utcnow().timestamp()

    async def _sleep(self, seconds: float) -> None:
        if self._clock is not None and hasattr(self._clock, "sleep"):
            await self._clock.sleep(max(0.0, seconds))
        else:
            await asyncio.sleep(max(0.0, seconds))

    def _maintenance_delay(self, now: datetime) -> float:
        _, end = next_window(now)
        return max(0.1, (end - now).total_seconds())


async def _quiet_cancel(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass


class _NoRedirectConnect(websocket_connect):
    """Production websocket connector that never forwards credentials on redirects."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc
