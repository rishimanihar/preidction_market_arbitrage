"""Atomic review decision spool writer; the review app never writes SQLite."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    """Atomically spool one decision.

    TODO: delegate to pmarb.review.spool.write_decision once PR-5a lands.
    """
    if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
        raise ValueError("decision payload has an invalid schema")
    # Revalidate caller-created payloads before creating a file.
    decision = build_decision(**decision)
    stamp = decision["created_utc"]
    group_id = decision["group_id"]
    action = decision["action"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", group_id):
        raise ValueError("invalid decision group_id filename component")
    spool = Path(data_dir) / "spool" / "reviews"
    spool.mkdir(parents=True, exist_ok=True)
    destination = spool / f"{stamp}-{group_id}-{action}.json"
    payload = json.dumps(
        decision, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    fd, temp_name = tempfile.mkstemp(prefix=".review-", suffix=".tmp", dir=spool)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
        dir_fd = os.open(spool, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return destination
