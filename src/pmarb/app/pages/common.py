"""Small Streamlit-only helpers shared by page functions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import streamlit as st

from pmarb.app.queries import open_reader


def database_path() -> Path:
    return Path(st.session_state.get("database_path", "data/pmarb.sqlite"))


def query_page(fetch: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one short query with a read-only connection, then close it."""
    path = database_path()
    if not path.exists():
        st.info(f"No local database found at {path}.")
        return None
    try:
        conn = open_reader(path)
    except (OSError, ValueError, sqlite3.Error) as exc:
        st.error(f"Could not open the read-only database at {path}: {exc}")
        return None
    try:
        return fetch(conn, *args, **kwargs)
    finally:
        conn.close()


def show_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return "—" if value is None else str(value)
