"""Deterministic, read-only fills against the book visible at each arrival."""

from __future__ import annotations

import random
import sqlite3
import tempfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pmarb.collector.books import BookMeta, BookState
from pmarb.collector.freshness import book_is_cached, is_fresh
from pmarb.config import Settings
from pmarb.ladder import InsufficientDepth, walk
from pmarb.models import Book, Leg, Level, OpportunityAssessment, Side
from pmarb.protocols import FeeModel
from pmarb.replay.engine import ReplayEngine, _load_source, load_recording, parse_rest_books
from pmarb.venues.kalshi.rest import KalshiAdapter
from pmarb.venues.polymarket_us.rest import PolymarketUSAdapter

ZERO = Decimal(0)
ONE = Decimal(1)
DEFAULT_DELAYS = (100, 250, 500, 1000, 2000)
DEFAULT_HAIRCUTS = (Decimal(0), Decimal("0.25"), Decimal("0.5"))


@dataclass(frozen=True, slots=True)
class Decision:
    mono_ns: int
    assessment: OpportunityAssessment


@dataclass(frozen=True, slots=True)
class BookEvent:
    mono_ns: int
    serial: int
    book: Book
    wall: datetime


@dataclass(slots=True)
class _PendingTrade:
    decision: Decision
    fills: list[tuple[Decimal, Decimal, Decimal] | None]
    done: list[bool]
    times: list[int]


