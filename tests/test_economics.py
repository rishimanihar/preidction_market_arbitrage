"""N-venue assessments: fees, freshness, and one-outcome group pairing."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pmarb.economics import LegInput, Limits, assess, assess_group
from pmarb.fees import KalshiFeeModel, PolymarketUSFeeModel
from pmarb.models import (
    AssessmentClass,
    EquivalenceGroup,
    FeeQuote,
    GroupMember,
    GroupStatus,
    Leg,
    Level,
    Side,
    Venue,
)
from tests.conftest import NOW, RECEIVED, make_book

DIRECT = Decimal("0.0001")


def _level(price: str, size: str) -> Level:
    return Level(price=Decimal(price), size=Decimal(size))


def _kalshi(
    multiplier: Decimal | None = Decimal(1),
    precision: Decimal | None = DIRECT,
    coefficient: Decimal | None = Decimal("0.07"),
) -> KalshiFeeModel:
    return KalshiFeeModel(coefficient=coefficient, multiplier=multiplier, precision=precision)


def _poly(theta: Decimal | None = Decimal("0.095")) -> PolymarketUSFeeModel:
    return PolymarketUSFeeModel(theta=theta)


def _input(venue: Venue, market_id: str, side: Side, book, fee) -> LegInput:
    return LegInput(Leg(venue=venue, market_id=market_id, side=side), book, fee)


def _pair(
    kalshi_yes: list[Level],
    kalshi_no: list[Level],
    poly_yes: list[Level],
    poly_no: list[Level],
    *,
    kalshi_monotonic: float = NOW,
    poly_monotonic: float = NOW,
    kalshi_valid: bool = True,
    poly_valid: bool = True,
    kalshi_received: datetime = RECEIVED,
    poly_received: datetime = RECEIVED,
    kalshi_source: datetime | None = None,
    poly_source: datetime | None = None,
    kalshi_fee: KalshiFeeModel | None = None,
    poly_fee: PolymarketUSFeeModel | None = None,
):
    kalshi = make_book(
        Venue.KALSHI,
        "KX-1",
        kalshi_yes,
        kalshi_no,
        received_monotonic=kalshi_monotonic,
        received_utc=kalshi_received,
        valid=kalshi_valid,
        source_ts=kalshi_source,
    )
    poly = make_book(
        Venue.POLYMARKET_US,
        "PM-1",
        poly_yes,
        poly_no,
        received_monotonic=poly_monotonic,
        received_utc=poly_received,
        valid=poly_valid,
        source_ts=poly_source,
    )
    yes = _input(Venue.KALSHI, "KX-1", Side.YES, kalshi, kalshi_fee or _kalshi())
    no = _input(Venue.POLYMARKET_US, "PM-1", Side.NO, poly, poly_fee or _poly())
    return yes, no


def _run(yes: LegInput, no: LegInput, qty: Decimal = Decimal(100), **overrides):
    kwargs = {
        "now_monotonic": NOW,
        "now_wall": RECEIVED,
        "books_atomic": True,
        "exception_risk": False,
        "model_version": "pr1-test",
    }
    kwargs.update(overrides)
    return assess("g1", yes, no, qty, **kwargs)


def _worked(**overrides):
    pair_keys = {
        key: overrides.pop(key) for key in list(overrides) if key.startswith(("kalshi_", "poly_"))
    }
    yes, no = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        **pair_keys,
    )
    return _run(yes, no, **overrides)


def test_worked_example_kalshi_yes_against_poly_no() -> None:
    # Kalshi YES 100 @ 0.45, direct member. Model fee
    # 0.07 * 100 * 0.45 * 0.55 = 1.7325. Revenue -45 is on the 0.0001 grid,
    # so the accumulator net fee is 1.7325 and the margin makes it 1.7326.
    # Poly NO acquisition price 0.50 is the native YES bid of 0.50.
    # 0.095 * 100 * 0.50 * 0.50 = 2.375, half-even to 2.38.
    # Capital 45 + 50 + 1.7326 + 2.38 = 99.1126. Gross edge 5. Net 0.8874.
    result = _worked()
    assert result.result_class is AssessmentClass.QUALIFIED
    assert result.reasons == ()
    assert result.books_atomic is True
    assert result.model_version == "pr1-test"
    assert result.computed_utc == RECEIVED
    assert result.exception_risk is False
    assert result.skew_ms == 0
    assert result.yes_result is not None and result.no_result is not None
    assert result.yes_result.leg.side is Side.YES
    assert result.yes_result.cost == Decimal(45)
    assert result.yes_result.avg_price == Decimal("0.45")
    assert result.yes_result.fee.amount == Decimal("1.7326")
    assert result.yes_result.fee.conservative_bound is True
    assert "per-level fills + 1 step margin" in result.yes_result.fee.provenance
    assert result.yes_result.book_age_ms == 0
    assert result.no_result.leg.venue is Venue.POLYMARKET_US
    assert result.no_result.leg.side is Side.NO
    assert result.no_result.cost == Decimal(50)
    assert result.no_result.fee.amount == Decimal("2.38")
    assert result.gross_edge == Decimal(5)
    assert result.capital == Decimal("99.1126")
    assert result.net_profit == Decimal("0.8874")
    assert result.roc == result.net_profit / result.capital
    assert result.roc.quantize(Decimal("0.000001")) == Decimal("0.008953")
    assert result.max_profitable_qty == Decimal(100)


def test_both_directions_are_independent() -> None:
    kalshi = make_book(
        Venue.KALSHI,
        "KX-1",
        [_level("0.45", "100")],
        [_level("0.80", "100")],
        received_monotonic=NOW,
    )
    poly = make_book(
        Venue.POLYMARKET_US,
        "PM-1",
        [_level("0.80", "100")],
        [_level("0.50", "100")],
        received_monotonic=NOW,
    )
    group = _group((_member(Venue.KALSHI, "KX-1"), _member(Venue.POLYMARKET_US, "PM-1")))
    books = {"kalshi:KX-1": kalshi, "polymarket_us:PM-1": poly}
    fees = {"kalshi:KX-1": _kalshi(), "polymarket_us:PM-1": _poly()}
    forward, reverse = assess_group(
        group,
        books,
        fees,
        Decimal(100),
        now_monotonic=NOW,
        now_wall=RECEIVED,
        books_atomic=True,
        model_version="pr1-test",
    )
    assert forward.yes_leg.venue is Venue.KALSHI
    assert forward.no_leg.venue is Venue.POLYMARKET_US
    assert forward.result_class is AssessmentClass.QUALIFIED
    assert forward.net_profit == Decimal("0.8874")
    # Reverse buys Poly YES @ 0.80 and Kalshi NO @ 0.80.
    # Poly fee 0.095 * 100 * 0.80 * 0.20 = 1.52.
    # Kalshi model fee 1.12, on the 0.0001 grid, plus 0.0001 = 1.1201.
    # Capital 160 + 1.52 + 1.1201 = 162.6401. Net -62.6401.
    assert reverse.yes_leg.venue is Venue.POLYMARKET_US
    assert reverse.yes_leg.side is Side.YES
    assert reverse.no_leg.venue is Venue.KALSHI
    assert reverse.no_leg.side is Side.NO
    assert reverse.yes_result is not None and reverse.no_result is not None
    assert reverse.yes_result.avg_price == Decimal("0.80")
    assert reverse.yes_result.fee.amount == Decimal("1.52")
    assert reverse.no_result.avg_price == Decimal("0.80")
    assert reverse.no_result.fee.amount == Decimal("1.1201")
    assert reverse.capital == Decimal("162.6401")
    assert reverse.net_profit == Decimal("-62.6401")
    assert reverse.result_class is AssessmentClass.REJECTED
    assert reverse.reasons == ("not_profitable", "below_min_profit", "below_min_roc")
    assert reverse.max_profitable_qty is None
    assert forward.net_profit != reverse.net_profit


def test_multi_level_fee_is_the_sum_of_fills() -> None:
    yes, no = _pair(
        [_level("0.10", "50"), _level("0.90", "50")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
        [_level("0.10", "100")],
    )
    result = _run(yes, no)
    assert result.yes_result is not None
    assert result.yes_result.levels_consumed == [_level("0.10", "50"), _level("0.90", "50")]
    # Two on-grid fills of 0.315, plus one 0.0001 step.
    assert result.yes_result.fee.amount == Decimal("0.6301")


def test_thin_book_fails_requested_qty_and_reports_smaller_profitable_qty() -> None:
    yes, no = _pair(
        [_level("0.40", "10")],
        [_level("0.40", "10")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
    )
    result = _run(yes, no, Decimal(50))
    assert result.result_class is AssessmentClass.REJECTED
    assert result.reasons == ("insufficient_depth_kalshi",)
    assert result.yes_result is None
    assert result.net_profit is None
    assert result.capital is None
    # Size 10 @ 0.40/0.40 nets 1.6019, which clears both thresholds.
    assert result.max_profitable_qty == Decimal(10)


def test_both_books_thin_and_dusty() -> None:
    yes, no = _pair(
        [_level("0.40", "5")],
        [_level("0.40", "5")],
        [_level("0.40", "3")],
        [_level("0.40", "3")],
    )
    result = _run(yes, no, Decimal(10))
    assert result.reasons == (
        "insufficient_depth_kalshi",
        "insufficient_depth_polymarket_us",
        "dust_kalshi",
        "dust_polymarket_us",
    )
    # Touch sizes 5 and 3 are under min_touch_qty 10, so every size fails.
    assert result.max_profitable_qty is None


def test_deeper_levels_stop_clearing_the_minimum() -> None:
    # First 10 Kalshi contracts are 0.40. Later ones are 0.70 against NO at 0.40.
    # At 50 contracts the costs are 32 + 20 and the fees are 0.7561 + 1.14,
    # so net is -3.8961. Size 18 still nets 0.5043. Size 19 nets 0.3696.
    yes, no = _pair(
        [_level("0.40", "10"), _level("0.70", "100")],
        [_level("0.40", "100")],
        [_level("0.40", "200")],
        [_level("0.40", "200")],
    )
    result = _run(yes, no, Decimal(50))
    assert result.reasons == ("not_profitable", "below_min_profit", "below_min_roc")
    assert result.net_profit == Decimal("-3.8961")
    assert result.max_profitable_qty == Decimal(18)


def test_scan_limit_caps_max_profitable_qty() -> None:
    # 0.20 + 0.20 clears $0.50 at every size, so the scan limit is the cap.
    yes, no = _pair(
        [_level("0.20", "100")],
        [_level("0.20", "100")],
        [_level("0.20", "100")],
        [_level("0.20", "100")],
    )
    capped = _run(yes, no, limits=Limits(scan_limit=Decimal(40)))
    assert capped.result_class is AssessmentClass.QUALIFIED
    assert capped.max_profitable_qty == Decimal(40)
    # On the 0.45/0.50 book, size 40 nets 0.3569. A window of 40 finds no
    # size that clears $0.50, while the requested 100-lot still qualifies.
    window = _worked(limits=Limits(scan_limit=Decimal(40)))
    assert window.result_class is AssessmentClass.QUALIFIED
    assert window.net_profit == Decimal("0.8874")
    assert window.max_profitable_qty is None


def test_stale_skew_and_invalid_books() -> None:
    # The receipts are 0.5s apart: inside the 1s indicative window, past the
    # 0.25s qualified window. Polled books keep this case to staleness alone.
    stale = _worked(
        books_atomic=False,
        kalshi_monotonic=NOW - 2.5,
        poly_monotonic=NOW - 2,
    )
    assert stale.reasons == ("book_stale_kalshi",)
    assert stale.result_class is AssessmentClass.REJECTED
    assert stale.net_profit == Decimal("0.8874")
    assert stale.max_profitable_qty is None
    assert stale.yes_result is not None
    assert stale.yes_result.book_age_ms == 2500

    on_the_boundary = _worked(kalshi_monotonic=NOW - 2, poly_monotonic=NOW - 2)
    assert on_the_boundary.result_class is AssessmentClass.QUALIFIED

    skewed = _worked(poly_monotonic=NOW - 1.5)
    assert skewed.reasons == ("book_skew",)
    assert skewed.skew_ms == 1500
    assert skewed.max_profitable_qty is None

    exact_skew = _worked(books_atomic=False, poly_monotonic=NOW - 1)
    assert exact_skew.result_class is AssessmentClass.INDICATIVE

    invalid = _worked(kalshi_valid=False, poly_valid=False, kalshi_monotonic=NOW - 5)
    assert invalid.reasons == (
        "book_invalid_kalshi",
        "book_invalid_polymarket_us",
        "book_stale_kalshi",
        "book_skew",
    )


def test_wall_clock_age_counts_with_the_monotonic_age() -> None:
    fresh_wall = _worked(now_wall=RECEIVED + timedelta(seconds=2))
    assert fresh_wall.result_class is AssessmentClass.QUALIFIED

    stale_wall = _worked(now_wall=RECEIVED + timedelta(seconds=2, microseconds=1))
    assert stale_wall.reasons == ("book_stale_kalshi", "book_stale_polymarket_us")

    one_book = _worked(kalshi_received=RECEIVED - timedelta(seconds=3))
    assert one_book.reasons == ("book_stale_kalshi",)
    assert one_book.yes_result is not None
    assert one_book.yes_result.book_age_ms == 3000


def test_source_timestamp_does_not_age_the_book() -> None:
    old = datetime(2020, 1, 1, tzinfo=RECEIVED.tzinfo)
    result = _worked(kalshi_source=old, poly_source=old)
    assert result.result_class is AssessmentClass.QUALIFIED


def test_future_or_non_finite_receipt_is_invalid() -> None:
    future = _worked(kalshi_monotonic=NOW + 1)
    assert future.reasons == ("book_invalid_kalshi",)
    assert future.skew_ms is None

    future_wall = _worked(kalshi_received=RECEIVED + timedelta(seconds=1))
    assert future_wall.reasons == ("book_invalid_kalshi",)

    missing_clock = _worked(kalshi_monotonic=float("nan"))
    assert missing_clock.reasons == ("book_invalid_kalshi",)

    with pytest.raises(ValueError, match="finite"):
        _worked(now_monotonic=float("nan"))
    with pytest.raises(ValueError, match="timezone-aware"):
        _worked(now_wall=datetime(2026, 11, 11, 18))


def test_fee_unknown_per_venue_withholds_profit() -> None:
    yes, no = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        poly_fee=_poly(None),
    )
    unknown_poly = _run(yes, no)
    assert unknown_poly.reasons == ("fee_unknown_polymarket_us",)
    assert unknown_poly.capital is None
    assert unknown_poly.net_profit is None
    assert unknown_poly.roc is None
    assert unknown_poly.gross_edge == Decimal(5)
    assert unknown_poly.max_profitable_qty is None
    assert unknown_poly.yes_result is not None and unknown_poly.no_result is not None
    assert unknown_poly.yes_result.fee.amount == Decimal("1.7326")
    assert unknown_poly.yes_result.cost == Decimal(45)
    assert unknown_poly.no_result.fee.amount is None
    assert unknown_poly.no_result.cost == Decimal(50)
    assert "not_profitable" not in unknown_poly.reasons

    yes_unknown, no_known = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        kalshi_fee=_kalshi(multiplier=None),
    )
    unknown_kalshi = _run(yes_unknown, no_known)
    assert unknown_kalshi.reasons == ("fee_unknown_kalshi",)
    assert unknown_kalshi.no_result is not None
    assert unknown_kalshi.no_result.fee.amount == Decimal("2.38")
    assert unknown_kalshi.capital is None


def test_thresholds() -> None:
    one = _worked(qty=Decimal(1))
    # q=1 Kalshi net fee is 0.0174 plus the 0.0001 margin = 0.0175.
    # Poly 0.02375 rounds half-even to 0.02. Capital 0.9875. Net 0.0125.
    assert one.net_profit == Decimal("0.0125")
    assert one.reasons == ("below_min_profit",)
    assert one.result_class is AssessmentClass.REJECTED

    high_roc = _worked(limits=Limits(min_roc=Decimal("0.02")))
    assert high_roc.reasons == ("below_min_roc",)

    losing = _pair(
        [_level("0.60", "100")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
    )
    priced_out = _run(*losing)
    # Kalshi fee 1.6801, Poly fee 2.28. Capital 123.9601. Net -23.9601.
    assert priced_out.net_profit == Decimal("-23.9601")
    assert priced_out.reasons == ("not_profitable", "below_min_profit", "below_min_roc")
    assert priced_out.max_profitable_qty is None


def test_dust_touch_rejects_even_when_depth_can_fill() -> None:
    yes, no = _pair(
        [_level("0.40", "9"), _level("0.41", "100")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
    )
    result = _run(yes, no, Decimal(20))
    assert result.reasons == ("dust_kalshi",)
    assert result.net_profit is not None
    assert result.max_profitable_qty is None

    touched = _pair(
        [_level("0.40", "10")],
        [_level("0.40", "10")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
    )
    assert "dust_kalshi" not in _run(*touched, Decimal(10)).reasons


def test_invalid_ladder_and_invalid_capital() -> None:
    yes, no = _pair(
        [_level("0.50", "1"), _level("0.40", "1")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
    )
    invalid = _run(yes, no, Decimal(1))
    assert invalid.reasons == ("book_invalid_kalshi",)
    assert invalid.yes_result is None
    assert invalid.no_result is not None
    assert invalid.max_profitable_qty is None

    class _Credit:
        def taker_fee(self, levels, side) -> FeeQuote:
            return FeeQuote(amount=Decimal(-100), provenance="test credit")

    priced, other = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )
    credited = _run(
        LegInput(priced.leg, priced.book, _Credit()),
        LegInput(other.leg, other.book, _Credit()),
    )
    # Costs 95 plus two -100 credits is -105. Gross edge stays 5.
    assert credited.reasons == ("invalid_capital",)
    assert credited.capital is None
    assert credited.net_profit is None
    assert credited.roc is None
    assert credited.gross_edge == Decimal(5)
    assert credited.yes_result is not None
    assert credited.max_profitable_qty is None


def test_books_atomic_chooses_qualified_or_indicative() -> None:
    polled = _worked(books_atomic=False)
    assert polled.result_class is AssessmentClass.INDICATIVE
    assert polled.reasons == ()
    assert polled.net_profit == Decimal("0.8874")

    atomic = _worked(books_atomic=True)
    assert atomic.result_class is AssessmentClass.QUALIFIED

    stale_polled = _worked(books_atomic=False, kalshi_monotonic=NOW - 2.5)
    assert stale_polled.result_class is AssessmentClass.REJECTED


def test_qualified_skew_threshold_is_tighter_than_indicative() -> None:
    half_second = {"poly_monotonic": NOW - 0.5}
    atomic = _worked(books_atomic=True, **half_second)
    assert atomic.reasons == ("book_skew",)
    assert atomic.skew_ms == 500
    polled = _worked(books_atomic=False, **half_second)
    assert polled.result_class is AssessmentClass.INDICATIVE
    assert polled.reasons == ()

    at_qualified_limit = _worked(poly_monotonic=NOW - 0.25)
    assert at_qualified_limit.result_class is AssessmentClass.QUALIFIED

    over_indicative = _worked(books_atomic=False, poly_monotonic=NOW - 1.5)
    assert over_indicative.reasons == ("book_skew",)


def test_max_profitable_qty_checks_past_an_unprofitable_size() -> None:
    # Kalshi coefficient 0 at 0.10: model fee 0 and revenue on the 0.0001
    # grid, so the charge is the constant 0.0001 margin.
    # Poly theta 0.0695 at 0.893. Exact fee per contract is 0.0066407945
    # and the gross edge per contract is 0.007. The cent cap stays $0.03
    # from q=4 to q=5, so net crosses zero twice:
    # q=1 -0.0031, q=2 +0.0039, q=4 -0.0021, q=5 +0.0049.
    limits = Limits(
        min_profit=Decimal(0),
        min_roc=Decimal(0),
        min_touch_qty=Decimal(1),
        scan_limit=Decimal(5),
    )
    yes, no = _pair(
        [_level("0.10", "5")],
        [_level("0.10", "5")],
        [_level("0.893", "5")],
        [_level("0.893", "5")],
        kalshi_fee=_kalshi(coefficient=Decimal(0)),
        poly_fee=_poly(Decimal("0.0695")),
    )
    at_four = _run(yes, no, Decimal(4), limits=limits)
    assert at_four.net_profit == Decimal("-0.0021")
    assert at_four.reasons == ("not_profitable", "below_min_profit", "below_min_roc")
    assert at_four.max_profitable_qty == Decimal(5)
    assert at_four.yes_result is not None and at_four.no_result is not None
    assert at_four.yes_result.fee.amount == DIRECT
    assert at_four.no_result.fee.amount == Decimal("0.03")

    at_one = _run(yes, no, Decimal(1), limits=limits)
    assert at_one.net_profit == Decimal("-0.0031")
    assert at_one.max_profitable_qty == Decimal(5)

    at_two = _run(yes, no, Decimal(2), limits=limits)
    assert at_two.result_class is AssessmentClass.QUALIFIED
    assert at_two.net_profit == Decimal("0.0039")
    assert at_two.max_profitable_qty == Decimal(5)

    at_five = _run(yes, no, Decimal(5), limits=limits)
    assert at_five.result_class is AssessmentClass.QUALIFIED
    assert at_five.net_profit == Decimal("0.0049")


def test_group_skips_unrepresentable_flips_and_same_venue() -> None:
    both_yes = _group((_member(Venue.KALSHI, "KX-1"), _member(Venue.POLYMARKET_US, "PM-1")))
    paired = _group_assess(both_yes, *_library(both_yes.members))
    assert [(item.yes_leg.market_id, item.no_leg.market_id) for item in paired] == [
        ("KX-1", "PM-1"),
        ("PM-1", "KX-1"),
    ]
    assert all(item.yes_leg.venue != item.no_leg.venue for item in paired)

    both_no = _group(
        (
            _member(Venue.KALSHI, "KX-1", Side.NO),
            _member(Venue.POLYMARKET_US, "PM-1", Side.NO),
        )
    )
    flipped = _group_assess(both_no, *_library(both_no.members))
    # Happens is a NO buy. The YES leg is the other member's complement.
    assert [(item.yes_leg.market_id, item.no_leg.market_id) for item in flipped] == [
        ("PM-1", "KX-1"),
        ("KX-1", "PM-1"),
    ]

    opposite = _group(
        (
            _member(Venue.KALSHI, "KX-1", Side.YES),
            _member(Venue.POLYMARKET_US, "PM-1", Side.NO),
        )
    )
    assert _group_assess(opposite, *_library(opposite.members)) == []

    duplicate = EquivalenceGroup.model_construct(
        group_id="g-dup",
        outcome="duplicate venue injected",
        members=(
            _member(Venue.KALSHI, "KX-1"),
            _member(Venue.KALSHI, "KX-2"),
            _member(Venue.POLYMARKET_US, "PM-1"),
        ),
        status=GroupStatus.CANDIDATE,
        hash_version=1,
        exception_risk=True,
        exception_table=(),
        reviewer=None,
        decided_utc=None,
        notes="",
    )
    injected = _group_assess(duplicate, *_library(duplicate.members))
    assert [(item.yes_leg.market_id, item.no_leg.market_id) for item in injected] == [
        ("KX-1", "PM-1"),
        ("KX-2", "PM-1"),
        ("PM-1", "KX-1"),
        ("PM-1", "KX-2"),
    ]
    assert all(item.exception_risk is True for item in injected)
    assert all(item.yes_leg.venue != item.no_leg.venue for item in injected)


def test_three_member_group_yields_six_ordered_pairs() -> None:
    members = (
        _member(Venue.KALSHI, "KX-1"),
        _member(Venue.POLYMARKET_US, "PM-1"),
        _member(Venue.GEMINI, "G-1"),
    )
    group = _group(members, exception_risk=True)
    results = _group_assess(group, *_library(members))
    assert [(item.yes_leg.market_id, item.no_leg.market_id) for item in results] == [
        ("KX-1", "PM-1"),
        ("KX-1", "G-1"),
        ("PM-1", "KX-1"),
        ("PM-1", "G-1"),
        ("G-1", "KX-1"),
        ("G-1", "PM-1"),
    ]
    assert all(item.yes_leg.side is Side.YES and item.no_leg.side is Side.NO for item in results)
    assert all(item.exception_risk is True for item in results)

    one_flipped = _group(
        (
            _member(Venue.KALSHI, "KX-1", Side.YES),
            _member(Venue.POLYMARKET_US, "PM-1", Side.YES),
            _member(Venue.GEMINI, "G-1", Side.NO),
        )
    )
    subset = _group_assess(one_flipped, *_library(one_flipped.members))
    assert [(item.yes_leg.market_id, item.no_leg.market_id) for item in subset] == [
        ("KX-1", "PM-1"),
        ("PM-1", "KX-1"),
    ]


def test_missing_book_and_missing_fee_model() -> None:
    group = _group((_member(Venue.KALSHI, "KX-1"), _member(Venue.POLYMARKET_US, "PM-1")))
    books, fees = _library(group.members)
    books["PM-1"] = books.pop("polymarket_us:PM-1")
    missing = _group_assess(group, books, fees)
    assert len(missing) == 2
    assert all(item.reasons == ("missing_book_polymarket_us",) for item in missing)
    assert all(item.result_class is AssessmentClass.REJECTED for item in missing)
    assert all(item.capital is None and item.yes_result is None for item in missing)

    books, fees = _library(group.members)
    books["polymarket_us:PM-1"] = make_book(
        Venue.POLYMARKET_US,
        "PM-1",
        [_level("0.60", "10")],
        [_level("0.50", "10")],
        received_monotonic=NOW,
    )
    books["kalshi:KX-1"] = make_book(
        Venue.KALSHI,
        "KX-1",
        [_level("0.45", "10")],
        [_level("0.40", "10")],
        received_monotonic=NOW,
    )
    del fees["polymarket_us:PM-1"]
    unknown = assess_group(
        group,
        books,
        fees,
        Decimal(10),
        now_monotonic=NOW,
        now_wall=RECEIVED,
        books_atomic=True,
        model_version="pr1-test",
        limits=Limits(scan_limit=Decimal(10)),
    )
    assert unknown[0].reasons == ("fee_unknown_polymarket_us",)
    assert unknown[0].gross_edge == Decimal("0.5")
    assert unknown[0].capital is None
    assert unknown[0].yes_result is not None
    assert (
        unknown[0].yes_result.fee.amount
        == _kalshi().taker_fee([_level("0.45", "10")], Side.YES).amount
    )


def test_inputs_reject_bad_types() -> None:
    yes, no = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )
    with pytest.raises(TypeError):
        _run(yes, no, qty=100)  # type: ignore[arg-type]
    other = make_book(
        Venue.KALSHI,
        "KX-2",
        [_level("0.40", "100")],
        [_level("0.40", "100")],
        received_monotonic=NOW,
    )
    with pytest.raises(ValueError, match="different venues"):
        _run(yes, _input(Venue.KALSHI, "KX-2", Side.NO, other, _kalshi()))


def _member(venue: Venue, market_id: str, side: Side = Side.YES) -> GroupMember:
    return GroupMember(venue=venue, market_id=market_id, native_side_for_outcome=side)


def _group(members: tuple[GroupMember, ...], **overrides) -> EquivalenceGroup:
    payload = {
        "group_id": "g1",
        "outcome": "NE beats DET, OT included",
        "members": members,
        "status": GroupStatus.CANDIDATE,
        "exception_risk": False,
    }
    payload.update(overrides)
    return EquivalenceGroup(**payload)


def _library(members: tuple[GroupMember, ...], price: str = "0.20", size: str = "100"):
    books = {}
    fees = {}
    level = _level(price, size)
    for member in members:
        key = f"{member.venue.value}:{member.market_id}"
        books[key] = make_book(
            member.venue,
            member.market_id,
            [level],
            [level],
            received_monotonic=NOW,
        )
        fees[key] = _kalshi() if member.venue is Venue.KALSHI else _poly()
    return books, fees


def _group_assess(group, books, fees):
    return assess_group(
        group,
        books,
        fees,
        Decimal(1),
        now_monotonic=NOW,
        now_wall=RECEIVED,
        books_atomic=True,
        model_version="pr1-test",
        limits=Limits(scan_limit=Decimal(1)),
    )
