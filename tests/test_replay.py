"""Deterministic replay of synthetic raw logs. No recorded market data."""

from __future__ import annotations

import os
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from pmarb import cli
from pmarb.collector.clock import parse_utc
from pmarb.config import Settings
from pmarb.models import EquivalenceGroup, GroupMember, GroupStatus, Market, Side, Venue
from pmarb.replay import ReplayEngine, ReplayError, parse_rest_books
from pmarb.replay.engine import ReplayResult
from pmarb.storage.db import (
    format_utc,
    insert_review_log,
    insert_rules_version,
    open_reader,
    open_writer,
    upsert_group,
    upsert_market,
)
from pmarb.storage.rawlog import Envelope, RawLogWriter

# Thursday 2026-11-12 18:00 UTC is 13:00 America/New_York, outside the
# Polymarket US maintenance window (Thu 06:00–08:00).
T0 = datetime(2026, 11, 12, 18, tzinfo=UTC)
GROUP = "g-replay"
KALSHI = "KX-REPLAY"
POLY = "pm-replay"
CHEAP = (("0.40", "0.70"), ("0.70", "0.40"))
EXPENSIVE = (("0.90", "0.80"), ("0.95", "0.95"))


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("PMARB_", "KALSHI_", "POLYUS_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def _settings() -> Settings:
    return Settings(_env_file=None)


def wall_at(ns: int) -> datetime:
    return T0 + timedelta(microseconds=ns // 1000)


def _envelope(
    venue: str,
    kind: str,
    ns: int,
    body: object = None,
    *,
    conn_gen: int = 2,
    status: int | None = 200,
    endpoint: str = "/",
) -> Envelope:
    return Envelope(
        recv_utc=format_utc(wall_at(ns)),
        recv_mono_ns=ns,
        venue=venue,
        kind=kind,
        conn_gen=conn_gen,
        endpoint=endpoint,
        status=status,
        cache_headers={},
        body=body,
    )


def _kalshi_body(yes_ask: str, no_ask: str, ticker: str = KALSHI) -> dict[str, object]:
    yes_bid = str(Decimal(1) - Decimal(no_ask))
    no_bid = str(Decimal(1) - Decimal(yes_ask))
    return {
        "orderbooks": [
            {
                "ticker": ticker,
                "orderbook_fp": {
                    "yes_dollars": [[yes_bid, "200"]],
                    "no_dollars": [[no_bid, "200"]],
                },
            }
        ]
    }


def _poly_body(yes_ask: str, no_ask: str, slug: str = POLY) -> dict[str, object]:
    bid = str(Decimal(1) - Decimal(no_ask))
    return {
        "marketData": {
            "marketSlug": slug,
            "state": "MARKET_STATE_OPEN",
            "bids": [{"px": {"value": bid}, "qty": "200"}],
            "offers": [{"px": {"value": yes_ask}, "qty": "200"}],
        }
    }


def _book_pair(
    ns: int,
    quotes: tuple[tuple[str, str], tuple[str, str]] = CHEAP,
    *,
    conn_gen: int = 2,
) -> list[Envelope]:
    kalshi, poly = quotes
    return [
        _envelope(
            "kalshi",
            "rest_book",
            ns,
            _kalshi_body(*kalshi),
            conn_gen=conn_gen,
            endpoint="/markets/orderbooks",
        ),
        _envelope(
            "polymarket_us",
            "rest_book",
            ns,
            _poly_body(*poly),
            conn_gen=conn_gen,
            endpoint=f"/v1/markets/{POLY}/book",
        ),
    ]


def _markets() -> tuple[Market, Market]:
    return (
        Market(
            venue=Venue.KALSHI,
            market_id=KALSHI,
            title="Replay",
            market_type="winner",
            event_start_utc=T0,
            status="open",
            rules_text="rules",
            rules_hash="hk",
            fee_meta={"fee_multiplier": "1"},
        ),
        Market(
            venue=Venue.POLYMARKET_US,
            market_id=POLY,
            title="Replay",
            market_type="winner",
            event_start_utc=T0,
            status="open",
            rules_text="rules",
            rules_hash="hp",
        ),
    )


def _group() -> EquivalenceGroup:
    return EquivalenceGroup(
        group_id=GROUP,
        outcome="replay",
        members=(
            GroupMember(
                venue=Venue.KALSHI,
                market_id=KALSHI,
                native_side_for_outcome=Side.YES,
                approved_rules_hash="hk",
            ),
            GroupMember(
                venue=Venue.POLYMARKET_US,
                market_id=POLY,
                native_side_for_outcome=Side.YES,
                approved_rules_hash="hp",
            ),
        ),
        status=GroupStatus.APPROVED,
        reviewer="ada",
        decided_utc=T0,
        exception_risk=False,
    )


def _seed(path: Path, *, review_at: datetime | None = None) -> None:
    """Write the approved snapshot. ``review_at`` is the log's applied time.

    The groups row always says the decision was T0. When ``review_at`` is later,
    replay must wait for the log instead of trusting that column.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_writer(path)) as conn:
        for market in _markets():
            upsert_market(conn, market, T0)
            insert_rules_version(
                conn,
                market.market_key,
                market.rules_hash,
                market.rules_text,
                "terms",
                T0,
            )
        upsert_group(conn, _group())
        if review_at is not None:
            insert_review_log(
                conn,
                group_id=GROUP,
                action="approve",
                reviewer="ada",
                shown_hashes={"kalshi:" + KALSHI: "hk", "polymarket_us:" + POLY: "hp"},
                result="applied",
                spool_file=None,
                applied_utc=review_at,
            )


def _write_log(root: Path, records: list[Envelope]) -> RawLogWriter:
    writer = RawLogWriter(root, clock=lambda: T0)
    for env in records:
        writer.append(env)
    writer.flush()
    return writer


def _raw_bytes(root: Path) -> dict[Path, bytes]:
    raw = root / "raw"
    if not raw.is_dir():
        return {}
    return {path: path.read_bytes() for path in raw.rglob("*") if path.is_file()}


def _replay(root: Path, source: Path, out: Path, **kwargs: object) -> ReplayResult:
    source_bytes = source.read_bytes()
    raw_bytes = _raw_bytes(root)
    result = ReplayEngine(root, source, out, settings=_settings(), **kwargs).run()
    assert source.read_bytes() == source_bytes
    assert _raw_bytes(root) == raw_bytes
    return result


def _hot(tmp_path: Path, records: list[Envelope] | None = None) -> tuple[Path, Path]:
    root = tmp_path / "recording"
    source = tmp_path / "source.sqlite"
    _seed(source)
    _write_log(root, records if records is not None else _book_pair(0))
    return root, source


def _assessment_rows(path: Path) -> list[tuple[object, ...]]:
    with closing(open_reader(path)) as conn:
        rows = conn.execute(
            """
            SELECT group_id, yes_market_key, no_market_key, qty, qualified, reasons_json,
                   gross_edge, net_profit, roc, capital, fee_yes, fee_no, fee_provenance_json,
                   avg_yes, avg_no, max_profitable_qty, age_yes_ms, age_no_ms, skew_ms,
                   books_atomic, "class", exception_risk, model_version, computed_utc
            FROM assessments
            """
        ).fetchall()
    return sorted(tuple(row) for row in rows)


def _avg_yes(path: Path, *, before: datetime | None = None) -> set[Decimal]:
    found: set[Decimal] = set()
    with closing(open_reader(path)) as conn:
        for avg, computed in conn.execute("SELECT avg_yes, computed_utc FROM assessments"):
            when = parse_utc(computed if isinstance(computed, str) else None)
            if before is not None and (when is None or when >= before):
                continue
            if isinstance(avg, str) and avg != "":
                found.add(Decimal(avg))
    return found


def test_parse_rest_books_reads_kalshi_batch_and_polymarket() -> None:
    kalshi = _envelope(
        "kalshi",
        "rest_book",
        0,
        {
            "orderbooks": [
                {
                    "ticker": "A",
                    "orderbook_fp": {
                        "yes_dollars": [["0.40", "10"]],
                        "no_dollars": [["0.40", "10"]],
                    },
                },
                {
                    "ticker": "B",
                    "orderbook_fp": {
                        "yes_dollars": [["0.25", "10"]],
                        "no_dollars": [["0.25", "10"]],
                    },
                },
            ]
        },
        conn_gen=4,
        endpoint="/markets/orderbooks",
    )
    books = parse_rest_books(kalshi)
    assert [book.market_id for book in books] == ["A", "B"]
    assert {book.connection_generation for book in books} == {4}
    assert {book.venue for book in books} == {Venue.KALSHI}

    poly = parse_rest_books(
        _envelope(
            "polymarket_us",
            "rest_book",
            0,
            _poly_body("0.55", "0.50", "pm-slug"),
            conn_gen=4,
            endpoint="/v1/markets/pm-slug/book",
        )
    )
    assert len(poly) == 1
    assert poly[0].market_id == "pm-slug"
    assert poly[0].connection_generation == 4
    assert poly[0].venue is Venue.POLYMARKET_US
    assert poly[0].valid


def test_receipt_order_merges_across_venues(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    source = tmp_path / "source.sqlite"
    with closing(open_writer(source)):
        pass
    late = 2_000_000_000
    early = 1_000_000_000
    # Written kalshi-first. A directory listing would replay that file first.
    _write_log(
        root,
        [
            _envelope("kalshi", "ws_msg", late, {"n": 1}, status=None),
            _envelope("polymarket_us", "ws_msg", early, {"n": 2}, status=None),
        ],
    )
    result = _replay(root, source, tmp_path / "out.sqlite")
    assert result.receipts == ((early, "polymarket_us"), (late, "kalshi"))
    assert result.assessments == 0


def test_no_lookahead(tmp_path: Path) -> None:
    late = 10_000_000_000
    root, source = _hot(tmp_path, _book_pair(0) + _book_pair(late, EXPENSIVE))
    result = _replay(root, source, tmp_path / "out.sqlite")
    early = _avg_yes(result.out_db, before=T0 + timedelta(seconds=10))
    assert Decimal("0.40") in early
    assert Decimal("0.90") not in early
    later = _avg_yes(result.out_db) - early
    assert Decimal("0.90") in later


def test_deterministic_assessments(tmp_path: Path) -> None:
    root, source = _hot(tmp_path)
    first = _replay(root, source, tmp_path / "a.sqlite")
    second = _replay(root, source, tmp_path / "b.sqlite")
    rows = _assessment_rows(first.out_db)
    assert rows
    assert rows == _assessment_rows(second.out_db)


def test_stale_book_expires_in_replay_time(tmp_path: Path) -> None:
    root, source = _hot(tmp_path)
    started = time.perf_counter()
    result = _replay(root, source, tmp_path / "out.sqlite")
    assert time.perf_counter() - started < 5
    with closing(open_reader(result.out_db)) as conn:
        stale = conn.execute("SELECT utc FROM collector_events WHERE kind = 'stale'").fetchall()
    assert stale
    assert all(
        (when := parse_utc(row[0] if isinstance(row[0], str) else None)) is not None
        and when >= T0 + timedelta(seconds=2)
        for row in stale
    )


def test_episode_closes_on_data_gap(tmp_path: Path) -> None:
    root, source = _hot(tmp_path)
    result = _replay(root, source, tmp_path / "out.sqlite")
    with closing(open_reader(result.out_db)) as conn:
        episodes = conn.execute(
            """
            SELECT observations, termination_reason, first_seen_utc, last_seen_utc
            FROM episodes
            """
        ).fetchall()
    closed = [row for row in episodes if row[1] == "data_gap"]
    assert closed
    assert any(isinstance(row[0], int) and row[0] >= 2 for row in closed)


def test_group_approved_mid_run_is_not_evaluated_early(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    source = tmp_path / "source.sqlite"
    approved_at = T0 + timedelta(seconds=1)
    _seed(source, review_at=approved_at)
    _write_log(root, _book_pair(0))
    result = _replay(root, source, tmp_path / "out.sqlite")
    with closing(open_reader(result.out_db)) as conn:
        times = [
            parse_utc(row[0] if isinstance(row[0], str) else None)
            for row in conn.execute("SELECT computed_utc FROM assessments")
        ]
    assert times
    assert all(when is not None and when >= approved_at for when in times)
    assert min(when for when in times if when is not None) == approved_at


def test_connection_generation_rejects_older_book(tmp_path: Path) -> None:
    half = 500_000_000
    records = _book_pair(0, conn_gen=2)
    records.append(
        _envelope(
            "kalshi",
            "rest_book",
            half,
            _kalshi_body("0.90", "0.80"),
            conn_gen=1,
            endpoint="/markets/orderbooks",
        )
    )
    root, source = _hot(tmp_path, records)
    result = _replay(root, source, tmp_path / "out.sqlite")
    assert (half, "kalshi") in result.receipts
    assert Decimal("0.40") in _avg_yes(result.out_db)
    assert Decimal("0.90") not in _avg_yes(result.out_db)


def test_torn_tail_is_skipped_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    source = tmp_path / "source.sqlite"
    _seed(source)
    records = _book_pair(0)
    records.append(_envelope("kalshi", "ws_msg", 50_000_000, {"tail": True}, status=None))
    writer = _write_log(root, records)
    path = writer.path_for(records[-1], T0)
    path.write_bytes(path.read_bytes()[:-5])
    torn = path.read_bytes()
    result = _replay(root, source, tmp_path / "out.sqlite")
    assert path.read_bytes() == torn
    gaps = [gap for gap in result.gaps if gap.kind == "torn_tail"]
    assert len(gaps) == 1
    assert gaps[0].skipped_bytes > 0
    assert gaps[0].records_kept > 0
    assert result.assessments > 0
    assert "repaired" not in gaps[0].detail


def test_engine_refuses_to_overwrite_the_source_database(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with closing(open_writer(source)):
        pass
    original = source.read_bytes()
    raw = tmp_path / "recording"
    raw.mkdir()
    with pytest.raises(ReplayError, match="source"):
        ReplayEngine(raw, source, source, settings=_settings()).run()
    assert source.read_bytes() == original


def _cli_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    monkeypatch.setattr(cli, "load_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(cli, "_repo_root", lambda: None)
    return settings


def test_replay_refuses_docs_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    docs = checkout / "docs"
    docs.mkdir()
    alias = checkout / "alias"
    alias.symlink_to(docs, target_is_directory=True)
    source = tmp_path / "source.sqlite"
    with closing(open_writer(source)):
        pass
    raw = tmp_path / "recording"
    raw.mkdir()
    _cli_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_repo_root", lambda: checkout)
    for output in (docs / "replay.sqlite", alias / "replay.sqlite"):
        with pytest.raises(SystemExit) as raised:
            cli.main(
                ["replay", "run", "--raw", str(raw), "--db", str(source), "--out", str(output)]
            )
        assert raised.value.code == 2
        assert not output.exists()


def test_replay_refuses_docs_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    docs = checkout / "docs"
    docs.mkdir()
    source = tmp_path / "source.sqlite"
    with closing(open_writer(source)):
        pass
    raw = tmp_path / "recording"
    raw.mkdir()
    _cli_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_repo_root", lambda: checkout)
    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "replay",
                "run",
                "--raw",
                str(raw),
                "--db",
                str(source),
                "--out",
                str(docs / "out.sqlite"),
            ]
        )
    assert raised.value.code == 2
    assert "docs" in capsys.readouterr().err
    assert not (docs / "out.sqlite").exists()


def test_replay_refuses_overwriting_source_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _cli_settings(tmp_path, monkeypatch)
    source = settings.data_dir / "source.sqlite"
    with closing(open_writer(source)):
        pass
    original = source.read_bytes()
    raw = tmp_path / "recording"
    raw.mkdir()
    linked = settings.data_dir / "linked.sqlite"
    os.link(source, linked)
    for output in (source, linked):
        with pytest.raises(SystemExit) as raised:
            cli.main(
                ["replay", "run", "--raw", str(raw), "--db", str(source), "--out", str(output)]
            )
        assert raised.value.code == 2
        err = capsys.readouterr().err
        assert "overwrite" in err or "source" in err
    assert source.read_bytes() == original
    assert linked.read_bytes() == original


def test_replay_refuses_output_outside_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_settings(tmp_path, monkeypatch)
    source = tmp_path / "source.sqlite"
    with closing(open_writer(source)):
        pass
    raw = tmp_path / "recording"
    raw.mkdir()
    output = tmp_path / "elsewhere" / "replay.sqlite"
    with pytest.raises(SystemExit) as raised:
        cli.main(["replay", "run", "--raw", str(raw), "--db", str(source), "--out", str(output)])
    assert raised.value.code == 2
    assert "data dir" in capsys.readouterr().err
    assert not output.exists()


def test_verify_reports_injected_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _cli_settings(tmp_path, monkeypatch)
    root, source = _hot(tmp_path)
    out = settings.data_dir / "replay.sqlite"
    with pytest.raises(SystemExit) as ran:
        cli.main(["replay", "run", "--raw", str(root), "--db", str(source), "--out", str(out)])
    assert ran.value.code == 0
    assert out.is_file()
    with closing(open_writer(source)) as conn:
        conn.execute("ATTACH DATABASE ? AS replay", (str(out),))
        conn.execute("INSERT INTO main.assessments SELECT * FROM replay.assessments")
        conn.execute("DETACH DATABASE replay")
    with pytest.raises(SystemExit) as clean:
        cli.main(["replay", "verify", "--raw", str(root), "--db", str(source)])
    assert clean.value.code == 0
    clean_out = capsys.readouterr().out
    assert "match_rate=1.000000" in clean_out
    with closing(open_writer(source)) as conn:
        conn.execute(
            """
            UPDATE assessments
            SET net_profit = '999.999'
            WHERE id = (SELECT MIN(id) FROM assessments)
            """
        )
    with pytest.raises(SystemExit) as mismatched:
        cli.main(["replay", "verify", "--raw", str(root), "--db", str(source)])
    assert mismatched.value.code == 1
    text = capsys.readouterr().out
    assert "field=net_profit" in text
    assert "999.999" in text
    rate = next(line for line in text.splitlines() if line.startswith("match_rate="))
    assert float(rate.split("=", 1)[1]) < 1
