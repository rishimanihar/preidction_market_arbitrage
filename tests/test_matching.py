"""Synthetic tests for winner-market normalization and candidate generation."""

import json
import os
from datetime import datetime
from functools import cache
from pathlib import Path

import pytest

from pmarb.matching.candidates import generate_candidates
from pmarb.matching.exceptions import default_exception_table
from pmarb.matching.normalize import normalize_market
from pmarb.models import GroupStatus, Market, Side, Venue
from pmarb.venues.kalshi.parse import SERIES_LEAGUES
from pmarb.venues.kalshi.parse import parse_market as parse_kalshi_market
from pmarb.venues.polymarket_us.parse import parse_market as parse_poly_market

REPO_ROOT = Path(__file__).resolve().parents[1]
_DATA_ROOT = Path(os.environ.get("PMARB_DATA_DIR", "data"))
MATCHING_DATA = (_DATA_ROOT if _DATA_ROOT.is_absolute() else REPO_ROOT / _DATA_ROOT) / (
    "research/matching"
)
RAW_DATA = MATCHING_DATA / "raw"


def kalshi(
    team_code,
    yes_label,
    *,
    opponent_code="NYJ",
    opponent_label="New York Jets",
    league="nfl",
    event_code="30JAN06NYGNYJ",
    start="2030-01-06T18:00:00Z",
    rules=None,
    title_override=None,
    expiration_hint=None,
):
    series = {
        "nfl": "KXNFLGAME",
        "nhl": "KXNHLGAME",
        "ncaaf": "KXNCAAFGAME",
        "ncaamb": "KXNCAAMBGAME",
        "ncaawb": "KXNCAAWBGAME",
    }[league]
    if rules is None:
        rules = "The game is scheduled for January 6, 2030."
    ticker = f"{series}-{event_code}-{team_code}"
    title = title_override or f"{yes_label} vs {opponent_label} winner"
    return Market(
        venue=Venue.KALSHI,
        market_id=ticker,
        title=title,
        league=league,
        market_type="winner",
        status="active",
        rules_text=rules,
        rules_hash="synthetic",
        event_start_utc=(datetime.fromisoformat(start) if start is not None else None),
        raw={
            "ticker": ticker,
            "series": {"ticker": series},
            "event": {"occurrence_datetime": start},
            "yes_sub_title": yes_label,
            "title": title,
            "expected_expiration_time_hint": expiration_hint,
        },
    )


def poly(
    market_id="aec-nfl-nyg-nyj-2030-01-06",
    yes_label="New York Giants",
    *,
    opponent_label="New York Jets",
    yes_code="nyg",
    opponent_code="nyj",
    league="nfl",
    start="2030-01-06T18:00:00Z",
    rules="",
):
    return Market(
        venue=Venue.POLYMARKET_US,
        market_id=market_id,
        title=f"{yes_label} to win",
        league=league,
        market_type="winner",
        status="MARKET_STATUS_OPEN",
        rules_text=rules,
        rules_hash="synthetic",
        event_start_utc=datetime.fromisoformat(start) if start else None,
        raw={
            "slug": market_id,
            "_eventLeagueSlug": league,
            "_eventStartTime": start,
            "participants": [yes_label, opponent_label],
            "marketSides": [
                {
                    "description": yes_label,
                    "long": True,
                    "team": {"abbreviation": yes_code},
                },
                {
                    "description": opponent_label,
                    "long": False,
                    "team": {"abbreviation": opponent_code},
                },
            ],
        },
    )


def test_one_outcome_uses_matching_kalshi_yes_only():
    giants = kalshi("NYG", "New York Giants")
    jets = kalshi("NYJ", "New York Jets", opponent_code="NYG", opponent_label="New York Giants")
    polymarket = poly()

    candidates = generate_candidates([giants, jets, polymarket])

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.group.status is GroupStatus.CANDIDATE
    assert {member.venue for member in candidate.group.members} == {
        Venue.KALSHI,
        Venue.POLYMARKET_US,
    }
    assert {
        (member.venue, member.market_id, member.native_side_for_outcome)
        for member in candidate.group.members
    } == {
        (Venue.KALSHI, giants.market_id, Side.YES),
        (Venue.POLYMARKET_US, polymarket.market_id, Side.YES),
    }
    assert "nfl:NYG" in candidate.group.outcome


def test_new_york_giants_and_jets_normalize_as_distinct_teams():
    giants = normalize_market(kalshi("NYG", "New York G"))
    jets = normalize_market(
        kalshi("NYJ", "New York J", opponent_code="NYG", opponent_label="New York G")
    )
    assert giants and jets
    assert giants.yes_participant == "nfl:NYG"
    assert jets.yes_participant == "nfl:NYJ"
    assert giants.participants == jets.participants


