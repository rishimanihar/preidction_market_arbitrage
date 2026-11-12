import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmarb.app.formatting import (
    assessment_display_rows,
    format_chicago_time,
    format_money,
    rank_assessments,
)
from pmarb.app.queries import (
    fetch_assessments,
    fetch_candidates,
    fetch_episodes,
    fetch_group_review,
    fetch_health_events,
    open_reader,
)
from pmarb.models import (
    AssessmentClass,
    Episode,
    EquivalenceGroup,
    ExceptionRow,
    FeeQuote,
    GroupMember,
    GroupStatus,
    Leg,
    LegResult,
    Market,
    OpportunityAssessment,
    Side,
    Venue,
)
from pmarb.storage.db import (
    insert_assessment,
    insert_collector_event,
    insert_rules_version,
    open_writer,
    upsert_episode,
    upsert_group,
    upsert_market,
)


def _market(venue: Venue, market_id: str, hash_: str) -> Market:
    return Market(
        venue=venue,
        market_id=market_id,
        title=f"{market_id} wins",
        league="nfl",
        participants=("NE", "NYJ"),
        yes_participant="NE",
        market_type="winner",
        status="active",
        rules_text="Synthetic rules",
        rules_hash=hash_,
        url=f"https://example.test/{market_id}",
    )


def _assessment(net: str, roc: str, stamp: datetime) -> OpportunityAssessment:
    yes_leg = Leg(venue=Venue.KALSHI, market_id="NE", side=Side.YES)
    no_leg = Leg(venue=Venue.POLYMARKET_US, market_id="poly", side=Side.NO)
    yes = LegResult(
        leg=yes_leg,
        levels_consumed=[],
        quantity=Decimal(1),
        cost=Decimal("0.4"),
        avg_price=Decimal("0.4"),
        fee=FeeQuote(amount=Decimal("0.01"), provenance="synthetic"),
        book_age_ms=20,
    )
    no = LegResult(
        leg=no_leg,
        levels_consumed=[],
        quantity=Decimal(1),
        cost=Decimal("0.5"),
        avg_price=Decimal("0.5"),
        fee=FeeQuote(amount=Decimal("0.01"), provenance="synthetic", conservative_bound=True),
        book_age_ms=25,
    )
    return OpportunityAssessment(
        group_id="grp",
        yes_leg=yes_leg,
        no_leg=no_leg,
        requested_qty=Decimal(1),
        yes_result=yes,
        no_result=no,
        capital=Decimal("0.9"),
        gross_edge=Decimal("0.1"),
        net_profit=Decimal(net),
        roc=Decimal(roc),
        max_profitable_qty=Decimal(1),
        skew_ms=5,
        books_atomic=False,
        exception_risk=True,
        result_class=AssessmentClass.INDICATIVE,
        reasons=("polling_skew",),
        model_version="synthetic",
        computed_utc=stamp,
    )


