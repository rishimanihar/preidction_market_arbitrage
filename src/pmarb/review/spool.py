"""JSON review spool. The review app writes files; the collector applies them.

Layout under the spool directory::

    <created_utc>-<group_id>-<action>.json   pending
    applied/                                 accepted decisions
    rejected/                                invalid or stale decisions

``write_decision`` is what the review app calls. It never opens SQLite. The
collector is the only writer: it passes a ``persist`` callback, normally one
that enqueues ``upsert_group`` and ``insert_review_log`` on ``DbWriterTask``.
``persist_sync`` does those two writes on a connection for tests and for a
collector op that already holds the writer connection.

Processing order for each pending file is: schema, unknown group,
already-applied recovery, the stale-review guard, action rules, then persist
and move. Recovery runs before the stale guard. A crash after persist and
before the move leaves the file in place; the next pass sees a group that
already records that reviewer, decision time, and hashes, and only moves the
file. A persist failure leaves the file pending and stops the pass. Files
already moved stay moved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pmarb.matching.rules import HASH_VERSION, member_market_key, rules_hash
from pmarb.models import EquivalenceGroup, ExceptionRow, GroupStatus, Market
from pmarb.storage.db import format_utc, insert_review_log, upsert_group

_log = logging.getLogger(__name__)

APPLIED_DIRNAME = "applied"
REJECTED_DIRNAME = "rejected"
MAX_DECISION_BYTES = 1 << 20
_UNSAFE_ID = re.compile(r"[/\\\x00]|\.\.")
_SORT_LAST = datetime.max.replace(tzinfo=UTC)

ResultName = Literal["applied", "stale_review", "invalid"]
ActionName = Literal["approve", "reject", "revoke", "mark_exception_risk"]


class ReviewDecision(BaseModel):
    """One human decision. The review app writes it; the collector reads it back.

    ``shown_hashes`` maps ``venue:market_id`` to the rules hash on screen.
    ``created_utc`` is when the reviewer submitted it, and becomes the group's
    ``decided_utc``. ``exception_table`` may be omitted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    group_id: str
    action: ActionName
    reviewer: str
    shown_hashes: dict[str, str]
    exception_table: tuple[ExceptionRow, ...] | None = None
    acknowledged_exceptions: bool = Field(strict=True)
    notes: str
    created_utc: datetime

    @field_validator("group_id", "reviewer", "notes", mode="before")
    @classmethod
    def _require_str(cls, value: object) -> object:
        if not isinstance(value, str):
            # Pydantic turns ValueError into ValidationError. TypeError would abort the pass.
            raise ValueError("must be a string")  # noqa: TRY004
        return value

    @field_validator("group_id")
    @classmethod
    def _group_id(cls, value: str) -> str:
        if value == "" or len(value) > 128 or _UNSAFE_ID.search(value):
            raise ValueError("group_id must be a safe non-empty token of at most 128 characters")
        return value

    @field_validator("reviewer")
    @classmethod
    def _reviewer(cls, value: str) -> str:
        if value.strip() == "" or len(value) > 200:
            raise ValueError("reviewer must be a non-empty name")
        return value

    @field_validator("shown_hashes", mode="before")
    @classmethod
    def _shown_hashes(cls, value: object) -> object:
        if not isinstance(value, dict):
            # Pydantic turns ValueError into ValidationError. TypeError would abort the pass.
            raise ValueError("shown_hashes must be an object")  # noqa: TRY004
        cleaned: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError("shown_hashes must map strings to strings")  # noqa: TRY004
            if key == "" or item == "":
                raise ValueError("shown_hashes keys and hashes must be non-empty")
            cleaned[key] = item
        return cleaned

    @field_validator("acknowledged_exceptions", mode="before")
    @classmethod
    def _ack(cls, value: object) -> object:
        # 1 and "true" are not an acknowledgement. bool is a subclass of int.
        if type(value) is not bool:
            raise ValueError("acknowledged_exceptions must be a boolean")
        return value

    @field_validator("created_utc", mode="before")
    @classmethod
    def _created(cls, value: object) -> datetime:
        return _parse_created_utc(value)

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str) -> str:
        if len(value) > 20_000:
            raise ValueError("notes are too long")
        return value


