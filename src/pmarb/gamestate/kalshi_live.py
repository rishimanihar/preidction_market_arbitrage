"""Read-only adapter for Kalshi's unauthenticated live game data API."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pmarb.http import ObservationHttpClient

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
BATCH_SIZE = 100
type RawLogCallback = Callable[
    [bytes, dict[str, Any], datetime, float, str, int, dict[str, str]], Any
]


@dataclass(frozen=True, slots=True)
class GameState:
    source: str
    event_ticker: str | None
    milestone_id: str | None
    league: str
    status: str | None
    period: str | int | None
    clock: str | None
    home_points: int | None
    away_points: int | None
    possession: str | None
    last_play: str | None
    result_pending: bool | None
    delayed_flag: bool | None
    received_utc: datetime
    received_mono: float
    raw: dict[str, Any]


def _obj(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _pick(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        if value.lower() in {"true", "yes", "1"}:
            return True
        if value.lower() in {"false", "no", "0"}:
            return False
    return None


def parse_live(
    raw: Mapping[str, Any],
    league: str,
    *,
    event_ticker: str | None = None,
    milestone_id: str | None = None,
    received_utc: datetime | None = None,
    received_mono: float | None = None,
) -> GameState:
    """Parse one ``live_datas`` row or live_data object without trusting optional fields."""
    outer = _obj(raw)
    live = _obj(outer.get("live_data")) or outer
    details = _obj(live.get("details"))
    # Keep the venue payload byte-for-byte representable; receive times live in
    # dedicated fields and the raw-log envelope, rather than being mixed in here.
    raw_copy = dict(outer)
    game_clock = _pick(details, "period_remaining_time", "clock")
    return GameState(
        source="kalshi_live_data",
        event_ticker=event_ticker,
        milestone_id=milestone_id or _as_str(live.get("milestone_id")),
        league=league,
        status=_as_str(details.get("status")),
        period=_period(_pick(details, "period", "quarter")),
        clock=_as_str(game_clock),
        home_points=_int_or_none(details.get("home_points")),
        away_points=_int_or_none(details.get("away_points")),
        possession=_as_str(details.get("possession")),
        last_play=_as_str(details.get("last_play")),
        result_pending=_bool_or_none(details.get("result_pending")),
        delayed_flag=_bool_or_none(details.get("live_data_delayed")),
        received_utc=received_utc or datetime.now(UTC),
        received_mono=received_mono if received_mono is not None else time.monotonic(),
        raw=raw_copy,
    )


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _period(value: Any) -> str | int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return value


class KalshiLiveClient:
    """Resolve watched events and fetch live snapshots through the observation HTTP client."""

    def __init__(
        self,
        http: ObservationHttpClient | None = None,
        *,
        clock_utc: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        raw_log: RawLogCallback | None = None,
        leagues: Mapping[str, str] | None = None,
    ) -> None:
        self._owns_http = http is None
        self.http = http or ObservationHttpClient()
        self._clock_utc = clock_utc
        self._monotonic = monotonic
        self.raw_log = raw_log
        self.leagues = dict(leagues or {})
        self._event_to_milestone: dict[str, str] = {}
        self._milestone_to_event: dict[str, str] = {}

    async def _get(
        self, path: str, *, params: dict[str, Any], endpoint: str
    ) -> tuple[Any, datetime, float]:
        result = await self.http.get(
            f"{BASE_URL}{path}", venue="kalshi", purpose="gamestate", params=params
        )
        received_utc, received_mono = self._clock_utc(), self._monotonic()
        if self.raw_log is not None:
            try:
                payload = result.json() if 200 <= result.status_code < 300 else {}
            except (ValueError, TypeError):
                payload = {}
            logged = self.raw_log(
                result.content,
                payload if isinstance(payload, dict) else {},
                received_utc,
                received_mono,
                endpoint,
                result.status_code,
                {
                    key: value
                    for key, value in result.headers.items()
                    if key.casefold()
                    in {"cache-control", "age", "date", "etag", "last-modified", "expires"}
                },
            )
            if inspect.isawaitable(logged):
                await logged
        if not 200 <= result.status_code < 300:
            raise RuntimeError(f"Kalshi GET {path} returned HTTP {result.status_code}")
        return result.json(), received_utc, received_mono

    async def resolve_milestones(self, event_tickers: Sequence[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for ticker in dict.fromkeys(event_tickers):
            cached = self._event_to_milestone.get(ticker)
            if cached is None:
                cursor = None
                seen_cursors: set[str] = set()
                while True:
                    params: dict[str, Any] = {"related_event_ticker": ticker, "limit": 500}
                    if cursor is not None:
                        params["cursor"] = cursor
                    payload, _, _ = await self._get(
                        "/milestones", params=params, endpoint="/milestones"
                    )
                    rows = payload.get("milestones") if isinstance(payload, dict) else None
                    if not isinstance(rows, list):
                        raise TypeError("Kalshi milestones response has no milestones list")
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                            continue
                        related = row.get("related_event_tickers", [])
                        primary = row.get("primary_event_tickers", [])
                        associated = {v for v in (*_strings(related), *_strings(primary))}
                        if ticker in associated:
                            cached = row["id"]
                            self._event_to_milestone[ticker] = cached
                            self._milestone_to_event.setdefault(cached, ticker)
                            break
                    if cached is not None:
                        break
                    next_cursor = payload.get("cursor") if isinstance(payload, dict) else None
                    if not next_cursor:
                        break
                    if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                        raise ValueError("Kalshi milestones returned an invalid or repeated cursor")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
            if cached is not None:
                result[ticker] = cached
        return result

    async def fetch_live(self, milestone_ids: Sequence[str]) -> list[GameState]:
        ids = list(dict.fromkeys(milestone_ids))
        states: dict[str, GameState] = {}
        for offset in range(0, len(ids), BATCH_SIZE):
            chunk = ids[offset : offset + BATCH_SIZE]
            endpoint = "/live_data/batch"
            payload, received_utc, received_mono = await self._get(
                endpoint, params={"milestone_ids": chunk}, endpoint=endpoint
            )
            rows = payload.get("live_datas") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise TypeError("Kalshi live-data batch response has no live_datas list")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mid = _as_str(row.get("milestone_id"))
                if mid is None or mid not in chunk:
                    continue
                if mid in states:
                    continue
                event_ticker = self._milestone_to_event.get(mid)
                league = self.leagues.get(event_ticker or "", _league(row))
                states[mid] = parse_live(
                    row,
                    league,
                    event_ticker=event_ticker,
                    milestone_id=mid,
                    received_utc=received_utc,
                    received_mono=received_mono,
                )
        return [states[mid] for mid in ids if mid in states]

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _league(row: Mapping[str, Any]) -> str:
    live = _obj(row.get("live_data")) or row
    details = _obj(live.get("details"))
    names = (
        row.get("league"),
        row.get("competition"),
        details.get("league"),
        details.get("competition"),
    )
    for name in names:
        if not isinstance(name, str):
            continue
        normalized = name.casefold()
        if "wnba" in normalized:
            return "WNBA"
        if "college football" in normalized or "ncaa football" in normalized:
            return "NCAAF"
        if "college basketball" in normalized or "ncaa basketball" in normalized:
            return "NCAAB"
        if "pro basketball" in normalized or normalized == "nba":
            return "NBA"
        if "pro football" in normalized or normalized == "nfl":
            return "NFL"
        if "hockey" in normalized:
            return "NHL"
        if "baseball" in normalized:
            return "MLB"
    return "UNKNOWN"
