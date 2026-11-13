"""Pure formatting and ordering helpers used by the review UI."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

_CHICAGO = ZoneInfo("America/Chicago")


def as_decimal(value: Decimal | str | int | None) -> Decimal | None:
    """Convert a SQLite TEXT money value without passing through float."""
    if value is None or value == "":
        return None
    if isinstance(value, (bool, float)):
        raise TypeError("money values must not be floats")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("money values must be finite")
    return result


def format_money(value: Decimal | str | int | None, *, places: int = 2) -> str:
    amount = as_decimal(value)
    if amount is None:
        return "—"
    return f"${amount:,.{places}f}"


def format_chicago_time(value: datetime | str | None) -> str:
    """Render an aware UTC timestamp in Chicago, including its zone label."""
    if value is None or value == "":
        return "—"
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(_CHICAGO).strftime("%Y-%m-%d %H:%M:%S %Z")


def rank_assessments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by net profit descending, then ROC descending; unknowns sort last."""

    def key(row: dict[str, Any]) -> tuple[bool, Decimal, bool, Decimal]:
        net = as_decimal(row.get("net_profit"))
        roc = as_decimal(row.get("roc"))
        return (net is None, -(net or Decimal(0)), roc is None, -(roc or Decimal(0)))

    return sorted(rows, key=key)


def assessment_display_rows(
    rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]] | None = None,
    *,
    ranked: bool = True,
) -> list[dict[str, Any]]:
    """Build display-safe live rows using only persisted assessment evidence."""
    ordered_rows = rank_assessments(rows) if ranked else rows
    lookup = all_rows if all_rows is not None else rows
    output: list[dict[str, Any]] = []
    for row in ordered_rows:
        yes_sum = _average_sum(row)
        opposite = _opposite_assessment(row, lookup)
        no_sum = _average_sum(opposite) if opposite is not None else None
        provenance = row.get("fee_provenance") or {}
        fees = (
            " · ".join(
                part
                for side in ("yes", "no")
                if (part := _fee_display(side, row.get(f"fee_{side}"), provenance.get(side)))
            )
            or "Unavailable"
        )
        reasons = row.get("reasons")
        dust = "Yes" if reasons is not None and "dust" in str(reasons).lower() else "Unknown"
        output.append(
            {
                "Updated": format_chicago_time(row.get("computed_utc")),
                "YES leg": _leg_display(row, "yes"),
                "NO leg": _leg_display(row, "no"),
                "This direction avg-price sum": format_money(yes_sum, places=4),
                "Opposite direction avg-price sum": format_money(no_sum, places=4),
                "Net profit": format_money(row.get("net_profit"), places=4),
                "ROC": _format_roc(row.get("roc")),
                "Fees and provenance": fees,
                "YES age ms": row.get("age_yes_ms", "Unavailable"),
                "NO age ms": row.get("age_no_ms", "Unavailable"),
                "Skew ms": row.get("skew_ms", "Unavailable"),
                "Size at touch": "Unavailable in persisted assessments",
                "Exception risk": _flag(row.get("exception_risk")),
                "Dust": dust,
                "Reasons": reasons if reasons is not None else "Unavailable",
            }
        )
    return output


def _average_sum(row: dict[str, Any] | None) -> Decimal | None:
    if row is None or row.get("avg_yes") is None or row.get("avg_no") is None:
        return None
    return as_decimal(row["avg_yes"]) + as_decimal(row["avg_no"])


def _opposite_assessment(row: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match only reverse legs recorded for the same group, size, and instant."""
    for candidate in rows:
        if candidate is row:
            continue
        if (
            candidate.get("group_id") == row.get("group_id")
            and candidate.get("qty") == row.get("qty")
            and candidate.get("computed_utc") == row.get("computed_utc")
            and candidate.get("yes_market_key") == row.get("no_market_key")
            and candidate.get("no_market_key") == row.get("yes_market_key")
        ):
            return candidate
    return None


def _leg_display(row: dict[str, Any], side: str) -> str:
    title = row.get(f"{side}_title") or row.get(f"{side}_market_key") or "Unavailable"
    average = format_money(row.get(f"avg_{side}"), places=4)
    return f"{title} @ {average} × {row.get('qty', 'Unavailable')}"


def _fee_display(side: str, amount: Any, provenance: Any) -> str:
    if amount is None:
        return ""
    details = provenance if isinstance(provenance, dict) else {}
    tag = " · conservative bound" if details.get("conservative_bound") is True else ""
    source = details.get("provenance") or "provenance unavailable"
    return f"{side.upper()} {format_money(amount, places=4)} · {source}{tag}"


def _format_roc(value: Any) -> str:
    amount = as_decimal(value)
    return "Unavailable" if amount is None else f"{amount * 100:.2f}%"


def _flag(value: Any) -> str:
    if value in (True, 1, "1"):
        return "Yes"
    if value in (False, 0, "0"):
        return "No"
    return "Unavailable"
