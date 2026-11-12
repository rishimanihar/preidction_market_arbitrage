"""Pure parsers for Kalshi market metadata and fixed-point books."""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pmarb.models import Book, Level, Market, Venue

SERIES_LEAGUES = {
    "KXNFLGAME": "nfl",
    "KXNBAGAME": "nba",
    "KXNHLGAME": "nhl",
    "KXNCAAFGAME": "ncaaf",
    "KXNCAAMBGAME": "ncaamb",
    "KXNCAAWBGAME": "ncaawb",
}


def _time(value: Any) -> datetime | None:
    """Parse an RFC3339 timestamp while preserving its represented instant."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise TypeError("wire timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("wire timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _wire_decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise TypeError("book prices and sizes must be wire strings")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("book values must be finite")
    return result


def parse_market(
    raw: dict[str, Any],
    series_raw: dict[str, Any],
    event_raw: dict[str, Any] | None = None,
) -> Market:
    """Convert one Kalshi market plus its series/event metadata to a Market."""
    ticker = str(raw.get("ticker") or "")
    series_ticker = str(series_raw.get("ticker") or raw.get("series_ticker") or "")
    expected_expiration = raw.get("expected_expiration_time")
    occurrence = (event_raw or {}).get("occurrence_datetime")
    if occurrence is None:
        occurrence = raw.get("occurrence_datetime")

    fee_meta: dict[str, str] = {}
    for key in ("fee_type", "fee_multiplier", "contract_terms_url"):
        value = series_raw.get(key)
        if value is not None:
            fee_meta[key] = str(value)
    settlement_sources = series_raw.get("settlement_sources")
    if settlement_sources is not None:
        # Preserve the structured source names/URLs in a string-valued field.
        fee_meta["settlement_sources"] = json.dumps(
            settlement_sources, sort_keys=True, separators=(",", ":")
        )

    rules = str(raw.get("rules_primary") or "") + str(raw.get("rules_secondary") or "")
    native = dict(raw)
    native["series"] = dict(series_raw)
    if event_raw is not None:
        native["event"] = dict(event_raw)
    if expected_expiration is not None:
        native["expected_expiration_time_hint"] = expected_expiration

    return Market(
        venue=Venue.KALSHI,
        market_id=ticker,
        title=str(raw.get("title") or raw.get("yes_sub_title") or ticker),
        league=SERIES_LEAGUES.get(series_ticker),
        participants=(),
        yes_participant=None,
        market_type=str(raw.get("market_type") or "unknown"),
        line=None,
        event_start_utc=_time(occurrence),
        expected_resolution_utc=_time(expected_expiration),
        expected_resolution_src=(
            "kalshi_expected_expiration_time" if expected_expiration else None
        ),
        expected_payout_utc=None,
        status=str(raw.get("status") or "unknown"),
        rules_text=rules,
        rules_hash="",
        fee_meta=fee_meta,
        url=f"https://kalshi.com/markets/{ticker}" if ticker else None,
        raw=native,
    )


def is_eligible(raw_market: dict[str, Any], series_raw: dict[str, Any]) -> tuple[bool, list[str]]:
    """Apply the narrow binary $1 game-market gate used by discovery."""
    reasons: list[str] = []
    if raw_market.get("market_type") != "binary":
        reasons.append("not_binary")
    try:
        notional = raw_market.get("notional_value_dollars")
        if not isinstance(notional, str) or Decimal(notional) != Decimal(1):
            reasons.append("not_one_dollar_notional")
    except (InvalidOperation, TypeError, ValueError):
        reasons.append("not_one_dollar_notional")
    if raw_market.get("status") != "active":
        reasons.append("market_not_active")
    if "settlement_bounds_type" not in raw_market:
        reasons.append("settlement_bounds_unknown")
    elif raw_market.get("settlement_bounds_type") != "default":
        reasons.append("settlement_bounds_not_default")
    if raw_market.get("mve_collection_ticker"):
        reasons.append("mve_market")
    series_ticker = str(series_raw.get("ticker") or raw_market.get("series_ticker") or "")
    if series_ticker not in SERIES_LEAGUES:
        reasons.append("unsupported_series")
    return not reasons, reasons


def _levels(rows: Any) -> list[Level]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise TypeError("book ladder must be an array")
    result: list[Level] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("book level must contain price and size")
        price, size = _wire_decimal(row[0]), _wire_decimal(row[1])
        result.append(Level(price=price, size=size))
    return result


def _is_cached(cache_headers: dict[str, Any] | None) -> bool:
    headers = {str(key).lower(): str(value) for key, value in (cache_headers or {}).items()}
    try:
        if Decimal(headers.get("x-kalshi-cache-hits", "0")) >= 1:
            return True
    except (InvalidOperation, TypeError, ValueError):
        pass
    for directive in headers.get("cache-control", "").split(","):
        name, separator, value = directive.strip().partition("=")
        if name.strip().lower() == "max-age" and separator:
            try:
                if Decimal(value.strip().strip('"')) > 0:
                    return True
            except (InvalidOperation, TypeError, ValueError):
                continue
    return False


def parse_book(
    orderbook_fp: dict[str, Any],
    *,
    ticker: str,
    received_utc: datetime,
    received_monotonic: float,
    connection_generation: int,
    cache_headers: dict[str, Any] | None,
) -> Book:
    """Parse bid-only fixed-point ladders and fail closed on bad snapshots."""
    yes: list[Level] = []
    no: list[Level] = []
    reason: str | None = None
    try:
        if not isinstance(orderbook_fp, dict):
            raise TypeError("orderbook_fp must be an object")
        yes_bids = _levels(orderbook_fp.get("yes_dollars", []))
        no_bids = _levels(orderbook_fp.get("no_dollars", []))
        yes = sorted(
            (Level(price=Decimal(1) - level.price, size=level.size) for level in no_bids),
            key=lambda level: level.price,
        )
        no = sorted(
            (Level(price=Decimal(1) - level.price, size=level.size) for level in yes_bids),
            key=lambda level: level.price,
        )
        if (
            yes_bids
            and no_bids
            and max(x.price for x in yes_bids) + max(x.price for x in no_bids) > Decimal(1)
        ):
            reason = "crossed_book"
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        yes, no = [], []
        reason = f"invalid_book:{type(exc).__name__}"

    if _is_cached(cache_headers):
        reason = "cached_response"
    return Book(
        venue=Venue.KALSHI,
        market_id=ticker,
        yes_asks=yes,
        no_asks=no,
        raw_native={
            "orderbook_fp": orderbook_fp,
            "cache_headers": dict(cache_headers or {}),
        },
        source_ts=None,
        received_utc=received_utc,
        received_monotonic=received_monotonic,
        connection_generation=connection_generation,
        valid=reason is None,
        invalid_reason=reason,
    )
