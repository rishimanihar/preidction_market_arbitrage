"""Synthetic tests for structured Polymarket v2 normalization."""

from datetime import datetime

import pytest

from pmarb.matching.normalize import normalize_market
from pmarb.models import Market, Venue


def _poly(
    sides,
    *,
    slug="aec-nfl-nyg-nyj-2030-01-06",
    league="nfl",
    start="2030-01-07T18:00:00Z",
):
    return Market(
        venue=Venue.POLYMARKET_US,
        market_id=slug,
        title="synthetic winner market",
        league=league,
        market_type="winner",
        status="MARKET_STATUS_OPEN",
        rules_text="",
        rules_hash="synthetic",
        event_start_utc=datetime.fromisoformat(start) if start else None,
        yes_participant=next((s["description"] for s in sides if s.get("long") is True), None),
        raw={
            "slug": slug,
            "_eventLeagueSlug": league,
            "marketSides": sides,
        },
    )


def _side(long, description, *, abbreviation=None, team_id=None, identifier=None, name=None):
    team = {}
    if abbreviation is not None:
        team["abbreviation"] = abbreviation
    if team_id is not None:
        team["id"] = team_id
    if name is not None:
        team["name"] = name
    side = {"long": long, "description": description}
    if identifier is not None:
        side["identifier"] = identifier
    if team:
        side["team"] = team
    return side


def test_structured_abbreviations_map_sides_and_allow_ambiguous_text():
    market = _poly(
        [
            _side(False, "New York", abbreviation="nyj", identifier="shared-instrument"),
            _side(True, "New York", abbreviation="nyg", identifier="shared-instrument"),
        ]
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.participants == frozenset({"nfl:NYG", "nfl:NYJ"})
    assert normalized.yes_participant == "nfl:NYG"
    assert "poly_side_mapping:long=nfl:NYG,other=nfl:NYJ" in normalized.evidence


def test_numeric_team_ids_resolve_through_top_level_team_registry():
    market = _poly(
        [
            _side(True, "New York Giants", team_id=101),
            _side(False, "New York Jets", team_id=202),
        ]
    )
    market.raw["teams"] = [
        {"id": 101, "abbreviation": "nyg"},
        {"id": 202, "abbreviation": "nyj"},
    ]
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "nfl:NYG"


def test_direct_team_code_is_structured_identity():
    sides = [
        {"long": True, "description": "New York Giants", "team": "nyg"},
        {"long": False, "description": "New York Jets", "team": "nyj"},
    ]
    normalized = normalize_market(_poly(sides))
    assert normalized is not None
    assert normalized.yes_participant == "nfl:NYG"


@pytest.mark.parametrize("field", ("identifier", "teamId"))
def test_direct_side_code_fields_use_strict_code_namespace(field):
    market = _poly(
        [
            {"long": True, "description": "New York Giants", field: "nyg"},
            {"long": False, "description": "New York Jets", field: "nyj"},
        ]
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "nfl:NYG"


def test_registry_uses_canonical_abbreviation_and_not_display_abbreviation():
    market = _poly(
        [
            _side(True, "Golden State Warriors", team_id=11),
            _side(False, "Los Angeles Lakers", team_id=22),
        ],
        slug="aec-nba-gs-lal-2030-01-06",
        league="nba",
    )
    market.raw["teams"] = [
        {"id": 11, "abbreviation": "gs", "displayAbbreviation": "GSW"},
        {"id": 22, "abbreviation": "lal", "displayAbbreviation": "LAL"},
    ]
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "nba:GSW"


def test_conflicting_nested_team_ids_are_rejected():
    market = _poly(
        [
            _side(True, "New York Giants", abbreviation="nyg", team_id=101),
            _side(False, "New York Jets", abbreviation="nyj", team_id=202),
        ]
    )
    market.raw["marketSides"][0]["teamId"] = 303
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "conflicting_nested_team_ids"


def test_conflicting_duplicate_registry_ids_are_rejected():
    market = _poly(
        [
            _side(True, "New York Giants", team_id=101),
            _side(False, "New York Jets", team_id=202),
        ]
    )
    market.raw["teams"] = [
        {"id": 101, "abbreviation": "nyg"},
        {"id": 101, "abbreviation": "nyj"},
    ]
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "conflicting_registry_team_id"


def test_without_structured_identity_uses_unique_labels_not_side_order():
    market = _poly(
        [
            _side(False, "New York Jets"),
            _side(True, "New York Giants"),
        ]
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "nfl:NYG"


def test_ambiguous_fallback_label_is_rejected():
    market = _poly([_side(True, "New York"), _side(False, "New York")])
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "ambiguous_fallback_team_label"


def test_wrong_league_gender_marker_is_rejected_in_label_fallback():
    market = _poly([_side(True, "New York Giants (W)"), _side(False, "New York Jets")])
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "wrong_league_gender_marker"


def test_structured_identity_rejects_conflicting_label():
    market = _poly(
        [
            _side(True, "New York Giants", abbreviation="nyj"),
            _side(False, "New York Jets", abbreviation="nyg"),
        ]
    )
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "conflicting_polymarket_team_evidence"


def test_slug_date_is_authoritative_and_records_conflicting_hint():
    market = _poly(
        [
            _side(True, "New York Giants", abbreviation="nyg"),
            _side(False, "New York Jets", abbreviation="nyj"),
        ],
        start="2030-01-08T18:00:00Z",
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.event_date_ny.isoformat() == "2030-01-06"
    assert normalized.flags == ("date_hint_conflict",)
    assert "poly_slug_date:2030-01-06" in normalized.evidence
    assert "poly_start_date_hint:2030-01-08" in normalized.evidence


def test_slug_team_codes_use_strict_code_namespace():
    market = _poly(
        [
            _side(True, "New York Giants", abbreviation="nyg"),
            _side(False, "New York Jets", abbreviation="nyj"),
        ],
        slug="aec-nfl-giants-jets-2030-01-06",
    )
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason.startswith("unresolved_polymarket_slug_code:")


def test_conflicting_raw_slug_and_market_id_reject():
    market = _poly(
        [
            _side(True, "New York Giants", abbreviation="nyg"),
            _side(False, "New York Jets", abbreviation="nyj"),
        ]
    ).model_copy(update={"market_id": "aec-nfl-nyg-nyj-2030-01-07"})
    diagnostics = []

    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "conflicting_polymarket_market_slugs"


def test_conflicting_market_and_raw_league_reject():
    market = _poly(
        [
            _side(True, "New York Giants", abbreviation="nyg"),
            _side(False, "New York Jets", abbreviation="nyj"),
        ]
    ).model_copy(update={"league": "nba"})
    diagnostics = []

    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics[0].reason == "conflicting_polymarket_league_evidence"


def test_shared_instrument_identifier_that_looks_like_team_code_is_not_team_identity():
    market = _poly(
        [
            _side(True, "New York Giants", abbreviation="nyg", identifier="nyg"),
            _side(False, "New York Jets", abbreviation="nyj", identifier="nyg"),
        ]
    )

    normalized = normalize_market(market)

    assert normalized is not None
    assert normalized.yes_participant == "nfl:NYG"
    assert any("shared_instrument_identifier_ignored:nyg" in item for item in normalized.evidence)
