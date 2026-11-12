"""Rules hash version 1, and revocation when an approved market's rules drift.

``rules_hash`` is ``v1:`` plus the sha256 of one canonical JSON object. The
review spool stores that string on the decision and, once approved, on each
group member. A later mismatch revokes the group.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from pmarb.models import EquivalenceGroup, GroupMember, GroupStatus, Market, Venue

HASH_VERSION = 1

# Kalshi strike objects that describe how the contract pays. Present on the
# wire only for markets that have them; omitted from the hash otherwise.
STRIKE_FIELDS: tuple[str, ...] = (
    "strike_type",
    "floor_strike",
    "cap_strike",
    "functional_strike",
    "custom_strike",
)

_NOTIONAL_KEYS: tuple[str, ...] = ("notional_value_dollars", "notional")
_WHITESPACE = re.compile(r"\s+")


def member_market_key(member: GroupMember) -> str:
    """Return ``venue:market_id``, the same key as ``Market.market_key``."""
    return f"{member.venue.value}:{member.market_id}"


def canonical_rules(market: Market) -> dict[str, Any]:
    """Build the version-1 rules object that :func:`rules_hash` digests.

    Text is NFC-normalized, then each whitespace run (spaces, tabs, newlines)
    is collapsed to a single space and the ends are stripped. ``venue`` and
    ``market_id`` are identifiers and are copied verbatim.

    Always included:

    - ``venue``: ``Market.venue`` value (``kalshi`` or ``polymarket_us``).
    - ``market_id``: the venue's market id.
    - ``rules``: the contract text. Kalshi reads ``raw["rules_primary"]`` and
      ``raw["rules_secondary"]`` (blank values skipped), joins the parts with
      a newline, and then normalizes, so the boundary becomes one space.
      Polymarket US reads ``raw["description"]``. When that venue field is
      missing or blank, ``Market.rules_text`` is used for either venue.
    - ``settlement_sources``: a list, possibly empty, sorted by each item's
      canonical JSON. Looked up from ``raw["settlement_sources"]``, then a
      ``series`` or ``event`` object nested in ``raw``, then
      ``fee_meta["settlement_sources"]`` (a JSON list or one source string).
      A dict source keeps only ``name`` and ``url`` when either key is
      present, so an extra API field does not revoke every group.

    Included only when present, ``raw`` first and then ``fee_meta``:

    - ``strike_type``, ``floor_strike``, ``cap_strike``, ``functional_strike``,
      ``custom_strike``.
    - ``notional``, taken from ``notional_value_dollars`` or ``notional``.
      A string or ``Decimal`` is its exact text (``Decimal("1.0000")`` stays
      ``"1.0000"``). The canonical key is always ``notional``.

    Not included: title, status, quotes, fees, timestamps, ``Market.rules_hash``,
    ``settlement_bounds_type``, and the terms PDF (``contract_terms_url`` and
    any extracted PDF text). The PDF is shown to the reviewer and is not
    hashed in v1, because a re-extract would revoke every group.
    """
    canonical: dict[str, Any] = {
        "venue": market.venue.value,
        "market_id": market.market_id,
        "rules": _rules_text(market),
        "settlement_sources": _settlement_sources(market),
    }
    canonical.update(_strike_fields(market))
    notional = _notional(market)
    if notional is not None:
        canonical["notional"] = notional
    return canonical


def rules_hash(market: Market) -> str:
    """Return ``v1:`` plus the sha256 hex digest of the canonical JSON.

    The JSON is ``json.dumps(canonical, sort_keys=True, separators=(",", ":"))``
    encoded as UTF-8. The ``v1:`` prefix is outside the digest. It is the
    durable hash version: the groups table does not store ``hash_version``.
    """
    payload = json.dumps(canonical_rules(market), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"v{HASH_VERSION}:{digest}"


def detect_changes(
    approved: EquivalenceGroup,
    current_markets: Mapping[str, Market],
) -> list[str]:
    """Return member market keys whose rules no longer match the approval.

    A key is included when its market is absent from ``current_markets`` (or
    stored under a different key) or when :func:`rules_hash` differs from
    ``approved_rules_hash``. Keys follow the group's member order.

    When ``approved.hash_version`` is not :data:`HASH_VERSION`, every member
    is included. Bumping the hash version revokes the group even if a stored
    hash string still compares equal.
    """
    if approved.hash_version != HASH_VERSION:
        return [member_market_key(member) for member in approved.members]
    changed: list[str] = []
    for member in approved.members:
        key = member_market_key(member)
        market = current_markets.get(key)
        if market is None or market.market_key != key:
            changed.append(key)
            continue
        if rules_hash(market) != member.approved_rules_hash:
            changed.append(key)
    return changed


def revoke_if_changed(
    group: EquivalenceGroup,
    current: Mapping[str, Market],
) -> EquivalenceGroup:
    """Return ``group`` itself, or a revoked copy when rules drifted.

    The copy keeps the reviewer, the decision time, and each member's
    approved hash, and appends one note line. No drift, or a group already
    revoked with that same line, returns the same object so a metadata
    refresh does not rewrite the row.
    """
    changed = detect_changes(group, current)
    if not changed:
        return group
    notes = _append_note(group.notes, _change_note(group, changed))
    if group.status == GroupStatus.REVOKED and notes == group.notes:
        return group
    return group.model_copy(update={"status": GroupStatus.REVOKED, "notes": notes})


def _change_note(group: EquivalenceGroup, changed: list[str]) -> str:
    listed = "rules changed: " + ", ".join(changed)
    if group.hash_version != HASH_VERSION:
        return f"hash version {group.hash_version} != {HASH_VERSION}; {listed}"
    return listed


def _append_note(notes: str, line: str) -> str:
    if line in notes.splitlines():
        return notes
    if notes == "":
        return line
    return f"{notes.rstrip()}\n{line}"


def _rules_text(market: Market) -> str:
    raw = market.raw if isinstance(market.raw, dict) else {}
    if market.venue is Venue.KALSHI:
        parts: list[str] = []
        for key in ("rules_primary", "rules_secondary"):
            text = _as_text(raw.get(key))
            if text is not None:
                parts.append(text)
        if parts:
            return _norm_text("\n".join(parts))
        return _norm_text(market.rules_text)
    if market.venue is Venue.POLYMARKET_US:
        description = _as_text(raw.get("description"))
        if description is not None:
            return _norm_text(description)
        return _norm_text(market.rules_text)
    return _norm_text(market.rules_text)


def _as_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip() != "":
        return value
    return None


def _norm_text(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()


def _settlement_sources(market: Market) -> list[object]:
    found = _lookup_sources(market)
    normalized = [_norm_source(item) for item in found if item is not None]
    normalized.sort(key=_source_sort_key)
    return normalized


def _lookup_sources(market: Market) -> list[object]:
    raw = market.raw if isinstance(market.raw, dict) else {}
    containers: list[object] = [raw]
    for nested_key in ("series", "event"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        if not isinstance(container, dict) or "settlement_sources" not in container:
            continue
        found = _coerce_sources(container.get("settlement_sources"))
        if found is not None:
            return found
    if market.fee_meta and "settlement_sources" in market.fee_meta:
        found = _coerce_sources(market.fee_meta.get("settlement_sources"))
        if found is not None:
            return found
    return []


def _coerce_sources(value: object) -> list[object] | None:
    """Return a source list, or None when the field is absent or blank."""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        if text[0] in "[{":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return [value]
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        return [value]
    return None


def _norm_source(item: object) -> object:
    if isinstance(item, dict) and ("name" in item or "url" in item):
        projected: dict[str, object] = {}
        for key in ("name", "url"):
            if key in item and item[key] is not None:
                projected[key] = _norm_json(item[key])
        return projected
    return _norm_json(item)


def _source_sort_key(item: object) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def _strike_fields(market: Market) -> dict[str, object]:
    raw = market.raw if isinstance(market.raw, dict) else {}
    found: dict[str, object] = {}
    for key in STRIKE_FIELDS:
        if key in raw and _present(raw.get(key)):
            found[key] = _norm_json(raw[key])
            continue
        fee_value = market.fee_meta.get(key) if market.fee_meta else None
        if isinstance(fee_value, str) and fee_value.strip() != "":
            found[key] = _norm_text(fee_value)
    return found


def _notional(market: Market) -> object | None:
    raw = market.raw if isinstance(market.raw, dict) else {}
    for key in _NOTIONAL_KEYS:
        if key in raw and _present(raw.get(key)):
            return _norm_json(raw[key])
    for key in _NOTIONAL_KEYS:
        fee_value = market.fee_meta.get(key) if market.fee_meta else None
        if isinstance(fee_value, str) and fee_value.strip() != "":
            return _norm_text(fee_value)
    return None


def _present(value: object) -> bool:
    if value is None:
        return False
    return not (isinstance(value, str) and value.strip() == "")


def _norm_json(value: object) -> object:
    if isinstance(value, str):
        return _norm_text(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number in canonical rules")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal in canonical rules")
        return str(value)
    if isinstance(value, dict):
        return {str(key): _norm_json(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_norm_json(item) if item is not None else None for item in value]
    raise ValueError(f"cannot canonicalize {type(value).__name__}")
