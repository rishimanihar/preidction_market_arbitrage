"""Apply the review spool, and auto-approve when settings say so.

``review_mode="auto"`` approves every candidate and every rules revocation
with reviewer ``auto:matching-v2``. A person who rejected the group is never
overridden. ``review_mode="manual"`` only applies the spool.

The spool directory is ``<data_dir>/spool/reviews``, the same path the review
app writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from pmarb.collector.clock import Clock
from pmarb.collector.episodes import EpisodeTracker
from pmarb.collector.persist import Op
from pmarb.collector.registry import Registry
from pmarb.config import Settings
from pmarb.matching.rules import HASH_VERSION, revoke_if_changed, rules_hash
from pmarb.models import EquivalenceGroup, GroupStatus, Market
from pmarb.review.spool import ReviewLogEntry, apply_pending
from pmarb.storage.db import insert_review_log, upsert_group

AUTO_REVIEWER = "auto:matching-v2"
RULES_REVIEWER = "auto:rules"
AUTO_ACTION = "auto_approve"


def spool_directory(data_dir: Path) -> Path:
    """Directory the review app writes and the collector reads."""
    return data_dir / "spool" / "reviews"


def market_closed(group: EquivalenceGroup) -> bool:
    """True when discovery revoked the group because a member left the catalog."""
    return any(line.startswith("market closed:") for line in group.notes.splitlines())


class ReviewMode:
    """One discovery cycle of revocation, spool intake, and optional auto-approval."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: Registry,
        enqueue: Callable[[Op], None],
        clock: Clock,
        episodes: EpisodeTracker,
        spool_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._enqueue = enqueue
        self._clock = clock
        self._episodes = episodes
        self._spool = spool_directory(settings.data_dir) if spool_dir is None else spool_dir
        self.human_rejected: set[str] = set()

    def remember_rejects(self, group_ids: set[str]) -> None:
        """Rejects loaded from ``review_log`` survive a restart."""
        self.human_rejected.update(group_ids)

    def apply_cycle(self, refreshed: Mapping[str, Market]) -> dict[str, int]:
        """Revoke drifted approvals, apply the spool, then auto-approve when enabled."""
        revoked = self.revoke_changed(refreshed)
        self.apply_spool()
        approved = self.auto_approve()
        return {"revoked": revoked, "auto_approved": approved}

    def revoke_changed(self, refreshed: Mapping[str, Market]) -> int:
        """Revoke approved groups whose refreshed rules hash no longer matches."""
        count = 0
        for group in tuple(self._registry.groups.values()):
            if group.status is not GroupStatus.APPROVED:
                continue
            keys = [f"{member.venue.value}:{member.market_id}" for member in group.members]
            if any(key not in refreshed for key in keys):
                continue
            updated = revoke_if_changed(group, refreshed)
            if updated is group:
                continue
            shown = {
                f"{member.venue.value}:{member.market_id}": member.approved_rules_hash or ""
                for member in group.members
            }
            self._registry.groups[group.group_id] = updated
            self._write(updated, action="revoke", reviewer=RULES_REVIEWER, shown=shown)
            self._episodes.on_rules_changed(group.group_id)
            count += 1
        return count

    def apply_spool(self) -> None:
        """Apply pending human decisions. A reject is remembered for auto mode."""

        def load_group(group_id: str) -> EquivalenceGroup | None:
            return self._registry.groups.get(group_id)

        def load_markets(group: EquivalenceGroup) -> Mapping[str, Market]:
            return self._registry.markets_for(group)

        def persist(group: EquivalenceGroup | None, entry: ReviewLogEntry) -> None:
            if (
                entry.action == "reject"
                and entry.result == "applied"
                and not entry.reviewer.startswith("auto:")
            ):
                self.human_rejected.add(entry.group_id)
            if group is not None:
                self._registry.groups[group.group_id] = group
            self._enqueue(_review_op(group, entry))

        apply_pending(
            self._spool,
            load_group=load_group,
            load_markets=load_markets,
            persist=persist,
        )

    def auto_approve(self) -> int:
        """Approve candidates and rules revocations. Manual mode and human rejects stay put."""
        if self._settings.review_mode != "auto":
            return 0
        count = 0
        now = self._clock.wall()
        for group in tuple(self._registry.groups.values()):
            approved = self._approve_one(group, now)
            if approved is None:
                continue
            shown = {
                f"{member.venue.value}:{member.market_id}": member.approved_rules_hash or ""
                for member in approved.members
            }
            self._registry.groups[group.group_id] = approved
            self._write(approved, action=AUTO_ACTION, reviewer=AUTO_REVIEWER, shown=shown)
            count += 1
        return count

    def _approve_one(self, group: EquivalenceGroup, now: datetime) -> EquivalenceGroup | None:
        if group.group_id in self.human_rejected:
            return None
        if group.status is GroupStatus.REJECTED or group.status is GroupStatus.APPROVED:
            return None
        if market_closed(group):
            return None
        if group.status not in (GroupStatus.CANDIDATE, GroupStatus.REVOKED):
            return None
        markets = self._registry.markets_for(group)
        if len(markets) != len(group.members):
            return None
        members = []
        for member in group.members:
            key = f"{member.venue.value}:{member.market_id}"
            market = markets.get(key)
            if market is None:
                return None
            members.append(member.model_copy(update={"approved_rules_hash": rules_hash(market)}))
        table = group.exception_table
        return group.model_copy(
            update={
                "members": tuple(members),
                "status": GroupStatus.APPROVED,
                "hash_version": HASH_VERSION,
                "exception_risk": any(not row.preserves_unit_payout for row in table),
                "exception_table": table,
                "reviewer": AUTO_REVIEWER,
                "decided_utc": now,
            }
        )

    def _write(
        self,
        group: EquivalenceGroup,
        *,
        action: str,
        reviewer: str,
        shown: dict[str, str],
    ) -> None:
        now = self._clock.wall()
        shown_hashes = dict(shown)

        def apply(
            conn: sqlite3.Connection,
            group: EquivalenceGroup = group,
            action: str = action,
            reviewer: str = reviewer,
            shown_hashes: dict[str, str] = shown_hashes,
            now: datetime = now,
        ) -> None:
            upsert_group(conn, group)
            insert_review_log(
                conn,
                group_id=group.group_id,
                action=action,
                reviewer=reviewer,
                shown_hashes=shown_hashes,
                result="applied",
                spool_file=None,
                applied_utc=now,
            )

        self._enqueue(apply)


def _review_op(group: EquivalenceGroup | None, entry: ReviewLogEntry) -> Op:
    def apply(
        conn: sqlite3.Connection,
        group: EquivalenceGroup | None = group,
        entry: ReviewLogEntry = entry,
    ) -> None:
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

    return apply
