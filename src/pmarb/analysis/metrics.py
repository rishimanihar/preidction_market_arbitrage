"""Pure, read-only metrics over the persisted M1 database.

Money and ratios are kept as :class:`~decimal.Decimal`; the report layer is
responsible for JSON encoding. Some requested metrics cannot be reconstructed
from schema v1 because it stores averages rather than the walked book levels.
Those metrics are explicitly marked unavailable instead of guessed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pmarb.events import FRESH, GAP_KINDS, HEALTH, VENUES, gap_bucket, is_error_event
from pmarb.fees import KalshiFeeModel, PolymarketUSFeeModel
from pmarb.models import Side

# Scenario fee models for repricing stored walks (labelled as scenarios in reports).
# Kalshi: 0.07 x multiplier 1, direct-member 0.0001 grid, via the documented
# fee_rounding accumulator plus the one-step conservative margin used live.
_SCENARIO_KALSHI = KalshiFeeModel(multiplier=Decimal(1), precision=Decimal("0.0001"))
_SCENARIO_POLY = {
    theta: PolymarketUSFeeModel(theta=Decimal(theta)) for theta in ("0.0695", "0.095")
}


def _scenario_fee(model: object, levels: list) -> Decimal:
    quote = model.taker_fee(levels, Side.YES)  # fee shape is side-independent
    if quote.amount is None:  # pragma: no cover - scenario models are fully specified
        raise ValueError("scenario fee model returned unknown amount")
    return quote.amount


from pmarb.models import Level

ZERO = Decimal(0)
ONE = Decimal(1)
_SKEW_BUCKETS = ((0, 100), (100, 250), (250, 500), (500, 1000))


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("since/until and stored timestamps must be timezone-aware")
    return parsed


def _bound(value: str | datetime | None) -> str | None:
    parsed = _parse_time(value)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") if parsed else None


def _fraction(numerator: int, denominator: int) -> Decimal | None:
    return Decimal(numerator) / Decimal(denominator) if denominator else None


def _seconds(later: datetime, earlier: datetime) -> Decimal:
    delta = later - earlier
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(
        1_000_000
    )


def _percentile(values: list[Decimal], p: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if p == Decimal("0.50") and len(ordered) % 2 == 0:
        middle = len(ordered) // 2
        return (ordered[middle - 1] + ordered[middle]) / Decimal(2)
    index = max(0, int((p * len(ordered)).to_integral_value(rounding="ROUND_CEILING")) - 1)
    return ordered[index]


def _distribution(values: list[Decimal]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": _percentile(values, Decimal("0.50")),
        "p90": _percentile(values, Decimal("0.90")),
        "p99": _percentile(values, Decimal("0.99")),
        "max": max(values) if values else None,
    }


def _scenario_profit(row: dict[str, Any], theta: str) -> Decimal | None:
    fees = row.get("scenario_fees")
    if not isinstance(fees, dict) or row.get("qty") is None:
        return None
    return row["qty"] - fees["acquisition_cost"] - fees["kalshi"] - fees[f"poly_{theta}"]


def _query_dicts(conn: sqlite3.Connection, sql: str, params: list[str]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    names = [item[0] for item in cursor.description or ()]
    return [
        dict(row) if isinstance(row, sqlite3.Row) else dict(zip(names, row, strict=True))
        for row in cursor.fetchall()
    ]


def _ratio_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    gross_values = [r["gross_edge"] for r in rows if r["gross_edge"] is not None]
    net_values = [r["net_profit"] for r in rows if r["net_profit"] is not None]
    gross_edge_per_contract = [r["gross_edge"] for r in rows if r["gross_edge"] is not None]
    scenario_by_theta: dict[str, Any] = {}
    for theta in ("0.0695", "0.095"):
        profits = [profit for r in rows if (profit := _scenario_profit(r, theta)) is not None]
        scenario_by_theta[theta] = {
            "repricable_assessments": len(profits),
            "net_profit": _distribution(profits),
            "net_edge_per_contract": _distribution(
                [
                    profit / r["qty"]
                    for r in rows
                    if (profit := _scenario_profit(r, theta)) is not None and r["qty"]
                ]
            ),
            "net_positive_fraction": _fraction(sum(x > 0 for x in profits), len(profits)),
        }
    max_qty = [r["max_profitable_qty"] for r in rows if r["max_profitable_qty"] is not None]
    output: dict[str, Any] = {
        "assessments": count,
        "gross_edge_positive_fraction": _fraction(
            sum(x > 0 for x in gross_values), len(gross_values)
        ),
        "net_positive_fraction": _fraction(sum(x > 0 for x in net_values), len(net_values)),
        "gross_edge": _distribution(gross_values),
        "gross_edge_per_contract": _distribution(gross_edge_per_contract),
        "net_profit": _distribution(net_values),
        "scenario_net_by_theta": scenario_by_theta,
        "max_profitable_qty": _distribution(max_qty),
        "qty_profitability": {},
    }
    for qty in (Decimal(1), Decimal(10), Decimal(100)):
        priced = [r["net_profit"] for r in rows if r["net_profit"] is not None and r["qty"] == qty]
        gross_priced = [
            r["gross_edge"] for r in rows if r["gross_edge"] is not None and r["qty"] == qty
        ]
        output["qty_profitability"][str(qty)] = {
            "exact_qty_assessments": sum(r.get("qty") == qty for r in rows),
            "priced_assessments": len(priced),
            "net_positive_fraction": _fraction(sum(x > 0 for x in priced), len(priced)),
            "gross_edge_priced_assessments": len(gross_priced),
            "gross_positive_fraction": _fraction(
                sum(x > 0 for x in gross_priced), len(gross_priced)
            ),
            "scenario_net_by_theta": {
                theta: {
                    "priced_assessments": sum(
                        r.get("qty") == qty and _scenario_profit(r, theta) is not None for r in rows
                    ),
                    "net_profit": _distribution(
                        [
                            profit
                            for r in rows
                            if r.get("qty") == qty
                            and (profit := _scenario_profit(r, theta)) is not None
                        ]
                    ),
                    "net_edge_per_contract": _distribution(
                        [
                            profit / r["qty"]
                            for r in rows
                            if r.get("qty") == qty
                            and r.get("qty")
                            and (profit := _scenario_profit(r, theta)) is not None
                        ]
                    ),
                    "net_positive_fraction": _fraction(
                        sum(
                            r.get("qty") == qty
                            and (profit := _scenario_profit(r, theta)) is not None
                            and profit > 0
                            for r in rows
                        ),
                        sum(
                            r.get("qty") == qty and _scenario_profit(r, theta) is not None
                            for r in rows
                        ),
                    ),
                }
                for theta in ("0.0695", "0.095")
            },
        }
    return output


def _load_rows(
    conn: sqlite3.Connection, since: str | None, until: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    assessments = _query_dicts(
        conn,
        """SELECT a.*, ym.league, ym.event_start_utc, ym.participants_json,
                  g.exception_risk AS group_exception_risk
           FROM assessments a LEFT JOIN markets ym ON ym.market_key=a.yes_market_key
           LEFT JOIN groups g ON g.group_id=a.group_id ORDER BY a.computed_utc, a.id""",
        [],
    )
    events = _query_dicts(conn, "SELECT * FROM collector_events ORDER BY utc,id", [])
    episodes = _query_dicts(
        conn,
        """SELECT e.*,m.league,m.event_start_utc,m.participants_json
        FROM episodes e LEFT JOIN markets m ON m.market_key=e.yes_market_key ORDER BY e.first_seen_utc,e.id""",
        [],
    )
    return assessments, events, episodes


def _scenario_fees(row: dict[str, Any]) -> tuple[dict[str, Decimal] | None, str | None]:
    """Reprice persisted fills exactly when fee provenance contains levels."""
    try:
        provenance = json.loads(row.get("fee_provenance_json") or "{}")
        market_venues = (
            row.get("yes_market_key", "").split(":", 1)[0],
            row.get("no_market_key", "").split(":", 1)[0],
        )
        if not isinstance(provenance, dict) or set(provenance) < {"yes", "no"}:
            return None, "missing per-leg fee provenance"
        levels: dict[str, list[Level]] = {}
        for leg in ("yes", "no"):
            if not isinstance(provenance[leg], dict):
                return None, "per-leg fee provenance unavailable"
            raw = provenance[leg].get("levels_consumed")
            if not isinstance(raw, list) or not raw:
                return None, "persisted walked levels are unavailable"
            levels[leg] = [
                Level(price=Decimal(str(item["price"])), size=Decimal(str(item["size"])))
                for item in raw
            ]
        qty = _decimal(row.get("qty"))
        if qty is None or any(
            sum((level.size for level in levels[leg]), ZERO) != qty for leg in ("yes", "no")
        ):
            return None, "walked levels do not match requested quantity"
        if "kalshi" not in market_venues or "polymarket_us" not in market_venues:
            return None, "cannot identify both venue legs"
        k_leg = "yes" if market_venues[0] == "kalshi" else "no"
        p_leg = "no" if k_leg == "yes" else "yes"
        kalshi_fee = _scenario_fee(_SCENARIO_KALSHI, levels[k_leg])
        return {
            "kalshi": kalshi_fee,
            "poly_0.0695": _scenario_fee(_SCENARIO_POLY["0.0695"], levels[p_leg]),
            "poly_0.095": _scenario_fee(_SCENARIO_POLY["0.095"], levels[p_leg]),
            "acquisition_cost": sum(
                (level.price * level.size for leg in ("yes", "no") for level in levels[leg]), ZERO
            ),
        }, None
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
        json.JSONDecodeError,
    ):
        return None, "invalid persisted walked-level provenance"


def _event_detail(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("detail_json")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or raw == "":
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _health_samples(
    event: dict[str, Any], detail: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """One sample per venue. Combined health rows carry a ``venues`` object."""
    venues = detail.get(VENUES)
    if isinstance(venues, dict):
        samples = [
            (str(name), state)
            for name, state in venues.items()
            if isinstance(state, dict) and FRESH in state
        ]
        if samples:
            return samples
    return [(str(event.get("venue") or "all"), detail)]


def build_metrics(
    conn: sqlite3.Connection,
    since: str | datetime | None = None,
    until: str | datetime | None = None,
) -> dict[str, Any]:
    """Build all report metrics from an existing read-only SQLite connection."""
    since_s, until_s = _bound(since), _bound(until)
    if since_s and until_s and _parse_time(since_s) > _parse_time(until_s):
        raise ValueError("since must be at or before until")
    assessments, events, episode_rows = _load_rows(conn, since_s, until_s)
    assessments = [
        r
        for r in assessments
        if (not since_s or _parse_time(r["computed_utc"]) >= _parse_time(since_s))
        and (not until_s or _parse_time(r["computed_utc"]) <= _parse_time(until_s))
    ]
    events = [
        r
        for r in events
        if (not since_s or _parse_time(r["utc"]) >= _parse_time(since_s))
        and (not until_s or _parse_time(r["utc"]) <= _parse_time(until_s))
    ]
    episode_rows = [
        r
        for r in episode_rows
        if (not since_s or _parse_time(r["last_seen_utc"]) >= _parse_time(since_s))
        and (not until_s or _parse_time(r["first_seen_utc"]) <= _parse_time(until_s))
    ]
    for row in assessments:
        for col in ("qty", "gross_edge", "net_profit", "max_profitable_qty", "avg_yes", "avg_no"):
            row[col] = _decimal(row.get(col))
        row["scenario_fees"], row["scenario_fee_error"] = _scenario_fees(row)
    for row in episode_rows:
        for col in ("peak_net", "min_size", "max_size", "qty"):
            row[col] = _decimal(row.get(col))

    timestamps = [r["utc"] for r in events if r.get("kind") == HEALTH]
    start = _bound(min(timestamps, key=_parse_time)) if timestamps else None
    end = _bound(max(timestamps, key=_parse_time)) if timestamps else None
    duration_seconds: Decimal | None = None
    if start and end:
        duration_seconds = _seconds(_parse_time(end), _parse_time(start))

    def gap_kind(event: dict[str, Any]) -> str | None:
        return gap_bucket(event.get("kind"), _event_detail(event))

    gaps = {kind: sum(1 for event in events if gap_kind(event) == kind) for kind in GAP_KINDS}
    health_by_venue: dict[str, list[dict[str, Any]]] = {}
    http429 = errors = 0
    for event in events:
        detail_obj = _event_detail(event)
        if event.get("kind") == HEALTH:
            for venue, state in _health_samples(event, detail_obj):
                health_by_venue.setdefault(venue, []).append({"utc": event["utc"], "_state": state})
        code = detail_obj.get("status_code", detail_obj.get("status"))
        if str(code) == "429":
            http429 += 1
        if is_error_event(event.get("kind")):
            errors += 1
    venues = {}
    for venue, rows in health_by_venue.items():
        fresh = 0
        for event in rows:
            if event["_state"].get(FRESH) is True:
                fresh += 1
        fresh_seconds = known_seconds = ZERO
        if start and end:
            ordered = sorted(rows, key=lambda item: _parse_time(item["utc"]))
            for index, event in enumerate(ordered[:-1]):
                t0 = max(_parse_time(event["utc"]), _parse_time(start))
                t1 = min(_parse_time(ordered[index + 1]["utc"]), _parse_time(end))
                if t1 <= t0:
                    continue
                state = event["_state"].get(FRESH)
                span = _seconds(t1, t0)
                if isinstance(state, bool):
                    known_seconds += span
                    if state is True:
                        fresh_seconds += span
        fresh_fraction = (
            fresh_seconds / duration_seconds if known_seconds and duration_seconds else None
        )
        fresh_known_fraction = fresh_seconds / known_seconds if known_seconds else None
        venues[venue] = {
            "health_events": len(rows),
            "fresh_health_events": fresh,
            "fresh_sample_fraction": _fraction(fresh, len(rows)),
            "fresh_seconds": fresh_seconds,
            "fresh_fraction": fresh_fraction,
            "fresh_pct": fresh_fraction * Decimal(100) if fresh_fraction is not None else None,
            "fresh_known_pct": fresh_known_fraction * Decimal(100)
            if fresh_known_fraction is not None
            else None,
            "known_state_seconds": known_seconds,
            "unknown_seconds": duration_seconds - known_seconds if duration_seconds else None,
            "uptime_method": "sampled health state held until next health event; no duration assigned after last observation",
            "fresh_pct_interpretation": "fresh seconds divided by the full run window; lower bound when unknown_seconds is positive",
        }

    for row in assessments:
        row["period"] = "unknown"
        if row.get("event_start_utc") and row.get("computed_utc"):
            try:
                row["period"] = (
                    "live"
                    if _parse_time(row["computed_utc"]) >= _parse_time(row["event_start_utc"])
                    else "pregame"
                )
            except ValueError:
                pass
    by_league: dict[str, list[dict[str, Any]]] = {}
    by_class: dict[str, list[dict[str, Any]]] = {}
    by_league_class: dict[str, list[dict[str, Any]]] = {}
    periods: dict[str, list[dict[str, Any]]] = {"live": [], "pregame": [], "unknown": []}
    for row in assessments:
        league = row.get("league") or "unknown"
        klass = row.get("class") or "unknown"
        by_league.setdefault(league, []).append(row)
        by_class.setdefault(klass, []).append(row)
        by_league_class.setdefault(f"{league}/{klass}", []).append(row)
        periods[row["period"]].append(row)

    skew: dict[str, Any] = {}
    for low, high in _SKEW_BUCKETS:
        key = f"{low}-{high}ms"
        rows = [
            r
            for r in assessments
            if r.get("skew_ms") is not None and low <= int(r["skew_ms"]) < high
        ]
        edges = [
            r["net_profit"] / r["qty"] for r in rows if r["net_profit"] is not None and r.get("qty")
        ]
        skew[key] = {**_ratio_stats(rows), "net_edge_per_contract": _distribution(edges)}
    skew["missing_or_out_of_range"] = sum(
        r.get("skew_ms") is None or not (0 <= int(r["skew_ms"]) < 1000) for r in assessments
    )
    known_skews = [r for r in assessments if r.get("skew_ms") is not None]
    gt250 = sum(int(r["skew_ms"]) > 250 for r in known_skews)

    # Pair reverse rows where the stored market sides are swapped. The threshold
    # test still needs spreads, which schema v1 does not persist.
    pair_ids: set[tuple[int, int]] = set()
    pair_crossings = 0
    priced_pairs = 0

    def direction_key(row: dict[str, Any], *, reverse: bool = False) -> tuple[Any, ...]:
        try:
            stamp = _parse_time(row.get("computed_utc")).astimezone(UTC)
        except (AttributeError, ValueError):
            stamp = row.get("computed_utc")
        sides = (
            (row.get("no_market_key"), row.get("yes_market_key"))
            if reverse
            else (row.get("yes_market_key"), row.get("no_market_key"))
        )
        return (row.get("group_id"), stamp, row.get("qty"), *sides)

    by_direction = {direction_key(r): (idx, r) for idx, r in enumerate(assessments)}
    for idx, row in enumerate(assessments):
        reverse_entry = by_direction.get(direction_key(row, reverse=True))
        if reverse_entry is None or reverse_entry[1] is row:
            continue
        reverse_idx, reverse = reverse_entry
        pair = (min(idx, reverse_idx), max(idx, reverse_idx))
        if pair in pair_ids:
            continue
        pair_ids.add(pair)
        if any(
            x is None
            for x in (row["avg_yes"], row["avg_no"], reverse["avg_yes"], reverse["avg_no"])
        ):
            continue
        priced_pairs += 1
        one_sum = row["avg_yes"] + row["avg_no"]
        reverse_sum = reverse["avg_yes"] + reverse["avg_no"]
        if (one_sum < ONE and reverse_sum > ONE) or (reverse_sum < ONE and one_sum > ONE):
            pair_crossings += 1
    fee_unknown = 0
    for row in assessments:
        unknown = False
        try:
            reasons = json.loads(row.get("reasons_json") or "[]")
            unknown = isinstance(reasons, list) and "fee_unknown" in reasons
        except (TypeError, json.JSONDecodeError):
            reasons = []
        try:
            prov = json.loads(row.get("fee_provenance_json") or "{}")
            if isinstance(prov, dict):
                for side, fee_column in (("yes", "fee_yes"), ("no", "fee_no")):
                    metadata = prov.get(side)
                    if not isinstance(metadata, dict):
                        continue
                    if metadata.get("provenance") in {"unknown", "fee_unknown"}:
                        unknown = True
                    elif row.get(fee_column) is None:
                        # A present LegResult with no fee amount is a real unknown
                        # fee quote. An absent leg has no side metadata and is
                        # generally a depth/validation rejection instead.
                        unknown = True
        except (TypeError, json.JSONDecodeError):
            pass
        fee_unknown += unknown
    scenario_rows = [r for r in assessments if r["scenario_fees"] is not None]
    scenario_values = {theta: [] for theta in ("0.0695", "0.095")}
    for row in scenario_rows:
        for theta, values in scenario_values.items():
            profit = _scenario_profit(row, theta)
            if profit is not None:
                values.append(profit)
    scenario_qty: dict[str, Any] = {}
    for qty in (Decimal(1), Decimal(10), Decimal(100)):
        exact_rows = [r for r in scenario_rows if r["qty"] == qty]
        scenario_qty[str(qty)] = {
            theta: {
                "priced_assessments": len(exact_rows),
                "net_profit": _distribution(
                    [p for r in exact_rows if (p := _scenario_profit(r, theta)) is not None]
                ),
                "net_positive_fraction": _fraction(
                    sum(p > 0 for r in exact_rows if (p := _scenario_profit(r, theta)) is not None),
                    sum(_scenario_profit(r, theta) is not None for r in exact_rows),
                ),
            }
            for theta in scenario_values
        }
    live_qty10 = [
        r
        for r in periods["live"]
        if r.get("qty") == Decimal(10) and r.get("scenario_fees") is not None
    ]
    live_qty10_net = {
        theta: [p for r in live_qty10 if (p := _scenario_profit(r, theta)) is not None]
        for theta in scenario_values
    }
    episode_summaries: dict[str, Any] = {}
    durations_by_class: dict[str, list[Decimal]] = {}
    per_game: dict[str, int] = {}
    peak_net = [r["peak_net"] for r in episode_rows if r["peak_net"] is not None]
    sizes = [x for r in episode_rows for x in (r["min_size"], r["max_size"]) if x is not None]
    for row in episode_rows:
        klass = row.get("class") or "unknown"
        try:
            episode_start, episode_end = (
                _parse_time(row["first_seen_utc"]),
                _parse_time(row["last_seen_utc"]),
            )
            clipped_start = max(episode_start, _parse_time(since_s)) if since_s else episode_start
            clipped_end = min(episode_end, _parse_time(until_s)) if until_s else episode_end
            duration = _seconds(clipped_end, clipped_start)
        except (ValueError, TypeError):
            continue
        durations_by_class.setdefault(klass, []).append(duration)
        try:
            participants = json.loads(row.get("participants_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            participants = []
        if participants and row.get("event_start_utc"):
            event_start = _bound(row["event_start_utc"])
            game = f"{row.get('league') or 'unknown'}:{','.join(sorted(map(str, participants)))}:{event_start}"
        else:
            game = f"group:{row.get('group_id') or 'unknown'}"
        per_game[game] = per_game.get(game, 0) + 1
    for klass, values in durations_by_class.items():
        episode_summaries[klass] = {
            "count": len(values),
            "duration_seconds": {
                "p50": _percentile(values, Decimal(".5")),
                "p90": _percentile(values, Decimal(".9")),
                "max": max(values),
            },
            "duration_label": "sampled" if klass == "indicative" else "observed",
            "window_censored": bool(since_s or until_s),
        }
    games_sorted = sorted(per_game.values(), reverse=True)
    total_episodes = len(episode_rows)
    top5_share = _fraction(sum(games_sorted[:5]), total_episodes)
    overall_durations = [x for vs in durations_by_class.values() for x in vs]
    median_depth_values = [
        r["max_profitable_qty"] for r in assessments if r["max_profitable_qty"] is not None
    ]

    return {
        "methodology": {
            "proportion_scale": "fractions are 0..1; fields ending _pct are 0..100",
            "positive_fraction_denominator": "gross and net positive fractions use only rows where the corresponding metric is known; priced_assessments and gross_edge_priced_assessments expose coverage",
            "scenario_coverage": "scenario fractions exclude rows without valid persisted fills; qty profitability uses rows whose requested quantity exactly equals the bucket and does not scale other quantities",
            "percentiles": "p50 is conventional median (average of middle pair); p90 and p99 use nearest-rank ceil(p*n)",
            "skew_buckets_ms": "half-open intervals [0,100), [100,250), [250,500), [500,1000); missing_or_out_of_range is reported separately",
            "scenario_fees": "Kalshi coefficient 0.07, multiplier 1, per-order ceiling to 0.0001; Polymarket US theta 0.0695 or 0.095, sum then half-even round to cents; repricing uses persisted walked fills",
            "live_skew_share": "share of live assessments with positive gross edge and known skew whose skew exceeds 250 ms; association, not causal attribution",
        },
        "window": {
            "start_utc": start,
            "end_utc": end,
            "duration_seconds": duration_seconds,
            "since": since_s,
            "until": until_s,
        },
        "coverage": {
            "events": len(events),
            "gaps_by_kind": gaps,
            "venues": venues,
            "http_429_count": http429,
            "error_count": errors,
            "fresh_books_pct": None,
            "fresh_books_reason": "collector events do not record book-fresh intervals",
        },
        "opportunities": {
            "overall": _ratio_stats(assessments),
            "by_league": {k: _ratio_stats(v) for k, v in sorted(by_league.items())},
            "by_class": {k: _ratio_stats(v) for k, v in sorted(by_class.items())},
            "by_league_class": {k: _ratio_stats(v) for k, v in sorted(by_league_class.items())},
            "live_pregame": {k: _ratio_stats(v) for k, v in periods.items()},
            "skew_buckets": skew,
            "both_directions_sanity": {
                "available": False,
                "matched_pairs": len(pair_ids),
                "priced_pairs": priced_pairs,
                "unknown_price_pairs": len(pair_ids) - priced_pairs,
                "opposite_sides_crossing_one": pair_crossings,
                "violations": None,
                "reason": "spread is not persisted, so the required 1 + 2×spread threshold cannot be evaluated",
            },
        },
        "episodes": {
            "overall": {
                "count": total_episodes,
                "duration_seconds": {
                    "p50": _percentile(overall_durations, Decimal(".5")),
                    "p90": _percentile(overall_durations, Decimal(".9")),
                    "max": max(overall_durations) if overall_durations else None,
                },
                "peak_net": _distribution(peak_net),
                "size_range": {
                    "min": min(sizes) if sizes else None,
                    "max": max(sizes) if sizes else None,
                },
                "duration_label": "sampled"
                if any(r.get("class") == "indicative" for r in episode_rows)
                else "observed",
                "window_censored": bool(since_s or until_s),
            },
            "by_class": episode_summaries,
            "termination_reasons": {
                str(reason or "ongoing"): sum(
                    r.get("termination_reason") == reason for r in episode_rows
                )
                for reason in sorted(
                    {r.get("termination_reason") for r in episode_rows}, key=lambda x: str(x)
                )
            },
            "per_game": per_game,
            "top5_game_share": top5_share,
        },
        "fees": {
            "fee_unknown_count": fee_unknown,
            "fee_unknown_share": _fraction(fee_unknown, len(assessments)),
            "scenario": {
                "available": bool(scenario_rows),
                "theta_values": [Decimal("0.0695"), Decimal("0.095")],
                "kalshi_formula": "0.07*p*(1-p)",
                "kalshi_multiplier": Decimal(1),
                "kalshi_rounding": "direct_member to 0.0001 per order",
                "method": "exact fee functions applied to persisted walked fills; scenario profit = quantity minus acquisition costs and scenario fees",
                "repricable_assessments": len(scenario_rows),
                "unavailable_assessments": len(assessments) - len(scenario_rows),
                "net_profit": {
                    theta: _distribution(vals) for theta, vals in scenario_values.items()
                },
                "by_exact_quantity": scenario_qty,
                "unavailable_reason": None
                if scenario_rows
                else "persisted rows lack walked price levels",
            },
        },
        "exception_risk": {
            "count": sum(
                bool(r.get("exception_risk")) or bool(r.get("group_exception_risk"))
                for r in assessments
            ),
            "share": _fraction(
                sum(
                    bool(r.get("exception_risk")) or bool(r.get("group_exception_risk"))
                    for r in assessments
                ),
                len(assessments),
            ),
        },
        "verdict_inputs": {
            "live_net_positive_qty10_fraction": _fraction(
                sum(x > 0 for x in live_qty10_net["0.095"]), len(live_qty10_net["0.095"])
            ),
            "live_net_positive_qty10_fraction_by_theta": {
                theta: _fraction(sum(x > 0 for x in values), len(values))
                for theta, values in live_qty10_net.items()
            },
            "median_episode_duration_s": _percentile(overall_durations, Decimal(".5")),
            "median_depth": _percentile(median_depth_values, Decimal(".5")),
            "skew_gt_250ms_share": _fraction(
                sum(
                    int(r["skew_ms"]) > 250
                    for r in periods["live"]
                    if r.get("skew_ms") is not None
                    and r.get("gross_edge") is not None
                    and r["gross_edge"] > 0
                ),
                sum(
                    r.get("skew_ms") is not None
                    and r.get("gross_edge") is not None
                    and r["gross_edge"] > 0
                    for r in periods["live"]
                ),
            ),
            "all_assessments_skew_gt_250ms_share": _fraction(gt250, len(known_skews)),
            "live_net_positive_qty10_priced_assessments": len(live_qty10_net["0.095"]),
        },
    }