def run_simulation(
    raw_root: Path | str,
    source_db: Path | str,
    out_db: Path | str,
    *,
    delays_ms: Sequence[int] = DEFAULT_DELAYS,
    asymmetric: Sequence[tuple[int, int]] = (),
    haircuts: Sequence[Decimal] = DEFAULT_HAIRCUTS,
    order: str = "fok",
    jitter_ms: int = 0,
    seed: int = 0,
    cash_per_venue: Decimal = Decimal(10000),
    since: str | datetime | None = None,
    until: str | datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Replay opportunities, then simulate their arrivals from recorded books."""
    if order not in {"fok", "ioc"}:
        raise ValueError("order must be fok or ioc")
    if jitter_ms < 0 or cash_per_venue <= 0:
        raise ValueError("jitter must be non-negative and cash must be positive")
    if any(delay < 0 for delay in delays_ms) or any(a < 0 or b < 0 for a, b in asymmetric):
        raise ValueError("delays must be non-negative")
    if any(h < 0 or h >= 1 for h in haircuts):
        raise ValueError("haircuts must be in [0, 1)")
    raw_root, source_db, out_db = Path(raw_root), Path(source_db), Path(out_db)
    if _same_file(out_db, source_db):
        raise ValueError("sim output cannot overwrite the source database")

    replay_settings = settings or Settings(_env_file=None)
    decisions: list[Decision] = []
    with tempfile.TemporaryDirectory(prefix="pmarb-sim-replay-") as tmp:
        replay_db = Path(tmp) / "replay.sqlite"
        ReplayEngine(
            raw_root,
            source_db,
            replay_db,
            since=since,
            until=until,
            settings=replay_settings,
            on_assessment=lambda mono, assessment: decisions.append(Decision(mono, assessment)),
        ).run()
        episode_rows = _read_episodes(replay_db)
    decisions = _episode_decisions(
        decisions,
        episode_rows,
        max_gap_s=2 * replay_settings.min_poll_cadence_s,
    )
    recorded, gaps = load_recording(raw_root, since=since, until=until)
    book_events: list[BookEvent] = []
    serial = 0
    for record in recorded:
        if record.envelope.kind != "rest_book" or record.envelope.status not in (None, 200):
            continue
        for book in parse_rest_books(record.envelope):
            serial += 1
            book_events.append(BookEvent(record.mono_ns, serial, book, record.wall))

    gap_events: list[tuple[int, str]] = []
    by_path: dict[str, int] = {}
    for record in recorded:
        by_path[record.path] = max(record.mono_ns, by_path.get(record.path, record.mono_ns))
    for gap in gaps:
        mono_ns = by_path.get(gap.path)
        if mono_ns is not None:
            gap_events.append((mono_ns + 1, Path(gap.path).parent.parent.name))
    gap_events.sort()

    histories, markets, _seen = _load_source(source_db)
    del histories
    kalshi = KalshiAdapter.__new__(KalshiAdapter)
    kalshi._settings = replay_settings
    poly = PolymarketUSAdapter.__new__(PolymarketUSAdapter)
    poly._settings = replay_settings
    fee_models = {}
    for market in markets.values():
        if market.venue.value == "kalshi":
            fee_models[market.market_key] = kalshi.fee_model(market)
        elif market.venue.value == "polymarket_us":
            fee_models[market.market_key] = poly.fee_model(market)

    delay_scenarios = list(
        dict.fromkeys([(value, value) for value in delays_ms] + list(asymmetric))
    )
    haircuts = tuple(sorted(set(haircuts)))
    if not delay_scenarios or not haircuts:
        raise ValueError("at least one delay scenario and one haircut are required")
    scenarios = [(delay_pair, haircut) for delay_pair in delay_scenarios for haircut in haircuts]
    jitter = _paired_jitter(decisions, delay_scenarios, jitter_ms, seed)
    prior_fills: dict[tuple[int, int], dict[int, tuple[Decimal, Decimal]]] = {}
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()
    conn = sqlite3.connect(out_db)
    try:
        _create_schema(conn)
        count = 0
        for delay_pair, haircut in scenarios:
            scenario_decisions = []
            previous_fills = prior_fills.get(delay_pair)
            current_fills: dict[int, list[Decimal]] = {}
            for decision_index, decision in enumerate(decisions):
                j0, j1 = jitter[(decision_index, delay_pair)]
                arrival0 = decision.mono_ns + (delay_pair[0] + j0) * 1_000_000
                arrival1 = decision.mono_ns + (delay_pair[1] + j1) * 1_000_000
                assessment = decision.assessment
                assert assessment.yes_result is not None and assessment.no_result is not None
                scenario_decisions.append((arrival0, 0, decision_index, decision, haircut))
                scenario_decisions.append((arrival1, 1, decision_index, decision, haircut))
            arrivals = sorted(scenario_decisions, key=lambda row: (row[0], row[1]))
            state: dict[str, tuple[Book, int, int, datetime]] = {}
            remaining: dict[tuple[str, int, str, str], list[Decimal]] = {}
            event_idx = 0
            gap_idx = 0
            generations: dict[str, int] = {}
            cash = {"kalshi": cash_per_venue, "polymarket_us": cash_per_venue}
            pending: dict[tuple[int, int], _PendingTrade] = {}
            for arrival_ns, leg_index, decision_index, decision, current_haircut in arrivals:
                while True:
                    next_book_ns = (
                        book_events[event_idx].mono_ns if event_idx < len(book_events) else None
                    )
                    next_gap_ns = gap_events[gap_idx][0] if gap_idx < len(gap_events) else None
                    if next_book_ns is None and next_gap_ns is None:
                        break
                    if next_gap_ns is not None and (
                        next_book_ns is None or next_gap_ns <= next_book_ns
                    ):
                        if next_gap_ns > arrival_ns:
                            break
                        _gap_ns, gap_venue = gap_events[gap_idx]
                        for old_key in tuple(state):
                            if old_key.startswith(f"{gap_venue}:"):
                                state.pop(old_key)
                        gap_idx += 1
                        continue
                    if next_book_ns is None or next_book_ns > arrival_ns:
                        break
                    event = book_events[event_idx]
                    key = _book_key(event.book)
                    venue = event.book.venue.value
                    generation = event.book.connection_generation
                    current_generation = generations.get(venue, 0)
                    if generation >= current_generation:
                        if generation > current_generation:
                            generations[venue] = generation
                            for old_key in tuple(state):
                                if old_key.startswith(f"{venue}:"):
                                    state.pop(old_key)
                        state[key] = (event.book, event.serial, event.mono_ns, event.wall)
                    event_idx += 1
                result = _leg_result(decision.assessment, leg_index)
                leg = decision.assessment.yes_leg if leg_index == 0 else decision.assessment.no_leg
                leg_key = _leg_key(leg)
                book_state = state.get(leg_key)
                trade = pending.setdefault(
                    (id(decision), delay_pair[0] + delay_pair[1]),
                    _PendingTrade(decision, [None, None], [False, False], [arrival_ns, arrival_ns]),
                )
                trade.times[leg_index] = arrival_ns
                fill = None
                requested = decision.assessment.requested_qty
                if previous_fills is not None:
                    prior_qty = previous_fills.get(decision_index, (ZERO, ZERO))[leg_index]
                    requested = _haircut_fill_cap(requested, prior_qty, order)
                if book_state is not None and result.levels_consumed:
                    book, book_serial, book_mono_ns, book_wall = book_state
                    arrival_wall = decision.assessment.computed_utc + timedelta(
                        microseconds=(arrival_ns - decision.mono_ns) / 1000
                    )
                    max_age_s = (
                        replay_settings.min_poll_cadence_s
                        * replay_settings.poll_max_age_cadence_multiple
                    )
                    metadata = BookMeta(
                        received_mono=book_mono_ns / 1_000_000_000,
                        received_wall=book_wall,
                        connection_generation=book.connection_generation,
                        source="poll",
                        cached=book_is_cached(book),
                    )
                    fresh, _reason = is_fresh(
                        BookState(book, metadata),
                        arrival_ns / 1_000_000_000,
                        arrival_wall,
                        max_age_s,
                    )
                    if not fresh or arrival_ns < book_mono_ns:
                        book_state = None
                if book_state is not None and result.levels_consumed:
                    book, book_serial, _book_mono_ns, _book_wall = book_state
                    all_levels = _book_ladder(book, leg.side)
                    price_limit = max(level.price for level in result.levels_consumed)
                    remaining_key = (
                        leg_key,
                        book_serial,
                        leg.side.value,
                        str(current_haircut),
                    )
                    remaining_sizes = remaining.setdefault(
                        remaining_key,
                        [level.size * (ONE - current_haircut) for level in all_levels],
                    )
                    eligible = [
                        (index, Level(price=level.price, size=remaining_sizes[index]))
                        for index, level in enumerate(all_levels)
                        if level.price <= price_limit and remaining_sizes[index] > ZERO
                    ]
                    eligible_indexes = [index for index, _level in eligible]
                    ladder = [level for _index, level in eligible]
                    model = fee_models.get(leg_key)
                    if model is not None and requested > ZERO:
                        fill = _fill(
                            ladder,
                            requested,
                            order,
                            cash[leg.venue.value],
                            model,
                            leg.side,
                        )
                        if fill is not None:
                            left = fill[0]
                            for index, level in zip(eligible_indexes, ladder, strict=True):
                                take = min(left, level.size)
                                remaining_sizes[index] -= take
                                left -= take
                                if left == ZERO:
                                    break
                            cash[leg.venue.value] -= fill[1] + fill[2]
                trade.fills[leg_index] = fill
                trade.done[leg_index] = True
                current_fills.setdefault(decision_index, [ZERO, ZERO])[leg_index] = (
                    fill[0] if fill is not None else ZERO
                )
                if trade.done == [True, True]:
                    count += 1
                    _insert_trade(
                        conn,
                        trade.decision,
                        delay_pair,
                        current_haircut,
                        order,
                        seed,
                        trade.fills,
                        trade.times,
                        cash,
                    )
                    pending.pop((id(decision), delay_pair[0] + delay_pair[1]), None)
            prior_fills[delay_pair] = {
                decision_index: (quantities[0], quantities[1])
                for decision_index, quantities in current_fills.items()
            }
        _write_summaries(conn, scenarios, len(decisions), order)
        conn.commit()
        return {"opportunities": len(decisions), "trades": count, "scenarios": len(scenarios)}
    finally:
        conn.close()


def _leg_result(assessment: OpportunityAssessment, index: int):
    return assessment.yes_result if index == 0 else assessment.no_result


def _book_ladder(book: Book, side: Side) -> list[Level]:
    return book.yes_asks if side is Side.YES else book.no_asks


def _book_key(book: Book) -> str:
    return f"{book.venue.value}:{book.market_id}"


def _leg_key(leg: Leg) -> str:
    return f"{leg.venue.value}:{leg.market_id}"


def _read_episodes(path: Path) -> tuple[tuple[object, ...], ...]:
    with closing(sqlite3.connect(path)) as conn:
        return tuple(
            conn.execute(
                """SELECT id, group_id, yes_market_key, no_market_key, qty, class,
                          model_version, first_seen_utc, last_seen_utc
                   FROM episodes ORDER BY first_seen_utc, id"""
            ).fetchall()
        )


def _episode_decisions(
    decisions: Sequence[Decision],
    episodes: Sequence[tuple[object, ...]],
    *,
    max_gap_s: float,
) -> list[Decision]:
    """Keep one positive sizing trial for each replay episode observation run."""
    selected: dict[int, Decision] = {}
    untracked: dict[tuple[str, ...], list[Decision]] = {}
    for decision in decisions:
        assessment = decision.assessment
        if (
            assessment.net_profit is None
            or assessment.net_profit <= ZERO
            or assessment.yes_result is None
            or assessment.no_result is None
        ):
            continue
        matched = False
        for row in episodes:
            (
                episode_id,
                group_id,
                yes_key,
                no_key,
                qty,
                result_class,
                model_version,
                first_seen,
                last_seen,
            ) = row
            if (
                group_id != assessment.group_id
                or yes_key != _leg_key(assessment.yes_leg)
                or no_key != _leg_key(assessment.no_leg)
                or Decimal(str(qty)) != assessment.requested_qty
                or model_version != assessment.model_version
                or result_class != assessment.result_class.value
            ):
                continue
            first = datetime.fromisoformat(str(first_seen))
            last = datetime.fromisoformat(str(last_seen))
            if first <= assessment.computed_utc <= last:
                selected.setdefault(int(episode_id), decision)
                matched = True
                break
        if not matched:
            key = (
                assessment.group_id,
                _leg_key(assessment.yes_leg),
                _leg_key(assessment.no_leg),
                str(assessment.requested_qty),
                assessment.model_version,
                assessment.result_class.value,
            )
            untracked.setdefault(key, []).append(decision)
    fallback: list[Decision] = []
    for items in untracked.values():
        items.sort(key=lambda item: item.mono_ns)
        previous_ns: int | None = None
        for item in items:
            if previous_ns is None or (item.mono_ns - previous_ns > max_gap_s * 1_000_000_000):
                fallback.append(item)
            previous_ns = item.mono_ns
    return sorted(
        [*selected.values(), *fallback],
        key=lambda item: (item.mono_ns, item.assessment.group_id),
    )


def _paired_jitter(
    decisions: Sequence[Decision],
    delay_scenarios: Sequence[tuple[int, int]],
    jitter_ms: int,
    seed: int,
) -> dict[tuple[int, tuple[int, int]], tuple[int, int]]:
    rng = random.Random(seed)
    return {
        (decision_index, delay_pair): (
            rng.randint(0, jitter_ms) if jitter_ms else 0,
            rng.randint(0, jitter_ms) if jitter_ms else 0,
        )
        for delay_pair in delay_scenarios
        for decision_index, _decision in enumerate(decisions)
    }


def _haircut_fill_cap(requested: Decimal, prior_filled: Decimal, order: str) -> Decimal:
    """Pair haircut trials so reduced depth cannot create a new later fill."""
    if prior_filled <= ZERO or (order == "fok" and prior_filled < requested):
        return ZERO
    return min(requested, prior_filled)


def _fill(
    ladder: Sequence[Level],
    requested: Decimal,
    order: str,
    cash: Decimal,
    model: FeeModel,
    side: Side,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return quantity, cost, and existing-model fee for one arrival."""
    try:
        used, cost = walk(ladder, requested)
        quote = model.taker_fee(used, side)
        if quote.amount is not None and cost + quote.amount <= cash:
            return requested, cost, quote.amount
    except InsufficientDepth:
        if order == "fok":
            return None
    if order == "fok":
        return None
    total = sum((level.size for level in ladder), ZERO)
    low, high = ZERO, min(requested, total)
    for _ in range(64):
        middle = (low + high) / Decimal(2)
        if middle == low or middle == high:
            break
        try:
            used, cost = walk(ladder, middle)
            quote = model.taker_fee(used, side)
        except (InsufficientDepth, ValueError):
            high = middle
            continue
        if quote.amount is not None and cost + quote.amount <= cash:
            low = middle
        else:
            high = middle
    if low <= ZERO:
        return None
    used, cost = walk(ladder, low)
    fee = model.taker_fee(used, side).amount
    return None if fee is None else (low, cost, fee)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """CREATE TABLE sim_trades (
            id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, decision_utc TEXT NOT NULL,
            yes_market_key TEXT NOT NULL, no_market_key TEXT NOT NULL, requested_qty TEXT NOT NULL,
            delay_yes_ms INTEGER NOT NULL, delay_no_ms INTEGER NOT NULL, haircut TEXT NOT NULL,
            arrival_yes_ms INTEGER NOT NULL, arrival_no_ms INTEGER NOT NULL,
            order_type TEXT NOT NULL, yes_filled TEXT NOT NULL, no_filled TEXT NOT NULL,
            yes_cost TEXT NOT NULL, no_cost TEXT NOT NULL, yes_fee TEXT NOT NULL, no_fee TEXT NOT NULL,
            paired_qty TEXT NOT NULL, realized_edge TEXT NOT NULL, naked_qty TEXT NOT NULL,
            worst_exposure TEXT NOT NULL, exposure_duration_ms INTEGER NOT NULL,
            outcome TEXT NOT NULL, seed INTEGER NOT NULL,
            cash_kalshi TEXT NOT NULL, cash_polymarket_us TEXT NOT NULL
        );
        CREATE TABLE sim_summary (
            delay_yes_ms INTEGER NOT NULL, delay_no_ms INTEGER NOT NULL, haircut TEXT NOT NULL,
            order_type TEXT NOT NULL, opportunities INTEGER NOT NULL, both_filled INTEGER NOT NULL,
            partial INTEGER NOT NULL,
            any_filled INTEGER NOT NULL, fill_rate TEXT NOT NULL, one_sided INTEGER NOT NULL,
            one_sided_rate TEXT NOT NULL, realized_edge TEXT NOT NULL,
            worst_exposure TEXT NOT NULL,
            PRIMARY KEY(delay_yes_ms, delay_no_ms, haircut, order_type)
        );"""
    )