def test_queries_use_read_only_db_and_return_exact_decimals(tmp_path):
    path = tmp_path / "data.sqlite"
    writer = open_writer(path)
    now = datetime(2026, 11, 1, 7, tzinfo=UTC)
    poly = _market(Venue.POLYMARKET_US, "poly", "p-hash")
    kalshi = _market(Venue.KALSHI, "NE", "k-hash")
    upsert_market(writer, poly, now)
    upsert_market(writer, kalshi, now)
    insert_rules_version(writer, poly.market_key, poly.rules_hash, "Poly rules old", "", now)
    poly = poly.model_copy(update={"rules_hash": "p-hash-current"})
    upsert_market(writer, poly, now)
    insert_rules_version(writer, poly.market_key, poly.rules_hash, "Poly rules current", "", now)
    insert_rules_version(
        writer, kalshi.market_key, kalshi.rules_hash, "K rules", "Terms PDF text", now
    )
    group = EquivalenceGroup(
        group_id="grp",
        outcome="NE wins",
        members=(
            GroupMember(venue=Venue.KALSHI, market_id="NE", native_side_for_outcome=Side.YES),
            GroupMember(
                venue=Venue.POLYMARKET_US, market_id="poly", native_side_for_outcome=Side.YES
            ),
        ),
        status=GroupStatus.CANDIDATE,
        exception_risk=True,
        exception_table=(
            ExceptionRow(
                outcome_case="cancelled",
                payouts={"kalshi:NE": "0.5", "polymarket_us:poly": "0.5"},
                preserves_unit_payout=True,
            ),
        ),
    )
    upsert_group(writer, group)
    insert_assessment(writer, _assessment("0.01230", "0.4", now))
    insert_assessment(writer, _assessment("0.02", "0.2", now.replace(minute=1)))
    upsert_episode(
        writer,
        Episode(
            group_id="grp",
            yes_leg=Leg(venue=Venue.KALSHI, market_id="NE", side=Side.YES),
            no_leg=Leg(venue=Venue.POLYMARKET_US, market_id="poly", side=Side.NO),
            qty=Decimal(1),
            result_class=AssessmentClass.INDICATIVE,
            model_version="synthetic",
            first_seen_utc=now,
            last_seen_utc=now,
            observations=2,
            peak_net=Decimal("0.02"),
            min_size=Decimal(1),
            max_size=Decimal(2),
        ),
    )
    insert_collector_event(writer, now, None, "health", {"state": "ok"})
    insert_collector_event(writer, now, None, "gap", {"reason": "synthetic"})
    writer.close()

    reader = open_reader(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("DELETE FROM groups")
        candidates = fetch_candidates(reader)
        assert candidates[0]["group_id"] == "grp"
        assert {m["market_key"] for m in candidates[0]["members"]} == {
            "kalshi:NE",
            "polymarket_us:poly",
        }
        review = fetch_group_review(reader, "grp")
        assert review["exception_table"][0]["outcome_case"] == "cancelled"
        assert {m["rules_text"] for m in review["members"]} == {"Poly rules current", "K rules"}
        poly_review = next(m for m in review["members"] if m["market_key"] == "polymarket_us:poly")
        assert poly_review["rules_hash"] == "p-hash-current"
        assessments = fetch_assessments(reader, "indicative")
        assert assessments[0]["net_profit"] == Decimal("0.02")
        assert assessments[1]["net_profit"] == Decimal("0.01230")
        assert assessments[1]["fee_provenance"]["no"]["conservative_bound"] is True
        assert assessments[0]["yes_title"] == "NE wins"
        assert [row["kind"] for row in fetch_health_events(reader)] == ["gap", "health"]
        episodes = fetch_episodes(reader, search="grp")
        assert len(episodes) == 1
        assert episodes[0]["yes_url"] == "https://example.test/NE"
        assert episodes[0]["no_url"] == "https://example.test/poly"
        assert fetch_episodes(reader, search="absent") == []
    finally:
        reader.close()


def test_money_and_chicago_formatting_across_dst():
    assert format_money(Decimal("1234.5")) == "$1,234.50"
    # UTC instants on either side of the autumn DST transition have distinct labels.
    assert format_chicago_time("2026-11-01T06:30:00Z").endswith("CDT")
    assert format_chicago_time("2026-11-01T07:30:00Z").endswith("CST")
    with pytest.raises(ValueError):
        format_chicago_time("2026-11-01T07:30:00")


def test_ranking_uses_net_then_roc():
    rows = [
        {"id": 1, "net_profit": "0.10", "roc": "0.2"},
        {"id": 2, "net_profit": "0.20", "roc": "0.1"},
        {"id": 3, "net_profit": "0.10", "roc": "0.3"},
    ]
    assert [row["id"] for row in rank_assessments(rows)] == [2, 3, 1]


def test_nonfinite_decimal_rejected():
    from pmarb.app.formatting import as_decimal

    with pytest.raises(ValueError, match="finite"):
        as_decimal("NaN")


def test_live_display_uses_only_exact_reverse_observation_and_marks_missing_evidence():
    stamp = "2026-11-01T07:30:00Z"
    common = {
        "group_id": "grp",
        "qty": Decimal(100),
        "computed_utc": stamp,
        "age_yes_ms": 10,
        "age_no_ms": 12,
        "skew_ms": 2,
        "exception_risk": 1,
        "reasons": [],
        "fee_yes": Decimal("0.01"),
        "fee_no": Decimal("0.02"),
        "fee_provenance": {
            "yes": {"provenance": "synthetic"},
            "no": {"provenance": "cap", "conservative_bound": True},
        },
    }
    first = {
        **common,
        "id": 1,
        "class": "indicative",
        "yes_market_key": "kalshi:A",
        "no_market_key": "polymarket_us:A",
        "yes_title": "A YES",
        "no_title": "A NO",
        "avg_yes": Decimal("0.4"),
        "avg_no": Decimal("0.5"),
        "net_profit": Decimal("0.1"),
        "roc": Decimal("0.01"),
    }
    second = {
        **common,
        "id": 2,
        "class": "rejected",
        "yes_market_key": "polymarket_us:A",
        "no_market_key": "kalshi:A",
        "avg_yes": Decimal("0.3"),
        "avg_no": Decimal("0.6"),
    }
    other_time = {
        **second,
        "id": 3,
        "computed_utc": "2026-11-01T07:30:01Z",
        "avg_yes": Decimal("0.1"),
        "avg_no": Decimal("0.1"),
    }
    display = assessment_display_rows([first], [first, second, other_time])[0]
    assert display["Opposite direction avg-price sum"] == "$0.9000"
    assert display["YES leg"] == "A YES @ $0.4000 × 100"
    assert display["ROC"] == "1.00%"
    assert "conservative bound" in display["Fees and provenance"]
    assert display["Size at touch"] == "Unavailable in persisted assessments"
    assert display["Dust"] == "Unknown"
