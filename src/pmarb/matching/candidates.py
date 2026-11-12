"""Deterministic cross-venue joins for game-winner markets."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from pmarb.models import (
    EquivalenceGroup,
    GroupMember,
    GroupStatus,
    Market,
    Side,
    Venue,
)

from .exceptions import default_exception_table
from .normalize import NormalizationDiagnostic, NormalizedMarket, normalize_market

CandidateDiagnostic = NormalizationDiagnostic


@dataclass(frozen=True, slots=True)
class Candidate:
    """A proposed one-outcome group and the evidence supporting the join."""

    group: EquivalenceGroup
    match_keys: tuple[str, tuple[str, ...], date]
    evidence: tuple[str, ...]
    title_similarity: float
    needs_manual_date_check: bool
    start_time_mismatch_minutes: float | None
    policy_version: str = "v2"
    flags: tuple[str, ...] = ()


def _join_key(market: NormalizedMarket) -> tuple[str, tuple[str, ...], date]:
    return market.league, tuple(sorted(market.participants)), market.event_date_ny


def _game_identity(market: NormalizedMarket, source: Market) -> str:
    """Identify a listed game within one venue, collapsing Kalshi's two outcomes."""
    if market.venue is Venue.KALSHI:
        raw = source.raw
        event_data = raw.get("event", {}) if isinstance(raw, dict) else {}
        event = (event_data.get("event_ticker") if isinstance(event_data, dict) else None) or (
            raw.get("event_ticker") if isinstance(raw, dict) else None
        )
        if event:
            return f"event:{event}"
        # Kalshi winner tickers commonly append the selected team to an event
        # ticker. The normalized event date/team set still scopes this fallback.
        return f"market:{source.market_id.rsplit('-', 1)[0]}"
    return f"market:{source.market_id}"


def _minute_difference(a: NormalizedMarket, b: NormalizedMarket) -> float | None:
    if a.start_utc is None or b.start_utc is None:
        return None
    return abs((a.start_utc - b.start_utc).total_seconds()) / 60.0


def _manual_date_check(
    poly: NormalizedMarket,
    kalshi: NormalizedMarket,
    *,
    doubleheader: bool,
    competitors: list[NormalizedMarket],
) -> bool:
    if not doubleheader:
        return False
    mismatch = _minute_difference(poly, kalshi)
    if mismatch is None or mismatch > 180:
        return True
    # When there are repeated games, require a unique in-window Kalshi start
    # for this Polymarket start. Distinct Kalshi outcomes were deduplicated by
    # event identity before this function is called.
    if poly.start_utc is None:
        return True
    valid = [
        candidate
        for candidate in competitors
        if (delta := _minute_difference(poly, candidate)) is not None and delta <= 180
    ]
    return len(valid) != 1 or valid[0].market_id != kalshi.market_id


def _similarity(a: Market, b: Market) -> float:
    return SequenceMatcher(None, a.title.casefold(), b.title.casefold()).ratio()