@dataclass(frozen=True, slots=True)
class ReviewLogEntry:
    """One ``review_log`` row. ``result`` matches the SQL check constraint."""

    group_id: str
    action: str
    reviewer: str
    shown_hashes: dict[str, str]
    result: ResultName
    spool_file: str
    applied_utc: datetime


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """What happened to one spool file."""

    source: Path
    destination: Path
    result: ResultName
    group_id: str | None
    action: str | None


@dataclass(frozen=True, slots=True)
class _Prepared:
    result: ResultName
    folder: str
    group: EquivalenceGroup | None
    entry: ReviewLogEntry | None
    group_id: str | None
    action: str | None


def write_decision(spool_dir: Path | str, decision: ReviewDecision | Mapping[str, Any]) -> Path:
    """Atomically write one decision file. Does not open or write SQLite.

    The file name is ``<created_utc>-<group_id>-<action>.json``, with
    ``created_utc`` in the collector's UTC ``Z`` form. The bytes hit a temp
    file in the same directory, are fsynced, then renamed into place.
    """
    if isinstance(decision, ReviewDecision):
        parsed = decision
    else:
        parsed = ReviewDecision.model_validate(dict(decision))
    root = Path(spool_dir)
    root.mkdir(parents=True, exist_ok=True)
    _fsync_dir(root)
    dest = root / _file_name(parsed)
    if dest.exists():
        raise FileExistsError(dest)
    payload = (
        json.dumps(
            _decision_body(parsed),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write(dest, payload)
    return dest


def apply_pending(
    spool_dir: Path | str,
    *,
    load_group: Callable[[str], EquivalenceGroup | None],
    load_markets: Callable[[EquivalenceGroup], Mapping[str, Market]],
    persist: Callable[[EquivalenceGroup | None, ReviewLogEntry], None],
) -> list[ApplyResult]:
    """Apply pending decision files, oldest ``created_utc`` first.

    ``load_group`` returns the group or ``None`` when the id is unknown.
    ``load_markets`` returns the member markets keyed by ``venue:market_id``.
    ``persist`` receives the updated group (``None`` when the group must not
    change) and the review-log row. A file that already landed on the group
    is moved to ``applied/`` without calling ``persist``.

    ``approve`` requires ``acknowledged_exceptions``. It stores the current
    rules hash on every member and sets ``exception_risk`` when any exception
    row does not preserve the $1 pair. That flag labels the group; it does
    not block approval. ``reject`` and ``revoke`` set those statuses.
    ``mark_exception_risk`` sets the flag and leaves status alone.

    A missing directory is an empty spool. A persist or filesystem error
    stops the pass; the file that failed stays pending.
    """
    root = Path(spool_dir)
    if not root.exists():
        return []
    if not root.is_dir():
        raise NotADirectoryError(root)
    results: list[ApplyResult] = []
    for path in _pending_files(root):
        prepared = _prepare(path, load_group, load_markets)
        if prepared.entry is not None:
            persist(prepared.group, prepared.entry)
        destination = _move(path, root / prepared.folder)
        results.append(
            ApplyResult(
                source=path,
                destination=destination,
                result=prepared.result,
                group_id=prepared.group_id,
                action=prepared.action,
            )
        )
        _log.info(
            "review %s group=%s action=%s file=%s",
            prepared.result,
            prepared.group_id,
            prepared.action,
            path.name,
        )
    return results


def persist_sync(
    conn: sqlite3.Connection,
) -> Callable[[EquivalenceGroup | None, ReviewLogEntry], None]:
    """Return a callback that upserts the group and appends the review log.

    Both writes share one transaction when ``conn`` is idle. When the
    collector's writer task has already started a transaction, the writes
    join it and this callback does not commit.
    """

    def persist(group: EquivalenceGroup | None, entry: ReviewLogEntry) -> None:
        if entry.result not in ("applied", "stale_review", "invalid"):
            raise ValueError(f"unsupported review result: {entry.result}")
        own = not conn.in_transaction
        if own:
            conn.execute("BEGIN IMMEDIATE")
        try:
            if group is not None:
                upsert_group(conn, group)
            insert_review_log(
                conn,
                group_id=entry.group_id,
                action=entry.action,
                reviewer=entry.reviewer,
                shown_hashes=entry.shown_hashes,
                result=entry.result,
                spool_file=entry.spool_file,
                applied_utc=entry.applied_utc,
            )
            if own:
                conn.commit()
        except Exception:
            if own and conn.in_transaction:
                conn.rollback()
            raise

    return persist


def _prepare(
    path: Path,
    load_group: Callable[[str], EquivalenceGroup | None],
    load_markets: Callable[[EquivalenceGroup], Mapping[str, Market]],
) -> _Prepared:
    decision, payload = _read_decision(path)
    if decision is None:
        entry = _invalid_entry(path, payload)
        return _Prepared(
            "invalid",
            REJECTED_DIRNAME,
            None,
            entry,
            _or_none(entry.group_id),
            _or_none(entry.action),
        )
    group = load_group(decision.group_id)
    if group is None:
        return _Prepared(
            "invalid",
            REJECTED_DIRNAME,
            None,
            _entry(decision, "invalid", path),
            decision.group_id,
            decision.action,
        )
    if _reflects(group, decision):
        return _Prepared("applied", APPLIED_DIRNAME, None, None, decision.group_id, decision.action)
    current = _current_hashes(group, load_markets(group))
    if current is None or current != decision.shown_hashes:
        return _Prepared(
            "stale_review",
            REJECTED_DIRNAME,
            None,
            _entry(decision, "stale_review", path),
            decision.group_id,
            decision.action,
        )
    if decision.action == "approve" and decision.acknowledged_exceptions is not True:
        return _Prepared(
            "invalid",
            REJECTED_DIRNAME,
            None,
            _entry(decision, "invalid", path),
            decision.group_id,
            decision.action,
        )
    updated = _apply_action(group, decision, current)
    return _Prepared(
        "applied",
        APPLIED_DIRNAME,
        updated,
        _entry(decision, "applied", path),
        decision.group_id,
        decision.action,
    )


def _reflects(group: EquivalenceGroup, decision: ReviewDecision) -> bool:
    """True when this file already landed: same reviewer, time, and hashes.

    Approve is identified by status plus each member's stored hash. Other
    actions do not rewrite those hashes, so they match on the status or flag
    they set. Current market text is not consulted: rules may have moved
    again after a successful persist, and this file must not be applied twice
    or filed as stale.
    """
    if group.reviewer != decision.reviewer or group.decided_utc != decision.created_utc:
        return False
    if decision.action == "approve":
        if group.status != GroupStatus.APPROVED:
            return False
        recorded = {
            member_market_key(member): member.approved_rules_hash for member in group.members
        }
        return recorded == decision.shown_hashes
    if decision.action == "reject":
        return group.status == GroupStatus.REJECTED
    if decision.action == "revoke":
        return group.status == GroupStatus.REVOKED
    if decision.action == "mark_exception_risk":
        return group.exception_risk is True
    return False


def _current_hashes(
    group: EquivalenceGroup,
    markets: Mapping[str, Market],
) -> dict[str, str] | None:
    hashes: dict[str, str] = {}
    for member in group.members:
        key = member_market_key(member)
        market = markets.get(key)
        if market is None or market.market_key != key:
            return None
        hashes[key] = rules_hash(market)
    return hashes


def _apply_action(
    group: EquivalenceGroup,
    decision: ReviewDecision,
    current: Mapping[str, str],
) -> EquivalenceGroup:
    stamp = {
        "reviewer": decision.reviewer,
        "decided_utc": decision.created_utc,
        "notes": decision.notes,
    }
    if decision.action == "approve":
        members = tuple(
            member.model_copy(update={"approved_rules_hash": current[member_market_key(member)]})
            for member in group.members
        )
        table = () if decision.exception_table is None else decision.exception_table
        # Any row that does not pay $1 labels the group. Approval still proceeds.
        risk = any(not row.preserves_unit_payout for row in table)
        return group.model_copy(
            update={
                **stamp,
                "members": members,
                "status": GroupStatus.APPROVED,
                "hash_version": HASH_VERSION,
                "exception_risk": risk,
                "exception_table": tuple(table),
            }
        )
    if decision.action == "reject":
        return group.model_copy(update={**stamp, "status": GroupStatus.REJECTED})
    if decision.action == "revoke":
        return group.model_copy(update={**stamp, "status": GroupStatus.REVOKED})
    if decision.action == "mark_exception_risk":
        update: dict[str, Any] = {**stamp, "exception_risk": True}
        if decision.exception_table is not None:
            update["exception_table"] = tuple(decision.exception_table)
        return group.model_copy(update=update)
    raise ValueError(f"unsupported review action: {decision.action}")


def _read_decision(path: Path) -> tuple[ReviewDecision | None, dict[str, Any] | None]:
    blob = path.read_bytes()
    if len(blob) > MAX_DECISION_BYTES:
        return None, None
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    try:
        return ReviewDecision.model_validate(payload), payload
    except ValidationError:
        return None, payload


def _entry(decision: ReviewDecision, result: ResultName, path: Path) -> ReviewLogEntry:
    return ReviewLogEntry(
        group_id=decision.group_id,
        action=decision.action,
        reviewer=decision.reviewer,
        shown_hashes=dict(decision.shown_hashes),
        result=result,
        spool_file=path.name,
        applied_utc=datetime.now(UTC),
    )


def _invalid_entry(path: Path, payload: dict[str, Any] | None) -> ReviewLogEntry:
    data = payload or {}
    hashes = data.get("shown_hashes")
    shown: dict[str, str] = {}
    if isinstance(hashes, dict):
        for key, item in hashes.items():
            if isinstance(key, str) and isinstance(item, str) and key != "" and item != "":
                shown[key] = item
    return ReviewLogEntry(
        group_id=_clip(data.get("group_id")),
        action=_clip(data.get("action")),
        reviewer=_clip(data.get("reviewer")),
        shown_hashes=shown,
        result="invalid",
        spool_file=path.name,
        applied_utc=datetime.now(UTC),
    )


def _pending_files(root: Path) -> list[Path]:
    files = [
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix == ".json" and not path.name.startswith(".")
    ]
    files.sort(key=_created_key)
    return files


def _created_key(path: Path) -> tuple[datetime, str]:
    try:
        if path.stat().st_size > MAX_DECISION_BYTES:
            return (_SORT_LAST, path.name)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return (_SORT_LAST, path.name)
    if not isinstance(payload, dict):
        return (_SORT_LAST, path.name)
    try:
        return (_parse_created_utc(payload.get("created_utc")), path.name)
    except (TypeError, ValueError):
        return (_SORT_LAST, path.name)


def _file_name(decision: ReviewDecision) -> str:
    return f"{format_utc(decision.created_utc)}-{decision.group_id}-{decision.action}.json"


def _decision_body(decision: ReviewDecision) -> dict[str, Any]:
    body: dict[str, Any] = {
        "acknowledged_exceptions": decision.acknowledged_exceptions,
        "action": decision.action,
        "created_utc": format_utc(decision.created_utc),
        "group_id": decision.group_id,
        "notes": decision.notes,
        "reviewer": decision.reviewer,
        "shown_hashes": dict(decision.shown_hashes),
    }
    if decision.exception_table is not None:
        body["exception_table"] = [row.model_dump(mode="json") for row in decision.exception_table]
    return body


def _parse_created_utc(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("created_utc must be timezone-aware")
        return value.astimezone(UTC)
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError("created_utc must be an ISO-8601 timestamp")
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("created_utc must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("created_utc must be timezone-aware")
    return parsed.astimezone(UTC)


def _atomic_write(dest: Path, payload: bytes) -> None:
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _move(path: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / path.name
    if dest.exists():
        if dest.is_file() and dest.read_bytes() == path.read_bytes():
            path.unlink()
            _fsync_dir(directory)
            _fsync_dir(path.parent)
            return dest
        dest = directory / f"{path.stem}-{os.getpid()}{path.suffix}"
    os.replace(path, dest)
    _fsync_dir(directory)
    if path.parent != directory:
        _fsync_dir(path.parent)
    return dest


def _clip(value: object, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _or_none(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value
