"""Integrity checks for venue codes, explicit aliases, and label normalization."""

from collections import defaultdict

import pytest

from pmarb.matching.teams import (
    LEAGUES,
    AmbiguousTeam,
    Team,
    TeamTable,
    _code_key,
    _label_key,
    load_league,
)


@pytest.mark.parametrize("league", LEAGUES)
@pytest.mark.parametrize("field", ("kalshi_code", "polymarket_code"))
def test_venue_codes_are_unique_within_league(league, field):
    seen = {}
    for team in load_league(league).teams:
        code = getattr(team, field)
        if not code:
            continue
        key = _code_key(code)
        assert key not in seen, (
            f"duplicate {field} {code!r}: {seen.get(key)} and {team.canonical_id}"
        )
        seen[key] = team.canonical_id


@pytest.mark.parametrize("league", LEAGUES)
def test_explicit_aliases_are_unique_within_league(league):
    aliases = defaultdict(set)
    for team in load_league(league).teams:
        for alias in team.aliases:
            if alias:
                aliases[_label_key(alias)].add(team.canonical_id)

    collisions = {key: ids for key, ids in aliases.items() if len(ids) > 1}
    assert collisions == {}


@pytest.mark.parametrize("league", LEAGUES)
def test_explicit_aliases_never_resolve_to_another_team(league):
    table = load_league(league)
    for team in table.teams:
        for alias in team.aliases:
            try:
                resolved = table.resolve_label(alias)
            except AmbiguousTeam as exc:
                assert team.canonical_id in exc.candidates, (league, alias, exc.candidates)
            else:
                assert resolved == team.canonical_id, (league, alias, resolved, team.canonical_id)


@pytest.mark.parametrize("label", ("MSU", "KSU"))
def test_ncaamb_code_collisions_remain_ambiguous_after_alias_cleanup(label):
    with pytest.raises(AmbiguousTeam):
        load_league("ncaamb").resolve_label(label)


@pytest.mark.parametrize("league", LEAGUES)
def test_shared_city_or_nickname_labels_remain_ambiguous(league):
    labels = defaultdict(set)
    for team in load_league(league).teams:
        for label in (team.city_or_school, team.nickname):
            if label:
                labels[_label_key(label)].add(team.canonical_id)

    for key, candidates in labels.items():
        if len(candidates) > 1:
            with pytest.raises(AmbiguousTeam):
                load_league(league).resolve_label(key)


@pytest.mark.parametrize(
    ("league", "input_label", "expected"),
    (
        ("ncaaf", "Alabama St.", "ncaaf:ALST"),  # trailing St. means State
        ("ncaaf", "Texas A and M", "ncaaf:TXAM"),  # ampersand means and
        ("nhl", "MONTRÉAL", "nhl:MTL"),  # accents fold to ASCII
        ("nhl", "  st.   LOUIS! ", "nhl:STL"),  # case, spacing, punctuation
        ("nba", "golden-st warriors", "nba:GSW"),
    ),
)
def test_label_normalization_rules(league, input_label, expected):
    assert load_league(league).resolve_label(input_label) == expected


def _team(canonical_id, display_name, city, nickname):
    return Team(
        canonical_id=canonical_id,
        league="test",
        display_name=display_name,
        city_or_school=city,
        nickname=nickname,
        kalshi_code="",
        polymarket_code="",
        aliases=(),
        source="observed",
    )


def test_state_and_saint_abbreviations_are_distinct():
    assert _label_key("Texas St.") == _label_key("Texas State")
    assert _label_key("St. Louis") == _label_key("Saint Louis")
    assert _label_key("St. Louis") != _label_key("State Louis")


def test_saint_abbreviation_collision_remains_ambiguous():
    table = TeamTable(
        "test",
        [
            _team("test:one", "St. Louis Red", "City One", "Reds"),
            _team("test:two", "Saint Louis Red", "City Two", "Blues"),
        ],
    )
    with pytest.raises(AmbiguousTeam):
        table.resolve_label("St. Louis Red")


def test_univ_abbreviation_matches_university():
    table = TeamTable(
        "test",
        [
            _team("test:one", "University of Example", "Example", "Eagles"),
        ],
    )
    assert table.resolve_label("Univ. of Example") == "test:one"