def generate_candidates(
    markets: Iterable[Market],
    *,
    diagnostics: list[NormalizationDiagnostic] | None = None,
) -> list[Candidate]:
    """Join exact league, team-set, and New York event-date keys.

    A candidate represents the Polymarket US YES outcome. Only the Kalshi
    contract whose YES pays on that same team is included.
    """
    normalized: list[tuple[Market, NormalizedMarket]] = []
    seen_market_keys: set[str] = set()
    for market in markets:
        if market.market_key in seen_market_keys:
            continue
        seen_market_keys.add(market.market_key)
        local_diagnostics: list[NormalizationDiagnostic] = []
        result = normalize_market(market, diagnostics=local_diagnostics)
        if result is None:
            if diagnostics is not None:
                diagnostics.extend(local_diagnostics)
            continue
        normalized.append((market, result))

    kalshi: dict[tuple[str, tuple[str, ...], date], list[tuple[Market, NormalizedMarket]]] = {}
    poly: dict[tuple[str, tuple[str, ...], date], list[tuple[Market, NormalizedMarket]]] = {}
    game_ids_by_team: dict[tuple[Venue, str, str, date], set[str]] = {}
    for source, item in normalized:
        if item.market_type != "winner":
            if diagnostics is not None:
                diagnostics.append(
                    CandidateDiagnostic(source.market_key, "unsupported market type")
                )
            continue
        key = _join_key(item)
        game_id = _game_identity(item, source)
        for team in item.participants:
            team_key = (item.venue, item.league, team, item.event_date_ny)
            game_ids_by_team.setdefault(team_key, set()).add(game_id)
        target = kalshi if item.venue is Venue.KALSHI else poly
        target.setdefault(key, []).append((source, item))

    output: list[Candidate] = []
    matched: set[str] = set()
    for key in sorted(set(kalshi) & set(poly), key=lambda k: (k[0], k[1], k[2])):
        k_items = sorted(kalshi[key], key=lambda pair: pair[0].market_key)
        p_items = sorted(poly[key], key=lambda pair: pair[0].market_key)
        # Keep one YES contract per team and game. Repeated identical adapter
        # rows are deterministically collapsed before pairing.
        unique_kalshi: dict[tuple[str, str], tuple[Market, NormalizedMarket]] = {}
        for source, item in k_items:
            if item.yes_participant:
                ident = (_game_identity(item, source), item.yes_participant)
                unique_kalshi.setdefault(ident, (source, item))

        for p_source, p_item in p_items:
            if not p_item.yes_participant:
                if diagnostics is not None:
                    diagnostics.append(
                        CandidateDiagnostic(p_source.market_key, "missing YES participant")
                    )
                continue
            choices = [
                pair
                for pair in unique_kalshi.values()
                if pair[1].yes_participant == p_item.yes_participant
            ]
            if not choices:
                if diagnostics is not None:
                    diagnostics.append(
                        CandidateDiagnostic(
                            p_source.market_key,
                            f"no Kalshi YES market for {p_item.yes_participant}",
                        )
                    )
                continue
            for k_source, k_item in sorted(choices, key=lambda pair: pair[0].market_key):
                if k_item.yes_participant != p_item.yes_participant:
                    continue
                competitors = [
                    item
                    for _, item in unique_kalshi.values()
                    if item.yes_participant == p_item.yes_participant
                ]
                doubleheader = any(
                    len(
                        game_ids_by_team.get(
                            (venue, p_item.league, team, p_item.event_date_ny), set()
                        )
                    )
                    > 1
                    for venue in (Venue.POLYMARKET_US, Venue.KALSHI)
                    for team in p_item.participants
                )
                mismatch = _minute_difference(p_item, k_item)
                needs_manual = _manual_date_check(
                    p_item, k_item, doubleheader=doubleheader, competitors=competitors
                )
                keys = tuple(sorted((p_source.market_key, k_source.market_key)))
                group_id = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]
                exception_table = default_exception_table(k_source, p_source)
                group = EquivalenceGroup(
                    group_id=group_id,
                    outcome=f"{p_item.yes_participant} wins ({p_item.league}, {p_item.event_date_ny})",
                    members=(
                        GroupMember(
                            venue=Venue.POLYMARKET_US,
                            market_id=p_source.market_id,
                            native_side_for_outcome=Side.YES,
                        ),
                        GroupMember(
                            venue=Venue.KALSHI,
                            market_id=k_source.market_id,
                            native_side_for_outcome=Side.YES,
                        ),
                    ),
                    status=GroupStatus.CANDIDATE,
                    exception_risk=True,
                    exception_table=exception_table,
                )
                output.append(
                    Candidate(
                        group=group,
                        match_keys=key,
                        evidence=(
                            "policy_version:v2",
                            f"league:{p_item.league}",
                            f"participants:{','.join(sorted(p_item.participants))}",
                            (
                                f"event_date_ny:{p_item.event_date_ny.isoformat()} "
                                f"(Poly source:{p_item.date_source}; Kalshi source:{k_item.date_source})"
                            ),
                            f"yes_participant:{p_item.yes_participant}",
                        )
                        + p_item.evidence
                        + k_item.evidence,
                        title_similarity=_similarity(p_source, k_source),
                        needs_manual_date_check=needs_manual,
                        start_time_mismatch_minutes=mismatch,
                        flags=tuple(sorted(set(p_item.flags + k_item.flags))),
                    )
                )
                matched.update((p_source.market_key, k_source.market_key))

    # A mismatch in any exact join component means there is no candidate.
    for source, item in normalized:
        if source.market_key in matched:
            continue
        if diagnostics is not None and item.market_type == "winner":
            other = poly if item.venue is Venue.KALSHI else kalshi
            reason = "no cross-venue market with matching league, teams, and NY date"
            if _join_key(item) in other:
                reason = "no cross-venue market whose YES pays on the same team"
            diagnostics.append(CandidateDiagnostic(source.market_key, reason))

    # Every key is exact; title similarity is only a deterministic tie-breaker.
    output.sort(
        key=lambda candidate: (
            -candidate.title_similarity,
            candidate.match_keys,
            candidate.group.group_id,
        )
    )
    return output
