"""Review spool: atomic decisions, stale guard, and collector apply."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pmarb.matching.rules import member_market_key, rules_hash
from pmarb.models import EquivalenceGroup, GroupMember, GroupStatus, Market, Side, Venue
from pmarb.review import ReviewDecision, ReviewLogEntry, apply_pending, persist_sync, write_decision
from pmarb.storage.db import open_writer

AWARE = datetime(2026, 11, 11, 18, tzinfo=UTC)


def _market(
    venue: Venue,
    market_id: str,
    *,
    raw: dict[str, Any] | None = None,
) -> Market:
    return Market(
        venue=venue,
        market_id=market_id,
        title=market_id,
        market_type="winner",
        status="active",
        rules_text="Rules",
        rules_hash="ignored",
        raw={} if raw is None else raw,
    )


def _markets() -> tuple[Market, Market]:
    kalshi = _market(
        Venue.KALSHI,
        "KX-1",
        raw={"rules_primary": "Kalshi rules", "notional_value_dollars": "1.0000"},
    )
    poly = _market(Venue.POLYMARKET_US, "pm-1", raw={"description": "Polymarket rules"})
    return kalshi, poly


def _group(group_id: str = "g1") -> EquivalenceGroup:
    return EquivalenceGroup(
        group_id=group_id,
        outcome="NE beats DET, OT included",
        members=(
            GroupMember(
                venue=Venue.KALSHI,
                market_id="KX-1",
                native_side_for_outcome=Side.YES,
            ),
            GroupMember(
                venue=Venue.POLYMARKET_US,
                market_id="pm-1",
                native_side_for_outcome=Side.NO,
            ),
        ),
        status=GroupStatus.CANDIDATE,
    )


def _decision_for(
    group: EquivalenceGroup,
    markets: tuple[Market, ...],
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "group_id": group.group_id,
        "action": "approve",
        "reviewer": "ada",
        "shown_hashes": {market.market_key: rules_hash(market) for market in markets},
        "acknowledged_exceptions": True,
        "notes": "ok",
        "created_utc": "2026-11-11T18:00:00Z",
    }
    payload.update(overrides)
    return payload


def _bare_decision() -> dict[str, Any]:
    return {
        "group_id": "g1",
        "action": "approve",
        "reviewer": "ada",
        "shown_hashes": {"kalshi:KX-1": "v1:abc", "polymarket_us:pm-1": "v1:def"},
        "acknowledged_exceptions": True,
        "notes": "ok",
        "created_utc": "2026-11-11T18:00:00Z",
    }


class _Store:
    def __init__(self, groups: dict[str, EquivalenceGroup]) -> None:
        self.groups = groups
        self.calls: list[tuple[EquivalenceGroup | None, ReviewLogEntry]] = []

    def load_group(self, group_id: str) -> EquivalenceGroup | None:
        return self.groups.get(group_id)

    def persist(self, group: EquivalenceGroup | None, entry: ReviewLogEntry) -> None:
        self.calls.append((group, entry))
        if group is not None:
            self.groups[group.group_id] = group


def _run(
    spool: Path,
    store: _Store,
    markets: tuple[Market, ...],
) -> list[Any]:
    index = {market.market_key: market for market in markets}
    return apply_pending(
        spool,
        load_group=store.load_group,
        load_markets=lambda _group: index,
        persist=store.persist,
    )


def test_write_decision_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    path = write_decision(tmp_path, _bare_decision())
    assert len(calls) >= 2
    assert path.name == "2026-11-11T18:00:00Z-g1-approve.json"
    assert [item.name for item in tmp_path.iterdir()] == [path.name]
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["action"] == "approve"
    assert loaded["created_utc"] == "2026-11-11T18:00:00Z"
    assert "exception_table" not in loaded
    parsed = ReviewDecision.model_validate(loaded)
    assert parsed.created_utc == AWARE


def test_write_decision_removes_temp_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_src: str, _dst: str) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk"):
        write_decision(tmp_path, _bare_decision())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("table", "risk"),
    [
        pytest.param(
            [
                {
                    "outcome_case": "played",
                    "payouts": {"kalshi:KX-1": "1", "polymarket_us:pm-1": "1"},
                    "preserves_unit_payout": True,
                },
                {
                    "outcome_case": "cancelled",
                    "payouts": {"kalshi:KX-1": "fair", "polymarket_us:pm-1": "fair"},
                    "preserves_unit_payout": False,
                },
            ],
            True,
            id="broken-payout",
        ),
        pytest.param(
            [
                {
                    "outcome_case": "played",
                    "payouts": {"kalshi:KX-1": "1", "polymarket_us:pm-1": "1"},
                    "preserves_unit_payout": True,
                }
            ],
            False,
            id="unit-payout",
        ),
        pytest.param([], False, id="empty-table"),
    ],
)
def test_approve_derives_exception_risk_and_still_approves(
    tmp_path: Path, table: list[dict[str, Any]], risk: bool
) -> None:
    group = _group()
    markets = _markets()
    write_decision(
        tmp_path,
        _decision_for(group, markets, exception_table=table, notes="acked"),
    )
    store = _Store({"g1": group})
    results = _run(tmp_path, store, markets)
    assert len(results) == 1
    assert results[0].result == "applied"
    assert results[0].destination.parent.name == "applied"
    assert not results[0].source.exists()
    updated, entry = store.calls[0]
    assert updated is not None
    assert entry.result == "applied"
    assert updated.status == GroupStatus.APPROVED
    assert updated.exception_risk is risk
    assert updated.reviewer == "ada"
    assert updated.notes == "acked"
    assert updated.decided_utc == AWARE
    assert updated.hash_version == 1
    by_key = {member_market_key(member): member.approved_rules_hash for member in updated.members}
    assert by_key == {market.market_key: rules_hash(market) for market in markets}
    assert [row.preserves_unit_payout for row in updated.exception_table] == [
        row["preserves_unit_payout"] for row in table
    ]


def test_approve_without_acknowledgement_is_invalid(tmp_path: Path) -> None:
    group = _group()
    markets = _markets()
    write_decision(tmp_path, _decision_for(group, markets, acknowledged_exceptions=False))
    store = _Store({"g1": group})
    results = _run(tmp_path, store, markets)
    assert results[0].result == "invalid"
    assert results[0].destination.parent.name == "rejected"
    assert store.calls[0][0] is None
    assert store.calls[0][1].result == "invalid"
    assert store.groups["g1"].status == GroupStatus.CANDIDATE
    assert store.groups["g1"] is group


def test_stale_hashes_are_stale_review(tmp_path: Path) -> None:
    group = _group()
    markets = _markets()
    payload = _decision_for(group, markets)
    payload["shown_hashes"] = dict(payload["shown_hashes"])
    payload["shown_hashes"][markets[0].market_key] = "v1:stale"
    write_decision(tmp_path, payload)
    store = _Store({"g1": group})
    results = _run(tmp_path, store, markets)
    assert results[0].result == "stale_review"
    assert results[0].destination.parent.name == "rejected"
    assert store.calls[0][0] is None
    assert store.calls[0][1].result == "stale_review"
    assert store.groups["g1"] is group


def test_invalid_json_and_schema_are_invalid(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    (tmp_path / "schema.json").write_text(json.dumps({"group_id": "g1"}), encoding="utf-8")
    store = _Store({})
    results = _run(tmp_path, store, _markets())
    assert len(results) == 2
    assert {item.result for item in results} == {"invalid"}
    assert {item.destination.name for item in results} == {"bad.json", "schema.json"}
    assert all(item.destination.parent.name == "rejected" for item in results)
    assert len(store.calls) == 2
    assert all(group is None and entry.result == "invalid" for group, entry in store.calls)


def test_unknown_group_is_invalid(tmp_path: Path) -> None:
    markets = _markets()
    write_decision(tmp_path, _decision_for(_group(), markets))
    store = _Store({})
    results = _run(tmp_path, store, markets)
    assert results[0].result == "invalid"
    assert results[0].group_id == "g1"
    assert store.calls[0][0] is None
    assert store.calls[0][1].result == "invalid"


def test_apply_pending_is_idempotent(tmp_path: Path) -> None:
    group = _group()
    markets = _markets()
    write_decision(tmp_path, _decision_for(group, markets))
    store = _Store({"g1": group})
    first = _run(tmp_path, store, markets)
    second = _run(tmp_path, store, markets)
    assert len(first) == 1
    assert first[0].result == "applied"
    assert second == []
    assert len(store.calls) == 1
    assert first[0].destination.is_file()


def test_crash_before_move_does_not_repersist(tmp_path: Path) -> None:
    group = _group()
    markets = _markets()
    decision = ReviewDecision.model_validate(_decision_for(group, markets, notes="ok"))
    write_decision(tmp_path, decision)
    approved = group.model_copy(
        update={
            "status": GroupStatus.APPROVED,
            "reviewer": decision.reviewer,
            "decided_utc": decision.created_utc,
            "notes": decision.notes,
            "members": tuple(
                member.model_copy(
                    update={"approved_rules_hash": decision.shown_hashes[member_market_key(member)]}
                )
                for member in group.members
            ),
        }
    )

    def load_group(group_id: str) -> EquivalenceGroup | None:
        assert group_id == "g1"
        return approved

    def load_markets(_group: EquivalenceGroup) -> dict[str, Market]:
        raise AssertionError("already-applied decision must not consult current markets")

    def persist(_group: EquivalenceGroup | None, _entry: ReviewLogEntry) -> None:
        raise AssertionError("already-applied decision must not be persisted again")

    results = apply_pending(
        tmp_path,
        load_group=load_group,
        load_markets=load_markets,
        persist=persist,
    )
    assert len(results) == 1
    assert results[0].result == "applied"
    assert results[0].destination.parent.name == "applied"
    assert results[0].destination.is_file()
    assert not results[0].source.exists()


def test_persist_sync_round_trip(tmp_path: Path) -> None:
    group = _group()
    markets = _markets()
    table = [
        {
            "outcome_case": "cancelled",
            "payouts": {"kalshi:KX-1": "fair", "polymarket_us:pm-1": "0.50"},
            "preserves_unit_payout": False,
        }
    ]
    spool = tmp_path / "spool"
    write_decision(spool, _decision_for(group, markets, exception_table=table, notes="acked"))
    conn = open_writer(tmp_path / "pmarb.db")
    try:
        store = _Store({"g1": group})
        writer = persist_sync(conn)

        def persist(updated: EquivalenceGroup | None, entry: ReviewLogEntry) -> None:
            writer(updated, entry)
            store.persist(updated, entry)

        index = {market.market_key: market for market in markets}
        results = apply_pending(
            spool,
            load_group=store.load_group,
            load_markets=lambda _group: index,
            persist=persist,
        )
        assert results[0].result == "applied"
        row = conn.execute(
            "SELECT status, exception_risk, reviewer, decided_utc, notes FROM groups"
        ).fetchone()
        assert row == ("approved", 1, "ada", "2026-11-11T18:00:00Z", "acked")
        stored_table = json.loads(
            conn.execute("SELECT exception_table_json FROM groups").fetchone()[0]
        )
        assert stored_table == [
            {
                "outcome_case": "cancelled",
                "payouts": {"kalshi:KX-1": "fair", "polymarket_us:pm-1": "0.50"},
                "preserves_unit_payout": False,
            }
        ]
        kalshi, poly = markets
        members = conn.execute(
            "SELECT market_key, approved_rules_hash FROM group_members ORDER BY market_key"
        ).fetchall()
        assert members == [
            ("kalshi:KX-1", rules_hash(kalshi)),
            ("polymarket_us:pm-1", rules_hash(poly)),
        ]
        log = conn.execute(
            "SELECT group_id, action, reviewer, result, spool_file FROM review_log"
        ).fetchone()
        assert log == ("g1", "approve", "ada", "applied", results[0].destination.name)
        again = apply_pending(
            spool,
            load_group=store.load_group,
            load_markets=lambda _group: index,
            persist=persist,
        )
        assert again == []
        assert conn.execute("SELECT COUNT(*) FROM review_log").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("action", "status", "risk"),
    [
        ("reject", GroupStatus.REJECTED, False),
        ("revoke", GroupStatus.REVOKED, False),
        ("mark_exception_risk", GroupStatus.CANDIDATE, True),
    ],
)
def test_reject_revoke_and_mark_exception_risk(
    tmp_path: Path, action: str, status: GroupStatus, risk: bool
) -> None:
    group = _group()
    markets = _markets()
    write_decision(
        tmp_path,
        _decision_for(
            group,
            markets,
            action=action,
            acknowledged_exceptions=False,
            notes="noted",
        ),
    )
    store = _Store({"g1": group})
    results = _run(tmp_path, store, markets)
    assert results[0].result == "applied"
    updated, entry = store.calls[0]
    assert updated is not None
    assert entry.action == action
    assert updated.status == status
    assert updated.exception_risk is risk
    assert updated.reviewer == "ada"
    assert updated.decided_utc == AWARE
    assert updated.notes == "noted"
    assert all(member.approved_rules_hash is None for member in updated.members)


def test_apply_pending_processes_oldest_created_utc_first(tmp_path: Path) -> None:
    markets = _markets()
    early = _group("z")
    late = _group("a")
    write_decision(
        tmp_path,
        _decision_for(
            early,
            markets,
            action="reject",
            acknowledged_exceptions=False,
            created_utc="2026-11-11T18:00:00Z",
        ),
    )
    write_decision(
        tmp_path,
        _decision_for(
            late,
            markets,
            action="reject",
            acknowledged_exceptions=False,
            created_utc="2026-11-11T18:00:00.000001Z",
        ),
    )
    store = _Store({"z": early, "a": late})
    results = _run(tmp_path, store, markets)
    assert [item.group_id for item in results] == ["z", "a"]
    assert [item.result for item in results] == ["applied", "applied"]
