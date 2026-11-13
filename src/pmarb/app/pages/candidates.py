"""Candidate queue page."""

from __future__ import annotations

from typing import Any

import streamlit as st

from pmarb.app.pages.common import query_page, show_value
from pmarb.app.queries import fetch_candidates
from pmarb.http import allowed_market_url


def render() -> None:
    st.title("Candidates")
    st.caption("Proposed market equivalences awaiting human review.")
    rows = query_page(fetch_candidates)
    if rows is None:
        return
    if not rows:
        st.info("No candidate groups are currently queued.")
        return
    for row in rows:
        group_id = str(row.get("group_id", "(unknown group)"))
        with st.expander(f"{group_id} · {row.get('status', 'candidate')}", expanded=False):
            match_keys = row.get("match_keys")
            st.write("Match keys", match_keys if match_keys else "Unavailable")
            if row.get("evidence"):
                st.write("Persisted match evidence")
                st.json(row["evidence"])
            else:
                st.caption(
                    "Original generator evidence is unavailable; the database schema stores market metadata only."
                )
            members = row.get("members") or []
            if members:
                for member in members:
                    venue = str(member.get("venue") or "Market")
                    title = str(member.get("title") or member.get("market_key") or "unknown")
                    st.text(f"{venue} · {title}")
                    link = allowed_market_url(member.get("url"))
                    if link is not None:
                        st.link_button("Open market", link)
                    st.caption(f"Match key: {member.get('market_key', 'unavailable')}")
                    if member.get("yes_maps_to"):
                        st.caption(f"YES mapping: {member['yes_maps_to']}")
                    evidence = member.get("evidence")
                    if evidence:
                        st.code(show_value(evidence), language="json")
                if not row.get("evidence"):
                    st.caption(
                        "Original generator match evidence is not persisted in the current database schema."
                    )
            else:
                st.write("Match keys", row.get("market_keys", "unavailable"))
                evidence: Any = row.get("evidence")
                if evidence:
                    st.json(evidence)
                else:
                    st.caption(
                        "Original generator match evidence is not persisted in the current database schema."
                    )
            if row.get("exception_table"):
                st.write("Proposed exception table")
                st.dataframe(row["exception_table"], use_container_width=True, hide_index=True)
            elif row.get("exception_table_json"):
                st.code(show_value(row["exception_table_json"]), language="json")
