"""Synthetic, typed-writer coverage for the local M1 analysis report."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pmarb.analysis.metrics import build_metrics
from pmarb.models import (
    AssessmentClass,
    Episode,
    EpisodeTermination,
    FeeQuote,
    Leg,
    LegResult,
    Level,
    Market,
    OpportunityAssessment,
    Side,
    Venue,
)
from pmarb.storage.db import (
    insert_assessment,
    insert_collector_event,
    open_writer,
    upsert_episode,
    upsert_market,
)

T0 = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def _market(venue: Venue, market_id: str, *, start: datetime) -> Market:
    return Market(
        venue=venue,
        market_id=market_id,
        title=f"Synthetic {market_id}",
        league="nfl",
        participants=("SYN_A", "SYN_B"),
        yes_participant="SYN_A",
        market_type="winner",
        event_start_utc=start,
        status="active",
        rules_text="Synthetic test rules",
        rules_hash=f"synthetic-{market_id}",
    )


def _assessment(
    *,
    group: str,
    stamp: datetime,
    gross: str,
    net: str | None,
    skew: int,
    qty: str = "10",
    result_class: AssessmentClass = AssessmentClass.INDICATIVE,
    exception_risk: bool = False,
    fee_unknown: bool = False,
    yes_price: str = "0.12345678901234567890",
) -> OpportunityAssessment:
    yes_leg = Leg(venue=Venue.KALSHI, market_id=f"k-{group}", side=Side.YES)
    no_leg = Leg(venue=Venue.POLYMARKET_US, market_id=f"p-{group}", side=Side.NO)
    fee = FeeQuote(
        amount=None if fee_unknown else Decimal("0.001"),
        provenance="fee_unknown" if fee_unknown else "synthetic",
    )
    yes = LegResult(
        leg=yes_leg,
        levels_consumed=[],
        quantity=Decimal(qty),
        cost=Decimal(yes_price) * Decimal(qty),
        avg_price=Decimal(yes_price),
        fee=fee,
    )
    no = LegResult(
        leg=no_leg,
        levels_consumed=[],
        quantity=Decimal(qty),
        cost=Decimal("0.4") * Decimal(qty),
        avg_price=Decimal("0.4"),
        fee=fee,
    )
    return OpportunityAssessment(
        group_id=group,
        yes_leg=yes_leg,
        no_leg=no_leg,
        requested_qty=Decimal(qty),
        yes_result=yes,
        no_result=no,
        capital=Decimal("1.234567890123456789") if net is not None else None,
        gross_edge=Decimal(gross),
        net_profit=None if net is None else Decimal(net),
        roc=None if net is None else Decimal(net) / Decimal("1.234567890123456789"),
        max_profitable_qty=Decimal("37.5"),
        skew_ms=skew,
        books_atomic=False,
        exception_risk=exception_risk,
        result_class=result_class,
        reasons=("polling_skew",),
        model_version="synthetic-test",
        computed_utc=stamp,
    )


@pytest.fixture
def synthetic_db(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    writer = open_writer(path)
    # Two games are in progress at their assessment times; one is pregame.
    starts = {
        "live-a": T0 - timedelta(minutes=1),
        "live-b": T0 - timedelta(minutes=2),
        "future": T0 + timedelta(hours=1),
    }
    for group, start in starts.items():
        upsert_market(writer, _market(Venue.KALSHI, f"k-{group}", start=start), T0)
        upsert_market(writer, _market(Venue.POLYMARKET_US, f"p-{group}", start=start), T0)

    rows = [
        # Four exact skew bucket boundaries and a value above the final bucket.
        _assessment(
            group="live-a",
            stamp=T0,
            gross="0.100000000000000001",
            net="0.080000000000000001",
            skew=0,
        ),
        _assessment(
            group="live-a",
            stamp=T0 + timedelta(seconds=1),
            gross="0.2",
            net="0.1",
            skew=100,
            exception_risk=True,
        ),
        _assessment(
            group="live-b",
            stamp=T0 + timedelta(seconds=2),
            gross="-0.01",
            net="-0.02",
            skew=250,
            fee_unknown=True,
        ),
        _assessment(
            group="live-b", stamp=T0 + timedelta(seconds=3), gross="0.03", net=None, skew=500
        ),
        _assessment(group="future", stamp=T0, gross="0.04", net="0.02", skew=1000, qty="1"),
    ]
    for row in rows:
        insert_assessment(writer, row)

    # First and last health events define a deterministic 100 second run window.
    insert_collector_event(writer, T0, "kalshi", "health", {"state": "ok", "fresh": True})
    insert_collector_event(writer, T0, "polymarket_us", "health", {"state": "ok"})
    insert_collector_event(
        writer, T0 + timedelta(seconds=5), "kalshi", "gap", {"kind": "maintenance"}
    )
    insert_collector_event(
        writer, T0 + timedelta(seconds=10), "kalshi", "gap", {"kind": "data_gap"}
    )
    insert_collector_event(writer, T0 + timedelta(seconds=15), "kalshi", "restart", {})
    insert_collector_event(
        writer, T0 + timedelta(seconds=20), "polymarket_us", "http", {"status_code": 429}
    )
    insert_collector_event(
        writer, T0 + timedelta(seconds=30), "polymarket_us", "error", {"type": "synthetic"}
    )
    insert_collector_event(writer, T0 + timedelta(seconds=100), "kalshi", "health", {"state": "ok"})
    insert_collector_event(
        writer, T0 + timedelta(seconds=100), "polymarket_us", "health", {"state": "ok"}
    )

    # Six synthetic games ensure concentration is hand-computable and not one-event driven.
    for index, duration in enumerate((10, 20, 30, 40, 50, 60)):
        group = f"episode-{index}"
        yes_leg = Leg(venue=Venue.KALSHI, market_id=f"k-{group}", side=Side.YES)
        no_leg = Leg(venue=Venue.POLYMARKET_US, market_id=f"p-{group}", side=Side.NO)
        upsert_episode(
            writer,
            Episode(
                group_id=group,
                yes_leg=yes_leg,
                no_leg=no_leg,
                qty=Decimal(10),
                result_class=AssessmentClass.INDICATIVE,
                model_version="synthetic-test",
                first_seen_utc=T0,
                last_seen_utc=T0 + timedelta(seconds=duration),
                observations=2,
                peak_net=Decimal("0.25"),
                min_size=Decimal(5),
                max_size=Decimal(15),
                termination=EpisodeTermination.DATA_GAP,
            ),
        )
    writer.close()
    return path


def test_metrics_are_exact_and_partitioned_by_skew_live_status_and_class(synthetic_db):
    from pmarb.storage.db import open_reader

    conn = open_reader(synthetic_db)
    try:
        report = build_metrics(conn)
    finally:
        conn.close()

    assert report["window"]["duration_seconds"] == Decimal(100)
    assert report["window"]["start_utc"].endswith("06:00:00Z")
    assert report["window"]["end_utc"].endswith("06:01:40Z")
    assert report["opportunities"]["overall"]["assessments"] == 5
    assert report["opportunities"]["overall"]["gross_edge"]["max"] == Decimal("0.2")
    assert report["opportunities"]["overall"]["gross_edge"]["p50"] == Decimal("0.04")
    assert report["opportunities"]["overall"]["gross_edge"]["p90"] == Decimal("0.2")
    assert report["opportunities"]["overall"]["gross_edge"]["p99"] == Decimal("0.2")
    assert report["opportunities"]["overall"]["net_profit"]["p50"] == Decimal(
        "0.0500000000000000005"
    )
    assert report["opportunities"]["overall"]["qty_profitability"]["1"][
        "net_positive_fraction"
    ] == Decimal(1)
    assert report["opportunities"]["overall"]["qty_profitability"]["10"][
        "net_positive_fraction"
    ] == Decimal(2) / Decimal(3)
    assert (
        report["opportunities"]["overall"]["qty_profitability"]["100"]["net_positive_fraction"]
        is None
    )
    assert report["opportunities"]["live_pregame"]["live"]["assessments"] == 4
    assert report["opportunities"]["live_pregame"]["pregame"]["assessments"] == 1
    assert report["opportunities"]["skew_buckets"]["0-100ms"]["assessments"] == 1
    assert report["opportunities"]["skew_buckets"]["100-250ms"]["assessments"] == 1
    assert report["opportunities"]["skew_buckets"]["250-500ms"]["assessments"] == 1
    assert report["opportunities"]["skew_buckets"]["500-1000ms"]["assessments"] == 1
    assert report["opportunities"]["skew_buckets"]["missing_or_out_of_range"] == 1
    assert report["verdict_inputs"]["skew_gt_250ms_share"] == Decimal(1) / Decimal(3)
    assert report["verdict_inputs"]["all_assessments_skew_gt_250ms_share"] == Decimal("0.4")
    assert report["opportunities"]["skew_buckets"]["0-100ms"]["net_edge_per_contract"][
        "max"
    ] == Decimal("0.0080000000000000001")
    assert report["coverage"]["gaps_by_kind"]["maintenance"] == 1
    assert report["coverage"]["gaps_by_kind"]["data_gap"] == 1
    assert report["coverage"]["gaps_by_kind"]["restart"] == 1
    assert report["coverage"]["http_429_count"] == 1
    assert report["coverage"]["error_count"] == 1
    assert report["coverage"]["venues"]["kalshi"]["fresh_sample_fraction"] == Decimal("0.5")
    assert report["coverage"]["venues"]["kalshi"]["fresh_pct"] == Decimal(100)
    assert report["coverage"]["venues"]["polymarket_us"]["fresh_pct"] is None
    assert report["fees"]["fee_unknown_count"] == 1
    assert report["fees"]["fee_unknown_share"] == Decimal("0.2")
    assert report["exception_risk"]["count"] == 1
    assert report["exception_risk"]["share"] == Decimal("0.2")
    assert report["opportunities"]["both_directions_sanity"]["available"] is False
    assert report["fees"]["scenario"]["available"] is False
    assert report["verdict_inputs"]["live_net_positive_qty10_fraction"] is None


def test_episode_statistics_and_filters(synthetic_db):
    from pmarb.storage.db import open_reader

    conn = open_reader(synthetic_db)
    try:
        all_metrics = build_metrics(conn)
        narrowed = build_metrics(
            conn, since=T0 + timedelta(seconds=1), until=T0 + timedelta(seconds=2)
        )
    finally:
        conn.close()
    episode_metrics = all_metrics["episodes"]
    assert episode_metrics["overall"]["count"] == 6
    assert episode_metrics["overall"]["duration_seconds"]["p50"] == Decimal(35)
    assert episode_metrics["overall"]["duration_seconds"]["max"] == Decimal(60)
    assert episode_metrics["termination_reasons"]["data_gap"] == 6
    assert episode_metrics["top5_game_share"] == Decimal(5) / Decimal(6)
    assert narrowed["opportunities"]["overall"]["assessments"] == 2


def test_subsecond_offset_filters_and_empty_reversed_interval(synthetic_db):
    from pmarb.storage.db import open_reader

    conn = open_reader(synthetic_db)
    try:
        offset = build_metrics(
            conn,
            since="2026-11-01T01:00:01-05:00",
            until="2026-11-01T06:00:02.500000Z",
        )
        with pytest.raises(ValueError, match="since must be at or before until"):
            build_metrics(
                conn,
                since="2026-11-01T06:00:03Z",
                until="2026-11-01T06:00:01Z",
            )
    finally:
        conn.close()
    assert offset["opportunities"]["overall"]["assessments"] == 2
    assert offset["window"]["duration_seconds"] is None


def test_empty_database_has_explicit_undefined_metrics(tmp_path):
    from pmarb.storage.db import open_reader, open_writer

    path = tmp_path / "empty.sqlite"
    open_writer(path).close()
    conn = open_reader(path)
    try:
        report = build_metrics(conn)
    finally:
        conn.close()
    assert report["opportunities"]["overall"]["assessments"] == 0
    assert report["window"]["duration_seconds"] is None
    assert report["episodes"]["overall"]["count"] == 0
    assert report["fees"]["fee_unknown_share"] is None
    assert report["fees"]["scenario"]["repricable_assessments"] == 0
    assert report["opportunities"]["both_directions_sanity"]["matched_pairs"] == 0


def test_unknown_event_start_is_not_misclassified_and_reader_stays_read_only(tmp_path):
    from pmarb.storage.db import open_reader

    path = tmp_path / "unknown-start.sqlite"
    writer = open_writer(path)
    for venue, market_id in ((Venue.KALSHI, "k-unknown"), (Venue.POLYMARKET_US, "p-unknown")):
        upsert_market(writer, _market(venue, market_id, start=None), T0)
    insert_assessment(
        writer, _assessment(group="unknown", stamp=T0, gross="0.1", net="0.05", skew=10)
    )
    writer.close()
    conn = open_reader(path)
    before = conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM assessments")
    assert conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == before
    try:
        report = build_metrics(conn)
    finally:
        conn.close()
    assert report["opportunities"]["live_pregame"]["unknown"]["assessments"] == 1
    assert report["opportunities"]["live_pregame"]["live"]["assessments"] == 0
    assert report["opportunities"]["live_pregame"]["pregame"]["assessments"] == 0


def test_uptime_fresh_pct_uses_full_window_with_partial_known_state(tmp_path):
    from pmarb.storage.db import open_reader

    path = tmp_path / "partial-health.sqlite"
    writer = open_writer(path)
    insert_collector_event(writer, T0, "kalshi", "health", {"fresh": True})
    insert_collector_event(writer, T0 + timedelta(seconds=50), "kalshi", "health", {"state": "ok"})
    insert_collector_event(
        writer, T0 + timedelta(seconds=100), "polymarket_us", "health", {"state": "ok"}
    )
    writer.close()
    conn = open_reader(path)
    try:
        report = build_metrics(conn)
    finally:
        conn.close()
    venue = report["coverage"]["venues"]["kalshi"]
    assert venue["fresh_seconds"] == Decimal(50)
    assert venue["known_state_seconds"] == Decimal(50)
    assert venue["unknown_seconds"] == Decimal(50)
    assert venue["fresh_pct"] == Decimal(50)
    assert venue["fresh_known_pct"] == Decimal(100)


def test_fee_unknown_excludes_missing_leg_results_from_depth_rejections(tmp_path):
    from pmarb.storage.db import open_reader

    path = tmp_path / "fee-unknown.sqlite"
    writer = open_writer(path)
    start = T0 - timedelta(minutes=1)
    for group in ("depth-rejected", "explicit-unknown"):
        upsert_market(writer, _market(Venue.KALSHI, f"k-{group}", start=start), T0)
        upsert_market(writer, _market(Venue.POLYMARKET_US, f"p-{group}", start=start), T0)
    rejected = OpportunityAssessment(
        group_id="depth-rejected",
        yes_leg=Leg(venue=Venue.KALSHI, market_id="k-depth-rejected", side=Side.YES),
        no_leg=Leg(venue=Venue.POLYMARKET_US, market_id="p-depth-rejected", side=Side.NO),
        requested_qty=Decimal(100),
        yes_result=None,
        no_result=None,
        capital=None,
        gross_edge=None,
        net_profit=None,
        roc=None,
        max_profitable_qty=None,
        skew_ms=5,
        books_atomic=False,
        exception_risk=False,
        result_class=AssessmentClass.REJECTED,
        reasons=("insufficient_depth_kalshi",),
        model_version="synthetic-test",
        computed_utc=T0,
    )
    insert_assessment(writer, rejected)
    insert_assessment(
        writer,
        _assessment(
            group="explicit-unknown",
            stamp=T0,
            gross="0.1",
            net="0.05",
            skew=5,
            fee_unknown=True,
        ),
    )
    writer.close()

    conn = open_reader(path)
    try:
        metrics = build_metrics(conn)
    finally:
        conn.close()
    assert metrics["fees"]["fee_unknown_count"] == 1
    assert metrics["fees"]["fee_unknown_share"] == Decimal("0.5")


def test_episode_window_clips_overlapping_episode(tmp_path):
    from pmarb.storage.db import open_reader

    path = tmp_path / "episode-window.sqlite"
    writer = open_writer(path)
    yes_leg = Leg(venue=Venue.KALSHI, market_id="episode", side=Side.YES)
    no_leg = Leg(venue=Venue.POLYMARKET_US, market_id="episode", side=Side.NO)
    upsert_episode(
        writer,
        Episode(
            group_id="episode",
            yes_leg=yes_leg,
            no_leg=no_leg,
            qty=Decimal(10),
            result_class=AssessmentClass.INDICATIVE,
            model_version="synthetic-test",
            first_seen_utc=T0,
            last_seen_utc=T0 + timedelta(seconds=60),
            observations=3,
            peak_net=Decimal("0.5"),
            min_size=Decimal(5),
            max_size=Decimal(20),
        ),
    )
    writer.close()
    conn = open_reader(path)
    try:
        report = build_metrics(
            conn,
            since=T0 + timedelta(seconds=20),
            until=T0 + timedelta(seconds=40),
        )
    finally:
        conn.close()
    assert report["episodes"]["overall"]["count"] == 1
    assert report["episodes"]["overall"]["duration_seconds"]["max"] == Decimal(20)


def test_reverse_direction_match_is_counted_but_stale_predicate_stays_unavailable(tmp_path):
    from pmarb.storage.db import open_reader

    path = tmp_path / "reverse.sqlite"
    writer = open_writer(path)
    start = T0 - timedelta(minutes=1)
    for venue, market_id in ((Venue.KALSHI, "k-reverse"), (Venue.POLYMARKET_US, "p-reverse")):
        upsert_market(writer, _market(venue, market_id, start=start), T0)
    forward = _assessment(group="reverse", stamp=T0, gross="0.1", net="0.05", skew=15)
    insert_assessment(writer, forward)
    reverse = OpportunityAssessment(
        group_id="reverse",
        yes_leg=Leg(venue=Venue.POLYMARKET_US, market_id="p-reverse", side=Side.YES),
        no_leg=Leg(venue=Venue.KALSHI, market_id="k-reverse", side=Side.NO),
        requested_qty=Decimal(10),
        yes_result=None,
        no_result=None,
        capital=None,
        gross_edge=Decimal("0.02"),
        net_profit=Decimal("0.01"),
        roc=None,
        max_profitable_qty=Decimal(20),
        skew_ms=16,
        books_atomic=False,
        exception_risk=False,
        result_class=AssessmentClass.INDICATIVE,
        reasons=("polling_skew",),
        model_version="synthetic-test",
        computed_utc=T0,
    )
    insert_assessment(writer, reverse)
    writer.close()
    conn = open_reader(path)
    try:
        check = build_metrics(conn)["opportunities"]["both_directions_sanity"]
    finally:
        conn.close()
    assert check["available"] is False
    assert check["matched_pairs"] == 1
    assert check["violations"] is None


def test_scenario_fees_walk_each_level_and_preserve_decimal_math(tmp_path):
    from pmarb.storage.db import open_reader

    path = tmp_path / "scenario.sqlite"
    writer = open_writer(path)
    start = T0 - timedelta(minutes=1)
    for index, qty in enumerate((1, 10, 100)):
        group = f"scenario-{qty}"
        upsert_market(writer, _market(Venue.KALSHI, f"k-{group}", start=start), T0)
        upsert_market(writer, _market(Venue.POLYMARKET_US, f"p-{group}", start=start), T0)
        row = _assessment(
            group=group,
            stamp=T0 + timedelta(seconds=index),
            gross=str(Decimal("0.15") * Decimal(qty)),
            net="0",
            skew=10,
            qty=str(qty),
        )
        half = Decimal(qty) / Decimal(2)
        yes = row.yes_result.model_copy(
            update={
                "levels_consumed": [
                    Level(price=Decimal("0.4"), size=half),
                    Level(price=Decimal("0.5"), size=half),
                ]
            }
        )
        no = row.no_result.model_copy(
            update={"levels_consumed": [Level(price=Decimal("0.4"), size=Decimal(qty))]}
        )
        insert_assessment(writer, row.model_copy(update={"yes_result": yes, "no_result": no}))
    writer.close()

    conn = open_reader(path)
    try:
        metrics = build_metrics(conn)
    finally:
        conn.close()
    scenario = metrics["fees"]["scenario"]
    # Kalshi scenario fees include the live model's one-step ($0.0001) conservative
    # margin (PR-1), so each net is $0.0001 below the raw fee_rounding result.
    assert scenario["available"] is True
    assert scenario["repricable_assessments"] == 3
    assert scenario["net_profit"]["0.0695"]["max"] == Decimal("11.6149")
    assert scenario["net_profit"]["0.095"]["max"] == Decimal("11.0049")
    assert metrics["opportunities"]["overall"]["qty_profitability"]["1"]["scenario_net_by_theta"][
        "0.0695"
    ]["net_profit"]["max"] == Decimal("0.1127")
    assert metrics["opportunities"]["overall"]["qty_profitability"]["10"]["scenario_net_by_theta"][
        "0.0695"
    ]["net_profit"]["max"] == Decimal("1.1584")
    assert metrics["opportunities"]["overall"]["qty_profitability"]["100"]["scenario_net_by_theta"][
        "0.095"
    ]["net_profit"]["max"] == Decimal("11.0049")
