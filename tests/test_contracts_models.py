"""Validators for the PR-0 group, assessment, and episode contracts."""

import inspect
from collections.abc import AsyncIterator, Sequence
from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from pmarb.models import (
    AssessmentClass,
    Book,
    Episode,
    EpisodeTermination,
    EquivalenceGroup,
    ExceptionRow,
    FeeQuote,
    GroupMember,
    GroupStatus,
    Leg,
    LegResult,
    Level,
    Market,
    OpportunityAssessment,
    Side,
    Venue,
)
from pmarb.protocols import BookEvent, FeeModel, VenueAdapter

AWARE = datetime(2026, 11, 11, 18, tzinfo=UTC)
LATER = datetime(2026, 11, 11, 18, 1, tzinfo=UTC)
NAIVE = datetime(2026, 11, 11, 18)


def _market(**overrides: Any) -> Market:
    payload: dict[str, Any] = {
        "venue": Venue.KALSHI,
        "market_id": "KX-1",
        "title": "NE at DET",
        "league": "nfl",
        "participants": ("NE", "DET"),
        "yes_participant": "NE",
        "market_type": "winner",
        "line": Decimal("3.5"),
        "event_start_utc": AWARE,
        "expected_resolution_utc": AWARE,
        "expected_resolution_src": "rules",
        "expected_payout_utc": None,
        "status": "active",
        "rules_text": "Rules",
        "rules_hash": "abc",
        "fee_meta": {"multiplier": "1"},
        "url": "https://example.test/KX-1",
        "raw": {"ticker": "KX-1"},
    }
    payload.update(overrides)
    return Market(**payload)


def _member(
    venue: Venue,
    market_id: str,
    *,
    side: Side = Side.YES,
    rules_hash: str | None = None,
) -> GroupMember:
    return GroupMember(
        venue=venue,
        market_id=market_id,
        native_side_for_outcome=side,
        approved_rules_hash=rules_hash,
    )


def _group(**overrides: Any) -> EquivalenceGroup:
    payload: dict[str, Any] = {
        "group_id": "g1",
        "outcome": "NE beats DET, OT included",
        "members": (
            _member(Venue.KALSHI, "KX-1", rules_hash="hash-k"),
            _member(Venue.POLYMARKET_US, "pm-1", side=Side.NO, rules_hash="hash-p"),
        ),
        "status": GroupStatus.CANDIDATE,
    }
    payload.update(overrides)
    return EquivalenceGroup(**payload)


def _quote(**overrides: Any) -> FeeQuote:
    payload: dict[str, Any] = {
        "amount": Decimal("0.02"),
        "provenance": "kalshi-fee-pdf-2026-07-07",
    }
    payload.update(overrides)
    return FeeQuote(**payload)


def _leg(venue: Venue, side: Side, market_id: str) -> Leg:
    return Leg(venue=venue, market_id=market_id, side=side)


def _leg_result(
    venue: Venue = Venue.KALSHI,
    side: Side = Side.YES,
    market_id: str = "KX-1",
    **overrides: Any,
) -> LegResult:
    payload: dict[str, Any] = {
        "leg": _leg(venue, side, market_id),
        "levels_consumed": [Level(price=Decimal("0.45"), size=Decimal(10))],
        "quantity": Decimal(10),
        "cost": Decimal("4.50"),
        "avg_price": Decimal("0.45"),
        "fee": _quote(),
        "book_age_ms": 100,
    }
    payload.update(overrides)
    return LegResult(**payload)


def _assessment(**overrides: Any) -> OpportunityAssessment:
    yes = _leg_result(Venue.KALSHI, Side.YES, "KX-1")
    no = _leg_result(Venue.POLYMARKET_US, Side.NO, "pm-1")
    payload: dict[str, Any] = {
        "group_id": "g1",
        "yes_leg": yes.leg,
        "no_leg": no.leg,
        "requested_qty": Decimal(10),
        "yes_result": yes,
        "no_result": no,
        "capital": Decimal("9.50"),
        "gross_edge": Decimal("0.50"),
        "net_profit": Decimal("0.40"),
        "roc": Decimal("0.04"),
        "max_profitable_qty": Decimal(10),
        "skew_ms": 20,
        "books_atomic": True,
        "exception_risk": False,
        "result_class": AssessmentClass.QUALIFIED,
        "reasons": (),
        "model_version": "m1",
        "computed_utc": AWARE,
    }
    payload.update(overrides)
    return OpportunityAssessment(**payload)


