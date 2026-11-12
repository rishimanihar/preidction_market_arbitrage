"""Pure parsing helpers for Polymarket US market and book payloads."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pmarb.models import Book, Level, Market, Venue


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("wire timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _market_type(raw: dict[str, Any]) -> tuple[str, dict[str, str]]:
    v2 = raw.get("sportsMarketTypeV2") == "SPORTS_MARKET_TYPE_MONEYLINE"
    legacy = str(raw.get("sportsMarketType", "")).endswith("_full_game_winner")
    meta: dict[str, str] = {}
    if "sportsMarketTypeV2" in raw and "sportsMarketType" in raw and v2 != legacy:
        meta["market_type_disagreement"] = (
            f"v2_winner={str(v2).lower()},legacy_winner={str(legacy).lower()}"
        )
    return (
        "winner"
        if v2 or legacy
        else str(raw.get("sportsMarketTypeV2") or raw.get("sportsMarketType") or "unknown"),
        meta,
    )


def parse_market(raw: dict[str, Any]) -> Market:
    """Convert a wire market or event-nested market into the domain contract."""
    market = raw.get("market", raw)
    event = raw.get("event", {}) if isinstance(raw.get("event", {}), dict) else {}
    sides = market.get("marketSides", [])
    yes_side = next((side for side in sides if side.get("long") is True), None)
    market_type, fee_meta = _market_type(market)
    if market.get("orderPriceMinTickSize") is not None:
        fee_meta["order_price_min_tick_size"] = str(Decimal(str(market["orderPriceMinTickSize"])))
    start = market.get("_eventStartTime") or event.get("startTime") or market.get("gameStartTime")
    title = (
        market.get("title")
        or market.get("question")
        or market.get("slug")
        or str(market.get("id", ""))
    )
    return Market(
        venue=Venue.POLYMARKET_US,
        market_id=str(market.get("slug") or market.get("id") or ""),
        title=str(title),
        league=market.get("_eventLeagueSlug")
        or market.get("leagueSlug")
        or event.get("leagueSlug"),
        participants=tuple(str(x) for x in market.get("participants", [])),
        yes_participant=(
            str(yes_side.get("description")) if yes_side and yes_side.get("description") else None
        ),
        market_type=market_type,
        line=Decimal(str(market["line"])) if market.get("line") is not None else None,
        event_start_utc=_time(start),
        expected_resolution_utc=None,
        expected_resolution_src=None,
        expected_payout_utc=None,
        status=str(market.get("status", "MARKET_STATUS_UNSPECIFIED")),
        rules_text=str(market.get("description") or ""),
        rules_hash="",
        fee_meta=fee_meta,
        url=f"https://polymarket.us/market/{market['slug']}" if market.get("slug") else None,
        raw=market,
    )


def is_eligible(raw_market: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return whether a raw market is an open, single-instrument winner."""
    raw = raw_market.get("market", raw_market)
    reasons: list[str] = []
    if raw.get("status") != "MARKET_STATUS_OPEN":
        reasons.append("market_not_open")
    if raw.get("closed") is not False:
        reasons.append("market_closed")
    # ``comboEnabled`` only means the market may be used as a leg of a
    # user-built combo. Combos are separate instruments with their own symbols,
    # and "a leg settles inside a combo exactly as it would settle on its own"
    # (https://docs.polymarket.us/faqs/combos-faqs.md). Every live game-winner
    # market observed on 2026-11-12 had ``comboEnabled: true``, so it must not
    # disqualify. Combo instruments themselves fail the single-instrument check.
    sides = raw.get("marketSides")
    sides_valid = (
        isinstance(sides, list)
        and len(sides) == 2
        and all(isinstance(side, dict) for side in sides)
    )
    identifiers = {side.get("identifier") for side in sides} if sides_valid else set()
    if (
        not sides_valid
        or any(side.get("marketSideType") != "MARKET_SIDE_TYPE_INSTRUMENT" for side in sides)
        or len(identifiers) != 1
        or not next(iter(identifiers), None)
        or sum(side.get("long") is True for side in sides) != 1
        or sum(side.get("long") is False for side in sides) != 1
    ):
        reasons.append("not_single_instrument")
    kind, _ = _market_type(raw)
    if kind != "winner":
        reasons.append("not_winner_market")
    return not reasons, reasons


def _levels(rows: Any) -> list[Level]:
    if not isinstance(rows, list):
        raise TypeError("book ladder must be an array")
    levels = []
    for row in rows:
        px, qty = row["px"]["value"], row["qty"]
        if not isinstance(px, str) or not isinstance(qty, str):
            raise TypeError("book prices and quantities must be wire strings")
        levels.append(Level(price=Decimal(px), size=Decimal(qty)))
    return levels


def parse_book(
    raw: dict[str, Any],
    received_utc: datetime,
    received_monotonic: float,
    connection_generation: int,
) -> Book:
    """Parse a complete snapshot; malformed or crossed books fail closed."""
    data = raw.get("marketData", raw)
    reason = None
    if not isinstance(data, dict):
        data = {}
        reason = "invalid_book:TypeError"
    yes: list[Level] = []
    no: list[Level] = []
    source_ts = None
    try:
        if reason is not None:
            raise TypeError("marketData must be an object")
        offers = _levels(data["offers"])
        bids = _levels(data["bids"])
        yes = sorted(offers, key=lambda level: level.price)
        no = sorted(
            (Level(price=Decimal(1) - level.price, size=level.size) for level in bids),
            key=lambda level: level.price,
        )
        if (
            offers
            and bids
            and max(level.price for level in bids) >= min(level.price for level in offers)
        ):
            reason = "crossed_book"
        source_ts = _time(data.get("transactTime"))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        yes, no = [], []
        reason = f"invalid_book:{type(exc).__name__}"
        source_ts = None
    state_open = data.get("state") == "MARKET_STATE_OPEN"
    if not state_open and reason is None:
        reason = f"book_state_{data.get('state', 'missing')}"
    return Book(
        venue=Venue.POLYMARKET_US,
        market_id=str(data.get("marketSlug") or data.get("marketId") or ""),
        yes_asks=yes,
        no_asks=no,
        raw_native=raw,
        source_ts=source_ts,
        received_utc=received_utc,
        received_monotonic=received_monotonic,
        connection_generation=connection_generation,
        valid=state_open and reason is None,
        invalid_reason=reason,
    )
