"""SQLite writer, reader, migrations, and the single-writer task."""

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pmarb.models import (
    AssessmentClass,
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
from pmarb.storage.db import (
    DbWriterTask,
    SchemaVersionError,
    Write,
    bool_int,
    decimal_str,
    dumps_json,
    format_utc,
    insert_assessment,
    insert_collector_event,
    insert_review_log,
    insert_rules_version,
    open_reader,
    open_writer,
    upsert_episode,
    upsert_group,
    upsert_market,
)

AWARE = datetime(2026, 11, 11, 18, tzinfo=UTC)
LATER = datetime(2026, 11, 11, 18, 1, tzinfo=UTC)
CENTRAL = datetime(2026, 11, 11, 12, tzinfo=timezone(timedelta(hours=-6)))


def _market(**overrides: object) -> Market:
    payload: dict[str, object] = {
        "venue": Venue.KALSHI,
        "market_id": "KX-1",
        "title": "NE at DET",
        "league": "nfl",
        "participants": ("NE", "DET"),
        "yes_participant": "NE",
        "market_type": "winner",
        "line": Decimal("0.1000"),
        "event_start_utc": CENTRAL,
        "expected_resolution_utc": AWARE,
        "expected_resolution_src": "rules",
        "expected_payout_utc": None,
        "status": "active",
        "rules_text": "Rules",
        "rules_hash": "abc",
        "fee_meta": {"multiplier": "1", "series": "KXNFLGAME"},
        "url": "https://example.test/KX-1",
        "raw": {"ticker": "KX-1"},
    }
    payload.update(overrides)
    return Market(**payload)  # type: ignore[arg-type]


def _leg(venue: Venue, side: Side, market_id: str) -> Leg:
    return Leg(venue=venue, market_id=market_id, side=side)


def _result(
    venue: Venue,
    side: Side,
    market_id: str,
    *,
    fee: Decimal | None = Decimal("0.0200"),
) -> LegResult:
    return LegResult(
        leg=_leg(venue, side, market_id),
        levels_consumed=[Level(price=Decimal("0.4500"), size=Decimal("10.00"))],
        quantity=Decimal("10.00"),
        cost=Decimal("4.5000"),
        avg_price=Decimal("0.4500"),
        fee=FeeQuote(amount=fee, provenance=f"{venue.value}-schedule", conservative_bound=False),
        book_age_ms=120,
    )


def _assessment(**overrides: object) -> OpportunityAssessment:
    payload: dict[str, object] = {
        "group_id": "g1",
        "yes_leg": _leg(Venue.KALSHI, Side.YES, "KX-1"),
        "no_leg": _leg(Venue.POLYMARKET_US, Side.NO, "pm-1"),
        "requested_qty": Decimal("10.00"),
        "yes_result": _result(Venue.KALSHI, Side.YES, "KX-1"),
        "no_result": _result(Venue.POLYMARKET_US, Side.NO, "pm-1"),
        "capital": Decimal("9.5000"),
        "gross_edge": Decimal("0.1000"),
        "net_profit": Decimal("0.4000"),
        "roc": Decimal("0.0400"),
        "max_profitable_qty": Decimal("10.00"),
        "skew_ms": 20,
        "books_atomic": True,
        "exception_risk": True,
        "result_class": AssessmentClass.QUALIFIED,
        "reasons": (),
        "model_version": "m1",
        "computed_utc": AWARE,
    }
    payload.update(overrides)
    return OpportunityAssessment(**payload)  # type: ignore[arg-type]


def _group() -> EquivalenceGroup:
    return EquivalenceGroup(
        group_id="g1",
        outcome="NE beats DET, OT included",
        members=(
            GroupMember(
                venue=Venue.KALSHI,
                market_id="KX-1",
                native_side_for_outcome=Side.YES,
                approved_rules_hash="hash-k",
            ),
            GroupMember(
                venue=Venue.POLYMARKET_US,
                market_id="pm-1",
                native_side_for_outcome=Side.NO,
                approved_rules_hash="hash-p",
            ),
        ),
        status=GroupStatus.APPROVED,
        exception_risk=True,
        exception_table=(
            ExceptionRow(
                outcome_case="tie",
                payouts={"kalshi:KX-1": "0.50", "polymarket_us:pm-1": "0.50"},
                preserves_unit_payout=True,
            ),
        ),
        reviewer="ada",
        decided_utc=AWARE,
        notes="checked",
    )


def _episode(**overrides: object) -> Episode:
    payload: dict[str, object] = {
        "group_id": "g1",
        "yes_leg": _leg(Venue.KALSHI, Side.YES, "KX-1"),
        "no_leg": _leg(Venue.POLYMARKET_US, Side.NO, "pm-1"),
        "qty": Decimal("10.00"),
        "result_class": AssessmentClass.INDICATIVE,
        "model_version": "m1",
        "first_seen_utc": AWARE,
        "last_seen_utc": LATER,
        "observations": 3,
        "peak_net": Decimal("0.4000"),
        "min_size": Decimal("1.00"),
        "max_size": Decimal("10.00"),
    }
    payload.update(overrides)
    return Episode(**payload)  # type: ignore[arg-type]


def _event(conn: sqlite3.Connection, kind: str) -> None:
    insert_collector_event(conn, AWARE, "kalshi", kind, {"kind": kind})


async def _stop(task: asyncio.Task[None]) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_writer_pragmas_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "pmarb.sqlite"
    assert not path.parent.exists()
    with closing(open_writer(path)) as conn:
        assert path.is_file()
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (5000,)
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone() == (1,)
        upsert_market(conn, _market(), AWARE)
        assert not conn.in_transaction
    with closing(open_writer(path)) as again:
        assert again.execute("SELECT COUNT(*) FROM schema_version").fetchone() == (1,)
        assert again.execute("SELECT version FROM schema_version").fetchone() == (2,)
        assert again.execute("SELECT title FROM markets").fetchone() == ("NE at DET",)
        names = {
            row[0]
            for row in again.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert "assessments" in names and "episodes" in names and "collector_events" in names


def test_newer_schema_and_broken_version_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "pmarb.sqlite"
    with closing(open_writer(path)) as conn:
        conn.execute("UPDATE schema_version SET version = 99")
    with pytest.raises(
        SchemaVersionError, match=r"schema_version 99 is newer than this code supports \(2\)"
    ):
        open_writer(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone() == (99,)

    empty = tmp_path / "empty.sqlite"
    with closing(open_writer(empty)) as conn:
        conn.execute("DELETE FROM schema_version")
    with pytest.raises(SchemaVersionError, match="exactly one row, found 0"):
        open_writer(empty)


def test_reader_is_read_only_and_missing_db_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        open_reader(missing)
    assert not missing.exists()

    path = tmp_path / "pmarb.sqlite"
    with closing(open_writer(path)) as writer:
        insert_collector_event(writer, AWARE, "kalshi", "boot", {"ok": True})
    with closing(open_reader(path)) as reader:
        assert reader.execute("PRAGMA query_only").fetchone() == (1,)
        assert reader.execute("SELECT kind FROM collector_events").fetchone() == ("boot",)
        with pytest.raises(sqlite3.OperationalError):
            reader.execute(
                "INSERT INTO collector_events (utc, kind, detail_json) VALUES ('t', 'x', '{}')"
            )
        assert reader.execute("SELECT COUNT(*) FROM collector_events").fetchone() == (1,)


def test_serialization_exact_decimal_utc_and_naive_rejected() -> None:
    assert decimal_str(Decimal("0.1000")) == "0.1000"
    assert decimal_str(Decimal("10.00")) == "10.00"
    assert decimal_str(Decimal("1E-7")) == "1E-7"
    assert Decimal(decimal_str(Decimal("0.1000"))) == Decimal("0.1000")
    for bad in ("NaN", "Infinity", "-Infinity", "sNaN"):
        with pytest.raises(ValueError, match="non-finite"):
            decimal_str(Decimal(bad))
    with pytest.raises(TypeError):
        decimal_str(1)  # type: ignore[arg-type]

    assert format_utc(AWARE) == "2026-11-11T18:00:00Z"
    assert format_utc(CENTRAL) == "2026-11-11T18:00:00Z"
    stamped = AWARE.replace(microsecond=123456)
    assert format_utc(stamped) == "2026-11-11T18:00:00.123456Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc(datetime(2026, 11, 11, 18))

    assert bool_int(True) == 1 and bool_int(False) == 0
    with pytest.raises(TypeError):
        bool_int(1)  # type: ignore[arg-type]

    encoded = dumps_json({"b": Decimal("0.10"), "a": 1})
    assert encoded == '{"a":1,"b":"0.10"}'
    assert json.loads(encoded) == {"a": 1, "b": "0.10"}
    with pytest.raises(ValueError):
        dumps_json({"x": Decimal("NaN")})
    with pytest.raises(TypeError):
        dumps_json({"x": object()})


def test_typed_writes_round_trip_and_checks_reject(tmp_path: Path) -> None:
    path = tmp_path / "pmarb.sqlite"
    with closing(open_writer(path)) as conn:
        market = _market()
        upsert_market(conn, market, AWARE)
        row = conn.execute("SELECT * FROM markets").fetchone()
        assert row is not None
        columns = [d[0] for d in conn.execute("SELECT * FROM markets").description]
        stored = dict(zip(columns, row, strict=True))
        assert stored["market_key"] == "kalshi:KX-1"
        assert stored["venue"] == "kalshi"
        assert stored["venue_market_id"] == "KX-1"
        assert stored["line"] == "0.1000"
        assert Decimal(stored["line"]) == Decimal("0.1000")
        assert stored["event_start_utc"] == "2026-11-11T18:00:00Z"
        assert stored["expected_payout_utc"] is None
        assert stored["first_seen_utc"] == stored["last_seen_utc"] == "2026-11-11T18:00:00Z"
        assert json.loads(stored["participants_json"]) == ["NE", "DET"]
        assert json.loads(stored["fee_meta_json"]) == {"multiplier": "1", "series": "KXNFLGAME"}
        assert stored["fee_meta_json"].index('"multiplier"') < stored["fee_meta_json"].index(
            '"series"'
        )

        upsert_market(
            conn,
            market.model_copy(update={"title": "NE at DET updated", "status": "closed"}),
            LATER,
        )
        updated = conn.execute(
            "SELECT title, status, first_seen_utc, last_seen_utc FROM markets"
        ).fetchone()
        assert updated == (
            "NE at DET updated",
            "closed",
            "2026-11-11T18:00:00Z",
            "2026-11-11T18:01:00Z",
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            upsert_market(conn, market, datetime(2026, 11, 11, 18))

        insert_rules_version(conn, "kalshi:KX-1", "abc", "Rules", "Terms", AWARE)
        insert_rules_version(conn, "kalshi:KX-1", "abc", "OTHER", "OTHER", LATER)
        insert_rules_version(conn, "kalshi:KX-1", "def", "Rules 2", "Terms 2", LATER)
        rules = conn.execute(
            "SELECT rules_hash, rules_text, captured_utc FROM rules_versions ORDER BY rules_hash"
        ).fetchall()
        assert rules == [
            ("abc", "Rules", "2026-11-11T18:00:00Z"),
            ("def", "Rules 2", "2026-11-11T18:01:00Z"),
        ]
        with pytest.raises(ValueError, match="timezone-aware"):
            insert_rules_version(conn, "kalshi:KX-1", "ghi", "x", "y", datetime(2026, 11, 11))

        group = _group()
        upsert_group(conn, group)
        grow = conn.execute(
            "SELECT status, exception_risk, reviewer, decided_utc, notes FROM groups"
        ).fetchone()
        assert grow == ("approved", 1, "ada", "2026-11-11T18:00:00Z", "checked")
        table = json.loads(conn.execute("SELECT exception_table_json FROM groups").fetchone()[0])
        assert table == [
            {
                "outcome_case": "tie",
                "payouts": {"kalshi:KX-1": "0.50", "polymarket_us:pm-1": "0.50"},
                "preserves_unit_payout": True,
            }
        ]
        members = conn.execute(
            "SELECT market_key, yes_maps_to, approved_rules_hash FROM group_members ORDER BY market_key"
        ).fetchall()
        assert members == [
            ("kalshi:KX-1", "yes", "hash-k"),
            ("polymarket_us:pm-1", "no", "hash-p"),
        ]
        replaced = group.model_copy(
            update={
                "notes": "swapped",
                "members": (
                    group.members[0],
                    GroupMember(
                        venue=Venue.POLYMARKET_US,
                        market_id="pm-2",
                        native_side_for_outcome=Side.YES,
                        approved_rules_hash="hash-p2",
                    ),
                ),
            }
        )
        upsert_group(conn, replaced)
        keys = [row[0] for row in conn.execute("SELECT market_key FROM group_members ORDER BY 1")]
        assert keys == ["kalshi:KX-1", "polymarket_us:pm-2"]
        assert conn.execute("SELECT notes FROM groups").fetchone() == ("swapped",)

        assessment_id = insert_assessment(conn, _assessment())
        assert assessment_id == 1
        arow = conn.execute("SELECT * FROM assessments").fetchone()
        assert arow is not None
        acols = [d[0] for d in conn.execute("SELECT * FROM assessments").description]
        got = dict(zip(acols, arow, strict=True))
        assert got["yes_market_key"] == "kalshi:KX-1"
        assert got["no_market_key"] == "polymarket_us:pm-1"
        assert got["qty"] == "10.00"
        assert got["gross_edge"] == "0.1000"
        assert Decimal(got["gross_edge"]) == Decimal("0.1000")
        assert got["net_profit"] == "0.4000"
        assert got["fee_yes"] == "0.0200" and got["fee_no"] == "0.0200"
        assert got["avg_yes"] == "0.4500"
        assert got["qualified"] == 1
        assert got["class"] == "qualified"
        assert got["books_atomic"] == 1 and got["exception_risk"] == 1
        assert got["age_yes_ms"] == 120 and got["skew_ms"] == 20
        assert got["reasons_json"] == "[]"
        assert got["computed_utc"] == "2026-11-11T18:00:00Z"
        provenance = json.loads(got["fee_provenance_json"])
        assert provenance["yes"]["provenance"] == "kalshi-schedule"
        assert provenance["no"]["conservative_bound"] is False
        for side in ("yes", "no"):
            assert provenance[side]["levels_consumed"] == [{"price": "0.4500", "size": "10.00"}]

        rejected_id = insert_assessment(
            conn,
            _assessment(
                result_class=AssessmentClass.REJECTED,
                reasons=("insufficient_depth_kalshi",),
                books_atomic=False,
                yes_result=None,
                no_result=None,
                capital=None,
                gross_edge=None,
                net_profit=None,
                roc=None,
                max_profitable_qty=None,
                skew_ms=None,
                exception_risk=False,
            ),
        )
        assert rejected_id == 2
        rejected = conn.execute(
            "SELECT qualified, class, gross_edge, fee_yes, reasons_json FROM assessments WHERE id = 2"
        ).fetchone()
        assert rejected == (0, "rejected", None, None, '["insufficient_depth_kalshi"]')

        episode_id = upsert_episode(conn, _episode())
        assert episode_id == 1
        closed = _episode(
            id=episode_id,
            observations=4,
            termination=EpisodeTermination.DISQUALIFIED,
            termination_detail="stale",
        )
        assert upsert_episode(conn, closed) == 1
        ep = conn.execute(
            'SELECT "class", qty, observations, peak_net, termination_reason FROM episodes'
        ).fetchone()
        assert ep == ("indicative", "10.00", 4, "0.4000", "disqualified:stale")
        assert Decimal(ep[1]) == Decimal("10.00")

        event_id = insert_collector_event(conn, CENTRAL, None, "heartbeat", {"queue_depth": 0})
        assert event_id == 1
        ev = conn.execute("SELECT utc, venue, kind, detail_json FROM collector_events").fetchone()
        assert ev == ("2026-11-11T18:00:00Z", None, "heartbeat", '{"queue_depth":0}')

        review_id = insert_review_log(
            conn,
            group_id="g1",
            action="approve",
            reviewer="ada",
            shown_hashes={"kalshi:KX-1": "hash-k", "polymarket_us:pm-1": "hash-p"},
            result="applied",
            spool_file="spool/reviews/1.json",
            applied_utc=AWARE,
        )
        assert review_id == 1
        review = conn.execute(
            "SELECT action, result, shown_hashes_json, applied_utc FROM review_log"
        ).fetchone()
        assert review[0] == "approve" and review[1] == "applied"
        assert review[3] == "2026-11-11T18:00:00Z"
        assert json.loads(review[2])["kalshi:KX-1"] == "hash-k"

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO groups (group_id, status) VALUES ('bad', 'nope')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO assessments (group_id, class) VALUES ('g1', 'nope')")
        with pytest.raises(sqlite3.IntegrityError):
            insert_review_log(
                conn,
                group_id="g1",
                action="approve",
                reviewer="ada",
                shown_hashes={"kalshi:KX-1": "hash-k"},
                result="nope",
                spool_file=None,
                applied_utc=AWARE,
            )
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM review_log").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE group_id = 'bad'").fetchone() == (0,)


@dataclass(frozen=True, slots=True)
class _Insert:
    kind: str

    def apply(self, conn: sqlite3.Connection) -> None:
        _event(conn, self.kind)


def test_writer_batches_on_size_checkpoints_and_reports_depth(tmp_path: Path) -> None:
    path = tmp_path / "pmarb.sqlite"
    queue: asyncio.Queue[object] = asyncio.Queue()
    writer = DbWriterTask(
        path,
        queue,
        commit_interval_s=0.05,
        batch_size=2,
        checkpoint_interval_s=0.05,
    )
    started = threading.Event()
    release = threading.Event()

    def block(conn: sqlite3.Connection) -> None:
        _event(conn, "block")
        started.set()
        if not release.wait(2):
            raise TimeoutError("writer test did not release the blocked op")

    async def main() -> None:
        task = asyncio.create_task(writer.run())
        try:
            await asyncio.wait_for(writer.ready.wait(), 2)
            assert writer.queue_depth == 0
            queue.put_nowait(block)
            queue.put_nowait(_Insert("two"))
            queue.put_nowait(Write(lambda conn: _event(conn, "three")))
            queue.put_nowait(_Insert("four"))
            queue.put_nowait(_Insert("five"))
            assert await asyncio.to_thread(started.wait, 2)
            assert writer.queue_depth == 3
            release.set()
            await asyncio.sleep(0.4)
            with closing(open_reader(path)) as reader:
                kinds = [
                    row[0]
                    for row in reader.execute("SELECT kind FROM collector_events ORDER BY id")
                ]
            assert kinds == ["block", "two", "three", "four", "five"]
            assert writer.checkpoint_count >= 1
            assert writer.failed_ops == 0
        finally:
            release.set()
            await _stop(task)

    asyncio.run(main())


def test_writer_drains_on_cancel_and_reraises(tmp_path: Path) -> None:
    path = tmp_path / "pmarb.sqlite"
    queue: asyncio.Queue[object] = asyncio.Queue()
    writer = DbWriterTask(
        path, queue, commit_interval_s=30, batch_size=100, checkpoint_interval_s=3600
    )

    async def main() -> None:
        task = asyncio.create_task(writer.run())
        try:
            await asyncio.wait_for(writer.ready.wait(), 2)
            for kind in ("a", "b", "c"):
                queue.put_nowait(_Insert(kind))
            await asyncio.sleep(0.01)
            for kind in ("d", "e"):
                queue.put_nowait(_Insert(kind))
            await asyncio.sleep(0.05)
            with closing(open_reader(path)) as reader:
                assert reader.execute("SELECT COUNT(*) FROM collector_events").fetchone() == (0,)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert writer.queue_depth == 0
        finally:
            await _stop(task)

    asyncio.run(main())
    with closing(open_reader(path)) as reader:
        kinds = [row[0] for row in reader.execute("SELECT kind FROM collector_events ORDER BY id")]
    assert kinds == ["a", "b", "c", "d", "e"]


def test_failing_op_is_counted_logged_and_later_ops_commit(tmp_path: Path) -> None:
    path = tmp_path / "pmarb.sqlite"
    queue: asyncio.Queue[object] = asyncio.Queue()
    writer = DbWriterTask(
        path, queue, commit_interval_s=0.05, batch_size=10, checkpoint_interval_s=3600
    )

    def bad(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO groups (group_id, status) VALUES ('bad', 'nope')")

    async def main() -> None:
        queue.put_nowait(bad)
        queue.put_nowait(_Insert("kept"))
        task = asyncio.create_task(writer.run())
        try:
            await asyncio.wait_for(writer.ready.wait(), 2)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while writer.failed_ops < 1 and loop.time() < deadline:
                await asyncio.sleep(0.02)
            assert writer.failed_ops == 1
            with closing(open_reader(path)) as reader:
                assert reader.execute(
                    "SELECT COUNT(*) FROM groups WHERE group_id = 'bad'"
                ).fetchone() == (0,)
                rows = reader.execute(
                    "SELECT kind, detail_json FROM collector_events ORDER BY id"
                ).fetchall()
            assert [row[0] for row in rows] == ["kept", "db_write_failed"]
            detail = json.loads(rows[1][1])
            assert detail["error_type"] == "IntegrityError"
            assert detail["op"] == "bad"
            assert "CHECK constraint failed" in detail["error"]
            queue.put_nowait(_Insert("later"))
            deadline = loop.time() + 2
            seen = False
            while loop.time() < deadline:
                with closing(open_reader(path)) as reader:
                    kinds = [
                        row[0]
                        for row in reader.execute("SELECT kind FROM collector_events ORDER BY id")
                    ]
                if "later" in kinds:
                    seen = True
                    break
                await asyncio.sleep(0.02)
            assert seen
            assert not task.done()
        finally:
            await _stop(task)

    asyncio.run(main())


def test_concurrent_reader_sees_committed_rows(tmp_path: Path) -> None:
    path = tmp_path / "pmarb.sqlite"
    queue: asyncio.Queue[object] = asyncio.Queue()
    writer = DbWriterTask(
        path, queue, commit_interval_s=0.05, batch_size=10, checkpoint_interval_s=3600
    )
    market = _market()

    async def main() -> None:
        task = asyncio.create_task(writer.run())
        try:
            await asyncio.wait_for(writer.ready.wait(), 2)
            with closing(open_reader(path)) as reader:
                assert reader.execute("SELECT COUNT(*) FROM markets").fetchone() == (0,)
                queue.put_nowait(lambda conn: upsert_market(conn, market, AWARE))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 2
                seen = None
                while loop.time() < deadline:
                    seen = reader.execute("SELECT market_key, line FROM markets").fetchone()
                    if seen is not None:
                        break
                    await asyncio.sleep(0.02)
                assert seen == ("kalshi:KX-1", "0.1000")
                assert Decimal(seen[1]) == Decimal("0.1000")
                assert not task.done()
        finally:
            await _stop(task)

    asyncio.run(main())
