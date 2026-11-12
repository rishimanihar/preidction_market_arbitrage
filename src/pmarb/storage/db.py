"""SQLite storage: migrations, typed writes, and a single-writer queue.

The collector is the only writer. Review and other readers open the file with
``mode=ro`` and must keep each transaction short: a long-held read stops WAL
checkpoints and the ``-wal`` file grows without bound.

Money and size columns are TEXT, holding ``str(Decimal)`` with no rescaling, so
``Decimal("0.1000")`` is stored as ``"0.1000"``. Instants are ISO-8601 UTC with
a ``Z`` suffix. INTEGER flags are ``0`` or ``1``.

``sqlite3`` connections are thread-bound. ``open_writer`` uses
``check_same_thread=True``. ``DbWriterTask`` opens the connection on one worker
thread and runs every statement there. Write ops must not ``commit``,
``rollback``, or ``close`` that connection; the task owns the transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue as thread_queue
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

from pmarb.models import (
    AssessmentClass,
    Episode,
    EquivalenceGroup,
    LegResult,
    Market,
    OpportunityAssessment,
)

_log = logging.getLogger(__name__)

COMMIT_INTERVAL_S = 0.5
CHECKPOINT_INTERVAL_S = 60.0
BUSY_TIMEOUT_MS = 5000
DEFAULT_BATCH_SIZE = 100
DB_WRITE_FAILED = "db_write_failed"

_MIGRATION_NAME = re.compile(r"^(\d+)_.+\.sql$")
_SYNC_NORMAL = 1


class SchemaVersionError(sqlite3.DatabaseError):
    """The on-disk ``schema_version`` cannot be opened by this code."""


def decimal_str(value: Decimal) -> str:
    """Exact ``str(Decimal)``. Rejects NaN and infinities."""
    if not isinstance(value, Decimal):
        raise TypeError(f"expected Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"non-finite Decimal: {value}")
    return str(value)


def format_utc(value: datetime) -> str:
    """ISO-8601 UTC with a ``Z`` suffix. Naive datetimes are rejected."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware")
    utc = value.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}"
    return text + "Z"


