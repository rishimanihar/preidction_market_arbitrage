"""Collector health and event history page."""

from __future__ import annotations

import streamlit as st

from pmarb.app.formatting import format_chicago_time
from pmarb.app.pages.common import query_page, show_value
from pmarb.app.queries import fetch_health_events


def render() -> None:
    st.title("Health")
    rows = query_page(fetch_health_events, limit=200)
    if rows is None:
        return
    health_rows = [row for row in rows if row.get("kind") == "health"]
    gap_error_rows = [row for row in rows if row.get("kind") in {"gap", "error"}]
    st.subheader("Latest collector health")
    if health_rows:
        latest = health_rows[0]
        st.caption(
            f"Newest heartbeat: {latest.get('venue') or 'All venues'} · {format_chicago_time(latest.get('utc'))}"
        )
        latest_by_venue = {}
        for row in health_rows:
            latest_by_venue.setdefault(row.get("venue") or "all venues", row)
        status_cols = st.columns(len(latest_by_venue))
        for col, (venue, row) in zip(status_cols, latest_by_venue.items(), strict=True):
            with col:
                st.markdown(f"**{venue}**")
                st.caption(format_chicago_time(row.get("utc")))
                st.code(show_value(row.get("detail", row.get("detail_json", {}))), language="json")
        st.dataframe(
            [
                {
                    "Time (Chicago)": format_chicago_time(row.get("utc")),
                    "Venue": row.get("venue") or "—",
                    "Details": row.get("detail", row.get("detail_json", {})),
                }
                for row in health_rows
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No health heartbeat events have been recorded.")
    st.subheader("Gaps and errors")
    if gap_error_rows:
        st.dataframe(
            [
                {
                    "Time (Chicago)": format_chicago_time(row.get("utc")),
                    "Kind": row.get("kind"),
                    "Venue": row.get("venue") or "—",
                    "Details": row.get("detail", row.get("detail_json", {})),
                }
                for row in gap_error_rows
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No gap or error events were recorded.")