def _episode(**overrides: Any) -> Episode:
    payload: dict[str, Any] = {
        "group_id": "g1",
        "yes_leg": _leg(Venue.KALSHI, Side.YES, "KX-1"),
        "no_leg": _leg(Venue.POLYMARKET_US, Side.NO, "pm-1"),
        "qty": Decimal(10),
        "result_class": AssessmentClass.QUALIFIED,
        "model_version": "m1",
        "first_seen_utc": AWARE,
        "last_seen_utc": LATER,
        "observations": 3,
        "peak_net": Decimal("0.40"),
        "min_size": Decimal(1),
        "max_size": Decimal(10),
    }
    payload.update(overrides)
    return Episode(**payload)


def test_enum_values() -> None:
    assert {item.value for item in AssessmentClass} == {"qualified", "indicative", "rejected"}
    assert {item.value for item in GroupStatus} == {
        "candidate",
        "approved",
        "revoked",
        "rejected",
    }
    assert {item.value for item in EpisodeTermination} == {
        "disqualified",
        "data_gap",
        "collector_restart",
        "collector_shutdown",
        "clock_jump",
        "maintenance",
        "rules_changed",
        "market_closed",
    }


def test_market_stores_contract_fields_and_key() -> None:
    built = _market()
    assert built.market_key == "kalshi:KX-1"
    assert built.participants == ("NE", "DET")
    assert built.yes_participant == "NE"
    assert built.line == Decimal("3.5")
    assert type(built.line) is Decimal
    assert built.expected_payout_utc is None
    assert built.fee_meta == {"multiplier": "1"}
    assert built.model_config["frozen"] is True
    assert built.model_config["extra"] == "forbid"


def test_market_rejects_float_line() -> None:
    with pytest.raises(TypeError, match="float is not permitted"):
        _market(line=0.5)


@pytest.mark.parametrize(
    "field_name",
    ["event_start_utc", "expected_resolution_utc", "expected_payout_utc"],
)
def test_market_rejects_naive_datetime(field_name: str) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _market(**{field_name: NAIVE})


def test_market_requires_rules_hash_and_market_type() -> None:
    payload = _market().model_dump()
    del payload["rules_hash"]
    with pytest.raises(ValidationError):
        Market(**payload)
    payload = _market().model_dump()
    del payload["market_type"]
    with pytest.raises(ValidationError):
        Market(**payload)


def test_market_rejects_unknown_fields_and_mutation() -> None:
    payload = _market().model_dump()
    payload["not_a_field"] = "x"
    with pytest.raises(ValidationError):
        Market(**payload)
    built = _market()
    with pytest.raises(ValidationError):
        built.title = "changed"


def test_candidate_group_allows_missing_hashes() -> None:
    built = _group(
        members=(
            _member(Venue.KALSHI, "KX-1"),
            _member(Venue.POLYMARKET_US, "pm-1", side=Side.NO),
        ),
        exception_table=(
            ExceptionRow(
                outcome_case="cancelled",
                payouts={"kalshi:KX-1": "fair price", "polymarket_us:pm-1": "fair price"},
                preserves_unit_payout=False,
            ),
        ),
        exception_risk=True,
    )
    assert built.status is GroupStatus.CANDIDATE
    assert built.hash_version == 1
    assert built.reviewer is None
    assert built.decided_utc is None
    assert built.notes == ""
    assert built.exception_risk is True
    assert built.exception_table[0].preserves_unit_payout is False


def test_approved_group_requires_reviewer_time_and_hashes() -> None:
    approved = _group(
        status=GroupStatus.APPROVED,
        reviewer="ada",
        decided_utc=AWARE,
        notes="tie pays 0.50/0.50",
    )
    assert approved.reviewer == "ada"
    assert approved.members[0].approved_rules_hash == "hash-k"
    assert approved.members[1].approved_rules_hash == "hash-p"

    with pytest.raises(ValidationError, match="reviewer"):
        _group(status=GroupStatus.APPROVED, reviewer=None, decided_utc=AWARE)
    with pytest.raises(ValidationError, match="reviewer"):
        _group(status=GroupStatus.APPROVED, reviewer="", decided_utc=AWARE)
    with pytest.raises(ValidationError, match="decided_utc"):
        _group(status=GroupStatus.APPROVED, reviewer="ada", decided_utc=None)
    with pytest.raises(ValidationError, match="approved_rules_hash"):
        _group(
            status=GroupStatus.APPROVED,
            reviewer="ada",
            decided_utc=AWARE,
            members=(
                _member(Venue.KALSHI, "KX-1", rules_hash="hash-k"),
                _member(Venue.POLYMARKET_US, "pm-1", rules_hash=None),
            ),
        )
    with pytest.raises(ValidationError, match="approved_rules_hash"):
        _group(
            status=GroupStatus.APPROVED,
            reviewer="ada",
            decided_utc=AWARE,
            members=(
                _member(Venue.KALSHI, "KX-1", rules_hash="hash-k"),
                _member(Venue.POLYMARKET_US, "pm-1", rules_hash=""),
            ),
        )


