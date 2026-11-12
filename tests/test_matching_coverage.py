"""Private local-data audit for reference-pair matching coverage."""

import json
from collections import Counter, defaultdict

import pytest

from pmarb.matching.candidates import generate_candidates
from pmarb.matching.normalize import normalize_market
from pmarb.models import Venue
from tests.test_matching import RAW_DATA, _local_markets, kalshi, poly


def _first_reason(market):
    diagnostics = []
    normalized = normalize_market(market, diagnostics=diagnostics)
    if normalized is not None:
        return normalized, None
    reason = diagnostics[0].reason if diagnostics else "no_normalization_diagnostic"
    if reason.startswith("ambiguous_team_label:"):
        reason = "ambiguous_label"
    elif "date_conflict" in reason or "date_conflicts" in reason:
        reason = "date_conflict"
    elif "unrecognized" in reason or "unresolved" in reason:
        reason = f"unrecognized_or_unresolved_label:{reason}"
    return None, reason


def _pair_blocker(k_market, p_market):
    k_norm, reason = _first_reason(k_market)
    if reason:
        return f"kalshi:{reason}"
    p_norm, reason = _first_reason(p_market)
    if reason:
        return f"polymarket:{reason}"
    if k_norm.league != p_norm.league:
        return f"league_mismatch:{k_norm.league}:{p_norm.league}"
    if k_norm.participants != p_norm.participants:
        return "participant_set_mismatch"
    if k_norm.event_date_ny != p_norm.event_date_ny:
        return "date_mismatch"
    if k_norm.yes_participant != p_norm.yes_participant:
        return f"yes_participant_mismatch:{k_norm.yes_participant}:{p_norm.yes_participant}"
    return "candidate_not_generated:normalized_pair_matches"


def _candidate_ref_key(candidate):
    kalshi_member = next(m for m in candidate.group.members if m.venue is Venue.KALSHI)
    poly_member = next(m for m in candidate.group.members if m.venue is Venue.POLYMARKET_US)
    return kalshi_member.market_id.rsplit("-", 1)[0], poly_member.market_id


def test_synthetic_coverage_blocker_classification():
    k = kalshi("NYG", "New York Giants")
    p = poly()
    assert _pair_blocker(k, p) == "candidate_not_generated:normalized_pair_matches"

    opposite_yes = poly(
        yes_label="New York Jets",
        yes_code="nyj",
        opponent_label="New York Giants",
        opponent_code="nyg",
    )
    assert _pair_blocker(k, opposite_yes).startswith("yes_participant_mismatch:")

    other_date = poly(market_id="aec-nfl-nyg-nyj-2030-01-07", start="2030-01-07T18:00:00Z")
    assert _pair_blocker(k, other_date) == "date_mismatch"

    ambiguous = kalshi("WILD", "Wildcats", league="ncaaf", event_code="LEGACY")
    assert _pair_blocker(ambiguous, p).startswith("kalshi:ambiguous_label")


@pytest.mark.localdata
@pytest.mark.skipif(
    not all(
        (RAW_DATA / name).is_file()
        for name in ("kalshi_primary_7d.json", "kalshi_close_time_7d.json", "candidate_pairs.json")
    )
    or not any(RAW_DATA.glob("poly_markets_enddate_7d_page_*.json")),
    reason="local matching market dumps not present",
)
def test_localdata_reference_pair_coverage():
    reference = json.loads((RAW_DATA / "candidate_pairs.json").read_text(encoding="utf-8"))["pairs"]
    kalshi_markets, poly_markets = _local_markets()
    k_by_id = {market.market_id: market for market in kalshi_markets}
    p_by_id = {market.market_id: market for market in poly_markets}

    candidates = generate_candidates([*kalshi_markets, *poly_markets])
    generated = {_candidate_ref_key(candidate): candidate for candidate in candidates}
    normalized = {m.market_key: normalize_market(m) for m in [*kalshi_markets, *poly_markets]}

    # Validate every generated match on all join dimensions and its YES outcome.
    for candidate in candidates:
        k_member = next(m for m in candidate.group.members if m.venue is Venue.KALSHI)
        p_member = next(m for m in candidate.group.members if m.venue is Venue.POLYMARKET_US)
        kn = normalized[f"{k_member.venue.value}:{k_member.market_id}"]
        pn = normalized[f"{p_member.venue.value}:{p_member.market_id}"]
        assert kn is not None and pn is not None
        assert kn.league == pn.league == candidate.match_keys[0]
        assert kn.participants == pn.participants == frozenset(candidate.match_keys[1])
        assert kn.event_date_ny == pn.event_date_ny == candidate.match_keys[2]
        assert kn.yes_participant == pn.yes_participant
        assert k_member.native_side_for_outcome.value == "yes"
        assert p_member.native_side_for_outcome.value == "yes"

    by_league = defaultdict(lambda: Counter())
    for row in reference:
        league = row["league"]
        k_event = row["kalshi_event"]
        p_id = row["poly_market_slug"]
        key = (k_event, p_id)
        bucket = by_league[league]
        bucket["total"] += 1
        candidate = generated.get(key)
        if candidate is not None:
            bucket["matched"] += 1
            continue

        long_sides = [side for side in row.get("sides", []) if side.get("poly_long") is True]
        if len(long_sides) != 1:
            reason = f"invalid_reference_long_side_count:{len(long_sides)}"
        else:
            target_ticker = long_sides[0].get("kalshi_ticker")
            k_market = k_by_id.get(target_ticker)
            p_market = p_by_id.get(p_id)
            if k_market is None:
                reason = "reference_market_missing:kalshi_long_side_ticker"
            elif p_market is None:
                reason = "reference_market_missing:polymarket"
            else:
                reason = _pair_blocker(k_market, p_market)
        assert reason != "candidate_not_generated:normalized_pair_matches", (league, key, reason)
        bucket[f"miss:{reason}"] += 1
        assert reason, (league, key)

    for league, counts in sorted(by_league.items()):
        misses = {key[5:]: value for key, value in counts.items() if key.startswith("miss:")}
        print(f"{league}: matched={counts['matched']}/{counts['total']}; misses={misses}")
        assert (
            counts["matched"] + sum(v for k, v in counts.items() if k.startswith("miss:"))
            == counts["total"]
        )
