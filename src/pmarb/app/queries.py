"""Short, read-only SQLite queries for app pages."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pmarb.storage.db import open_reader as _open_reader

from .formatting import as_decimal

_MONEY_COLUMNS = {
    "qty",
    "gross_edge",
    "net_profit",
    "roc",
    "capital",
    "fee_yes",
    "fee_no",
    "avg_yes",
    "avg_no",
    "max_profitable_qty",
    "peak_net",
    "min_size",
    "max_size",
}


def open_reader(path: str | Path) -> sqlite3.Connection:
    """Open the storage layer's read-only connection for a short page query."""
    conn = _open_reader(path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in conn.execute(sql, params).fetchall():
        row = dict(raw)
        for name in _MONEY_COLUMNS & row.keys():
            row[name] = as_decimal(row[name])
        for name in (
            "participants_json",
            "reasons_json",
            "fee_provenance_json",
            "detail_json",
            "exception_table_json",
            "fee_meta_json",
        ):
            if row.get(name):
                try:
                    row[name.removesuffix("_json")] = json.loads(row[name])
                except (TypeError, json.JSONDecodeError):
                    row[name.removesuffix("_json")] = row[name]
        output.append(row)
    return output


def fetch_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Candidate groups with their matched markets and available evidence."""
    groups = _rows(conn, "SELECT * FROM groups WHERE status = 'candidate' ORDER BY group_id")
    for group in groups:
        members = _rows(
            conn,
            """SELECT gm.market_key, gm.yes_maps_to, gm.approved_rules_hash,
            m.venue, m.venue_market_id, m.title, m.league, m.participants_json,
            m.yes_participant, m.event_start_utc, m.url, m.rules_hash
            FROM group_members gm LEFT JOIN markets m ON m.market_key = gm.market_key
            WHERE gm.group_id = ? ORDER BY gm.market_key""",
            (group["group_id"],),
        )
        for member in members:
            if "participants" not in member and member.get("participants_json"):
                try:
                    member["participants"] = json.loads(member["participants_json"])
                except json.JSONDecodeError:
                    member["participants"] = member["participants_json"]
        group["members"] = members
        # Candidate.match_keys/evidence are not persisted. Reconstruct only
        # comparisons supported by current metadata and label the provenance.
        details = []
        match_keys = []
        for member in members:
            date_ny = None
            start = member.get("event_start_utc")
            if start:
                try:
                    parsed = datetime.fromisoformat(start)
                    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                        date_ny = parsed.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                except ValueError:
                    date_ny = None
            participants = tuple(sorted(member.get("participants") or ()))
            match_keys.append((member.get("league"), participants, date_ny))
            details.append(
                {
                    "market_key": member["market_key"],
                    "league": member.get("league"),
                    "participants": participants,
                    "event_date_ny": date_ny,
                    "yes_maps_to": member.get("yes_maps_to"),
                }
            )
        complete = len(details) >= 2
        league_match = complete and all(
            item["league"] == details[0]["league"] for item in details[1:]
        )
        participants_match = complete and all(
            item["participants"] == details[0]["participants"] for item in details[1:]
        )
        date_match = complete and all(
            item["event_date_ny"] is not None
            and item["event_date_ny"] == details[0]["event_date_ny"]
            for item in details[1:]
        )
        equal = league_match and participants_match and date_match
        group["match_keys"] = (match_keys[0],) if equal else tuple(match_keys)
        group["evidence"] = (
            "Reconstructed from stored metadata; original generator evidence unavailable.",
            f"league match: {bool(league_match and details[0]['league'])}",
            f"participant match: {bool(participants_match and details[0]['participants'])}",
            f"New York event-date match: {bool(date_match)}",
            "YES mapping: "
            + ", ".join(f"{item['market_key']}={item['yes_maps_to']}" for item in details),
        )
    return groups


def fetch_group_review(conn: sqlite3.Connection, group_id: str) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM groups WHERE group_id = ?", (group_id,))
    if not rows:
        return None
    group = rows[0]
    group["members"] = _rows(
        conn,
        """SELECT gm.market_key, gm.yes_maps_to,
        gm.approved_rules_hash, m.venue, m.venue_market_id, m.title, m.url, m.rules_hash,
        m.event_start_utc, m.expected_resolution_utc, m.expected_resolution_src,
        m.expected_payout_utc, m.fee_meta_json,
        rv.rules_text, rv.terms_text, rv.captured_utc
        FROM group_members gm LEFT JOIN markets m ON m.market_key = gm.market_key
        LEFT JOIN rules_versions rv ON rv.market_key = gm.market_key AND rv.rules_hash = m.rules_hash
        WHERE gm.group_id = ? ORDER BY gm.market_key""",
        (group_id,),
    )
    return group


def fetch_assessments(
    conn: sqlite3.Connection, class_name: str | None = None, limit: int = 1000
) -> list[dict[str, Any]]:
    """Rank a bounded window of the newest persisted assessments."""
    if limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, 1000)
    where = "WHERE a.class = ?" if class_name is not None else ""
    params = (class_name, limit) if class_name is not None else (limit,)
    rows = _rows(
        conn,
        f"""SELECT a.*, ym.title AS yes_title, ym.url AS yes_url,
        nm.title AS no_title, nm.url AS no_url
        FROM assessments a LEFT JOIN markets ym ON ym.market_key = a.yes_market_key
        LEFT JOIN markets nm ON nm.market_key = a.no_market_key
        {where} ORDER BY a.id DESC LIMIT ?""",
        params,
    )
    from .formatting import rank_assessments

    return rank_assessments(rows)


def fetch_episodes(
    conn: sqlite3.Connection, search: str = "", limit: int = 1000
) -> list[dict[str, Any]]:
    """Search the most recent bounded episode window, including market links."""
    if limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, 1000)
    pattern = f"%{search}%"
    return _rows(
        conn,
        """SELECT e.*, ym.url AS yes_url, nm.url AS no_url
        FROM episodes e LEFT JOIN markets ym ON ym.market_key = e.yes_market_key
        LEFT JOIN markets nm ON nm.market_key = e.no_market_key
        WHERE ? = '' OR e.group_id LIKE ? OR e.yes_market_key LIKE ? OR e.no_market_key LIKE ?
        ORDER BY e.id DESC LIMIT ?""",
        (search, pattern, pattern, pattern, limit),
    )


def fetch_health_events(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    return _rows(
        conn,
        """SELECT * FROM collector_events
        WHERE kind IN ('health','gap','error') ORDER BY utc DESC, id DESC LIMIT ?""",
        (limit,),
    )
