"""In-memory markets and groups, loaded back from SQLite after a restart.

The groups table does not store ``outcome`` (the scheduler and the evaluator
do not read it). A group loaded from disk uses its id as the outcome text.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from pmarb.collector.clock import parse_utc
from pmarb.models import (
    EquivalenceGroup,
    ExceptionRow,
    GroupMember,
    GroupStatus,
    Market,
    Side,
    Venue,
)

_log = logging.getLogger(__name__)


class Registry:
    """Markets and equivalence groups the scheduler and the evaluator share."""

    def __init__(self) -> None:
        self.markets: dict[str, Market] = {}
        self.groups: dict[str, EquivalenceGroup] = {}

    def market(self, venue: Venue, market_id: str) -> Market | None:
        return self.markets.get(f"{venue.value}:{market_id}")

    def markets_for(self, group: EquivalenceGroup) -> dict[str, Market]:
        """Member markets keyed by ``venue:market_id``. Missing members are omitted."""
        found: dict[str, Market] = {}
        for member in group.members:
            key = f"{member.venue.value}:{member.market_id}"
            market = self.markets.get(key)
            if market is not None:
                found[key] = market
        return found


def read_registry(conn: sqlite3.Connection) -> Registry:
    """Load markets and groups. Rows that cannot be rebuilt are skipped."""
    registry = Registry()
    rules = _rules_text(conn)
    for row in conn.execute(
        """
        SELECT market_key, venue, venue_market_id, title, league, participants_json,
               yes_participant, market_type, line, event_start_utc, expected_resolution_utc,
               expected_resolution_src, expected_payout_utc, status, rules_hash,
               fee_meta_json, url
        FROM markets
        """
    ):
        market = _market_from_row(row, rules.get(row[0], ""))
        if market is not None:
            registry.markets[market.market_key] = market
    members = _members(conn)
    for row in conn.execute(
        """
        SELECT group_id, status, exception_risk, exception_table_json,
               reviewer, decided_utc, notes
        FROM groups
        """
    ):
        group = _group_from_row(row, members.get(row[0], ()))
        if group is not None:
            registry.groups[group.group_id] = group
    return registry


def read_human_rejects(conn: sqlite3.Connection) -> set[str]:
    """Group ids a person rejected. Auto mode must not approve these again."""
    rows = conn.execute(
        """
        SELECT DISTINCT group_id FROM review_log
        WHERE action = 'reject' AND result = 'applied'
          AND reviewer NOT LIKE 'auto:%'
          AND group_id IS NOT NULL AND group_id != ''
        """
    )
    return {str(row[0]) for row in rows}


def _rules_text(conn: sqlite3.Connection) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, _rules_hash, text in conn.execute(
        """
        SELECT rv.market_key, rv.rules_hash, rv.rules_text
        FROM rules_versions rv
        JOIN markets m ON m.market_key = rv.market_key AND m.rules_hash = rv.rules_hash
        """
    ):
        found[str(key)] = "" if text is None else str(text)
    return found


def _members(conn: sqlite3.Connection) -> dict[str, tuple[GroupMember, ...]]:
    grouped: dict[str, list[GroupMember]] = {}
    rows = conn.execute(
        """
        SELECT group_id, market_key, yes_maps_to, approved_rules_hash
        FROM group_members
        ORDER BY group_id, market_key
        """
    )
    for group_id, market_key, yes_maps_to, approved_hash in rows:
        member = _member(market_key, yes_maps_to, approved_hash)
        if member is None:
            continue
        grouped.setdefault(str(group_id), []).append(member)
    return {key: tuple(value) for key, value in grouped.items()}


def _member(market_key: object, yes_maps_to: object, approved_hash: object) -> GroupMember | None:
    if not isinstance(market_key, str) or ":" not in market_key:
        return None
    venue_text, market_id = market_key.split(":", 1)
    if market_id == "":
        return None
    try:
        venue = Venue(venue_text)
        side = Side(str(yes_maps_to))
    except ValueError:
        return None
    stored = approved_hash if isinstance(approved_hash, str) and approved_hash != "" else None
    try:
        return GroupMember(
            venue=venue,
            market_id=market_id,
            native_side_for_outcome=side,
            approved_rules_hash=stored,
        )
    except ValidationError:
        return None


def _market_from_row(row: tuple[object, ...], rules_text: str) -> Market | None:
    key, venue_text, market_id, title, league = row[:5]
    if not isinstance(venue_text, str) or not isinstance(market_id, str) or market_id == "":
        return None
    try:
        venue = Venue(venue_text)
    except ValueError:
        _log.warning("skipping market %s with unknown venue", key)
        return None
    try:
        return Market(
            venue=venue,
            market_id=market_id,
            title=title if isinstance(title, str) and title != "" else market_id,
            league=league if isinstance(league, str) and league != "" else None,
            participants=_participants(row[5]),
            yes_participant=row[6] if isinstance(row[6], str) and row[6] != "" else None,
            market_type=row[7] if isinstance(row[7], str) and row[7] != "" else "unknown",
            line=_decimal(row[8]),
            event_start_utc=parse_utc(row[9] if isinstance(row[9], str) else None),
            expected_resolution_utc=parse_utc(row[10] if isinstance(row[10], str) else None),
            expected_resolution_src=row[11] if isinstance(row[11], str) and row[11] != "" else None,
            expected_payout_utc=parse_utc(row[12] if isinstance(row[12], str) else None),
            status=row[13] if isinstance(row[13], str) and row[13] != "" else "unknown",
            rules_text=rules_text,
            rules_hash=row[14] if isinstance(row[14], str) else "",
            fee_meta=_fee_meta(row[15]),
            url=row[16] if isinstance(row[16], str) and row[16] != "" else None,
        )
    except (ValidationError, ValueError):
        _log.warning("skipping unreadable market %s", key)
        return None


def _group_from_row(
    row: tuple[object, ...], members: tuple[GroupMember, ...]
) -> EquivalenceGroup | None:
    group_id = row[0]
    if not isinstance(group_id, str) or group_id == "" or len(members) < 2:
        if isinstance(group_id, str) and group_id != "":
            _log.warning("skipping group %s with %s members", group_id, len(members))
        return None
    try:
        status = GroupStatus(str(row[1]))
    except ValueError:
        _log.warning("skipping group %s with unknown status", group_id)
        return None
    reviewer = row[4] if isinstance(row[4], str) and row[4] != "" else None
    try:
        return EquivalenceGroup(
            group_id=group_id,
            outcome=group_id,
            members=members,
            status=status,
            exception_risk=bool(row[2]),
            exception_table=_exception_table(row[3]),
            reviewer=reviewer,
            decided_utc=parse_utc(row[5] if isinstance(row[5], str) else None),
            notes=row[6] if isinstance(row[6], str) else "",
        )
    except (ValidationError, ValueError):
        _log.warning("skipping unreadable group %s", group_id)
        return None


def _participants(value: object) -> tuple[str, ...]:
    parsed = _json(value)
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str))


def _fee_meta(value: object) -> dict[str, str]:
    parsed = _json(value)
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(item) for key, item in parsed.items() if item is not None}


def _exception_table(value: object) -> tuple[ExceptionRow, ...]:
    parsed = _json(value)
    if not isinstance(parsed, list):
        return ()
    rows: list[ExceptionRow] = []
    for item in parsed:
        if not isinstance(item, Mapping):
            continue
        try:
            rows.append(ExceptionRow.model_validate(dict(item)))
        except ValidationError:
            continue
    return tuple(rows)


def _json(value: object) -> object:
    if not isinstance(value, str) or value == "":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or value.strip() == "":
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number
