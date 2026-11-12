"""Reject contradictory metadata before proposing a cross-venue outcome."""

import pytest

from pmarb.matching.candidates import generate_candidates
from pmarb.matching.exceptions import default_exception_table
from pmarb.matching.normalize import _versus_labels, normalize_market
from pmarb.matching.teams import Team, TeamTable
from pmarb.models import Venue
from tests.test_matching import kalshi, poly


@pytest.mark.parametrize("label", ["New York", "Unknown Club", "New York Jets"])
def test_kalshi_explicit_bad_yes_label_is_not_overridden_by_ticker(label):
    market = kalshi("NYG", label)
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics


@pytest.mark.parametrize("opponent", ["Unknown Club", "New York Jets Unknown Club"])
def test_unknown_matchup_label_is_not_overridden_by_ticker(opponent):
    market = kalshi(
        "NYG", "New York Giants", title_override=f"New York Giants vs {opponent} Winner?"
    )
    diagnostics = []
    assert normalize_market(market, diagnostics=diagnostics) is None
    assert diagnostics


def test_missing_kalshi_long_team_contract_has_diagnostic():
    market = kalshi("NYJ", "New York Jets", opponent_code="NYG", opponent_label="New York Giants")
    diagnostics = []
    assert generate_candidates([market, poly()], diagnostics=diagnostics) == []
    assert any("YES" in item.reason for item in diagnostics)


@pytest.mark.parametrize("sides", [[], [{"description": "New York Giants", "long": True}]])
def test_incomplete_polymarket_polarity_is_rejected(sides):
    market = poly()
    market = market.model_copy(update={"raw": {**market.raw, "marketSides": sides}})
    assert normalize_market(market) is None


def test_exception_proposal_preserves_window_wording_and_independent_payouts():
    k = kalshi(
        "NYG",
        "New York Giants",
        rules="A tie pays $0.50. Postponed games have 48 hours. Cancellation uses fair price.",
    )
    p = poly(rules="Postponed games have two days. Overtime is excluded.")
    rows = {row.outcome_case: row for row in default_exception_table(k, p)}
    assert rows["postponement_window"].payouts == {
        k.market_key: "48 hours",
        p.market_key: "two days",
    }
    assert rows["tie"].payouts[k.market_key] == "0.50"
    assert not any(row.preserves_unit_payout for row in rows.values())


def test_negated_tie_payout_does_not_claim_unit_payout():
    k = kalshi("NYG", "New York Giants", rules="A tie does not pay $0.50.")
    p = poly(rules="A tie pays $0.50.")
    assert not any(row.preserves_unit_payout for row in default_exception_table(k, p))


def test_doubleheader_without_start_time_requires_review():
    first = kalshi("NYG", "New York Giants")
    second = kalshi(
        "NYG",
        "New York Giants",
        opponent_code="BUF",
        opponent_label="Buffalo Bills",
        event_code="30JAN06NYGBUF",
    )
    market = poly(start=None)
    proposals = generate_candidates([first, second, market])
    assert len(proposals) == 1
    assert proposals[0].needs_manual_date_check
    assert proposals[0].start_time_mismatch_minutes is None


def test_different_utc_dates_can_share_the_new_york_game_date():
    k = kalshi("NYG", "New York Giants", start="2030-01-06T23:00:00Z")
    p = poly(start="2030-01-07T02:00:00Z")
    assert len(generate_candidates([k, p])) == 1


@pytest.mark.parametrize("league", ["ncaaf", "ncaamb", "ncaawb"])
@pytest.mark.parametrize("separator", ["vs", "at"])
@pytest.mark.parametrize("matchup_title", [False, True])
def test_at_inside_school_name_is_not_a_matchup_separator(league, separator, matchup_title):
    market = kalshi(
        "ALBY",
        "University at Albany",
        opponent_code="HC",
        opponent_label="Holy Cross",
        league=league,
        event_code="30JAN06ALBYHC",
        title_override=(
            f"University at Albany {separator} Holy Cross winner"
            if matchup_title
            else "University at Albany wins"
        ),
        rules=(
            f"If University at Albany wins the University at Albany {separator} Holy Cross game "
            "originally scheduled for Jan 6, 2030, then the market resolves to Yes."
        ),
    )
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == f"{league}:ALBY"
    assert normalized.participants == {f"{league}:ALBY", f"{league}:HC"}


def test_school_name_with_at_does_not_hide_conflicting_title_polarity():
    market = kalshi(
        "HC",
        "Holy Cross",
        opponent_code="ALBY",
        opponent_label="University at Albany",
        league="ncaamb",
        event_code="30JAN06ALBYHC",
        title_override="University at Albany wins",
    )
    assert normalize_market(market) is None


def test_multiple_valid_at_matchup_interpretations_are_rejected():
    table = TeamTable(
        "test",
        [
            Team(
                canonical_id=f"test:{index}",
                league="test",
                display_name=label,
                city_or_school=label,
                nickname="",
                kalshi_code="",
                polymarket_code="",
                aliases=(),
                source="observed",
            )
            for index, label in enumerate(("A", "B at C", "A at B", "C"))
        ],
    )
    assert _versus_labels(table, "A at B at C winner", Venue.KALSHI) == (set(), True)


def test_at_inside_school_name_does_not_hide_unknown_opponent():
    market = kalshi(
        "ALBY",
        "University at Albany",
        league="ncaamb",
        event_code="30JAN06ALBYHC",
        title_override="University at Albany wins",
        rules="If University at Albany wins the University at Albany vs Unknown School game.",
    )
    assert normalize_market(market) is None


def test_matching_explicit_half_dollar_ties_preserve_unit_payout():
    k = kalshi("NYG", "New York Giants", rules="A tie pays $0.50.")
    p = poly(rules="A tie settles at $0.50.")
    rows = default_exception_table(k, p)
    assert rows[0].outcome_case == "tie"
    assert rows[0].preserves_unit_payout
