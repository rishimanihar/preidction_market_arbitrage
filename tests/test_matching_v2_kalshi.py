"""Policy-v2 checks for Kalshi team and event-date evidence."""

from pmarb.matching.candidates import generate_candidates
from pmarb.matching.normalize import normalize_market
from pmarb.matching.teams import load_league
from tests.test_matching import kalshi, poly


def test_unknown_matchup_label_is_ignored_when_ticker_identifies_both_teams():
    market = kalshi(
        "ALA",
        "Alabama",
        league="ncaaf",
        event_code="30JAN06ALAAKR",
        opponent_code="AKR",
        opponent_label="Akron",
        title_override="Alabama vs Mystery School winner",
    )

    normalized = normalize_market(market)

    assert normalized is not None
    assert normalized.yes_participant == "ncaaf:ALA"
    assert normalized.participants == {"ncaaf:ALA", "ncaaf:AKR"}


def test_ambiguous_label_disjoint_from_ticker_candidates_is_rejected():
    market = kalshi(
        "ALA",
        "Wildcats",
        league="ncaaf",
        event_code="30JAN06ALAAKR",
        opponent_code="AKR",
        opponent_label="Akron",
        title_override="Alabama vs Akron winner",
    )
    diagnostics = []

    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics


def test_authoritative_rules_date_mismatch_with_poly_slug_rejects_candidate():
    market = kalshi("NYG", "New York Giants", rules="Game is scheduled for January 6, 2030.")
    other_date = poly(
        market_id="aec-nfl-nyg-nyj-2030-01-07",
        start="2030-01-07T18:00:00Z",
    )

    assert generate_candidates([market, other_date]) == []


def test_occurrence_date_conflict_is_flagged_as_hint_with_authoritative_evidence():
    market = kalshi(
        "NYG",
        "New York Giants",
        start="2030-01-07T12:00:00Z",
        rules="Game is scheduled for January 6, 2030.",
    )

    normalized = normalize_market(market)

    assert normalized is not None
    assert normalized.event_date_ny.isoformat() == "2030-01-06"
    assert "date_hint_conflict" in normalized.flags
    assert any("2030-01-06" in item and "rules" in item for item in normalized.evidence)
    assert any("2030-01-07" in item for item in normalized.evidence)


def test_title_and_rules_with_different_dates_reject_as_conflicting_authority():
    market = kalshi(
        "NYG",
        "New York Giants",
        rules="Game is scheduled for January 6, 2030.",
        title_override="New York Giants vs New York Jets winner on January 7, 2030",
    )
    diagnostics = []

    assert normalize_market(market, diagnostics=diagnostics) is None
    assert any(item.reason == "conflicting_authoritative_event_dates" for item in diagnostics)


def test_start_and_expiration_hint_without_rules_or_title_date_reject():
    market = kalshi(
        "NYG",
        "New York Giants",
        rules="",
        expiration_hint="2030-01-06T03:00:00Z",
    )
    diagnostics = []

    assert normalize_market(market, diagnostics=diagnostics) is None
    assert any(item.reason == "missing_authoritative_event_date" for item in diagnostics)


def test_ncaamb_venue_codes_resolve_only_in_their_own_code_namespace():
    table = load_league("ncaamb")

    assert table.resolve_kalshi_code("CHAR") == "ncaamb:CHAR"
    assert table.resolve_polymarket_code("charlt") == "ncaamb:CHAR"
    assert table.resolve_polymarket_code("CHAR") == "ncaamb:COFC"

    assert table.resolve_kalshi_code("MSU") == "ncaamb:MSU"
    assert table.resolve_polymarket_code("mst") == "ncaamb:MSU"
    assert table.resolve_polymarket_code("MSU") == "ncaamb:MSU2"

    assert table.resolve_kalshi_code("CSU") == "ncaamb:CSU"
    assert table.resolve_polymarket_code("colst") == "ncaamb:CSU"
    assert table.resolve_polymarket_code("CSU") == "ncaamb:CSB"


def test_kalshi_market_ticker_series_must_match_selected_series():
    market = kalshi("NYG", "New York Giants")
    market.raw["series"]["ticker"] = "KXNHLGAME"
    market = market.model_copy(update={"league": "nhl"})
    diagnostics = []

    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "kalshi_ticker_series_conflict"
