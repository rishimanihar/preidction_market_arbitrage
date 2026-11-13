"""Independent cross-component acceptance checks; no network or venue keys.

These assert intended behavior, so defects produce ordinary failing tests.
Production source is untouched. Direct SQLite migration setup avoids making
reporting checks depend on Windows directory-fsync compatibility.
"""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from types import SimpleNamespace

import pytest

from pmarb import cli
from pmarb.analysis.metrics import build_metrics
from pmarb.app.decisions import build_decision, write_decision
from pmarb.collector.clock import FakeClock
from pmarb.collector.runtime import Collector
from pmarb.config import Settings
from pmarb.http import RetriesExhaustedError
from pmarb.models import Book, Level, Venue
from pmarb.storage.db import insert_collector_event, open_writer


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "pmarb.sqlite"
    conn = sqlite3.connect(path)
    migration_dir = files("pmarb.storage.migrations")
    for name in ("0001_initial.sql", "0002_game_state.sql"):
        conn.executescript(migration_dir.joinpath(name).read_text(encoding="utf-8"))
    yield path, conn
    conn.close()


def collector(tmp_path, **kwargs):
    return Collector(
        kalshi=SimpleNamespace(venue=Venue.KALSHI),
        polyus=SimpleNamespace(venue=Venue.POLYMARKET_US),
        clock=FakeClock(mono=1000),
        settings=Settings(_env_file=None, data_dir=tmp_path),
        on_books_updated=lambda _: None,
        on_gap=lambda _: None,
        install_signals=False,
        disk_free=lambda _: 10**12,
        **kwargs,
    )


def metrics_for(conn, runtime):
    for event in runtime.events:
        insert_collector_event(conn, event.utc, event.venue, event.kind, event.detail)
    return build_metrics(conn)


def test_doctor_accepts_current_migrated_schema(database, monkeypatch):
    path, conn = database
    assert conn.execute("SELECT version FROM schema_version").fetchone() == (2,)
    monkeypatch.setattr(cli.importlib, "import_module", lambda _: object())
    errors = cli._doctor(Settings(_env_file=None, data_dir=path.parent), None)
    assert not errors, f"doctor rejects the current migrated database: {errors}"


def test_report_counts_real_collector_adapter_errors(database, tmp_path):
    _, conn = database
    runtime = collector(tmp_path)
    runtime._record_adapter_error(Venue.KALSHI, RuntimeError("synthetic outage"))
    assert runtime.error_count == 1
    metrics = metrics_for(conn, runtime)
    assert metrics["coverage"]["error_count"] == 1, metrics["coverage"]


def test_report_counts_real_collector_rate_limits(database, tmp_path):
    _, conn = database
    runtime = collector(tmp_path)
    runtime._record_adapter_error(
        Venue.KALSHI, RetriesExhaustedError("synthetic rate limit", status_code=429)
    )
    metrics = metrics_for(conn, runtime)
    assert metrics["coverage"]["http_429_count"] == 1


def test_report_counts_real_collector_backpressure(database, tmp_path):
    _, conn = database
    queue = asyncio.Queue()
    queue.put_nowait(object())
    runtime = collector(tmp_path, raw_queue=queue, queue_limit=0)
    asyncio.run(runtime._sync_backpressure())
    assert any(e.kind == "recording_backpressure" for e in runtime.events)
    metrics = metrics_for(conn, runtime)
    assert metrics["coverage"]["gaps_by_kind"]["backpressure"] == 1


def test_report_measures_freshness_from_actual_collector_health(database, tmp_path):
    _, conn = database
    runtime = collector(tmp_path)
    for venue in Venue:
        book = Book(
            venue=venue,
            market_id=f"synthetic-{venue}",
            yes_asks=[Level(price=Decimal("0.4"), size=Decimal("100"))],
            no_asks=[Level(price=Decimal("0.7"), size=Decimal("100"))],
            received_utc=runtime._clock.wall(),
            received_monotonic=runtime._clock.monotonic(),
            connection_generation=0,
            valid=True,
        )
        runtime._accept_book(book, runtime._clock.monotonic(), runtime._clock.wall())
    runtime._health_tick()
    runtime._clock.advance(mono=1, wall=timedelta(seconds=1))
    runtime._health_tick()
    metrics = metrics_for(conn, runtime)
    venues = metrics["coverage"]["venues"]
    for venue in Venue:
        assert venues.get(str(venue), {}).get("fresh_pct") == Decimal(100), venues


def test_manual_review_spool_round_trip_on_current_platform(tmp_path):
    decision = build_decision(
        group_id="synthetic-group",
        action="reject",
        reviewer="independent-review",
        shown_hashes={"kalshi:synthetic": "v1:synthetic"},
        exception_table=[],
        acknowledged_exceptions=False,
        created_utc=datetime(2026, 6, 3, 18, tzinfo=UTC),
    )
    destination = write_decision(tmp_path, decision)
    assert destination.is_file()


def test_database_creation_on_current_platform(tmp_path):
    conn = open_writer(tmp_path / "new" / "pmarb.sqlite")
    try:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (2,)
    finally:
        conn.close()