def test_group_rejects_fewer_than_two_members_and_duplicate_venues() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        _group(members=(_member(Venue.KALSHI, "KX-1"),))
    with pytest.raises(ValidationError, match="at least 2"):
        _group(members=())
    with pytest.raises(ValidationError, match="distinct"):
        _group(
            members=(
                _member(Venue.KALSHI, "KX-1"),
                _member(Venue.KALSHI, "KX-2"),
            )
        )


def test_group_rejects_empty_outcome_and_naive_decided_utc() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        _group(outcome="")
    with pytest.raises(ValidationError, match="timezone-aware"):
        _group(decided_utc=NAIVE)


def test_revoked_group_does_not_require_hashes() -> None:
    revoked = _group(
        status=GroupStatus.REVOKED,
        members=(
            _member(Venue.KALSHI, "KX-1"),
            _member(Venue.POLYMARKET_US, "pm-1"),
        ),
        reviewer="ada",
        decided_utc=AWARE,
    )
    assert revoked.status is GroupStatus.REVOKED


def test_fee_quote_unknown_amount_and_rejects_float_or_empty_provenance() -> None:
    unknown = FeeQuote(provenance="unset")
    assert unknown.amount is None
    assert unknown.conservative_bound is False
    bounded = _quote(amount=Decimal("0.02"), conservative_bound=True)
    assert type(bounded.amount) is Decimal
    assert bounded.conservative_bound is True
    with pytest.raises(TypeError, match="float is not permitted"):
        _quote(amount=0.02)
    with pytest.raises(ValidationError, match="non-empty"):
        _quote(provenance="")


def test_leg_result_keeps_decimals_and_rejects_float() -> None:
    result = _leg_result()
    assert result.quantity == Decimal(10)
    assert type(result.cost) is Decimal
    assert result.book_age_ms == 100
    with pytest.raises(TypeError, match="float is not permitted"):
        _leg_result(quantity=10.0)
    with pytest.raises(TypeError, match="float is not permitted"):
        _leg_result(cost=4.5)
    with pytest.raises(TypeError, match="float is not permitted"):
        _leg_result(avg_price=0.45)


def test_qualified_assessment_requires_atomic_books_and_no_reasons() -> None:
    built = _assessment()
    assert built.result_class is AssessmentClass.QUALIFIED
    assert built.books_atomic is True
    assert built.reasons == ()
    assert type(built.net_profit) is Decimal
    with pytest.raises(ValidationError, match="books_atomic"):
        _assessment(books_atomic=False)
    with pytest.raises(ValidationError, match="cannot include reasons"):
        _assessment(reasons=("book_skew",))


def test_rejected_assessment_requires_reasons_and_indicative_does_not() -> None:
    rejected = _assessment(
        result_class=AssessmentClass.REJECTED,
        books_atomic=False,
        reasons=("insufficient_depth_kalshi",),
        yes_result=None,
        no_result=None,
        capital=None,
        gross_edge=None,
        net_profit=None,
        roc=None,
        max_profitable_qty=None,
    )
    assert rejected.reasons == ("insufficient_depth_kalshi",)
    with pytest.raises(ValidationError, match="requires reasons"):
        _assessment(
            result_class=AssessmentClass.REJECTED,
            books_atomic=False,
            reasons=(),
        )
    indicative = _assessment(
        result_class=AssessmentClass.INDICATIVE,
        books_atomic=False,
        reasons=("book_skew",),
    )
    assert indicative.result_class is AssessmentClass.INDICATIVE
    assert indicative.books_atomic is False


def test_assessment_legs_must_be_yes_and_no_on_different_venues() -> None:
    with pytest.raises(ValidationError, match="yes_leg.side"):
        _assessment(yes_leg=_leg(Venue.KALSHI, Side.NO, "KX-1"))
    with pytest.raises(ValidationError, match="no_leg.side"):
        _assessment(no_leg=_leg(Venue.POLYMARKET_US, Side.YES, "pm-1"))
    with pytest.raises(ValidationError, match="different venues"):
        _assessment(no_leg=_leg(Venue.KALSHI, Side.NO, "KX-2"))


