from __future__ import annotations

import sqlite3
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from pmarb.storage.db import SchemaVersionError, open_writer


def test_game_state_migration_applies_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "game-state.sqlite"
    with closing(sqlite3.connect(path)) as old:
        old.executescript(
            files("pmarb.storage.migrations")
            .joinpath("0001_initial.sql")
            .read_text(encoding="utf-8")
        )
    with closing(open_writer(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (2,)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(game_state)")}
        assert {"event_ticker", "home_points", "received_utc"} <= cols
    with closing(open_writer(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (2,)
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(game_state)")}
        assert "game_state_event_ticker_received_utc" in indexes


def test_future_schema_is_refused_after_v2(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    with closing(open_writer(path)) as conn:
        conn.execute("UPDATE schema_version SET version = 3")
    try:
        open_writer(path)
    except SchemaVersionError as exc:
        assert "newer than this code supports (2)" in str(exc)
    else:
        raise AssertionError("future schema was accepted")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone() == (3,)
