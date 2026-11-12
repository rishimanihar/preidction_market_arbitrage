"""Live assessment page."""

from __future__ import annotations

import streamlit as st

from pmarb.app.formatting import assessment_display_rows, rank_assessments
from pmarb.app.pages.common import query_page, show_value
from pmarb.app.queries import fetch_assessments


def render() -> None:
    st.title("Live assessments")
    refresh = st.checkbox("Auto-refresh every 5 seconds", value=False)

    @st.fragment(run_every="5s" if refresh else None)
    def show_assessments() -> None:
        fetched = query_page(fetch_assessments)
        if fetched is None:
            return
        rows = list(fetched)
        st.caption("Showing up to the 1,000 most recent persisted assessments.")
        tabs = st.tabs(["Qualified", "Indicative", "Rejected"])
        for tab, class_name in zip(tabs, ("qualified", "indicative", "rejected"), strict=True):
            with tab:
                if class_name == "indicative":
                    st.warning("Indicative results are not tradable evidence.")
                subset = [row for row in rows if row.get("class") == class_name]
                if class_name == "rejected":
                    reason_filter = st.text_input(
                        "Filter rejection reasons", key="rejection-filter"
                    )
                    if reason_filter:
                        subset = [
                            row
                            for row in subset
                            if reason_filter.casefold() in str(row.get("reasons", "")).casefold()
                        ]
                dust_rows = [row for row in subset if "dust" in str(row.get("reasons", "")).lower()]
                ranked_rows = [row for row in subset if row not in dust_rows]
                if not ranked_rows and not dust_rows:
                    st.info(f"No {class_name} assessments are available.")
                if ranked_rows:
                    st.dataframe(
                        assessment_display_rows(ranked_rows, rows),
                        use_container_width=True,
                        hide_index=True,
                    )
                    for row in rank_assessments(ranked_rows):
                        with st.expander(
                            f"{row.get('group_id', 'group')} · {row.get('class', class_name)} details"
                        ):
                            st.write(
                                f"YES market: {row.get('yes_title') or row.get('yes_market_key') or 'Unavailable'}"
                            )
                            st.write(
                                f"NO market: {row.get('no_title') or row.get('no_market_key') or 'Unavailable'}"
                            )
                            yes_url = row.get("yes_url")
                            no_url = row.get("no_url")
                            st.write(f"YES venue URL: {yes_url or 'Unavailable'}")
                            st.write(f"NO venue URL: {no_url or 'Unavailable'}")
                            st.write("Full persisted evidence")
                            st.code(show_value(row), language="json")
                if dust_rows:
                    st.subheader("Dust results · shown, not ranked")
                    st.dataframe(
                        assessment_display_rows(dust_rows, rows, ranked=False),
                        use_container_width=True,
                        hide_index=True,
                    )

    show_assessments()