def bool_int(value: bool) -> int:
    """``True`` -> ``1``, ``False`` -> ``0``. Other ints are rejected."""
    if not isinstance(value, bool):
        raise TypeError(f"expected bool, got {type(value).__name__}")
    return 1 if value else 0


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, datetime):
        return format_utc(value)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def dumps_json(value: object) -> str:
    """JSON for TEXT columns. Keys are sorted; Decimal becomes its exact string."""
    return json.dumps(
        value,
        default=_json_default,
        sort_keys=True,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _opt_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_str(value)


def _opt_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return format_utc(value)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_parent(path: Path) -> None:
    parent = path.parent
    if parent == Path("") or parent.exists():
        return
    parent.mkdir(parents=True, exist_ok=True)
    _fsync_dir(parent)


def _statements(sql: str) -> list[str]:
    """Split a migration script on semicolons, respecting quotes."""
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_single = not in_single
            buf.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
        elif ch == ";" and not in_single and not in_double:
            statement = "".join(buf).strip()
            if statement:
                parts.append(statement)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _migration_scripts() -> list[tuple[int, str, str]]:
    root = files("pmarb.storage.migrations")
    found: list[tuple[int, str, str]] = []
    for entry in root.iterdir():
        name = entry.name
        if not name.endswith(".sql"):
            continue
        match = _MIGRATION_NAME.match(name)
        if match is None:
            raise SchemaVersionError(f"migration filename must be NNNN_name.sql, got {name}")
        found.append((int(match.group(1)), name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda item: (item[0], item[1]))
    versions = [version for version, _, _ in found]
    if len(versions) != len(set(versions)):
        raise SchemaVersionError("duplicate migration version numbers")
    if not found:
        raise SchemaVersionError("no migrations packaged in pmarb.storage.migrations")
    return found


def _known_version() -> int:
    return _migration_scripts()[-1][0]


def _read_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    if len(rows) != 1:
        raise SchemaVersionError(f"schema_version must contain exactly one row, found {len(rows)}")
    value = rows[0][0]
    if type(value) is not int:
        raise SchemaVersionError(f"schema_version is not an integer: {value!r}")
    return value


def _configure_writer(conn: sqlite3.Connection) -> None:
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        raise sqlite3.OperationalError(f"journal_mode WAL not enabled (got {mode})")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    sync = conn.execute("PRAGMA synchronous").fetchone()
    fk = conn.execute("PRAGMA foreign_keys").fetchone()
    busy = conn.execute("PRAGMA busy_timeout").fetchone()
    if sync is None or sync[0] != _SYNC_NORMAL:
        raise sqlite3.OperationalError(f"synchronous NORMAL not set (got {sync})")
    if fk is None or fk[0] != 1:
        raise sqlite3.OperationalError(f"foreign_keys not ON (got {fk})")
    if busy is None or busy[0] != BUSY_TIMEOUT_MS:
        raise sqlite3.OperationalError(f"busy_timeout not {BUSY_TIMEOUT_MS} (got {busy})")


def _apply_migration(conn: sqlite3.Connection, version: int, name: str, sql: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _statements(sql):
            conn.execute(statement)
        got = _read_version(conn)
        if got is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
        elif got < version:
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        elif got > version:
            raise SchemaVersionError(
                f"migration {name} left schema_version at {got}, expected {version}"
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    scripts = _migration_scripts()
    known = scripts[-1][0]
    current = _read_version(conn)
    applied = 0 if current is None else current
    if applied > known:
        raise SchemaVersionError(
            f"database schema_version {applied} is newer than this code supports ({known})"
        )
    for version, name, sql in scripts:
        if version <= applied:
            continue
        _apply_migration(conn, version, name, sql)
        applied = version


def open_writer(path: Path | str) -> sqlite3.Connection:
    """Open the collector's writer and apply any migrations it does not have yet.

    Parent directories are created. The connection is not thread-safe: use it
    only on the thread that opened it (see ``DbWriterTask``). Calling this again
    on a database already at the packaged ``schema_version`` is a no-op aside
    from setting the connection pragmas. A database whose ``schema_version`` is
    newer than the packaged migrations is left unchanged and raises
    ``SchemaVersionError``.
    """
    db_path = Path(path)
    _ensure_parent(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=True)
    try:
        current = _read_version(conn)
        known = _known_version()
        if current is not None and current > known:
            raise SchemaVersionError(
                f"database schema_version {current} is newer than this code supports ({known})"
            )
        _configure_writer(conn)
        _migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def open_reader(path: Path | str) -> sqlite3.Connection:
    """Open a read-only connection (``file:...?mode=ro`` and ``query_only=ON``).

    Raises ``FileNotFoundError`` when ``path`` is not an existing file; a missing
    database is never created here.

    Readers must keep transactions short. In WAL mode a long-held read prevents
    checkpoints, so the WAL file grows without bound even though the collector
    is the only writer.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        query_only = conn.execute("PRAGMA query_only").fetchone()
        if query_only is None or query_only[0] != 1:
            raise sqlite3.OperationalError(f"query_only not ON (got {query_only})")
    except Exception:
        conn.close()
        raise
    return conn


def _atomic(conn: sqlite3.Connection, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` in a transaction unless the caller already started one.

    ``DbWriterTask`` batches many ops inside one transaction, so writes join
    that transaction instead of committing early. A direct caller gets one
    transaction per call.
    """
    if conn.in_transaction:
        return fn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn()
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return result


def upsert_market(conn: sqlite3.Connection, market: Market, now: datetime) -> None:
    """Insert ``market`` or refresh it. ``first_seen_utc`` is kept on update.

    ``now`` is stored as both timestamps on insert, and as ``last_seen_utc`` on
    update. Rules text is not stored here; use ``insert_rules_version``.
    """
    seen = format_utc(now)
    params = (
        market.market_key,
        market.venue.value,
        market.market_id,
        market.title,
        market.league,
        dumps_json(list(market.participants)),
        market.yes_participant,
        market.market_type,
        _opt_decimal(market.line),
        _opt_dt(market.event_start_utc),
        _opt_dt(market.expected_resolution_utc),
        market.expected_resolution_src,
        _opt_dt(market.expected_payout_utc),
        market.status,
        market.rules_hash,
        dumps_json(market.fee_meta),
        market.url,
        seen,
        seen,
    )

    def run() -> None:
        conn.execute(
            """
            INSERT INTO markets (
                market_key, venue, venue_market_id, title, league, participants_json,
                yes_participant, market_type, line, event_start_utc, expected_resolution_utc,
                expected_resolution_src, expected_payout_utc, status, rules_hash,
                fee_meta_json, url, first_seen_utc, last_seen_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(market_key) DO UPDATE SET
                venue = excluded.venue,
                venue_market_id = excluded.venue_market_id,
                title = excluded.title,
                league = excluded.league,
                participants_json = excluded.participants_json,
                yes_participant = excluded.yes_participant,
                market_type = excluded.market_type,
                line = excluded.line,
                event_start_utc = excluded.event_start_utc,
                expected_resolution_utc = excluded.expected_resolution_utc,
                expected_resolution_src = excluded.expected_resolution_src,
                expected_payout_utc = excluded.expected_payout_utc,
                status = excluded.status,
                rules_hash = excluded.rules_hash,
                fee_meta_json = excluded.fee_meta_json,
                url = excluded.url,
                last_seen_utc = excluded.last_seen_utc
            """,
            params,
        )

    _atomic(conn, run)


def insert_rules_version(
    conn: sqlite3.Connection,
    market_key: str,
    rules_hash: str,
    rules_text: str,
    terms_text: str,
    captured_utc: datetime,
) -> None:
    """Insert one rules capture. A duplicate ``(market_key, rules_hash)`` is ignored."""
    captured = format_utc(captured_utc)

    def run() -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO rules_versions (
                market_key, rules_hash, rules_text, terms_text, captured_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (market_key, rules_hash, rules_text, terms_text, captured),
        )

    _atomic(conn, run)


def upsert_group(conn: sqlite3.Connection, group: EquivalenceGroup) -> None:
    """Insert or replace a group and its member rows.

    ``yes_maps_to`` stores ``GroupMember.native_side_for_outcome`` (``yes`` when
    that venue's YES is the group's outcome, ``no`` when YES is the complement).
    Members no longer on the group are deleted.
    """
    members = [
        (
            group.group_id,
            f"{member.venue.value}:{member.market_id}",
            member.native_side_for_outcome.value,
            member.approved_rules_hash,
        )
        for member in group.members
    ]
    table = [
        {
            "outcome_case": row.outcome_case,
            "payouts": row.payouts,
            "preserves_unit_payout": row.preserves_unit_payout,
        }
        for row in group.exception_table
    ]
    group_params = (
        group.group_id,
        group.status.value,
        bool_int(group.exception_risk),
        dumps_json(table),
        group.reviewer,
        _opt_dt(group.decided_utc),
        group.notes,
    )

    def run() -> None:
        conn.execute(
            """
            INSERT INTO groups (
                group_id, status, exception_risk, exception_table_json,
                reviewer, decided_utc, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                status = excluded.status,
                exception_risk = excluded.exception_risk,
                exception_table_json = excluded.exception_table_json,
                reviewer = excluded.reviewer,
                decided_utc = excluded.decided_utc,
                notes = excluded.notes
            """,
            group_params,
        )
        for params in members:
            conn.execute(
                """
                INSERT INTO group_members (
                    group_id, market_key, yes_maps_to, approved_rules_hash
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, market_key) DO UPDATE SET
                    yes_maps_to = excluded.yes_maps_to,
                    approved_rules_hash = excluded.approved_rules_hash
                """,
                params,
            )
        keys = [params[1] for params in members]
        if keys:
            placeholders = ",".join("?" * len(keys))
            conn.execute(
                f"DELETE FROM group_members WHERE group_id = ? AND market_key NOT IN ({placeholders})",
                (group.group_id, *keys),
            )
        else:
            conn.execute("DELETE FROM group_members WHERE group_id = ?", (group.group_id,))

    _atomic(conn, run)


def _fee_side(result: LegResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "provenance": result.fee.provenance,
        "conservative_bound": result.fee.conservative_bound,
        # Retain the actual fills: nonlinear fee scenarios cannot be recovered
        # from the average acquisition price. Older rows remain readable.
        "levels_consumed": [
            {"price": decimal_str(level.price), "size": decimal_str(level.size)}
            for level in result.levels_consumed
        ],
    }


def _row_id(cur: sqlite3.Cursor) -> int:
    row_id = cur.lastrowid
    if row_id is None or row_id <= 0:
        raise RuntimeError("insert did not return a row id")
    return int(row_id)


def insert_assessment(conn: sqlite3.Connection, assessment: OpportunityAssessment) -> int:
    """Append one assessment. ``class`` is ``result_class``; ``qualified`` is 1 only then."""
    yes = assessment.yes_result
    no = assessment.no_result
    params = (
        assessment.group_id,
        f"{assessment.yes_leg.venue.value}:{assessment.yes_leg.market_id}",
        f"{assessment.no_leg.venue.value}:{assessment.no_leg.market_id}",
        decimal_str(assessment.requested_qty),
        bool_int(assessment.result_class is AssessmentClass.QUALIFIED),
        dumps_json(list(assessment.reasons)),
        _opt_decimal(assessment.gross_edge),
        _opt_decimal(assessment.net_profit),
        _opt_decimal(assessment.roc),
        _opt_decimal(assessment.capital),
        _opt_decimal(None if yes is None else yes.fee.amount),
        _opt_decimal(None if no is None else no.fee.amount),
        dumps_json({"no": _fee_side(no), "yes": _fee_side(yes)}),
        _opt_decimal(None if yes is None else yes.avg_price),
        _opt_decimal(None if no is None else no.avg_price),
        _opt_decimal(assessment.max_profitable_qty),
        None if yes is None else yes.book_age_ms,
        None if no is None else no.book_age_ms,
        assessment.skew_ms,
        bool_int(assessment.books_atomic),
        assessment.result_class.value,
        bool_int(assessment.exception_risk),
        assessment.model_version,
        format_utc(assessment.computed_utc),
    )

    def run() -> int:
        cur = conn.execute(
            """
            INSERT INTO assessments (
                group_id, yes_market_key, no_market_key, qty, qualified, reasons_json,
                gross_edge, net_profit, roc, capital, fee_yes, fee_no, fee_provenance_json,
                avg_yes, avg_no, max_profitable_qty, age_yes_ms, age_no_ms, skew_ms,
                books_atomic, "class", exception_risk, model_version, computed_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            params,
        )
        return _row_id(cur)

    return int(_atomic(conn, run))


def _termination_reason(episode: Episode) -> str | None:
    if episode.termination is None:
        return None
    reason = episode.termination.value
    if episode.termination_detail:
        return f"{reason}:{episode.termination_detail}"
    return reason


def upsert_episode(conn: sqlite3.Connection, episode: Episode) -> int:
    """Insert an episode, or update it when ``episode.id`` is set. Returns the id.

    ``class`` is ``result_class``. ``termination_reason`` is the termination
    value, or ``value:detail`` when ``termination_detail`` is set (for example
    ``disqualified:stale``).
    """
    params_tail = (
        episode.group_id,
        f"{episode.yes_leg.venue.value}:{episode.yes_leg.market_id}",
        f"{episode.no_leg.venue.value}:{episode.no_leg.market_id}",
        decimal_str(episode.qty),
        episode.result_class.value,
        episode.model_version,
        format_utc(episode.first_seen_utc),
        format_utc(episode.last_seen_utc),
        episode.observations,
        decimal_str(episode.peak_net),
        decimal_str(episode.min_size),
        decimal_str(episode.max_size),
        _termination_reason(episode),
    )

    def run() -> int:
        if episode.id is None:
            cur = conn.execute(
                """
                INSERT INTO episodes (
                    group_id, yes_market_key, no_market_key, qty, "class", model_version,
                    first_seen_utc, last_seen_utc, observations, peak_net, min_size,
                    max_size, termination_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                params_tail,
            )
            return _row_id(cur)
        conn.execute(
            """
            INSERT INTO episodes (
                id, group_id, yes_market_key, no_market_key, qty, "class", model_version,
                first_seen_utc, last_seen_utc, observations, peak_net, min_size,
                max_size, termination_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                group_id = excluded.group_id,
                yes_market_key = excluded.yes_market_key,
                no_market_key = excluded.no_market_key,
                qty = excluded.qty,
                "class" = excluded."class",
                model_version = excluded.model_version,
                first_seen_utc = excluded.first_seen_utc,
                last_seen_utc = excluded.last_seen_utc,
                observations = excluded.observations,
                peak_net = excluded.peak_net,
                min_size = excluded.min_size,
                max_size = excluded.max_size,
                termination_reason = excluded.termination_reason
            """,
            (episode.id, *params_tail),
        )
        return int(episode.id)

    return int(_atomic(conn, run))


def insert_collector_event(
    conn: sqlite3.Connection,
    utc: datetime,
    venue: str | None,
    kind: str,
    detail: dict[str, Any],
) -> int:
    """Append one collector event. ``detail`` is stored as JSON."""
    if not isinstance(detail, dict):
        raise TypeError("detail must be a dict")
    stamped = format_utc(utc)
    payload = dumps_json(detail)

    def run() -> int:
        cur = conn.execute(
            """
            INSERT INTO collector_events (utc, venue, kind, detail_json)
            VALUES (?, ?, ?, ?)
            """,
            (stamped, venue, kind, payload),
        )
        return _row_id(cur)

    return int(_atomic(conn, run))


def insert_review_log(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    action: str,
    reviewer: str,
    shown_hashes: Mapping[str, Any] | Sequence[Any],
    result: str,
    spool_file: str | None,
    applied_utc: datetime,
) -> int:
    """Append one review decision. ``result`` is checked by the schema."""
    applied = format_utc(applied_utc)
    hashes = dumps_json(shown_hashes)

    def run() -> int:
        cur = conn.execute(
            """
            INSERT INTO review_log (
                group_id, action, reviewer, shown_hashes_json, result, spool_file, applied_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (group_id, action, reviewer, hashes, result, spool_file, applied),
        )
        return _row_id(cur)

    return int(_atomic(conn, run))


def _op_name(op: object) -> str:
    name = getattr(op, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return type(op).__name__


def _invoke(op: object, conn: sqlite3.Connection) -> None:
    if callable(op):
        op(conn)
        return
    apply = getattr(op, "apply", None)
    if callable(apply):
        apply(conn)
        return
    raise TypeError(f"write op must be a callable or have apply(conn), got {type(op).__name__}")


def _apply_batch(conn: sqlite3.Connection, ops: Sequence[object]) -> int:
    """Run ``ops`` in one transaction. A failed op rolls back only its savepoint.

    Successful siblings still commit. Each failure is then written to
    ``collector_events`` (kind ``db_write_failed``) in a new transaction.
    Returns the number of failed ops.
    """
    if not ops:
        return 0
    failures: list[tuple[object, Exception]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for op in ops:
            conn.execute("SAVEPOINT pmarb_op")
            try:
                _invoke(op, conn)
            except Exception as exc:  # noqa: BLE001 - any op error stays in its savepoint
                conn.execute("ROLLBACK TO SAVEPOINT pmarb_op")
                conn.execute("RELEASE SAVEPOINT pmarb_op")
                failures.append((op, exc))
                _log.exception("sqlite write op failed (%s)", _op_name(op))
            else:
                conn.execute("RELEASE SAVEPOINT pmarb_op")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    if not failures:
        return 0
    _log_write_failures(conn, failures)
    return len(failures)


def _log_write_failures(
    conn: sqlite3.Connection, failures: Sequence[tuple[object, Exception]]
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for op, exc in failures:
            insert_collector_event(
                conn,
                datetime.now(UTC),
                None,
                DB_WRITE_FAILED,
                {
                    "error": str(exc)[:500],
                    "error_type": type(exc).__name__,
                    "op": _op_name(op),
                },
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _checkpoint(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        _log.warning("rolling back an open transaction before wal checkpoint")
        conn.rollback()
    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")


def _drain_queue(q: asyncio.Queue[object]) -> list[object]:
    ops: list[object] = []
    while True:
        try:
            ops.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return ops


def _settle(fut: asyncio.Future[None], exc: BaseException | None) -> None:
    if fut.done():
        return
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(None)


def _retrieve_exception(fut: asyncio.Future[None]) -> None:
    if fut.cancelled():
        return
    fut.exception()


@dataclass(frozen=True, slots=True)
class Write:
    """A queued write. ``fn`` runs inside the writer's current batch transaction."""

    fn: Callable[[sqlite3.Connection], None]

    def apply(self, conn: sqlite3.Connection) -> None:
        self.fn(conn)

    def __call__(self, conn: sqlite3.Connection) -> None:
        self.apply(conn)


class DbWriterTask:
    """Single-writer task: batch commits, passive checkpoints, drain on cancel.

    The queue holds callables ``(conn) -> None`` or objects with
    ``apply(conn)``. Ops are committed every ``commit_interval_s`` (default
    0.5 s) or when the pending batch reaches ``batch_size``.
    ``PRAGMA wal_checkpoint(PASSIVE)`` runs every ``checkpoint_interval_s``
    (default 60 s).

    ``queue_depth`` is the number of ops still waiting in the queue, not
    including the batch currently being committed. ``failed_ops`` counts ops
    whose savepoint was rolled back. Those failures are also logged and stored
    as ``collector_events``.

    On ``CancelledError`` the task drains the queue, commits, closes the
    connection, and re-raises. ``CancelledError`` is never swallowed.
    """

    def __init__(
        self,
        path: Path | str,
        queue: asyncio.Queue[object],
        *,
        commit_interval_s: float = COMMIT_INTERVAL_S,
        batch_size: int = DEFAULT_BATCH_SIZE,
        checkpoint_interval_s: float = CHECKPOINT_INTERVAL_S,
    ) -> None:
        if not isinstance(queue, asyncio.Queue):
            raise TypeError("queue must be an asyncio.Queue")
        if commit_interval_s <= 0:
            raise ValueError("commit_interval_s must be positive")
        if checkpoint_interval_s <= 0:
            raise ValueError("checkpoint_interval_s must be positive")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.path = Path(path)
        self.queue = queue
        self.commit_interval_s = commit_interval_s
        self.batch_size = batch_size
        self.checkpoint_interval_s = checkpoint_interval_s
        self.failed_ops = 0
        self.checkpoint_count = 0
        self.ready = asyncio.Event()
        self._pending: list[object] = []
        self._next_commit = 0.0
        self._next_checkpoint = 0.0
        self._conn: sqlite3.Connection | None = None
        self._open_error: BaseException | None = None
        self._opened = threading.Event()
        self._jobs: thread_queue.Queue[Callable[[], None] | None] = thread_queue.Queue()
        self._thread: threading.Thread | None = None
        self._did_shutdown = False

    @property
    def queue_depth(self) -> int:
        """Ops waiting in the queue, not yet pulled into a batch."""
        return self.queue.qsize()

    def _worker(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = open_writer(self.path)
            self._conn = conn
        except Exception as exc:  # noqa: BLE001 - run() raises this on the event loop
            self._open_error = exc
            return
        finally:
            self._opened.set()
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                job()
        finally:
            self._conn = None
            if conn is not None:
                conn.close()

    def _submit(self, fn: Callable[[sqlite3.Connection], None]) -> asyncio.Future[None]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        fut.add_done_callback(_retrieve_exception)

        def work() -> None:
            assert self._conn is not None
            try:
                fn(self._conn)
            except BaseException as exc:
                loop.call_soon_threadsafe(_settle, fut, exc)
                if not isinstance(exc, Exception):
                    raise
            else:
                loop.call_soon_threadsafe(_settle, fut, None)

        self._jobs.put(work)
        return fut

    async def _exec(self, fn: Callable[[sqlite3.Connection], None]) -> None:
        await self._submit(fn)

    def _apply(self, conn: sqlite3.Connection, ops: Sequence[object]) -> None:
        self.failed_ops += _apply_batch(conn, ops)

    def _do_checkpoint(self, conn: sqlite3.Connection) -> None:
        try:
            _checkpoint(conn)
        except sqlite3.Error:
            _log.exception("passive wal checkpoint failed")
            return
        self.checkpoint_count += 1

    def _take_available(self) -> None:
        while len(self._pending) < self.batch_size:
            try:
                self._pending.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return

    async def _wait_for_work(self) -> None:
        self._take_available()
        if len(self._pending) >= self.batch_size:
            return
        timeout = min(self._next_commit, self._next_checkpoint) - time.monotonic()
        if timeout <= 0:
            return
        try:
            op = await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return
        self._pending.append(op)
        self._take_available()

    async def _loop(self) -> None:
        now = time.monotonic()
        self._next_commit = now + self.commit_interval_s
        self._next_checkpoint = now + self.checkpoint_interval_s
        while True:
            await self._wait_for_work()
            now = time.monotonic()
            full = len(self._pending) >= self.batch_size
            due = now >= self._next_commit
            if self._pending and (full or due):
                ops = self._pending
                self._pending = []
                await self._exec(lambda conn, ops=ops: self._apply(conn, ops))
                self._next_commit = time.monotonic() + self.commit_interval_s
            elif due:
                self._next_commit = now + self.commit_interval_s
            if time.monotonic() >= self._next_checkpoint:
                await self._exec(self._do_checkpoint)
                self._next_checkpoint = time.monotonic() + self.checkpoint_interval_s

    def _run_on_worker(self, fn: Callable[[sqlite3.Connection], None]) -> None:
        thread = self._thread
        if thread is None or not thread.is_alive():
            raise RuntimeError("db writer thread is not running")
        done = threading.Event()
        error: list[BaseException] = []

        def work() -> None:
            try:
                assert self._conn is not None
                fn(self._conn)
            except Exception as exc:  # noqa: BLE001 - raised on the event-loop thread
                error.append(exc)
            finally:
                done.set()

        self._jobs.put(work)
        if not done.wait(timeout=30):
            raise TimeoutError("db writer did not finish")
        if error:
            raise error[0]

    def _stop_thread(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            self._jobs.put(None)
            thread.join(timeout=10)
            if thread.is_alive():
                _log.error("db writer thread did not stop")

    def _shutdown(self, *, drain: bool) -> None:
        if self._did_shutdown:
            return
        self._did_shutdown = True
        try:
            alive = self._thread is not None and self._thread.is_alive()
            if drain and self._conn is not None and self._open_error is None and alive:
                ops = list(self._pending)
                self._pending = []
                ops.extend(_drain_queue(self.queue))
                if ops:
                    self._run_on_worker(lambda conn: self._apply(conn, ops))
        finally:
            self._stop_thread()

    async def run(self) -> None:
        """Open the database on the worker thread and write until cancelled."""
        self._thread = threading.Thread(
            target=self._worker,
            name="pmarb-db-writer",
            daemon=True,
        )
        self._thread.start()
        try:
            await asyncio.to_thread(self._opened.wait)
            if self._open_error is not None:
                raise self._open_error
            self.ready.set()
            await self._loop()
        except asyncio.CancelledError:
            try:
                self._shutdown(drain=True)
            except Exception:  # noqa: BLE001 - CancelledError must stay the task result
                _log.exception("db writer shutdown failed")
            raise
        except Exception:
            _log.exception("db writer stopped")
            try:
                self._shutdown(drain=False)
            except Exception:  # noqa: BLE001 - the original error must stay the task result
                _log.exception("db writer shutdown failed")
            raise
