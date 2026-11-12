"""Candidate audit evidence and review gates for policy v2."""

from pmarb.matching.candidates import generate_candidates
from pmarb.matching.normalize import normalize_market
from pmarb.models import GroupStatus, Side
from tests.test_matching import kalshi, poly


def test_v2_candidate_keeps_hint_conflict_and_authoritative_evidence_for_review():
    market = kalshi("NYG", "New York Giants", start="2030-01-07T12:00:00Z")
    candidate = generate_candidates([market, poly()])[0]

    assert candidate.policy_version == "v2"
    assert candidate.flags == ("date_hint_conflict",)
    assert "policy_version:v2" in candidate.evidence
    assert any("poly_slug" in item for item in candidate.evidence)
    assert any("kalshi_ticker_yes:NYG->nfl:NYG" in item for item in candidate.evidence)
    assert any("kalshi_rules_event_date_ny:2030-01-06" in item for item in candidate.evidence)
    assert any(
        "hint_date_ny:2030-01-07" in item and "authoritative_date_ny:2030-01-06" in item
        for item in candidate.evidence
    )
    assert candidate.group.status is GroupStatus.CANDIDATE
    assert candidate.group.reviewer is None
    assert candidate.group.decided_utc is None
    assert len(candidate.group.members) == 2
    assert all(member.native_side_for_outcome is Side.YES for member in candidate.group.members)
    assert all(not member.approved_rules_hash for member in candidate.group.members)


def test_v2_does_not_pair_opposite_yes_contract_with_ambiguous_label():
    opposite = kalshi("NYJ", "New York", opponent_code="NYG", opponent_label="New York")
    assert generate_candidates([opposite, poly()]) == []


def test_kalshi_without_structured_identity_can_use_unique_labels():
    market = kalshi("LEGACY", "New York Giants", event_code="LEGACY")
    normalized = normalize_market(market)
    assert normalized is not None
    assert normalized.yes_participant == "nfl:NYG"
    assert any("kalshi_yes_label_fallback" in item for item in normalized.evidence)


def test_kalshi_without_structured_identity_rejects_ambiguous_labels():
    market = kalshi("LEGACY", "New York", event_code="LEGACY")
    assert normalize_market(market) is None


def test_conflicting_kalshi_market_and_event_tickers_are_rejected():
    market = kalshi("NYG", "New York Giants")
    market.raw["event_ticker"] = "KXNFLGAME-30JAN06NYGBUF"
    assert normalize_market(market) is None
