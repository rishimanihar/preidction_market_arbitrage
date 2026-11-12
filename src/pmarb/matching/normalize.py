"""Fail-closed normalization of supported Kalshi and Polymarket US winners."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    policy_version: str = "v2"
    flags: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizationDiagnostic:
    market_key: str
    reason: str


def _diagnose(
    market: Market, diagnostics: list[NormalizationDiagnostic] | None, reason: str
) -> None:
    if diagnostics is not None:
        diagnostics.append(NormalizationDiagnostic(market_key=market.market_key, reason=reason))


def _corroborates(
    table: TeamTable, label: object, expected: str, evidence: list[str], source: str
) -> bool:
    """Check text against a structured identity without guessing from the text."""
    if label is None or label == "":
        return True
    if not isinstance(label, str):
        return False
    if table.league != "ncaawb" and re.search(r"\(\s*w\s*\)|\bwomen'?s\b", label, re.IGNORECASE):
        return False
    try:
        resolved = table.resolve_label(label)
    except AmbiguousTeam as exc:
        if expected not in exc.candidates:
            return False
        evidence.append(f"{source}:ambiguous_label_ignored:{label!r}; structured_team:{expected}")
        return True
    if resolved is None:
        evidence.append(f"{source}:unknown_label_ignored:{label!r}; structured_team:{expected}")
        return True
    if resolved != expected:
        return False
    evidence.append(f"{source}:corroborated:{label!r}->{expected}")
    return True


def _text_dates(text: str) -> set[date]:
    """Collect explicit calendar dates; never derive a date from a timestamp hint."""
    found: set[date] = set()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            values = match.groupdict()
            month = (
                _MONTHS[values["month_name"].lower().rstrip(".")]
                if values.get("month_name")
                else int(values["month"])
            )
            found.add(date(int(values["year"]), month, int(values["day"])))
    return found


def _kalshi_event_participants(market: Market, table: TeamTable) -> set[str]:
    event = market.raw.get("event", {})
    ticker = market.market_id.rsplit("-", 1)[0]
    for event_ticker in (
        event.get("event_ticker") if isinstance(event, dict) else None,
        market.raw.get("event_ticker"),
    ):
        if event_ticker is not None and event_ticker != ticker:
            raise ValueError("conflicting_kalshi_event_tickers")
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
    if len(pairs) != 1:
        raise ValueError("unresolved_or_ambiguous_kalshi_ticker_teams")
    return set(next(iter(pairs)))


def _versus_labels(
    table: TeamTable,
    text: str,
    venue: Venue,
    *,
    structured: set[str] | None = None,
    evidence: list[str] | None = None,
) -> tuple[set[str], bool]:
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
            if single_team != line:
                try:
                    if table.resolve_label(single_team) is not None:
                        continue
                except AmbiguousTeam:
                    if not structured:
                        raise
                    # A single-team title is checked against the ticker YES
                    # identity separately, including contradictory ambiguity.
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
            labels = [label.strip(" ,:?") for label in (left, right)]
            if structured:
                # An internal "at" cannot establish a split from two unknown
                # fragments. Complete labels must establish the grammar first.
                if len(separators) > 1:
                    known = False
                    for label in labels:
                        try:
                            known |= table.resolve_label(label) is not None
                        except AmbiguousTeam:
                            known = True
                    if not known:
                        continue
                first, second = sorted(structured)
                for expected in ((first, second), (second, first)):
                    corroboration: list[str] = []
                    if all(
                        _corroborates(table, label, team, corroboration, "kalshi_matchup")
                        for label, team in zip(labels, expected, strict=True)
                    ):
                        interpretations.append(set(structured))
                        if evidence is not None:
                            evidence.extend(corroboration)
                        break
                continue
            ids = [table.resolve_label(label) for label in labels]
            if None not in ids and ids[0] != ids[1]:
                interpretations.append(set(ids))
        # Multiple valid parses are ambiguous even when one looks more likely.
        if len(interpretations) != 1:
            return set(), True
        found.update(interpretations[0])
    return found, False


def _kalshi_yes(
    market: Market, table: TeamTable, evidence: list[str], structured: set[str] | None
) -> str | None:
    suffix = market.market_id.rsplit("-", 1)[-1]
    subtitle = market.raw.get("yes_sub_title")
    structured_yes = table.resolve_kalshi_code(suffix)
    if structured and structured_yes is None:
        return None
    if structured_yes:
        evidence.append(f"kalshi_ticker_yes:{suffix}->{structured_yes}")
        if not _corroborates(table, subtitle, structured_yes, evidence, "kalshi_yes_subtitle"):
            return None
        sources = [structured_yes]
    else:
        resolved = table.resolve_label(subtitle) if isinstance(subtitle, str) else None
        sources = [resolved] if resolved else []
        if resolved:
            evidence.append(f"kalshi_yes_label_fallback:{subtitle!r}->{resolved}")
    # A single-team winner title supplies independent polarity evidence.
    if not re.search(r"\b(?:vs\.?|versus)\s+", market.title, re.IGNORECASE):
        title = re.sub(
            r"\s+(?:to\s+win|wins?|winner)\s*[?!]*$", "", market.title, flags=re.IGNORECASE
        )
        if title != market.title:
            matchup, _ = _versus_labels(table, market.title, Venue.KALSHI, structured=structured)
            if not matchup:
                if structured_yes:
                    if not _corroborates(
                        table, title, structured_yes, evidence, "kalshi_yes_title"
                    ):
                        return None
                else:
                    resolved = table.resolve_label(title)
                    if resolved is None:
                        return None
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
    if market.market_id.split("-", 1)[0] != series_ticker:
        _diagnose(market, diagnostics, "kalshi_ticker_series_conflict")
        return None
    table = load_league(league)
    evidence: list[str] = []
    ticker_participants = _kalshi_event_participants(market, table)
    if ticker_participants:
        evidence.append(
            f"kalshi_ticker_participants:{market.market_id.rsplit('-', 1)[0]}"
            f"->{','.join(sorted(ticker_participants))}"
        )
    text_participants, invalid_matchup_text = _versus_labels(
        table,
        f"{market.title}\n{market.rules_text}",
        Venue.KALSHI,
        structured=ticker_participants or None,
        evidence=evidence,
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
    yes_participant = _kalshi_yes(market, table, evidence, ticker_participants or None)
    if yes_participant is None or yes_participant not in participants:
        _diagnose(market, diagnostics, "unresolved_or_conflicting_yes_team")
        return None
    if market.market_type not in {"binary", "winner"}:
        _diagnose(market, diagnostics, "unsupported_kalshi_market_type")
        return None

    start = market.event_start_utc
    authoritative_dates = _text_dates(market.title) | _text_dates(market.rules_text)
    if not authoritative_dates:
        _diagnose(market, diagnostics, "missing_authoritative_event_date")
        return None
    if len(authoritative_dates) != 1:
        _diagnose(market, diagnostics, "conflicting_authoritative_event_dates")
        return None
    event_date = next(iter(authoritative_dates))
    date_source = "kalshi_title_or_rules"
    for source, text in (("title", market.title), ("rules", market.rules_text)):
        if _text_dates(text):
            evidence.append(f"kalshi_{source}_event_date_ny:{event_date.isoformat()}")
    flags: tuple[str, ...] = ()
    if start is not None:
        hint_date = start.astimezone(_NY).date()
        evidence.append(
            f"kalshi_occurrence_datetime_hint:{start.isoformat()}; "
            f"hint_date_ny:{hint_date}; authoritative_date_ny:{event_date}"
        )
        if hint_date != event_date:
            flags = ("date_hint_conflict",)
    return NormalizedMarket(
        venue=market.venue,
        market_id=market.market_id,
        league=league,
        participants=frozenset(participants),
        yes_participant=yes_participant,
        event_date_ny=event_date,
        start_utc=start.astimezone(UTC) if start else None,
        date_source=date_source,
        flags=flags,
        evidence=tuple(evidence),
    )


def _poly_team_registry(
    raw: dict[str, object], table: TeamTable
) -> tuple[dict[str, str], str | None]:
    """Map top-level team IDs to canonical teams when the payload supplies a registry."""
    registry: dict[str, str] = {}
    values = raw.get("teams")
    if isinstance(values, dict):
        records = [(key, value) for key, value in values.items()]
    elif isinstance(values, list):
        records = [(None, value) for value in values]
    else:
        records = []
    for key, value in records:
        if isinstance(value, str):
            team_id, codes = key, (value,)
        elif isinstance(value, dict):
            team_id = value.get("id", value.get("teamId", key))
            codes = tuple(value.get(field) for field in ("abbreviation", "code", "slug"))
        else:
            continue
        code_values = [code for code in codes if isinstance(code, str) and code.strip()]
        hits = {table.resolve_polymarket_code(code) for code in code_values}
        if None in hits:
            return {}, "unknown_registry_team_code"
        if team_id is None or not hits:
            continue
        if len(hits) != 1:
            return {}, "conflicting_registry_team_identity"
        team_key = str(team_id)
        canonical = next(iter(hits))
        if team_key in registry and registry[team_key] != canonical:
            return {}, "conflicting_registry_team_id"
        registry[team_key] = canonical
    return registry, None


def _poly_structured_team(
    side: dict[str, object],
    table: TeamTable,
    registry: dict[str, str],
    shared_identifier: object = None,
) -> tuple[str | None, list[str], str | None]:
    """Return canonical team identity, evidence fields, or a malformed-identity reason."""
    identities: set[str] = set()
    evidence: list[str] = []
    nested = side.get("team")
    team = nested if isinstance(nested, dict) else {}
    if isinstance(nested, str) and nested.strip():
        direct = table.resolve_polymarket_code(nested)
        if direct is None:
            direct = registry.get(nested)
        if direct is None:
            return None, evidence, "unknown_structured_team_reference"
        identities.add(direct)
        evidence.append(f"side_team:{nested}->{direct}")
    for field in ("abbreviation",):
        code = team.get(field)
        if code is None or code == "":
            continue
        if not isinstance(code, str):
            return None, evidence, "invalid_structured_team_code"
        hit = table.resolve_polymarket_code(code)
        if hit is None:
            return None, evidence, "unknown_structured_team_code"
        identities.add(hit)
        evidence.append(f"side_{field}:{code}->{hit}")
    team_id, side_team_id = team.get("id"), side.get("teamId")
    if team_id is not None and side_team_id is not None and str(team_id) != str(side_team_id):
        nested_known = registry.get(str(team_id))
        side_known = registry.get(str(side_team_id))
        nested_code = table.resolve_polymarket_code(team_id) if isinstance(team_id, str) else None
        side_code = (
            table.resolve_polymarket_code(side_team_id) if isinstance(side_team_id, str) else None
        )
        if not (nested_known and side_known and nested_known == side_known) and not (
            nested_code and side_code and nested_code == side_code
        ):
            return None, evidence, "conflicting_nested_team_ids"
    for field, value in (
        ("identifier", side.get("identifier")),
        ("teamId", side.get("teamId")),
        ("team.id", team.get("id")),
    ):
        if value is None or value == "":
            continue
        if field == "identifier" and value == shared_identifier:
            evidence.append(f"shared_instrument_identifier_ignored:{value}")
            continue
        text = str(value)
        hit = registry.get(text)
        if hit is None and isinstance(value, str):
            hit = table.resolve_polymarket_code(value)
        if hit is not None:
            identities.add(hit)
            evidence.append(f"side_{field}:{value}->{hit}")
    if len(identities) > 1:
        return None, evidence, "conflicting_structured_side_identity"
    return (next(iter(identities)) if identities else None), evidence, None


def _poly_side_labels(side: dict[str, object]) -> list[object]:
    nested = side.get("team")
    team = nested if isinstance(nested, dict) else {}
    return [
        side.get("description"),
        team.get("name"),
        team.get("safeName"),
        team.get("alias"),
        team.get("abbreviation"),
        team.get("displayAbbreviation"),
    ]


def _poly_fallback_identity(
    table: TeamTable, labels: list[object]
) -> tuple[str | None, str | None]:
    """Use only uniquely resolved labels when the side has no structured identity."""
    hits: set[str] = set()
    for label in labels:
        if not isinstance(label, str) or not label.strip():
            continue
        if table.league != "ncaawb" and re.search(
            r"\(\s*w\s*\)|\bwomen'?s\b", label, re.IGNORECASE
        ):
            return None, "wrong_league_gender_marker"
        try:
            hit = table.resolve_label(label)
        except AmbiguousTeam:
            return None, "ambiguous_fallback_team_label"
        if hit is not None:
            hits.add(hit)
    if len(hits) > 1:
        return None, "conflicting_fallback_team_labels"
    if not hits:
        return None, "unresolved_fallback_team_label"
    return next(iter(hits)), None


def _normalize_polyus(
    market: Market, diagnostics: list[NormalizationDiagnostic] | None
) -> NormalizedMarket | None:
    slug = str(market.raw.get("slug") or market.market_id)
    match = _SLUG.fullmatch(slug)
    if _SLUG.fullmatch(market.market_id) and slug != market.market_id:
        _diagnose(market, diagnostics, "conflicting_polymarket_market_slugs")
        return None
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
    for value in (
        market.raw.get("_eventLeagueSlug"),
        market.raw.get("leagueSlug"),
        market.league,
    ):
        if value and _POLY_LEAGUES.get(str(value).lower(), str(value).lower()) != league:
            _diagnose(market, diagnostics, "conflicting_polymarket_league_evidence")
            return None
    if market.market_type != "winner":
        _diagnose(market, diagnostics, "unsupported_polymarket_market_type")
        return None
    if not match or slug_league != league:
        _diagnose(market, diagnostics, "unrecognized_polymarket_game_slug")
        return None
    table = load_league(league)
    slug_teams: dict[str, str] = {}
    for name in ("a", "b"):
        code = match.group(name)
        hit = table.resolve_polymarket_code(code)
        if hit is None:
            _diagnose(market, diagnostics, f"unresolved_polymarket_slug_code:{name}:{code}")
            return None
        slug_teams[name] = hit
    slug_ids = set(slug_teams.values())
    if len(slug_ids) != 2:
        _diagnose(market, diagnostics, "duplicate_polymarket_slug_team")
        return None
    try:
        slug_date = date.fromisoformat(match.group("date"))
    except ValueError:
        _diagnose(market, diagnostics, "invalid_polymarket_slug_date")
        return None

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
    long_side = next(side for side in sides if side.get("long") is True)
    registry, registry_error = _poly_team_registry(market.raw, table)
    if registry_error:
        _diagnose(market, diagnostics, registry_error)
        return None
    side_ids: dict[int, str] = {}
    side_evidence: list[str] = []
    shared_identifier = (
        sides[0].get("identifier")
        if sides[0].get("identifier") == sides[1].get("identifier")
        else None
    )
    for index, side in enumerate(sides):
        structured, evidence, error = _poly_structured_team(
            side, table, registry, shared_identifier
        )
        if error:
            _diagnose(market, diagnostics, error)
            return None
        labels = _poly_side_labels(side)
        if side is long_side and market.yes_participant:
            labels.append(market.yes_participant)
        if structured is None:
            structured, error = _poly_fallback_identity(table, labels)
            if error:
                _diagnose(market, diagnostics, error)
                return None
            evidence.append(f"side_label_identity:{structured}")
        else:
            for label in labels:
                if not _corroborates(table, label, structured, evidence, f"poly_side_{index}"):
                    _diagnose(market, diagnostics, "conflicting_polymarket_team_evidence")
                    return None
        if structured not in slug_ids:
            _diagnose(market, diagnostics, "polymarket_side_not_in_slug_teams")
            return None
        side_ids[index] = structured
        side_evidence.extend(evidence)
    if set(side_ids.values()) != slug_ids or len(set(side_ids.values())) != 2:
        _diagnose(market, diagnostics, "polymarket_slug_side_mapping_not_bijective")
        return None
    yes_id = side_ids[sides.index(long_side)]
    start = market.event_start_utc
    flags: tuple[str, ...] = ()
    evidence = [
        f"poly_slug_date:{slug_date.isoformat()}",
        *(f"poly_slug_code:{match.group(name)}->{slug_teams[name]}" for name in ("a", "b")),
        f"poly_side_mapping:long={yes_id},other={next(v for v in side_ids.values() if v != yes_id)}",
        *side_evidence,
    ]
    if start is not None and start.astimezone(_NY).date() != slug_date:
        flags = ("date_hint_conflict",)
        evidence.append(f"poly_start_date_hint:{start.astimezone(_NY).date().isoformat()}")
    return NormalizedMarket(
        venue=market.venue,
        market_id=market.market_id,
        league=league,
        participants=frozenset(slug_ids),
        yes_participant=yes_id,
        event_date_ny=slug_date,
        start_utc=start.astimezone(UTC) if start else None,
        date_source="polymarket_slug",
        flags=flags,
        evidence=tuple(evidence),
        policy_version="v2",
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
