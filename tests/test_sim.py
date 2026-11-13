"""Deterministic simulator checks using synthetic books only."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from pmarb import cli
from pmarb.config import Settings
from pmarb.fees.kalshi import KalshiFeeModel
from pmarb.fees.polymarket_us import PolymarketUSFeeModel
from pmarb.models import Level, Side
from pmarb.sim.engine import Decision, _episode_decisions, run_simulation
from pmarb.sim.report import render_sim_report
from pmarb.storage.db import open_writer, upsert_group
from tests import test_replay as replay


def _limited_pair(ns: int, *, qty: str = "1"):
    kalshi = replay._kalshi_body("0.30", "0.70")
    for level in kalshi["orderbooks"][0]["orderbook_fp"].values():
        level[0][1] = qty
    poly = replay._poly_body("0.90", "0.30")
    poly["marketData"]["offers"][0]["qty"] = qty
    poly["marketData"]["bids"][0]["qty"] = qty
    return [
        replay._envelope("kalshi", "rest_book", ns, kalshi, endpoint="/markets/orderbooks"),
        replay._envelope("polymarket_us", "rest_book", ns, poly, endpoint="/book"),
    ]


def _rows(path: Path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            """SELECT delay_yes_ms, delay_no_ms, haircut, yes_filled, no_filled,
                      paired_qty, naked_qty, outcome, realized_edge
               FROM sim_trades ORDER BY delay_yes_ms, delay_no_ms, haircut, id"""
        ).fetchall()


def test_asymmetric_arrivals_use_books_visible_at_each_leg(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="5")
    changed = replay._poly_body("0.90", "0.80")
    for level in changed["marketData"]["offers"] + changed["marketData"]["bids"]:
        level["qty"] = "5"
    records.append(replay._envelope("polymarket_us", "rest_book", 50_000_000, changed))
    raw, source = replay._hot(tmp_path, records)
    output = tmp_path / "sim.sqlite"

    result = run_simulation(
        raw,
        source,
        output,
        delays_ms=(),
        asymmetric=((0, 100),),
        haircuts=(Decimal(0),),
        order="fok",
        settings=replay._settings(),
    )

    assert result["opportunities"] >= 1
    rows = _rows(output)
    assert any(
        row[7] == "one_sided" and Decimal(row[3]) > 0 and Decimal(row[4]) == 0 for row in rows
    )


def test_ioc_partial_fill_and_fok_one_sided_fill(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="5")
    changed = replay._poly_body("0.90", "0.30")
    changed["marketData"]["bids"][0]["qty"] = "0.5"
    records.append(replay._envelope("polymarket_us", "rest_book", 50_000_000, changed))
    raw, source = replay._hot(tmp_path, records)

    fok = tmp_path / "fok.sqlite"
    ioc = tmp_path / "ioc.sqlite"
    common = {
        "delays_ms": (),
        "asymmetric": ((0, 100),),
        "haircuts": (Decimal(0),),
        "settings": replay._settings(),
    }
    run_simulation(raw, source, fok, order="fok", **common)
    run_simulation(raw, source, ioc, order="ioc", **common)

    assert any(row[7] == "one_sided" for row in _rows(fok))
    assert any(row[7] == "partial" and Decimal(row[6]) > 0 for row in _rows(ioc))


def test_haircut_reduces_depth_and_cash_ledger_can_prevent_fills(tmp_path: Path) -> None:
    raw, source = replay._hot(tmp_path, _limited_pair(0, qty="1"))
    output = tmp_path / "sim.sqlite"
    run_simulation(
        raw,
        source,
        output,
        delays_ms=(0,),
        haircuts=(Decimal(0), Decimal("0.5")),
        order="fok",
        settings=replay._settings(),
    )
    rows = _rows(output)
    assert any(row[2] == "0" and row[7] == "both" for row in rows)
    assert any(row[2] == "0.5" and row[7] == "neither" for row in rows)

    low_cash = tmp_path / "low-cash.sqlite"
    run_simulation(
        raw,
        source,
        low_cash,
        delays_ms=(0,),
        haircuts=(Decimal(0),),
        order="fok",
        cash_per_venue=Decimal("0.01"),
        settings=replay._settings(),
    )
    assert all(row[7] == "neither" for row in _rows(low_cash))


def test_liquidity_is_consumed_across_groups_until_book_updates(tmp_path: Path) -> None:
    raw, source = replay._hot(tmp_path, _limited_pair(0, qty="1"))
    with closing(open_writer(source)) as conn:
        original = replay._group()
        duplicate = original.model_copy(update={"group_id": "g-replay-2"})
        upsert_group(conn, duplicate)
    output = tmp_path / "sim.sqlite"
    run_simulation(
        raw,
        source,
        output,
        delays_ms=(0,),
        haircuts=(Decimal(0),),
        order="fok",
        settings=replay._settings(),
    )
    outcomes = [row[7] for row in _rows(output)]
    assert "both" in outcomes
    assert "neither" in outcomes
    assert "Simulation report" in render_sim_report(output, "md")
    assert json.loads(render_sim_report(output, "json"))["scenarios"]


def test_later_book_update_does_not_affect_earlier_arrival(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="5")
    changed = replay._poly_body("0.90", "0.80")
    for level in changed["marketData"]["offers"] + changed["marketData"]["bids"]:
        level["qty"] = "5"
    records.append(replay._envelope("polymarket_us", "rest_book", 200_000_000, changed))
    raw, source = replay._hot(tmp_path, records)
    output = tmp_path / "sim.sqlite"
    run_simulation(
        raw,
        source,
        output,
        delays_ms=(100,),
        haircuts=(Decimal(0),),
        order="fok",
        settings=replay._settings(),
    )
    assert any(row[7] == "both" for row in _rows(output))


def test_sim_cli_run_and_json_report(tmp_path: Path, monkeypatch, capsys) -> None:
    raw, source = replay._hot(tmp_path, _limited_pair(0, qty="5"))
    data_dir = tmp_path / "private-data"
    settings = Settings(data_dir=data_dir)
    monkeypatch.setattr(cli, "load_settings", lambda _env_file=None: settings)
    monkeypatch.setattr(cli, "_repo_root", lambda: None)
    output = data_dir / "simulation.sqlite"

    try:
        cli.main(
            [
                "sim",
                "run",
                "--raw",
                str(raw),
                "--db",
                str(source),
                "--out",
                str(output),
                "--delays",
                "0",
                "--haircut",
                "0",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 0
    assert output.is_file()
    capsys.readouterr()

    try:
        cli.main(["sim", "report", "--db", str(output), "--format", "json", "--out", "-"])
    except SystemExit as exc:
        assert exc.code == 0
    assert json.loads(capsys.readouterr().out)["scenarios"]


def test_haircut_scenarios_share_jitter_and_are_monotone(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="5")
    changed = replay._poly_body("0.90", "0.80")
    for level in changed["marketData"]["offers"] + changed["marketData"]["bids"]:
        level["qty"] = "5"
    records.append(replay._envelope("polymarket_us", "rest_book", 50_000_000, changed))
    raw, source = replay._hot(tmp_path, records)
    output = tmp_path / "monotone.sqlite"

    run_simulation(
        raw,
        source,
        output,
        delays_ms=(0,),
        haircuts=(Decimal(0), Decimal("0.25"), Decimal("0.5")),
        jitter_ms=100,
        seed=0,
        order="fok",
        settings=replay._settings(),
    )

    with closing(sqlite3.connect(output)) as conn:
        rows = conn.execute(
            """SELECT haircut, arrival_yes_ms, arrival_no_ms, outcome, realized_edge
               FROM sim_trades ORDER BY haircut"""
        ).fetchall()
    assert len(rows) == 3
    assert len({(row[1], row[2]) for row in rows}) == 1
    assert [row[3] == "both" for row in rows] == sorted(
        [row[3] == "both" for row in rows], reverse=True
    )
    edges = [Decimal(row[4]) for row in rows]
    assert edges == sorted(edges, reverse=True)


def test_delay_on_decaying_gap_reduces_fills_and_edge(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="5")
    changed = replay._poly_body("0.90", "0.80")
    for level in changed["marketData"]["offers"] + changed["marketData"]["bids"]:
        level["qty"] = "5"
    records.append(replay._envelope("polymarket_us", "rest_book", 150_000_000, changed))
    raw, source = replay._hot(tmp_path, records)
    output = tmp_path / "delay-decay.sqlite"

    run_simulation(
        raw,
        source,
        output,
        delays_ms=(100, 250),
        haircuts=(Decimal(0),),
        order="fok",
        settings=replay._settings(),
    )

    with closing(sqlite3.connect(output)) as conn:
        rows = conn.execute(
            """SELECT delay_yes_ms, SUM(outcome = 'both'), SUM(CAST(realized_edge AS REAL))
               FROM sim_trades GROUP BY delay_yes_ms ORDER BY delay_yes_ms"""
        ).fetchall()
    assert rows[0][1] > rows[1][1]
    assert rows[0][2] > rows[1][2]


def test_one_simulation_trial_per_positive_episode_size(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="1")
    better = replay._kalshi_body("0.20", "0.70")
    records.append(
        replay._envelope("kalshi", "rest_book", 250_000_000, better, endpoint="/markets/orderbooks")
    )
    raw, source = replay._hot(tmp_path, records)
    output = tmp_path / "episode-dedup.sqlite"

    result = run_simulation(
        raw,
        source,
        output,
        delays_ms=(0,),
        haircuts=(Decimal(0),),
        order="fok",
        settings=replay._settings(),
    )

    assert result["opportunities"] == 1
    assert len(_rows(output)) == 1


def test_repeated_assessments_map_to_one_episode_decision(tmp_path: Path) -> None:
    raw, source = replay._hot(tmp_path, _limited_pair(0, qty="1"))
    captured = []
    replay.ReplayEngine(
        raw,
        source,
        tmp_path / "episode-source.sqlite",
        settings=replay._settings(),
        on_assessment=lambda mono, assessment: captured.append(Decision(mono, assessment)),
    ).run()
    first = next(
        item for item in captured if item.assessment.net_profit and item.assessment.net_profit > 0
    )
    later_assessment = first.assessment.model_copy(
        update={"computed_utc": first.assessment.computed_utc + timedelta(milliseconds=500)}
    )
    repeated = Decision(first.mono_ns + 500_000_000, later_assessment)
    assessment = first.assessment
    episode = (
        1,
        assessment.group_id,
        f"{assessment.yes_leg.venue.value}:{assessment.yes_leg.market_id}",
        f"{assessment.no_leg.venue.value}:{assessment.no_leg.market_id}",
        str(assessment.requested_qty),
        assessment.result_class.value,
        assessment.model_version,
        assessment.computed_utc.isoformat(),
        later_assessment.computed_utc.isoformat(),
    )

    unique = _episode_decisions([first, repeated], [episode], max_gap_s=2)

    assert unique == [first]


def test_fill_prices_and_both_pr1_fees_match_visible_ladders(tmp_path: Path) -> None:
    raw, source = replay._hot(tmp_path, _limited_pair(0, qty="1"))
    output = tmp_path / "fees.sqlite"
    run_simulation(
        raw, source, output, delays_ms=(0,), haircuts=(Decimal(0),), settings=replay._settings()
    )

    with closing(sqlite3.connect(output)) as conn:
        row = conn.execute(
            "SELECT yes_filled, no_filled, yes_cost, no_cost, yes_fee, no_fee FROM sim_trades"
        ).fetchone()
    assert row is not None
    assert Decimal(row[0]) == Decimal(1) and Decimal(row[1]) == Decimal(1)
    assert Decimal(row[2]) / Decimal(row[0]) == Decimal("0.30")
    assert Decimal(row[3]) / Decimal(row[1]) == Decimal("0.30")
    yes_fee = KalshiFeeModel(multiplier=Decimal(1), precision=Decimal("0.01")).taker_fee(
        [Level(price=Decimal("0.30"), size=Decimal(1))], Side.YES
    )
    no_fee = PolymarketUSFeeModel(theta=Decimal("0.095")).taker_fee(
        [Level(price=Decimal("0.30"), size=Decimal(1))], Side.NO
    )
    assert Decimal(row[4]) == yes_fee.amount
    assert Decimal(row[5]) == no_fee.amount


def test_cached_arrival_book_cannot_create_a_fill(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="1")
    records[1] = replace(records[1], cache_headers={"Cache-Control": "max-age=60"})
    raw, source = replay._hot(tmp_path, records)
    output = tmp_path / "cached.sqlite"

    run_simulation(
        raw, source, output, delays_ms=(0,), haircuts=(Decimal(0),), settings=replay._settings()
    )

    assert all(row[7] == "neither" for row in _rows(output))


def test_stale_arrival_book_cannot_create_a_fill(tmp_path: Path) -> None:
    raw, source = replay._hot(tmp_path, _limited_pair(0, qty="1"))
    output = tmp_path / "stale.sqlite"

    run_simulation(
        raw, source, output, delays_ms=(2501,), haircuts=(Decimal(0),), settings=replay._settings()
    )

    assert all(row[7] == "neither" for row in _rows(output))


def test_invalid_arrival_book_cannot_create_a_fill(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="1")
    crossed = replay._poly_body("0.90", "0.30")
    crossed["marketData"]["bids"][0]["px"]["value"] = "0.95"
    records.append(replay._envelope("polymarket_us", "rest_book", 50_000_000, crossed))
    raw, source = replay._hot(tmp_path, records)
    output = tmp_path / "invalid.sqlite"

    run_simulation(
        raw, source, output, delays_ms=(100,), haircuts=(Decimal(0),), settings=replay._settings()
    )

    assert all(row[5] == "0" for row in _rows(output))


def test_raw_gap_invalidates_pre_gap_books_until_a_new_snapshot(tmp_path: Path) -> None:
    records = _limited_pair(0, qty="1")
    records.append(replay._envelope("kalshi", "ws_msg", 50_000_000, {"tail": True}, status=None))
    raw, source = replay._hot(tmp_path, records)
    path = next((raw / "raw" / "kalshi").rglob("*.gz"))
    path.write_bytes(path.read_bytes()[:-5])
    output = tmp_path / "raw-gap.sqlite"

    run_simulation(
        raw, source, output, delays_ms=(100,), haircuts=(Decimal(0),), settings=replay._settings()
    )

    assert all(row[5] == "0" for row in _rows(output))
