"""Worked example and qualification rules."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pmarb.calculator import assess, assess_both
from pmarb.fees import FeeConfig
from pmarb.models import Level, Venue
from tests.conftest import NOW, make_book, standard_fees


def _level(price: str, size: str) -> Level:
    return Level(price=Decimal(price), size=Decimal(size))


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
    kalshi_reason: str | None = None,
    poly_reason: str | None = None,
    kalshi_source: datetime | None = None,
    poly_source: datetime | None = None,
):
    kalshi = make_book(
        Venue.KALSHI,
        "KX-1",
        kalshi_yes,
        kalshi_no,
        received_monotonic=kalshi_monotonic,
        valid=kalshi_valid,
        invalid_reason=kalshi_reason,
        source_ts=kalshi_source,
    )
    poly = make_book(
        Venue.POLYMARKET_US,
        "PM-1",
        poly_yes,
        poly_no,
        received_monotonic=poly_monotonic,
        valid=poly_valid,
        invalid_reason=poly_reason,
        source_ts=poly_source,
    )
    return kalshi, poly


def test_worked_example_direction_a() -> None:
    kalshi, poly = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )

    assert result.qualified is True
    assert result.rejection_reasons == []
    assert result.legs is not None
    kalshi_leg, poly_leg = result.legs
    assert kalshi_leg.venue is Venue.KALSHI
    assert kalshi_leg.side.value == "yes"
    assert kalshi_leg.cost == Decimal(45)
    assert kalshi_leg.avg_price == Decimal("0.45")
    assert kalshi_leg.fee == Decimal("1.7325")
    assert poly_leg.venue is Venue.POLYMARKET_US
    assert poly_leg.side.value == "no"
    assert poly_leg.cost == Decimal(50)
    assert poly_leg.fee == Decimal("2.38")
    assert result.capital == Decimal("99.1125")
    assert result.net_profit == Decimal("0.8875")
    assert result.roc == Decimal("0.8875") / Decimal("99.1125")
    assert result.roc is not None
    assert result.roc.quantize(Decimal("0.000001")) == Decimal("0.008954")
    assert result.max_profitable_qty == Decimal(100)
    assert result.fee_model_version == "phase1-test"
    assert result.extra_costs == Decimal(0)
    assert result.computed_utc.tzinfo is not None


def test_multi_level_fee_is_not_the_vwap_fee() -> None:
    kalshi, poly = _pair(
        [_level("0.10", "50"), _level("0.90", "50")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
        [_level("0.10", "100")],
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert result.legs is not None
    assert result.legs[0].fee == Decimal("0.63")
    assert result.legs[0].levels_consumed == [_level("0.10", "50"), _level("0.90", "50")]


def test_thin_book_fails_requested_qty_and_reports_smaller_profitable_qty() -> None:
    kalshi, poly = _pair(
        [_level("0.40", "10")],
        [_level("0.40", "10")],
        [_level("0.40", "100")],
        [_level("0.40", "100")],
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(50),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert result.qualified is False
    assert result.rejection_reasons == ["insufficient_depth_kalshi"]
    assert result.legs is None
    assert result.net_profit is None
    assert result.capital is None
    assert result.max_profitable_qty == Decimal(10)


def test_deeper_levels_stop_being_profitable_before_depth_runs_out() -> None:
    kalshi, poly = _pair(
        [_level("0.40", "10"), _level("0.70", "100")],
        [_level("0.40", "100")],
        [_level("0.40", "200")],
        [_level("0.40", "200")],
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(50),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert "insufficient_depth_kalshi" not in result.rejection_reasons
    assert result.qualified is False
    assert result.rejection_reasons == [
        "not_profitable",
        "below_min_profit",
        "below_min_roc",
    ]
    assert result.max_profitable_qty == Decimal(21)
    assert result.net_profit is not None
    assert result.net_profit < 0


def test_scan_limit_caps_max_profitable_qty() -> None:
    kalshi, poly = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
        scan_limit=Decimal(40),
    )
    assert result.qualified is True
    assert result.max_profitable_qty == Decimal(40)


def test_stale_skew_and_invalid_books_collect_reasons() -> None:
    fresh_source = datetime(2026, 11, 11, 18, tzinfo=UTC)
    stale_kalshi, fresh_poly = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        kalshi_monotonic=NOW - 2.5,
        poly_monotonic=NOW - 2,
        kalshi_source=fresh_source,
    )
    stale = assess(
        "pair-1",
        "A",
        Decimal(100),
        stale_kalshi,
        fresh_poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert stale.rejection_reasons == ["book_stale_kalshi"]
    assert stale.qualified is False
    assert stale.net_profit == Decimal("0.8875")

    same_time = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        kalshi_monotonic=NOW - 2,
        poly_monotonic=NOW - 2,
    )
    on_the_age_boundary = assess(
        "pair-1",
        "A",
        Decimal(100),
        same_time[0],
        same_time[1],
        standard_fees(),
        now_monotonic=NOW,
    )
    assert on_the_age_boundary.qualified is True

    skewed = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        kalshi_monotonic=NOW,
        poly_monotonic=NOW - 1.5,
    )
    skew_result = assess(
        "pair-1",
        "A",
        Decimal(100),
        skewed[0],
        skewed[1],
        standard_fees(),
        now_monotonic=NOW,
    )
    assert skew_result.rejection_reasons == ["book_skew"]

    exact_skew = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        kalshi_monotonic=NOW,
        poly_monotonic=NOW - 1,
    )
    exact = assess(
        "pair-1",
        "A",
        Decimal(100),
        exact_skew[0],
        exact_skew[1],
        standard_fees(),
        now_monotonic=NOW,
    )
    assert exact.qualified is True

    invalid = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        kalshi_valid=False,
        kalshi_reason="sequence_gap",
        poly_valid=False,
        poly_reason="disconnected",
        kalshi_monotonic=NOW - 5,
        poly_monotonic=NOW,
    )
    invalid_result = assess(
        "pair-1",
        "A",
        Decimal(100),
        invalid[0],
        invalid[1],
        standard_fees(),
        now_monotonic=NOW,
    )
    assert invalid_result.rejection_reasons == [
        "book_invalid_kalshi",
        "book_invalid_polymarket_us",
        "book_stale_kalshi",
        "book_skew",
    ]
    assert invalid_result.qualified is False


def test_old_source_timestamp_does_not_make_a_fresh_receipt_stale() -> None:
    kalshi, poly = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
        kalshi_source=datetime(2020, 1, 1, tzinfo=UTC),
        poly_source=datetime(2020, 1, 1, tzinfo=UTC),
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert result.qualified is True


def test_fee_unknown_is_scenario_only() -> None:
    kalshi, poly = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(theta=None),
        now_monotonic=NOW,
    )
    assert result.qualified is False
    assert result.rejection_reasons == ["fee_unknown"]
    assert result.net_profit is None
    assert result.capital is None
    assert result.roc is None
    assert result.max_profitable_qty is None
    assert result.legs is not None
    assert result.legs[0].fee == Decimal("1.7325")
    assert result.legs[0].cost == Decimal(45)
    assert result.legs[1].fee is None
    assert result.legs[1].cost == Decimal(50)
    assert "not_profitable" not in result.rejection_reasons


def test_thresholds_and_extra_costs() -> None:
    kalshi, poly = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )
    one = assess(
        "pair-1",
        "A",
        Decimal(1),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert one.net_profit == Decimal("0.0126")
    assert one.rejection_reasons == ["below_min_profit"]
    assert one.qualified is False

    high_roc = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
        min_roc=Decimal("0.02"),
    )
    assert high_roc.rejection_reasons == ["below_min_roc"]

    priced_out = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(),
        extra_costs=Decimal("0.8875"),
        now_monotonic=NOW,
    )
    assert priced_out.net_profit == Decimal(0)
    assert priced_out.capital == Decimal(100)
    assert priced_out.rejection_reasons == [
        "not_profitable",
        "below_min_profit",
        "below_min_roc",
    ]
    assert priced_out.max_profitable_qty is None


def test_both_directions_are_independent() -> None:
    kalshi, poly = _pair(
        [_level("0.45", "100")],
        [_level("0.80", "100")],
        [_level("0.80", "100")],
        [_level("0.50", "100")],
    )
    fees = FeeConfig(
        kalshi_multiplier=Decimal(1),
        kalshi_rounding="direct_member",
        polyus_theta=Decimal("0.095"),
        version="phase1-test",
    )
    direction_a, direction_b = assess_both(
        "pair-1",
        Decimal(100),
        kalshi,
        poly,
        fees,
        now_monotonic=NOW,
    )
    assert direction_a.direction == "A"
    assert direction_b.direction == "B"
    assert direction_a.qualified is True
    assert direction_a.net_profit == Decimal("0.8875")
    assert direction_b.qualified is False
    assert direction_b.rejection_reasons == [
        "not_profitable",
        "below_min_profit",
        "below_min_roc",
    ]
    assert direction_b.legs is not None
    assert direction_b.legs[0].venue is Venue.POLYMARKET_US
    assert direction_b.legs[0].side.value == "yes"
    assert direction_b.legs[0].avg_price == Decimal("0.80")
    assert direction_b.legs[1].venue is Venue.KALSHI
    assert direction_b.legs[1].side.value == "no"
    assert direction_b.legs[1].avg_price == Decimal("0.80")
    assert direction_a.net_profit != direction_b.net_profit


def test_future_or_non_finite_receipt_is_not_qualified() -> None:
    levels = (
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )
    future = _pair(*levels, kalshi_monotonic=NOW + 1)
    future_result = assess(
        "pair-1",
        "A",
        Decimal(100),
        future[0],
        future[1],
        standard_fees(),
        now_monotonic=NOW,
    )
    assert future_result.qualified is False
    assert future_result.rejection_reasons == ["book_invalid_kalshi"]

    missing_clock = _pair(*levels, kalshi_monotonic=float("nan"))
    nan_result = assess(
        "pair-1",
        "A",
        Decimal(100),
        missing_clock[0],
        missing_clock[1],
        standard_fees(),
        now_monotonic=NOW,
    )
    assert nan_result.qualified is False
    assert "book_invalid_kalshi" in nan_result.rejection_reasons

    with pytest.raises(ValueError, match="finite"):
        assess(
            "pair-1",
            "A",
            Decimal(100),
            future[0],
            future[1],
            standard_fees(),
            now_monotonic=float("nan"),
        )


def test_both_venues_can_be_thin() -> None:
    kalshi, poly = _pair(
        [_level("0.40", "5")],
        [_level("0.40", "5")],
        [_level("0.40", "3")],
        [_level("0.40", "3")],
    )
    result = assess(
        "pair-1",
        "A",
        Decimal(10),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert result.rejection_reasons == [
        "insufficient_depth_kalshi",
        "insufficient_depth_polymarket_us",
    ]
    assert result.max_profitable_qty == Decimal(3)


def test_non_positive_capital_rejects_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid ladders cannot produce capital <= 0, so the fee functions are stubbed."""
    kalshi, poly = _pair(
        [_level("0.45", "100")],
        [_level("0.40", "100")],
        [_level("0.60", "100")],
        [_level("0.50", "100")],
    )

    def _negative_fee(*_args: object, **_kwargs: object) -> Decimal:
        return Decimal(-100)

    monkeypatch.setattr("pmarb.calculator.kalshi_taker_fee", _negative_fee)
    monkeypatch.setattr("pmarb.calculator.polyus_taker_fee", _negative_fee)
    result = assess(
        "pair-1",
        "A",
        Decimal(100),
        kalshi,
        poly,
        standard_fees(),
        now_monotonic=NOW,
    )
    assert result.qualified is False
    assert result.capital is None
    assert result.net_profit is None
    assert result.roc is None
    assert result.rejection_reasons == ["invalid_capital"]
    assert result.legs is not None