def _insert_trade(
    conn: sqlite3.Connection,
    decision: Decision,
    delays: tuple[int, int],
    haircut: Decimal,
    order: str,
    seed: int,
    fills: list[tuple[Decimal, Decimal, Decimal] | None],
    times: list[int],
    cash: dict[str, Decimal],
) -> None:
    assessment = decision.assessment
    yes, no = fills
    yes_qty, yes_cost, yes_fee = yes or (ZERO, ZERO, ZERO)
    no_qty, no_cost, no_fee = no or (ZERO, ZERO, ZERO)
    paired = min(yes_qty, no_qty)
    yes_total = yes_cost + yes_fee
    no_total = no_cost + no_fee
    edge = ZERO
    if paired > ZERO:
        edge = paired * ONE - yes_total * paired / yes_qty - no_total * paired / no_qty
    naked = abs(yes_qty - no_qty)
    exposure_duration_ms = abs(times[0] - times[1]) // 1_000_000 if naked > ZERO else 0
    exposure = ZERO
    if naked > ZERO:
        exposure = (yes_total if yes_qty > no_qty else no_total) * naked / max(yes_qty, no_qty)
    outcome = (
        "both"
        if yes_qty == assessment.requested_qty and no_qty == assessment.requested_qty
        else (
            "one_sided"
            if (yes_qty == ZERO) != (no_qty == ZERO)
            else "partial"
            if yes_qty > ZERO or no_qty > ZERO
            else "neither"
        )
    )
    values = (
        assessment.group_id,
        assessment.computed_utc.isoformat(),
        _leg_key(assessment.yes_leg),
        _leg_key(assessment.no_leg),
        str(assessment.requested_qty),
        delays[0],
        delays[1],
        str(haircut),
        (times[0] - decision.mono_ns) // 1_000_000,
        (times[1] - decision.mono_ns) // 1_000_000,
        order,
        str(yes_qty),
        str(no_qty),
        str(yes_cost),
        str(no_cost),
        str(yes_fee),
        str(no_fee),
        str(paired),
        str(edge),
        str(naked),
        str(exposure),
        exposure_duration_ms,
        outcome,
        seed,
        str(cash["kalshi"]),
        str(cash["polymarket_us"]),
    )
    columns = (
        "group_id, decision_utc, yes_market_key, no_market_key, requested_qty, "
        "delay_yes_ms, delay_no_ms, haircut, arrival_yes_ms, arrival_no_ms, order_type, yes_filled, no_filled, "
        "yes_cost, no_cost, yes_fee, no_fee, paired_qty, realized_edge, naked_qty, "
        "worst_exposure, exposure_duration_ms, outcome, seed, cash_kalshi, cash_polymarket_us"
    )
    placeholders = ",".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO sim_trades ({columns}) VALUES ({placeholders})",
        values,
    )


