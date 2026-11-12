"""Human review page. Decisions are written to the spool, never SQLite."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import streamlit as st

from pmarb.app.decisions import build_decision, write_decision
from pmarb.app.formatting import format_chicago_time
from pmarb.app.pages.common import database_path, query_page, show_value
from pmarb.app.queries import fetch_candidates, fetch_group_review


def _members(review: dict[str, Any]) -> list[dict[str, Any]]:
    value = review.get("markets", review.get("members", []))
    return list(value or [])


def render() -> None:
    st.title("Review")
    st.caption("Review both contracts and their settlement exceptions before recording a decision.")
    candidates = query_page(fetch_candidates)
    if candidates is None:
        return
    ids = [str(row["group_id"]) for row in candidates if row.get("group_id")]
    if not ids:
        st.info("No candidate groups are available for review.")
        return
    group_id = st.selectbox("Candidate group", ids)
    detail = query_page(fetch_group_review, group_id)
    if detail is None:
        return
    members = _members(detail)
    if not members:
        st.error("The selected group has no market details.")
        return

    exception_table = detail.get("exception_table", detail.get("exception_table_json"))
    revision = hashlib.sha256(
        json.dumps(
            {
                "hashes": [member.get("rules_hash") for member in members],
                "exception_table": exception_table,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]
    columns = st.columns(max(2, len(members)))
    for index, market in enumerate(members):
        col = columns[index]
        with col:
            st.subheader(market.get("title") or market.get("market_key", f"Market {index + 1}"))
            st.caption(str(market.get("venue", "Venue unavailable")))
            if market.get("url"):
                st.link_button("Open market", market["url"])
            st.markdown(f"**Current rules hash**  \n`{market.get('rules_hash') or 'unavailable'}`")
            st.markdown("**Rules text**")
            st.text_area(
                "Rules",
                str(market.get("rules_text") or "Unavailable"),
                height=230,
                key=f"rules-{group_id}-{market.get('rules_hash') or 'missing'}-{index}",
                disabled=True,
                label_visibility="collapsed",
            )
            st.markdown("**Contract terms PDF text**")
            terms = market.get("terms_text")
            st.text_area(
                "Terms",
                str(terms or "No terms PDF text stored"),
                height=160,
                key=f"terms-{group_id}-{market.get('rules_hash') or 'missing'}-{index}",
                disabled=True,
                label_visibility="collapsed",
            )
            st.caption(
                "Kalshi terms PDF (text only; no fetch performed)."
                if "kalshi" in str(market.get("venue", "")).lower()
                else "Settlement terms captured with this market."
            )
            fee_meta = market.get("fee_meta") or {}
            terms_pdf_url = market.get("terms_pdf_url") or fee_meta.get("terms_pdf_url")
            st.caption(f"Terms PDF link: {terms_pdf_url or 'Unavailable in persisted metadata'}")
            for label, key in (
                ("Settlement source", "settlement_source"),
                ("Event start", "event_start_utc"),
                ("Expected resolution", "expected_resolution_utc"),
                ("Expected payout", "expected_payout_utc"),
                ("Resolution source", "expected_resolution_src"),
            ):
                value = market.get(key)
                if key.endswith("_utc"):
                    value = format_chicago_time(value)
                st.write(f"**{label}:** {value or 'Unavailable'}")

    table = exception_table
    st.subheader("Proposed exception table")
    if isinstance(table, list) and table:
        st.dataframe(table, use_container_width=True, hide_index=True)
    elif table:
        st.code(show_value(table), language="json")
    else:
        st.warning("No exception table is stored for this group.")

    st.subheader("Review checklist")
    checklist_a = st.checkbox(
        "I reviewed the settlement exceptions and acknowledge this table.",
        key=f"ack-{group_id}-{revision}",
    )
    checklist_b = st.checkbox(
        "I reviewed the contract terms PDF text for the Kalshi market.",
        key=f"pdf-{group_id}-{revision}",
    )
    reviewer = st.session_state.get("reviewer", "").strip()
    notes = st.text_area("Reviewer notes", key=f"notes-{group_id}")
    if not reviewer:
        st.warning("Enter an explicit reviewer name in the sidebar before recording a decision.")

    hashes_complete = len(members) >= 2 and all(
        member.get("rules_hash") and member.get("rules_text") for member in members
    )
    table_complete = isinstance(table, list) and bool(table)
    kalshi_terms_complete = any(
        "kalshi" in str(member.get("venue", "")).lower() and member.get("terms_text")
        for member in members
    )
    if not hashes_complete:
        st.warning("Approval is unavailable until both current rules texts and hashes are present.")
    if not table_complete:
        st.warning("Approval is unavailable until a proposed exception table is present.")
    if not kalshi_terms_complete:
        st.warning("Approval is unavailable because the Kalshi contract terms PDF text is missing.")

    action_cols = st.columns(3)
    actions = (
        ("Approve", "approve"),
        ("Reject", "reject"),
        ("Mark exception risk", "mark_exception_risk"),
    )
    for col, (label, action) in zip(action_cols, actions, strict=True):
        disabled = not reviewer or (
            action == "approve"
            and not (
                checklist_a
                and checklist_b
                and hashes_complete
                and table_complete
                and kalshi_terms_complete
            )
        )
        if col.button(label, disabled=disabled, key=f"{action}-{group_id}"):
            try:
                shown_hashes = {
                    str(market.get("market_key")): str(market["rules_hash"])
                    for market in members
                    if market.get("market_key") and market.get("rules_hash")
                }
                decision = build_decision(
                    group_id=group_id,
                    action=action,
                    reviewer=reviewer,
                    shown_hashes=shown_hashes,
                    exception_table=table if isinstance(table, list) else [],
                    acknowledged_exceptions=checklist_a,
                    notes=notes,
                    created_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                )
                write_decision(database_path().parent, decision)
            except (TypeError, ValueError, OSError) as exc:
                st.error(f"Could not record decision: {exc}")
            else:
                st.success(f"{label} decision saved to the review spool.")