def test_nhl_jets_resolve_to_winnipeg():
    market = kalshi(
        "WPG",
        "Winnipeg Jets",
        league="nhl",
        event_code="30JAN06WPGTOR",
        opponent_code="TOR",
        opponent_label="Toronto Maple Leafs",
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "nhl:WPG"


@pytest.mark.parametrize(
    ("league", "slug_league"),
    [("ncaaf", "cfb"), ("ncaamb", "cbb"), ("ncaawb", "wcbb")],
)
def test_polymarket_college_league_slug_aliases(league, slug_league):
    market = poly(
        market_id=f"aec-{slug_league}-ala-akron-2030-01-06",
        yes_label="Alabama",
        opponent_label="Akron",
        yes_code="ala",
        opponent_code="akron",
        league=league,
    )
    diagnostics = []
    normalized = normalize_market(market, diagnostics=diagnostics)
    assert normalized is not None, diagnostics
    assert normalized.league == league
    assert normalized.yes_participant == f"{league}:ALA"


def test_womens_marker_does_not_match_mens_league():
    market = kalshi(
        "ALA",
        "Alabama (W)",
        league="ncaaf",
        event_code="30JAN06ALAAKR",
        opponent_code="AKR",
        opponent_label="Akron",
    )
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics

    womens_market = kalshi(
        "ALA",
        "Alabama (W)",
        league="ncaawb",
        event_code="30JAN06ALAAKR",
        opponent_code="AKR",
        opponent_label="Akron (W)",
    )
    normalized = normalize_market(womens_market)
    assert normalized is not None
    assert normalized.yes_participant == "ncaawb:ALA"


def test_unknown_yes_label_can_be_ignored_when_ticker_supplies_identity():
    market = kalshi(
        "NYG",
        "Mystery Team",
        title_override="New York Giants vs New York Jets winner",
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "nfl:NYG"
    assert any("unknown_label_ignored" in item for item in normalized.evidence)


def test_kalshi_title_date_is_authoritative_and_recorded():
    from_title = kalshi(
        "NYG",
        "New York Giants",
        start=None,
        title_override="New York Giants vs New York Jets on January 6, 2030",
    )
    title_result = normalize_market(from_title)
    assert title_result is not None
    assert title_result.date_source == "kalshi_title_or_rules"
    assert title_result.event_date_ny.isoformat() == "2030-01-06"


def test_kalshi_start_time_and_expiration_hint_do_not_replace_authoritative_date():
    no_date = kalshi(
        "NYG",
        "New York Giants",
        rules="",
        expiration_hint="2030-01-07T03:00:00Z",
    )
    diagnostics = []
    assert normalize_market(no_date, diagnostics=diagnostics) is None
    assert diagnostics
    assert any("missing_authoritative_event_date" in item.reason for item in diagnostics)


def test_ambiguous_team_label_is_rejected_without_structured_identity():
    market = kalshi(
        "WILD",
        "Wildcats",
        league="ncaaf",
        event_code="30JAN06WILDUNKNOWN",
        rules="The game is scheduled for January 6, 2030.",
    )
    diagnostics = []
    assert generate_candidates([market, poly()], diagnostics=diagnostics) == []
    assert diagnostics
    assert any(item.market_key == market.market_key for item in diagnostics)


def test_ambiguous_label_is_resolved_by_structured_ticker_candidates():
    market = kalshi(
        "ARIZ",
        "Wildcats",
        league="ncaaf",
        event_code="30JAN06ARIZKSU",
        opponent_code="KSU",
        opponent_label="Kansas State",
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "ncaaf:ARIZ"
    assert normalized.participants == {"ncaaf:ARIZ", "ncaaf:KSU"}
    assert any("ambiguous_label_ignored" in item for item in normalized.evidence)


def test_same_day_doubleheader_requires_manual_date_check():
    game_one_k = kalshi("NYG", "New York Giants", start="2030-01-06T15:00:00Z")
    game_one_p = poly(start="2030-01-06T22:30:00Z")
    game_two_k = kalshi(
        "NYG",
        "New York Giants",
        opponent_code="BUF",
        opponent_label="Buffalo Bills",
        event_code="30JAN06NYGBUF",
        start="2030-01-06T22:30:00Z",
    )
    game_two_p = poly(
        market_id="aec-nfl-nyg-buf-2030-01-06",
        opponent_label="Buffalo Bills",
        opponent_code="buf",
        start="2030-01-06T22:30:00Z",
    )
    candidates = generate_candidates([game_one_k, game_one_p, game_two_k, game_two_p])
    assert len(candidates) == 2
    assert sum(candidate.needs_manual_date_check for candidate in candidates) == 1


def test_normal_kalshi_pair_does_not_trigger_doubleheader():
    k_yes = kalshi("NYG", "New York Giants", start="2030-01-06T18:00:00Z")
    p = poly(start="2030-01-06T18:00:00Z")
    k_no = kalshi("NYJ", "New York Jets", start="2030-01-06T21:59:00Z")
    candidates = generate_candidates([k_yes, p, k_no])
    assert len(candidates) == 1
    assert not candidates[0].needs_manual_date_check


def test_duplicate_input_rows_collapse_to_one_candidate():
    k = kalshi("NYG", "New York Giants")
    p = poly()
    assert len(generate_candidates([k, p, k, p])) == 1


def test_different_new_york_dates_do_not_join():
    k = kalshi("NYG", "New York Giants", start="2030-01-06T18:00:00Z")
    p = poly(
        market_id="aec-nfl-nyg-nyj-2030-01-07",
        start="2030-01-07T18:00:00Z",
    )
    assert normalize_market(k) is not None
    assert normalize_market(p) is not None
    assert generate_candidates([k, p]) == []


def test_group_id_is_deterministic_and_groups_validate():
    markets = [kalshi("NYG", "New York Giants"), poly()]
    one = generate_candidates(markets)[0]
    two = generate_candidates(list(reversed(markets)))[0]
    assert one.group.group_id == two.group.group_id
    for candidate in (one, two):
        assert len({member.venue for member in candidate.group.members}) == len(
            candidate.group.members
        )


def test_exception_rows_are_proposed_from_rules_text():
    rows = default_exception_table(
        kalshi("NYG", "New York Giants", rules="Overtime counts; cancellations settle fairly."),
        poly(rules="A tie settles at $0.50. Postponed games are rescheduled within 48 hours."),
    )
    cases = {row.outcome_case.lower() for row in rows}
    assert any("overtime" in case for case in cases)
    assert any("tie" in case for case in cases)
    assert all(row.payouts for row in rows)


def test_missing_matching_kalshi_yes_has_diagnostic():
    p = poly()
    diagnostics = []
    assert generate_candidates([p], diagnostics=diagnostics) == []
    assert diagnostics
    assert any(
        p.market_key in item.market_key or p.market_key == item.market_key for item in diagnostics
    )


@cache
def _local_markets():
    """Parse captured payloads and the field-preserving event-window projection.

    The broad market pages use endDate and miss many game-window contracts.
    The overlap study also saved those contracts as a reduced projection;
    restore its recorded field names without inferring any missing values.
    """
    kalshi_markets = {}
    for filename in ("kalshi_primary_7d.json", "kalshi_close_time_7d.json"):
        payload = json.loads((RAW_DATA / filename).read_text(encoding="utf-8"))
        for raw in payload.get("markets", []):
            event_ticker = str(raw.get("event_ticker", ""))
            series = event_ticker.split("-", 1)[0]
            if series not in SERIES_LEAGUES:
                continue
            parsed = parse_kalshi_market(
                raw,
                {"ticker": series},
                {"occurrence_datetime": raw.get("occurrence_datetime")},
            )
            kalshi_markets[parsed.market_key] = parsed

    poly_markets = {}
    for path in sorted(RAW_DATA.glob("poly_markets_enddate_7d_page_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for raw in payload.get("markets", []):
            parsed = parse_poly_market(raw)
            if parsed.market_id.startswith("aec-"):
                poly_markets[parsed.market_key] = parsed
    window = RAW_DATA / "poly_league_events_window.json"
    if window.is_file():
        for event in json.loads(window.read_text(encoding="utf-8"))["events"]:
            parsed = parse_poly_market(
                {
                    "slug": event.get("market_slug"),
                    "question": event.get("question"),
                    "sportsMarketType": event.get("sports_market_type"),
                    "description": event.get("description"),
                    "marketSides": event.get("sides"),
                    "_eventLeagueSlug": event.get("league"),
                    "_eventStartTime": event.get("start"),
                }
            )
            poly_markets[parsed.market_key] = parsed
    return list(kalshi_markets.values()), list(poly_markets.values())


@pytest.mark.localdata
@pytest.mark.skipif(
    not all(
        (RAW_DATA / filename).is_file()
        for filename in ("kalshi_primary_7d.json", "kalshi_close_time_7d.json")
    )
    or not any(RAW_DATA.glob("poly_markets_enddate_7d_page_*.json")),
    reason="local matching market dumps not present",
)
def test_localdata_matching_candidate_coverage():
    kalshi_markets, poly_markets = _local_markets()
    diagnostics = []
    candidates = generate_candidates([*kalshi_markets, *poly_markets], diagnostics=diagnostics)
    counts = {league: 0 for league in SERIES_LEAGUES.values()}
    for candidate in candidates:
        counts[candidate.match_keys[0]] += 1
    unresolved = [(item.market_key, item.reason) for item in diagnostics]
    print(f"\nLocal matching candidate counts per league: {counts}")
    print(f"Local unresolved market examples: {unresolved[:10]}")
    assert candidates