def _write_summaries(conn, scenarios, opportunities: int, order: str) -> None:
    grouped: dict[tuple[int, int, str], list[tuple]] = {}
    for row in conn.execute(
        """SELECT delay_yes_ms, delay_no_ms, haircut, yes_filled, no_filled, outcome,
                  realized_edge, worst_exposure FROM sim_trades"""
    ):
        grouped.setdefault((row[0], row[1], row[2]), []).append(row)
    for delays, haircut in scenarios:
        trades = grouped.get((delays[0], delays[1], str(haircut)), [])
        count = len(trades)
        both = sum(row[5] == "both" for row in trades)
        partial = sum(row[5] == "partial" for row in trades)
        any_filled = sum(Decimal(row[3]) > ZERO or Decimal(row[4]) > ZERO for row in trades)
        one_sided = sum(row[5] == "one_sided" for row in trades)
        realized_edge = sum((Decimal(row[6]) for row in trades), ZERO)
        worst_exposure = max((Decimal(row[7]) for row in trades), default=ZERO)
        conn.execute(
            """INSERT OR REPLACE INTO sim_summary VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                delays[0],
                delays[1],
                str(haircut),
                order,
                opportunities,
                both,
                partial,
                any_filled,
                str(Decimal(both) / count if count else ZERO),
                one_sided,
                str(Decimal(one_sided) / count if count else ZERO),
                str(realized_edge),
                str(worst_exposure),
            ),
        )


def _same_file(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )
