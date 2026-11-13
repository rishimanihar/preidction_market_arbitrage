"""Streamlit entry point for the local, read-only review app."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from pmarb.app.pages import candidates, episodes, health, live, review
from pmarb.config import load_settings


def _database_path() -> Path:
    return load_settings().data_dir.expanduser() / "pmarb.sqlite"


def main() -> None:
    st.set_page_config(page_title="PM Arb Review", layout="wide")
    st.sidebar.caption("Local observation and review app · read-only market data")
    st.sidebar.text_input("Reviewer", key="reviewer", help="Name recorded with review decisions")
    try:
        database_path = _database_path()
    except (ValidationError, SettingsError, OSError):
        st.error(
            "Could not load local PM Arb settings. Check the config file and PMARB environment variables."
        )
        return
    st.session_state["database_path"] = str(database_path)

    pages = [
        st.Page(candidates.render, title="Candidates", icon="🔎", default=True),
        st.Page(review.render, title="Review", icon="📋"),
        st.Page(live.render, title="Live", icon="📈"),
        st.Page(episodes.render, title="Episodes", icon="🕘"),
        st.Page(health.render, title="Health", icon="🩺"),
    ]
    st.navigation(pages).run()


if __name__ == "__main__":
    main()
