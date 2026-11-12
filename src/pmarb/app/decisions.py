"""Review decision validation. The review app never writes SQLite.

The bytes are written by ``pmarb.review.spool.write_decision``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pmarb.review.spool import write_decision as _spool_write_decision

_ACTIONS = {"approve", "reject", "revoke", "mark_exception_risk"}
_DECISION_KEYS = {
    "group_id",
    "action",
    "reviewer",
    "shown_hashes",
    "exception_table",
    "acknowledged_exceptions",
    "notes",
    "created_utc",
}
_EXCEPTION_KEYS = {"outcome_case", "payouts", "preserves_unit_payout"}


def build_decision(
    *,
    group_id: str,
    action: str,
    reviewer: str,
    shown_hashes: dict[str, str],
    exception_table: list[dict[str, Any]],
    acknowledged_exceptions: bool,
    notes: str = "",
    created_utc: str | datetime,
) -> dict[str, Any]:
    """Build the frozen spool payload and enforce explicit approval acknowledgment."""
    if (
        not isinstance(group_id, str)
        or not group_id.strip()
        or not isinstance(reviewer, str)
        or not reviewer.strip()
    ):
        raise ValueError("group_id and reviewer are required")
    if not isinstance(notes, str):
        raise TypeError("notes must be a string")
    if action not in _ACTIONS:
        raise ValueError(f"unsupported review action: {action}")
    if type(acknowledged_exceptions) is not bool:
        raise TypeError("acknowledged_exceptions must be bool")
    if action == "approve" and not acknowledged_exceptions:
        raise ValueError("approval requires acknowledgment of the exception table")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in shown_hashes.items()):
        raise TypeError("shown_hashes must map market keys to rules hashes")
    checked_exceptions: list[dict[str, Any]] = []
    for row in exception_table:
        if set(row) != _EXCEPTION_KEYS:
            raise ValueError("exception row has an invalid schema")
        if not isinstance(row["outcome_case"], str) or not isinstance(row["payouts"], dict):
            raise TypeError("exception row fields have invalid types")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in row["payouts"].items()):
            raise TypeError("exception payouts must map market keys to strings")
        if type(row["preserves_unit_payout"]) is not bool:
            raise TypeError("preserves_unit_payout must be bool")
        checked_exceptions.append(dict(row))
    if isinstance(created_utc, datetime):
        parsed_stamp = created_utc
    elif isinstance(created_utc, str):
        try:
            parsed_stamp = datetime.fromisoformat(created_utc)
        except ValueError as exc:
            raise ValueError("created_utc must be a valid aware timestamp") from exc
    else:
        raise TypeError("created_utc must be a datetime or ISO timestamp")
    if parsed_stamp.tzinfo is None or parsed_stamp.utcoffset() is None:
        raise ValueError("created_utc must be timezone-aware")
    stamp = parsed_stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "group_id": group_id,
        "action": action,
        "reviewer": reviewer,
        "shown_hashes": dict(shown_hashes),
        "exception_table": checked_exceptions,
        "acknowledged_exceptions": acknowledged_exceptions,
        "notes": notes,
        "created_utc": stamp,
    }


def write_decision(data_dir: str | Path, decision: dict[str, Any]) -> Path:
    """Validate one decision and spool it. Does not open SQLite."""
    if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
        raise ValueError("decision payload has an invalid schema")
    # Revalidate caller-created payloads before creating a file.
    decision = build_decision(**decision)
    group_id = decision["group_id"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", group_id):
        raise ValueError("invalid decision group_id filename component")
    return _spool_write_decision(Path(data_dir) / "spool" / "reviews", decision)
