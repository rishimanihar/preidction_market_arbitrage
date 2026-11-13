"""Episode history page."""

from __future__ import annotations

import streamlit as st

from pmarb.app.formatting import format_chicago_time, format_money
from pmarb.app.pages.common import query_page
from pmarb.app.queries import fetch_episodes
from pmarb.http import allowed_market_url


def render() -> None:
    st.title("Episodes")
    search = st.text_input("Search group or market", placeholder="Team, market, or group")
    rows = query_page(fetch_episodes, search=search)
    if rows is None:
        return
    if not rows:
        st.info("No matching episodes were recorded.")
        return
    st.caption("Showing up to the 1,000 most recent episodes matching this search.")
    output = []
    for row in rows:
        output.append(
            {
                "Group": row.get("group_id", "Unavailable"),
                "YES market": row.get("yes_title") or row.get("yes_market_key", "Unavailable"),
                "NO market": row.get("no_title") or row.get("no_market_key", "Unavailable"),
                "Class": row.get("class", "Unavailable"),
                "First seen (Chicago)": format_chicago_time(row.get("first_seen_utc")),
                "Last seen (Chicago)": format_chicago_time(row.get("last_seen_utc")),
                "Observations": row.get("observations", "Unavailable"),
                "Peak net": format_money(row.get("peak_net")),
                "End reason": row.get("termination_reason", "Open"),
            }
        )
    st.dataframe(output, use_container_width=True, hide_index=True)
    for row in rows:
        with st.expander(f"{row.get('group_id', 'group')} · episode {row.get('id', '')}"):
            for label, key in (("YES market URL", "yes_url"), ("NO market URL", "no_url")):
                link = allowed_market_url(row.get(key))
                if link is not None:
                    st.link_button(label, link)
                else:
                    st.text(f"{label}: unavailable")
