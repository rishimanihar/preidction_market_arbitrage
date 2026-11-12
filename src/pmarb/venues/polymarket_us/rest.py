"""Public, observation-only Polymarket US REST adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import quote

from pmarb.config import Settings
from pmarb.fees.polymarket_us import PolymarketUSFeeModel
from pmarb.http import ObservationHttpClient
from pmarb.models import Book, Market, Venue
from pmarb.protocols import BookEvent, FeeModel
from pmarb.venues.polymarket_us.parse import is_eligible, parse_book, parse_market

BASE_URL = "https://gateway.polymarket.us"
PAGE_SIZE = 50
# Configured target leagues: NFL/NBA/NHL and NCAA football, men's basketball,
# and women's basketball. The API's league catalog is authoritative at runtime.
DEFAULT_LEAGUES = frozenset({"nfl", "nba", "nhl", "cfb", "cbb", "wcbb"})


class PolymarketUSAdapter:
    """Polymarket US REST reader. Raw book JSON is retained on ``Book.raw_native``.

    ``horizon_start`` is honored as supplied; standard M1 callers pass the
    inclusive now-minus-12-hour boundary to retain recently started events.
    """

    venue = Venue.POLYMARKET_US

    def __init__(
        self,
        http: ObservationHttpClient | None = None,
        *,
        leagues: Sequence[str] | None = None,
        clock_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        connection_generation: int = 0,
        settings: Settings | None = None,
    ) -> None:
        self._owns_http = http is None
        self.http = http or ObservationHttpClient()
        self.leagues = frozenset(DEFAULT_LEAGUES if leagues is None else leagues)
        self._clock_utc = clock_utc
        self._monotonic = monotonic
        self.connection_generation = connection_generation
        self._settings = settings

    async def _get_json(
        self, path: str, *, purpose: str, params: dict[str, Any] | None = None
    ) -> Any:
        result = await self.http.get(
            f"{BASE_URL}{path}", venue="polyus", purpose=purpose, params=params
        )
        if not 200 <= result.status_code < 300:
            raise RuntimeError(f"Polymarket US GET {path} returned HTTP {result.status_code}")
        return result.json()

    async def _pages(self, path: str, collection: str, *, purpose: str) -> AsyncIterator[Any]:
        offset = 0
        while True:
            payload = await self._get_json(
                path, purpose=purpose, params={"limit": PAGE_SIZE, "offset": offset}
            )
            if not isinstance(payload, dict) or collection not in payload:
                raise ValueError(f"Polymarket US response is missing {collection!r}")
            rows = payload[collection]
            if not isinstance(rows, list):
                raise TypeError(f"Polymarket US response field {collection!r} is not a list")
            for row in rows:
                yield row
            offset += len(rows)
            if len(rows) < PAGE_SIZE:
                break

    async def list_markets(
        self, horizon_start: datetime, horizon_end: datetime
    ) -> AsyncIterator[Market]:
        start = _utc(horizon_start)
        end = _utc(horizon_end)
        if end < start:
            raise ValueError("horizon_end must not precede horizon_start")

        discovered = {
            str(row.get("slug"))
            async for row in self._pages("/v2/leagues", "leagues", purpose="discovery")
            if row.get("slug")
        }
        for league in sorted(self.leagues & discovered):
            path = f"/v2/leagues/{quote(league, safe='')}/events"
            async for event in self._pages(path, "events", purpose="discovery"):
                if "markets" not in event:
                    raise ValueError("Polymarket US event response is missing 'markets'")
                markets = event["markets"]
                if not isinstance(markets, list):
                    raise TypeError("Polymarket US event field 'markets' is not a list")
                for raw in markets:
                    if not isinstance(raw, dict) or not is_eligible(raw)[0]:
                        continue
                    contextual = dict(raw)
                    contextual["_eventStartTime"] = event.get("startTime")
                    contextual["_eventLeagueSlug"] = league
                    market = parse_market(contextual)
                    market_start = market.event_start_utc
                    if market_start is not None and start <= market_start.astimezone(UTC) <= end:
                        yield market

    async def get_market(self, slug: str) -> Market:
        payload = await self._get_json(
            f"/v1/market/slug/{quote(slug, safe='')}", purpose="discovery"
        )
        raw = payload.get("market")
        if not isinstance(raw, dict):
            raise TypeError("Polymarket US market detail response has no market object")
        return parse_market(raw)

    async def get_books(self, market_ids: Sequence[str]) -> list[Book]:
        async def fetch(slug: str) -> Book:
            payload = await self._get_json(
                f"/v1/markets/{quote(slug, safe='')}/book", purpose="books"
            )
            received_utc = _utc(self._clock_utc())
            received_monotonic = self._monotonic()
            book = parse_book(
                payload,
                received_utc=received_utc,
                received_monotonic=received_monotonic,
                connection_generation=self.connection_generation,
            )
            if book.market_id != slug:
                raise ValueError(
                    f"Polymarket US book identity mismatch: requested {slug!r}, received {book.market_id!r}"
                )
            return book

        return list(await asyncio.gather(*(fetch(slug) for slug in market_ids)))

    def book_stream(self, market_ids: Sequence[str]) -> AsyncIterator[BookEvent] | None:
        """Streaming is a later milestone; the M1 adapter polls REST."""
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client when this adapter created it."""
        if self._owns_http:
            await self.http.aclose()

    def fee_model(self, market: Market) -> FeeModel:
        """Account Θ from settings. Unset Θ leaves the quote unknown.

        ``market`` is unused: Polymarket US fees follow the account tier, not
        the market's ``feeCoefficient``. Unknown Θ is scenario-only
        (``fee_unknown_polymarket_us``).
        """
        del market
        theta = None if self._settings is None else self._settings.polyus_theta
        return PolymarketUSFeeModel(theta=theta)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("horizon datetimes must be timezone-aware")
    return value.astimezone(UTC)
