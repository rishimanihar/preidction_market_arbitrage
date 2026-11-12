"""Rules hash v1: normalization, drift, and revocation."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pmarb.matching.rules import (
    HASH_VERSION,
    STRIKE_FIELDS,
    canonical_rules,
    detect_changes,
    member_market_key,
    revoke_if_changed,
    rules_hash,
)
from pmarb.models import EquivalenceGroup, GroupMember, GroupStatus, Market, Side, Venue

AWARE = datetime(2026, 11, 11, 18, tzinfo=UTC)
_CANONICAL_KEYS = {
    "venue",
    "market_id",
    "rules",
    "settlement_sources",
    "notional",
    *STRIKE_FIELDS,
}


def _market(
    venue: Venue,
    market_id: str,
    *,
    rules_text: str = "Rules",
    raw: dict[str, Any] | None = None,
    fee_meta: dict[str, str] | None = None,
) -> Market:
    return Market(
        venue=venue,
        market_id=market_id,
        title=market_id,
        market_type="winner",
        status="active",
        rules_text=rules_text,
        rules_hash="ignored",
        fee_meta={} if fee_meta is None else fee_meta,
        raw={} if raw is None else raw,
    )


def _kalshi(**raw: Any) -> Market:
    payload: dict[str, Any] = {
        "rules_primary": "If the team wins.",
        "rules_secondary": "Overtime counts.",
        "notional_value_dollars": "1.0000",
        "settlement_sources": [
            {"name": "ESPN", "url": "https://espn.example/a"},
            {"name": "NBA", "url": "https://nba.example/b"},
        ],
    }
    payload.update(raw)
    return _market(Venue.KALSHI, "KX-1", raw=payload)


def test_rules_hash_stable_under_whitespace_and_nfc() -> None:
    base = _kalshi()
    spaced = _kalshi(
        rules_primary="If   the\tteam\nwins.",
        rules_secondary="Overtime   counts.",
        notional_value_dollars="  1.0000\n",
    )
    decimal_notional = _kalshi(notional_value_dollars=Decimal("1.0000"))
    reversed_sources = _kalshi(
        settlement_sources=[
            {"name": "NBA", "url": "https://nba.example/b", "note": "ignore"},
            {"name": "ESPN", "url": " https://espn.example/a ", "extra": 1},
        ]
    )
    assert canonical_rules(base)["rules"] == "If the team wins. Overtime counts."
    assert canonical_rules(base)["venue"] == "kalshi"
    assert canonical_rules(base)["market_id"] == "KX-1"
    assert canonical_rules(base)["notional"] == "1.0000"
    assert set(canonical_rules(base)) <= _CANONICAL_KEYS
    assert rules_hash(base) == rules_hash(spaced)
    assert rules_hash(base) == rules_hash(decimal_notional)
    assert rules_hash(base) == rules_hash(reversed_sources)
    assert canonical_rules(base)["settlement_sources"] == [
        {"name": "ESPN", "url": "https://espn.example/a"},
        {"name": "NBA", "url": "https://nba.example/b"},
    ]

    text_only = _market(Venue.KALSHI, "KX-1", rules_text="Hello world")
    from_parts = _market(
        Venue.KALSHI,
        "KX-1",
        rules_text="other",
        raw={"rules_primary": "Hello", "rules_secondary": "world"},
    )
    assert canonical_rules(from_parts)["rules"] == "Hello world"
    assert rules_hash(text_only) == rules_hash(from_parts)

    composed = _market(Venue.POLYMARKET_US, "pm-1", raw={"description": "If café wins"})
    decomposed = _market(
        Venue.POLYMARKET_US,
        "pm-1",
        rules_text="unused",
        raw={"description": "If   cafe\u0301\nwins"},
    )
    fallback = _market(Venue.POLYMARKET_US, "pm-1", rules_text="If café wins")
    assert canonical_rules(composed)["rules"] == "If café wins"
    assert rules_hash(composed) == rules_hash(decomposed) == rules_hash(fallback)
    described = _market(
        Venue.POLYMARKET_US,
        "pm-1",
        rules_text="from rules_text",
        raw={"description": "from description"},
    )
    assert canonical_rules(described)["rules"] == "from description"

    payload = json.dumps(canonical_rules(base), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert HASH_VERSION == 1
    assert rules_hash(base) == f"v1:{digest}"


def test_rules_hash_changes_when_rules_source_or_notional_change() -> None:
    base = _kalshi()
    assert rules_hash(base) != rules_hash(_kalshi(rules_primary="If the other team wins."))
    assert rules_hash(base) != rules_hash(
        _kalshi(settlement_sources=[{"name": "ESPN", "url": "https://espn.example/other"}])
    )
    assert rules_hash(base) != rules_hash(_kalshi(notional_value_dollars="2.0000"))
    struck = _kalshi(strike_type="structured")
    assert rules_hash(base) != rules_hash(struck)
    assert canonical_rules(struck)["strike_type"] == "structured"


def test_hash_ignores_fields_outside_v1() -> None:
    market = _kalshi()
    noisy = market.model_copy(
        update={
            "title": "other title",
            "status": "closed",
            "rules_hash": "different",
            "raw": {
                **market.raw,
                "contract_terms_url": "https://assets.kalshi.com/terms.pdf",
                "volume": 3,
                "settlement_bounds_type": "floor",
            },
        }
    )
    assert rules_hash(market) == rules_hash(noisy)


def test_fee_meta_supplies_sources_and_notional() -> None:
    via_raw = _market(
        Venue.KALSHI,
        "KX-1",
        raw={
            "rules_primary": "Hello",
            "notional_value_dollars": "1.0000",
            "settlement_sources": [
                {"name": "ESPN", "url": "https://espn.example/a"},
                {"name": "NBA", "url": "https://nba.example/b"},
            ],
        },
    )
    via_fee = _market(
        Venue.KALSHI,
        "KX-1",
        rules_text="Hello",
        fee_meta={
            "notional_value_dollars": " 1.0000 ",
            "settlement_sources": json.dumps(
                [
                    {"url": "https://nba.example/b", "name": "NBA"},
                    {"name": "ESPN", "url": "https://espn.example/a"},
                ]
            ),
        },
    )
    assert rules_hash(via_raw) == rules_hash(via_fee)
    assert canonical_rules(via_fee)["notional"] == "1.0000"
    assert canonical_rules(via_fee)["settlement_sources"] == [
        {"name": "ESPN", "url": "https://espn.example/a"},
        {"name": "NBA", "url": "https://nba.example/b"},
    ]


def test_detect_changes_and_revoke_if_changed() -> None:
    kalshi = _market(
        Venue.KALSHI,
        "KX-1",
        raw={"rules_primary": "Kalshi rules", "notional_value_dollars": "1.0000"},
    )
    poly = _market(Venue.POLYMARKET_US, "pm-1", raw={"description": "Polymarket rules"})
    group = EquivalenceGroup(
        group_id="g1",
        outcome="NE beats DET, OT included",
        members=(
            GroupMember(
                venue=Venue.KALSHI,
                market_id="KX-1",
                native_side_for_outcome=Side.YES,
                approved_rules_hash=rules_hash(kalshi),
            ),
            GroupMember(
                venue=Venue.POLYMARKET_US,
                market_id="pm-1",
                native_side_for_outcome=Side.NO,
                approved_rules_hash=rules_hash(poly),
            ),
        ),
        status=GroupStatus.APPROVED,
        reviewer="ada",
        decided_utc=AWARE,
        notes="checked",
    )
    assert member_market_key(group.members[0]) == kalshi.market_key
    current = {kalshi.market_key: kalshi, poly.market_key: poly}
    assert detect_changes(group, current) == []
    assert revoke_if_changed(group, current) is group

    edited = kalshi.model_copy(update={"raw": {**kalshi.raw, "rules_primary": "Different rules"}})
    drifted = {kalshi.market_key: edited, poly.market_key: poly}
    assert detect_changes(group, drifted) == ["kalshi:KX-1"]
    revoked = revoke_if_changed(group, drifted)
    assert revoked.status == GroupStatus.REVOKED
    assert revoked.notes == "checked\nrules changed: kalshi:KX-1"
    assert revoked.reviewer == "ada"
    assert revoked.decided_utc == AWARE
    assert revoked.members[0].approved_rules_hash == rules_hash(kalshi)
    assert group.status == GroupStatus.APPROVED
    assert group.notes == "checked"
    assert revoke_if_changed(revoked, drifted) is revoked

    assert detect_changes(group, {kalshi.market_key: kalshi}) == ["polymarket_us:pm-1"]
    assert detect_changes(group, {}) == ["kalshi:KX-1", "polymarket_us:pm-1"]

    bumped = group.model_copy(update={"hash_version": 2})
    assert detect_changes(bumped, current) == ["kalshi:KX-1", "polymarket_us:pm-1"]
    version_revoked = revoke_if_changed(bumped, current)
    assert version_revoked.status == GroupStatus.REVOKED
    assert "hash version 2 != 1" in version_revoked.notes
    assert revoke_if_changed(version_revoked, current) is version_revoked
