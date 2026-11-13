"""Deterministic simulator checks using synthetic books only."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from pmarb import cli
from pmarb.config import Settings
from pmarb.sim.engine import run_simulation
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