def test_assessment_rejects_float_money_and_naive_timestamp() -> None:
    with pytest.raises(TypeError, match="float is not permitted"):
        _assessment(requested_qty=10.0)
    with pytest.raises(TypeError, match="float is not permitted"):
        _assessment(capital=1.0)
    with pytest.raises(TypeError, match="float is not permitted"):
        _assessment(gross_edge=0.5)
    with pytest.raises(TypeError, match="float is not permitted"):
        _assessment(net_profit=0.4)
    with pytest.raises(TypeError, match="float is not permitted"):
        _assessment(roc=0.04)
    with pytest.raises(TypeError, match="float is not permitted"):
        _assessment(max_profitable_qty=10.0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _assessment(computed_utc=NAIVE)


def test_episode_qualified_and_indicative_only() -> None:
    qualified = _episode()
    assert qualified.id is None
    assert qualified.termination is None
    assert qualified.observations == 3
    same_instant = _episode(last_seen_utc=AWARE)
    assert same_instant.last_seen_utc == same_instant.first_seen_utc
    indicative = _episode(
        result_class=AssessmentClass.INDICATIVE,
        termination=EpisodeTermination.DATA_GAP,
        termination_detail="gap exceeded 2x cadence",
        id=7,
    )
    assert indicative.result_class is AssessmentClass.INDICATIVE
    assert indicative.termination is EpisodeTermination.DATA_GAP
    assert indicative.id == 7
    with pytest.raises(ValidationError, match="qualified or indicative"):
        _episode(result_class=AssessmentClass.REJECTED)


def test_episode_rejects_inverted_window_few_observations_float_and_naive_time() -> None:
    with pytest.raises(ValidationError, match="last_seen_utc"):
        _episode(first_seen_utc=LATER, last_seen_utc=AWARE)
    with pytest.raises(ValidationError, match="observations"):
        _episode(observations=0)
    with pytest.raises(TypeError, match="float is not permitted"):
        _episode(qty=10.0)
    with pytest.raises(TypeError, match="float is not permitted"):
        _episode(peak_net=0.4)
    with pytest.raises(TypeError, match="float is not permitted"):
        _episode(min_size=1.0)
    with pytest.raises(TypeError, match="float is not permitted"):
        _episode(max_size=10.0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _episode(first_seen_utc=NAIVE)
    with pytest.raises(ValidationError, match="timezone-aware"):
        _episode(last_seen_utc=NAIVE)


def test_fee_model_and_venue_adapter_signatures() -> None:
    fee_signature = inspect.signature(FeeModel.taker_fee)
    assert list(fee_signature.parameters) == ["self", "levels", "side"]
    fee_hints = inspect.get_annotations(FeeModel.taker_fee, eval_str=True)
    assert fee_hints["levels"] == Sequence[Level]
    assert fee_hints["side"] is Side
    assert fee_hints["return"] is FeeQuote

    adapter_hints = inspect.get_annotations(VenueAdapter, eval_str=True)
    assert adapter_hints["venue"] is Venue

    listed = VenueAdapter.list_markets
    assert inspect.iscoroutinefunction(listed)
    assert list(inspect.signature(listed).parameters) == [
        "self",
        "horizon_start",
        "horizon_end",
    ]
    listed_hints = inspect.get_annotations(listed, eval_str=True)
    assert listed_hints["horizon_start"] is datetime
    assert listed_hints["horizon_end"] is datetime
    assert listed_hints["return"] == AsyncIterator[Market]

    get_market = VenueAdapter.get_market
    assert inspect.iscoroutinefunction(get_market)
    assert list(inspect.signature(get_market).parameters) == ["self", "market_id"]
    assert inspect.get_annotations(get_market, eval_str=True)["return"] is Market

    get_books = VenueAdapter.get_books
    assert inspect.iscoroutinefunction(get_books)
    book_hints = inspect.get_annotations(get_books, eval_str=True)
    assert book_hints["market_ids"] == Sequence[str]
    assert book_hints["return"] == list[Book]

    stream = VenueAdapter.book_stream
    assert not inspect.iscoroutinefunction(stream)
    stream_hints = inspect.get_annotations(stream, eval_str=True)
    assert stream_hints["market_ids"] == Sequence[str]
    assert stream_hints["return"] == AsyncIterator[BookEvent] | None

    fee_model = VenueAdapter.fee_model
    assert not inspect.iscoroutinefunction(fee_model)
    fee_model_hints = inspect.get_annotations(fee_model, eval_str=True)
    assert fee_model_hints["market"] is Market
    assert fee_model_hints["return"] is FeeModel

    assert [field.name for field in fields(BookEvent)] == [
        "kind",
        "book",
        "market_id",
        "venue",
        "reason",
    ]
