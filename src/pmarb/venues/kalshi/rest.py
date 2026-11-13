"""Public, observation-only Kalshi REST adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Self
from urllib.parse import quote

from pmarb.config import Settings
from pmarb.fees.kalshi import KalshiFeeModel
from pmarb.fees.scenario import (
    KALSHI_CONSERVATIVE_PRECISION,
    account_fee_value,
    with_scenario,
)
from pmarb.http import ObservationHttpClient, UnexpectedStatusError
from pmarb.models import Book, Market, Venue
from pmarb.protocols import BookEvent, FeeModel, collect_books
from pmarb.venues.kalshi.parse import SERIES_LEAGUES, is_eligible, parse_book, parse_market

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
SERIES_TICKERS = tuple(SERIES_LEAGUES)
PAGE_SIZE = 1000
EVENT_PAGE_SIZE = 200
BOOK_BATCH_SIZE = 100


class KalshiAdapter:
    """Read-only Kalshi adapter. Each normalized book retains the full wire payload."""

    venue = Venue.KALSHI

    def __init__(
        self,
        http: ObservationHttpClient | None = None,
        *,
        series_tickers: Sequence[str] = SERIES_TICKERS,
        clock_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        connection_generation: int = 0,
        settings: Settings | None = None,
    ) -> None:
        self._owns_http = http is None
        self.http = http or ObservationHttpClient()
        self.series_tickers = tuple(series_tickers)
        self._clock_utc = clock_utc
        self._monotonic = monotonic
        self.connection_generation = connection_generation
        self._settings = settings
        self._series_cache: dict[str, dict[str, Any]] = {}
        self._event_cache: dict[str, dict[str, Any]] = {}
        self.discovery_pages = 0

    async def _get_json(
        self,
        path: str,
        *,
        purpose: str,
        params: dict[str, Any] | None = None,
        route: str | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """GET ``path``. ``route`` is the id-free label used in errors (default ``path``)."""
        route = route or path
        result = await self.http.get(
            f"{BASE_URL}{path}", venue="kalshi", purpose=purpose, params=params, route=route
        )
        if not 200 <= result.status_code < 300:
            raise UnexpectedStatusError(
                f"Kalshi GET {route} returned HTTP {result.status_code}",
                status_code=result.status_code,
                venue="kalshi",
                route=route,
            )
        return result.json(), dict(result.headers)

    async def _series(self, ticker: str) -> dict[str, Any]:
        cached = self._series_cache.get(ticker)
        if cached is not None:
            return cached
        payload, _ = await self._get_json(
            f"/series/{quote(ticker, safe='')}", purpose="discovery", route="/series/{ticker}"
        )
        series = payload.get("series") if isinstance(payload, dict) else None
        if not isinstance(series, dict):
            raise TypeError("Kalshi series response has no series object")
        self._series_cache[ticker] = series
        return series

    async def _event(self, ticker: str) -> dict[str, Any]:
        cached = self._event_cache.get(ticker)
        if cached is not None:
            return cached
        payload, _ = await self._get_json(
            f"/events/{quote(ticker, safe='')}", purpose="discovery", route="/events/{ticker}"
        )
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict):
            raise TypeError("Kalshi event response has no event object")
        self._event_cache[ticker] = event
        return event

    async def _event_pages(self, series_ticker: str) -> AsyncIterator[dict[str, Any]]:
        """List event metadata for a configured series without nested markets.

        The Events endpoint supports a series filter and a maximum page size of
        200; see https://docs.kalshi.com/api-reference/events/get-events.md.
        """
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": "open",
                "limit": EVENT_PAGE_SIZE,
                "with_nested_markets": "false",
            }
            if cursor:
                params["cursor"] = cursor
            payload, _ = await self._get_json("/events", purpose="discovery", params=params)
            self.discovery_pages += 1
            if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
                raise TypeError("Kalshi events response has no events list")
            for event in payload["events"]:
                if not isinstance(event, dict):
                    raise TypeError("Kalshi events response contains a non-object event")
                ticker = event.get("event_ticker")
                event_series = event.get("series_ticker")
                if not isinstance(ticker, str) or not ticker:
                    raise TypeError("Kalshi event metadata has no event_ticker")
                if not isinstance(event_series, str) or event_series != series_ticker:
                    raise ValueError("Kalshi event metadata series_ticker does not match request")
                self._event_cache[ticker] = event
                yield event
            next_cursor = payload.get("cursor")
            if next_cursor is None or next_cursor == "":
                break
            if not isinstance(next_cursor, str):
                raise TypeError("Kalshi event pagination cursor must be a string, null, or empty")
            cursor = next_cursor

    async def _pages(self, series_ticker: str) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": "open",
                "limit": PAGE_SIZE,
            }
            if cursor:
                params["cursor"] = cursor
            payload, _ = await self._get_json("/markets", purpose="discovery", params=params)
            self.discovery_pages += 1
            if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
                raise TypeError("Kalshi markets response has no markets list")
            for row in payload["markets"]:
                if not isinstance(row, dict):
                    raise TypeError("Kalshi markets response contains a non-object market")
                yield row
            next_cursor = payload.get("cursor")
            if next_cursor is None or next_cursor == "":
                break
            if not isinstance(next_cursor, str):
                raise TypeError("Kalshi pagination cursor must be a string, null, or empty")
            cursor = next_cursor

    async def list_markets(
        self, horizon_start: datetime, horizon_end: datetime
    ) -> AsyncIterator[Market]:
        start, end = _utc(horizon_start), _utc(horizon_end)
        if end < start:
            raise ValueError("horizon_end must not precede horizon_start")
        self._event_cache.clear()
        for series_ticker in self.series_tickers:
            series = await self._series(series_ticker)
            async for _event in self._event_pages(series_ticker):
                pass
            async for raw in self._pages(series_ticker):
                if not is_eligible(raw, series)[0]:
                    continue
                event_ticker = raw.get("event_ticker")
                event = None
                if isinstance(event_ticker, str) and event_ticker:
                    event = self._event_cache.get(event_ticker)
                    if event is None:
                        event = await self._event(event_ticker)
                market = parse_market(raw, series, event)
                # The event occurrence is preferred. The parser leaves the expected
                # expiration hint in raw when no occurrence is available.
                candidate = market.event_start_utc
                if candidate is None:
                    expiration = _time(raw.get("expected_expiration_time"))
                    candidate = expiration - timedelta(hours=3) if expiration else None
                in_horizon = candidate is not None and start <= candidate.astimezone(UTC) <= end
                # Status is filtered as active by discovery; expected expiration
                # only supplies the fallback event-time hint and never expires it.
                if in_horizon:
                    yield market

    async def get_market(self, ticker: str) -> Market:
        payload, _ = await self._get_json(
            f"/markets/{quote(ticker, safe='')}", purpose="discovery", route="/markets/{ticker}"
        )
        raw = payload.get("market") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            raise TypeError("Kalshi market detail response has no market object")
        event_ticker = raw.get("event_ticker")
        event = await self._event(str(event_ticker)) if event_ticker else None
        # Markets do not reliably carry series_ticker; join through the event's
        # field instead of inferring it from the market ticker.
        series_ticker = (event or {}).get("series_ticker") or raw.get("series_ticker")
        if not series_ticker:
            raise ValueError("Kalshi market/event detail has no series_ticker")
        series = await self._series(str(series_ticker))
        return parse_market(raw, series, event)

    async def get_books(self, market_ids: Sequence[str]) -> list[Book]:
        """Batched books, up to 100 tickers per request; batches run concurrently.

        A failed batch does not discard the others: the collector gets
        :class:`~pmarb.protocols.PartialBooksError` with the books that arrived.
        """
        tickers = list(dict.fromkeys(market_ids))
        chunks = [
            tickers[offset : offset + BOOK_BATCH_SIZE]
            for offset in range(0, len(tickers), BOOK_BATCH_SIZE)
        ]
        results = await asyncio.gather(
            *(self._book_chunk(chunk) for chunk in chunks), return_exceptions=True
        )
        books = {
            book.market_id: book
            for book in collect_books(self.venue, list(zip(chunks, results, strict=True)))
        }
        return [books[ticker] for ticker in tickers if ticker in books]

    async def _book_chunk(self, chunk: list[str]) -> list[Book]:
        payload, headers = await self._get_json(
            "/markets/orderbooks",
            purpose="books",
            params={"tickers": chunk},
        )
        rows = payload.get("orderbooks") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise TypeError("Kalshi batch orderbook response has no orderbooks list")
        returned = [row.get("ticker") for row in rows if isinstance(row, dict)]
        if (
            len(returned) != len(rows)
            or any(not isinstance(ticker, str) for ticker in returned)
            or len(set(returned)) != len(returned)
        ):
            raise ValueError(
                "Kalshi batch orderbook response contains invalid or duplicate tickers"
            )
        if set(returned) != set(chunk):
            missing = sorted(set(chunk) - set(returned))
            unexpected = sorted(set(returned) - set(chunk))
            raise ValueError(
                f"Kalshi batch orderbook ticker mismatch: missing={missing}, unexpected={unexpected}"
            )
        received_utc = _utc(self._clock_utc())
        received_monotonic = self._monotonic()
        books: list[Book] = []
        for row in rows:
            ticker = row["ticker"]
            orderbook_fp = row.get("orderbook_fp")
            if not isinstance(orderbook_fp, dict):
                raise TypeError(f"Kalshi orderbook for {ticker!r} has no orderbook_fp object")
            book = parse_book(
                orderbook_fp,
                ticker=ticker,
                received_utc=received_utc,
                received_monotonic=received_monotonic,
                connection_generation=self.connection_generation,
                cache_headers=headers,
            )
            books.append(
                book.model_copy(update={"raw_native": {**payload, "cache_headers": dict(headers)}})
            )
        return books

    def book_stream(self, market_ids: Sequence[str]) -> AsyncIterator[BookEvent] | None:
        """Streaming is a later milestone; M1 polls REST."""
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    def fee_model(self, market: Market) -> FeeModel:
        """Series multiplier from ``fee_meta``; member precision from settings.

        A missing series multiplier leaves the quote unknown in every mode.
        Unset precision uses the non-direct ``0.01`` grid when
        ``fee_scenario`` is ``conservative``, and that quote is prefixed as a
        scenario bound. ``fee_scenario="none"`` leaves unset precision unknown.
        """
        precision, scenario = _member_precision(self._settings)
        return with_scenario(
            KalshiFeeModel(multiplier=_fee_multiplier(market), precision=precision),
            scenario,
        )


def _member_precision(settings: Settings | None) -> tuple[Decimal | None, bool]:
    configured = None
    scenario = None
    if settings is not None:
        scenario = settings.fee_scenario
        if settings.kalshi_member_precision is not None:
            configured = Decimal(settings.kalshi_member_precision)
    return account_fee_value(scenario, configured, KALSHI_CONSERVATIVE_PRECISION)


def _fee_multiplier(market: Market) -> Decimal | None:
    """Series ``fee_multiplier`` when it is a finite non-negative decimal."""
    raw = market.fee_meta.get("fee_multiplier") if market.fee_meta else None
    if raw is None or raw.strip() == "":
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("horizon datetimes must be timezone-aware")
    return value.astimezone(UTC)


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("wire timestamp must include a timezone")
    return parsed.astimezone(UTC)
