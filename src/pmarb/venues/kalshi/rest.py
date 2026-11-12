"""Public, observation-only Kalshi REST adapter."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Self
from urllib.parse import quote

from pmarb.config import Settings
from pmarb.fees.kalshi import KalshiFeeModel
from pmarb.http import ObservationHttpClient
from pmarb.models import Book, Market, Venue
from pmarb.protocols import BookEvent, FeeModel
from pmarb.venues.kalshi.parse import SERIES_LEAGUES, is_eligible, parse_book, parse_market

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
SERIES_TICKERS = tuple(SERIES_LEAGUES)
PAGE_SIZE = 1000
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

    async def _get_json(
        self,
        path: str,
        *,
        purpose: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        result = await self.http.get(
            f"{BASE_URL}{path}", venue="kalshi", purpose=purpose, params=params
        )
        if not 200 <= result.status_code < 300:
            raise RuntimeError(f"Kalshi GET {path} returned HTTP {result.status_code}")
        return result.json(), dict(result.headers)

    async def _series(self, ticker: str) -> dict[str, Any]:
        cached = self._series_cache.get(ticker)
        if cached is not None:
            return cached
        payload, _ = await self._get_json(f"/series/{quote(ticker, safe='')}", purpose="discovery")
        series = payload.get("series") if isinstance(payload, dict) else None
        if not isinstance(series, dict):
            raise TypeError("Kalshi series response has no series object")
        self._series_cache[ticker] = series
        return series

    async def _event(self, ticker: str) -> dict[str, Any]:
        cached = self._event_cache.get(ticker)
        if cached is not None:
            return cached
        payload, _ = await self._get_json(f"/events/{quote(ticker, safe='')}", purpose="discovery")
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict):
            raise TypeError("Kalshi event response has no event object")
        self._event_cache[ticker] = event
        return event

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
        for series_ticker in self.series_tickers:
            series = await self._series(series_ticker)
            async for raw in self._pages(series_ticker):
                if not is_eligible(raw, series)[0]:
                    continue
                event_ticker = raw.get("event_ticker")
                event = await self._event(event_ticker) if event_ticker else None
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
        payload, _ = await self._get_json(f"/markets/{quote(ticker, safe='')}", purpose="discovery")
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
        tickers = list(dict.fromkeys(market_ids))
        books: dict[str, Book] = {}
        for offset in range(0, len(tickers), BOOK_BATCH_SIZE):
            chunk = tickers[offset : offset + BOOK_BATCH_SIZE]
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
                books[ticker] = book.model_copy(
                    update={"raw_native": {**payload, "cache_headers": dict(headers)}}
                )
        return [books[ticker] for ticker in tickers]

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

        Either one missing leaves the quote unknown. Assessments then stay
        scenario-only (``fee_unknown_kalshi``) instead of assuming a schedule.
        """
        return KalshiFeeModel(
            multiplier=_fee_multiplier(market),
            precision=_member_precision(self._settings),
        )


def _member_precision(settings: Settings | None) -> Decimal | None:
    if settings is None or settings.kalshi_member_precision is None:
        return None
    return Decimal(settings.kalshi_member_precision)


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
