"""Fail-closed normalization of supported Kalshi and Polymarket US winners."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pmarb.matching.teams import AmbiguousTeam, TeamTable, load_league
from pmarb.models import Market, Venue
from pmarb.venues.kalshi.parse import SERIES_LEAGUES

_NY = ZoneInfo("America/New_York")
_SLUG = re.compile(
    r"^aec-(?P<league>[a-z0-9]+)-(?P<a>[a-z0-9]+)-(?P<b>[a-z0-9]+)-(?P<date>\d{4}-\d{2}-\d{2})$",
    re.IGNORECASE,
)
_DATE_PATTERNS = (
    re.compile(r"\b(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b"),
    re.compile(r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>20\d{2})\b"),
    re.compile(
        r"\b(?P<month_name>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+(?P<day>\d{1,2}),?\s+(?P<year>20\d{2})\b",
        re.IGNORECASE,
    ),
)
_MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        (
            ("Jan", "January"),
            ("Feb", "February"),
            ("Mar", "March"),
            ("Apr", "April"),
            ("May",),
            ("Jun", "June"),
            ("Jul", "July"),
            ("Aug", "August"),
            ("Sep", "September"),
            ("Oct", "October"),
            ("Nov", "November"),
            ("Dec", "December"),
        ),
        start=1,
    )
    for name in names
}
_POLY_LEAGUES = {
    "nfl": "nfl",
    "nba": "nba",
    "nhl": "nhl",
    "cfb": "ncaaf",
    "cbb": "ncaamb",
    "wcbb": "ncaawb",
}
_KALSHI_SERIES_PREFIXES = {ticker: league for ticker, league in SERIES_LEAGUES.items()}


@dataclass(frozen=True)
class NormalizedMarket:
    venue: Venue
    market_id: str
    league: str
    participants: frozenset[str]
    yes_participant: str
    event_date_ny: date
    start_utc: datetime | None
    market_type: str = "winner"
    date_source: str = ""


@dataclass(frozen=True)
class NormalizationDiagnostic:
    market_key: str
    reason: str


def _diagnose(
    market: Market, diagnostics: list[NormalizationDiagnostic] | None, reason: str
) -> None:
    if diagnostics is not None:
        diagnostics.append(NormalizationDiagnostic(market_key=market.market_key, reason=reason))


def _resolve(table: TeamTable, value: object, venue: Venue) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if venue == Venue.KALSHI:
        return table.resolve_kalshi(value)
    return table.resolve_polymarket(value)


def _text_date(text: str) -> date | None:
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        values = match.groupdict()
        try:
            month = (
                _MONTHS[values["month_name"].lower().rstrip(".")]
                if values.get("month_name")
                else int(values["month"])
            )
            return date(int(values["year"]), month, int(values["day"]))
        except (KeyError, ValueError):
            continue
    return None


def _kalshi_event_participants(market: Market, table: TeamTable) -> set[str]:
    event = market.raw.get("event", {})
    ticker = (event.get("event_ticker") if isinstance(event, dict) else None) or market.raw.get(
        "event_ticker"
    )
    if not isinstance(ticker, str):
        ticker = market.market_id.rsplit("-", 1)[0]
    tail = ticker.rsplit("-", 1)[-1]
    match = re.match(r"^\d{2}[A-Z]{3}\d{2}(?P<codes>[A-Z]+)$", tail, re.IGNORECASE)
    if not match:
        return set()
    codes = match.group("codes").upper()
    known = [
        (team.kalshi_code.upper(), team.canonical_id) for team in table.teams if team.kalshi_code
    ]
    pairs: set[frozenset[str]] = set()
    for left_code, left_id in known:
        if codes.startswith(left_code):
            right_code = codes[len(left_code) :]
            for code, team_id in known:
                if code == right_code and team_id != left_id:
                    pairs.add(frozenset((left_id, team_id)))
    return set(next(iter(pairs))) if len(pairs) == 1 else set()


def _versus_labels(table: TeamTable, text: str, venue: Venue) -> tuple[set[str], bool]:
    """Resolve complete matchup labels after removing known venue prose."""
    found: set[str] = set()
    for line in text.splitlines():
        # An explicit versus separator takes precedence over an "at" inside
        # a school name (for example, University at Albany).
        separator = re.search(r"\b(?:vs\.?|versus)\s+", line, re.IGNORECASE)
        separators = [separator] if separator is not None else []
        if separator is None:
            single_team = re.sub(
                r"\s+(?:to\s+win|wins?|winner)\s*[?!]*$", "", line, flags=re.IGNORECASE
            )
            if single_team != line and _resolve(table, single_team, venue) is not None:
                continue
            separators = list(re.finditer(r"\s+at\s+", line, re.IGNORECASE))
        if not separators:
            continue
        interpretations: list[set[str]] = []
        for separator in separators:
            left, right = line[: separator.start()], line[separator.end() :]
            left = re.split(r"\bwins?\s+(?:in\s+)?the\s+", left, flags=re.IGNORECASE)[-1]
            right = re.split(
                r"\s+(?:winner\b|game\b|originally\b|on\s+|"
                r"(?:men's|women's|mens|womens)\s+college\b|college\s+"
                r"(?:football|basketball)\b|pro\s+(?:football|basketball)\b|NHL\b)",
                right,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            ids = [_resolve(table, label.strip(" ,:?"), venue) for label in (left, right)]
            if None not in ids and ids[0] != ids[1]:
                interpretations.append(set(ids))
        # Multiple valid parses are ambiguous even when one looks more likely.
        if len(interpretations) != 1:
            return set(), True
        found.update(interpretations[0])
    return found, False


def _kalshi_yes(market: Market, table: TeamTable) -> str | None:
    suffix = market.market_id.rsplit("-", 1)[-1]
    subtitle = market.raw.get("yes_sub_title")
    sources = []
    for label in (suffix, subtitle):
        if label:
            resolved = _resolve(table, label, Venue.KALSHI)
            if resolved is None:
                return None
            sources.append(resolved)
    # A single-team winner title supplies independent polarity evidence.
    if not re.search(r"\b(?:vs\.?|versus)\s+", market.title, re.IGNORECASE):
        title = re.sub(
            r"\s+(?:to\s+win|wins?|winner)\s*[?!]*$", "", market.title, flags=re.IGNORECASE
        )
        if title != market.title:
            resolved = _resolve(table, title, Venue.KALSHI)
            if resolved is None and not re.search(r"\s+at\s+", title, re.IGNORECASE):
                return None
            if resolved is not None:
                sources.append(resolved)
    return sources[0] if len(set(sources)) == 1 else None


def _normalize_kalshi(
    market: Market, diagnostics: list[NormalizationDiagnostic] | None
) -> NormalizedMarket | None:
    series = market.raw.get("series", {})
    series_ticker = (series.get("ticker") if isinstance(series, dict) else None) or market.raw.get(
        "series_ticker"
    )
    if not series_ticker:
        event_ticker = market.raw.get("event_ticker")
        if not isinstance(event_ticker, str):
            event = market.raw.get("event", {})
            event_ticker = event.get("event_ticker") if isinstance(event, dict) else None
        if isinstance(event_ticker, str):
            series_ticker = event_ticker.split("-", 1)[0]
        if not series_ticker:
            series_ticker = market.market_id.split("-", 1)[0]
    league = SERIES_LEAGUES.get(str(series_ticker))
    if league not in SERIES_LEAGUES.values():
        _diagnose(market, diagnostics, "unsupported_or_missing_kalshi_series")
        return None
    if market.league is not None and market.league != league:
        _diagnose(market, diagnostics, "kalshi_series_league_conflict")
        return None
    table = load_league(league)
    ticker_participants = _kalshi_event_participants(market, table)
    text_participants, invalid_matchup_text = _versus_labels(
        table, f"{market.title}\n{market.rules_text}", Venue.KALSHI
    )
    if invalid_matchup_text:
        _diagnose(market, diagnostics, "unresolved_or_ambiguous_matchup_label")
        return None
    if (
        ticker_participants
        and text_participants
        and (
            not text_participants.issubset(ticker_participants)
            or (len(text_participants) == 2 and ticker_participants != text_participants)
        )
    ):
        _diagnose(market, diagnostics, "conflicting_participant_evidence")
        return None
    participants = ticker_participants or text_participants
    if len(participants) != 2:
        _diagnose(market, diagnostics, "unresolved_or_ambiguous_participants")
        return None
    yes_participant = _kalshi_yes(market, table)
    if yes_participant is None or yes_participant not in participants:
        _diagnose(market, diagnostics, "unresolved_or_conflicting_yes_team")
        return None
    if market.market_type not in {"binary", "winner"}:
        _diagnose(market, diagnostics, "unsupported_kalshi_market_type")
        return None

    start = market.event_start_utc
    text_date = _text_date(f"{market.title}\n{market.rules_text}")
    if start is not None:
        event_date = start.astimezone(_NY).date()
        date_source = "kalshi_event_start_ny"
        if text_date is not None and text_date != event_date:
            _diagnose(market, diagnostics, "event_date_conflicts_with_rules")
            return None
    elif text_date is not None:
        event_date, date_source = text_date, "kalshi_title_or_rules"
    else:
        hint = market.raw.get("expected_expiration_time_hint") or market.raw.get(
            "expected_expiration_time"
        )
        if not isinstance(hint, str):
            _diagnose(market, diagnostics, "missing_event_date")
            return None
        try:
            parsed = datetime.fromisoformat(hint)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone missing")
            event_date = (parsed.astimezone(UTC) - timedelta(hours=3)).astimezone(_NY).date()
        except ValueError:
            _diagnose(market, diagnostics, "invalid_expected_expiration_hint")
            return None
        date_source = "kalshi_expected_expiration_minus_3h"
    return NormalizedMarket(
        venue=market.venue,
        market_id=market.market_id,
        league=league,
        participants=frozenset(participants),
        yes_participant=yes_participant,
        event_date_ny=event_date,
        start_utc=start.astimezone(UTC) if start else None,
        date_source=date_source,
    )


def _normalize_polyus(
    market: Market, diagnostics: list[NormalizationDiagnostic] | None
) -> NormalizedMarket | None:
    slug = str(market.raw.get("slug") or market.market_id)
    match = _SLUG.fullmatch(slug)
    league_raw = (
        market.raw.get("_eventLeagueSlug")
        or market.raw.get("leagueSlug")
        or market.league
        or (match.group("league") if match else None)
    )
    league_slug = str(league_raw).lower() if league_raw else ""
    league = _POLY_LEAGUES.get(league_slug, league_slug)
    slug_league = _POLY_LEAGUES.get(match.group("league").lower()) if match else None
    if league not in SERIES_LEAGUES.values():
        _diagnose(market, diagnostics, "unsupported_or_missing_polymarket_league")
        return None
    if market.market_type != "winner":
        _diagnose(market, diagnostics, "unsupported_polymarket_market_type")
        return None
    if not match or slug_league != league:
        _diagnose(market, diagnostics, "unrecognized_polymarket_game_slug")
        return None
    table = load_league(league)
    sides = market.raw.get("marketSides", [])
    if (
        not isinstance(sides, list)
        or len(sides) != 2
        or any(not isinstance(side, dict) for side in sides)
        or sum(side.get("long") is True for side in sides) != 1
        or sum(side.get("long") is False for side in sides) != 1
    ):
        _diagnose(market, diagnostics, "invalid_polymarket_side_polarity")
        return None
    long_sides = [side for side in sides if isinstance(side, dict) and side.get("long") is True]
    if len(long_sides) != 1:
        _diagnose(market, diagnostics, "missing_or_ambiguous_polymarket_long_side")
        return None
    long_side = long_sides[0]
    nested_team = long_side.get("team")
    nested_team = nested_team if isinstance(nested_team, dict) else {}
    yes_sources = [
        market.yes_participant,
        long_side.get("description"),
        nested_team.get("abbreviation"),
        nested_team.get("name"),
    ]
    yes_ids: set[str] = set()
    for source in yes_sources:
        if source is not None:
            hit = _resolve(table, source, Venue.POLYMARKET_US)
            if hit is None:
                _diagnose(market, diagnostics, "unresolved_or_ambiguous_yes_team_label")
                return None
            yes_ids.add(hit)
    if len(yes_ids) != 1:
        _diagnose(market, diagnostics, "missing_or_conflicting_yes_team")
        return None
    yes_id = next(iter(yes_ids))
    slug_ids: set[str] = set()
    for name in ("a", "b"):
        hit = _resolve(table, match.group(name), Venue.POLYMARKET_US)
        if hit is None:
            _diagnose(market, diagnostics, "unresolved_or_ambiguous_slug_team")
            return None
        slug_ids.add(hit)
    if len(slug_ids) != 2 or yes_id not in slug_ids:
        _diagnose(market, diagnostics, "conflicting_polymarket_team_evidence")
        return None
    for side in sides:
        if not isinstance(side, dict) or side is long_side:
            continue
        if side.get("long") is not False:
            _diagnose(market, diagnostics, "invalid_polymarket_side_polarity")
            return None
        nested = side.get("team")
        nested = nested if isinstance(nested, dict) else {}
        labels = [side.get("description"), nested.get("abbreviation"), nested.get("name")]
        for label in labels:
            if label is None:
                continue
            hit = _resolve(table, label, Venue.POLYMARKET_US)
            if hit is None:
                _diagnose(market, diagnostics, "unresolved_or_ambiguous_opponent_label")
                return None
            if hit not in slug_ids or hit == yes_id:
                _diagnose(market, diagnostics, "conflicting_polymarket_opponent_label")
                return None
    try:
        slug_date = date.fromisoformat(match.group("date"))
    except ValueError:
        _diagnose(market, diagnostics, "invalid_polymarket_slug_date")
        return None
    start = market.event_start_utc
    if start is not None and start.astimezone(_NY).date() != slug_date:
        _diagnose(market, diagnostics, "slug_date_conflicts_with_start_time")
        return None
    return NormalizedMarket(
        venue=market.venue,
        market_id=market.market_id,
        league=league,
        participants=frozenset(slug_ids),
        yes_participant=yes_id,
        event_date_ny=slug_date,
        start_utc=start.astimezone(UTC) if start else None,
        date_source="polymarket_slug" if start is None else "polymarket_slug_checked_start",
    )


def normalize_market(
    market: Market,
    *,
    diagnostics: list[NormalizationDiagnostic] | None = None,
) -> NormalizedMarket | None:
    """Normalize one supported winner market and optionally collect rejection reasons."""
    try:
        if market.venue == Venue.KALSHI:
            return _normalize_kalshi(market, diagnostics)
        if market.venue == Venue.POLYMARKET_US:
            return _normalize_polyus(market, diagnostics)
        _diagnose(market, diagnostics, "unsupported_venue")
    except AmbiguousTeam as exc:
        _diagnose(market, diagnostics, f"ambiguous_team_label:{exc.text}")
    except (KeyError, TypeError, ValueError) as exc:
        _diagnose(market, diagnostics, f"invalid_market_metadata:{type(exc).__name__}")
    return None
