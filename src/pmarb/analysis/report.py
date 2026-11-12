"""Markdown and JSON rendering for M1 analysis reports."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

_VERDICT_QUESTIONS = (
    (
        "live_net_positive_qty10_fraction",
        "What fraction of live-game assessments have scenario net > 0 at qty 10?",
    ),
    ("median_episode_duration_s", "What is the median episode duration in seconds?"),
    ("median_depth", "What is the median profitable depth?"),
    ("skew_gt_250ms_share", "What share of results comes from skew > 250 ms?"),
)


def _json_value(value: Any) -> Any:
    """Convert exact monetary values to strings for lossless JSON output."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def render_json(metrics: dict[str, Any]) -> str:
    """Serialize metrics with Decimal values preserved as exact strings."""
    return json.dumps(_json_value(metrics), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return format(value, ".8g")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_markdown_value(value), sort_keys=True, ensure_ascii=False)
    return str(value)


def _markdown_value(value: Any) -> Any:
    """Round Decimal values for Markdown only; JSON rendering stays exact."""
    if isinstance(value, Decimal):
        return format(value, ".8g")
    if isinstance(value, dict):
        return {str(key): _markdown_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_markdown_value(item) for item in value]
    return value


def _table(rows: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    if not keys:
        return ["_No data._"]
    if len(keys) > 8:
        pivoted = []
        for index, row in enumerate(rows, start=1):
            group = row.get("group", f"row {index}")
            for key, value in row.items():
                if key != "group":
                    pivoted.append({"group": group, "metric": key, "value": value})
        return _table(pivoted)
    lines = ["| " + " | ".join(keys) + " |", "| " + " | ".join("---" for _ in keys) + " |"]
    for row in rows:
        cells = []
        for key in keys:
            value = row.get(key)
            metric_name = str(row.get("metric", ""))
            is_fresh_pct = key == "fresh_pct" or (
                key == "value"
                and (
                    metric_name in {"fresh_pct", "fresh_known_pct"}
                    or metric_name.endswith((".fresh_pct", ".fresh_known_pct"))
                )
            )
            if is_fresh_pct and isinstance(value, Decimal):
                display = f"{format(value, '.8g')}%"
            else:
                display = _display(value)
            cells.append(display.replace("|", "\\|").replace("\n", " "))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in record.items():
        label = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, label))
        else:
            flat[label] = value
    return flat


def _section(name: str, value: Any) -> list[str]:
    lines = [f"## {name.replace('_', ' ').title()}", ""]
    if name == "coverage":
        lines.extend(
            [
                "Venue `fresh_pct` is fresh time as a percentage of the full run window and is a lower bound when state is unknown. `fresh_known_pct` uses only intervals with a known health state; `fresh_sample_fraction` is the fraction of health samples marked fresh.",
                "",
            ]
        )
    if name == "fees":
        lines.extend(
            [
                "Fee figures are conditional scenarios repriced across exact walked levels. Kalshi assumes multiplier 1 and `0.07 × p × (1−p)`, rounded up to 0.0001; Polymarket US uses Θ = 0.0695 or 0.095 and rounds the order total half-even to cents. These assumptions do not establish account-specific fees; rows without exact fills are unavailable and excluded from the repricable-row verdict denominator.",
                "",
            ]
        )
    if isinstance(value, dict):
        # Summary scalars are shown together; grouped records become readable tables.
        scalar = {
            key: item for key, item in value.items() if not isinstance(item, (dict, list, tuple))
        }
        if scalar:
            lines.extend(_table([scalar]))
            lines.append("")
        for key, item in value.items():
            if isinstance(item, list) and item and all(isinstance(row, dict) for row in item):
                lines.extend([f"### {key.replace('_', ' ').title()}", ""])
                lines.extend(_table(item))
                lines.append("")
            elif isinstance(item, dict):
                lines.extend([f"### {key.replace('_', ' ').title()}", ""])
                records = []
                for group, metrics in item.items():
                    records.append(
                        {"group": group, **_flatten(metrics)}
                        if isinstance(metrics, dict)
                        else {"group": group, "value": metrics}
                    )
                lines.extend(_table(records))
                lines.append("")
            elif isinstance(item, list):
                lines.extend([f"### {key.replace('_', ' ').title()}", "", "| Value |", "| --- |"])
                lines.extend("| " + _display(entry).replace("|", "\\|") + " |" for entry in item)
                lines.append("")
    elif isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        lines.extend(_table(value))
    else:
        lines.extend(_table([{"value": value}]))
    return lines


def _verdict_lines(metrics: dict[str, Any]) -> list[str]:
    inputs = metrics.get("verdict_inputs", {})
    lines = ["## M1 verdict inputs"]
    if not isinstance(inputs, dict):
        inputs = {}
    for key, question in _VERDICT_QUESTIONS:
        extra = ""
        if key == "live_net_positive_qty10_fraction":
            per_theta = inputs.get("live_net_positive_qty10_fraction_by_theta")
            priced = inputs.get("live_net_positive_qty10_priced_assessments")
            if per_theta is not None or priced is not None:
                extra = (
                    f" (by Θ: {_display(per_theta)}; repricable live qty-10 rows: "
                    f"{_display(priced)})"
                )
        lines.append(f"- {question} **{_display(inputs.get(key))}**{extra}")
    return lines


def render_markdown(metrics: dict[str, Any]) -> str:
    """Render a concise human report with a four-question M1 verdict block."""
    lines = ["# M1 Soak Analysis", "", *_verdict_lines(metrics), ""]
    if "methodology" in metrics:
        lines.extend(_section("methodology", metrics["methodology"]))
    for section in ("window", "coverage", "opportunities", "episodes", "fees", "exception_risk"):
        if section in metrics:
            lines.extend(_section(section, metrics[section]))
    return "\n".join(lines).rstrip() + "\n"


def render_report(metrics: dict[str, Any], output_format: str = "md") -> str:
    """Render metrics as Markdown (``md``) or lossless machine-readable JSON."""
    if output_format == "md":
        return render_markdown(metrics)
    if output_format == "json":
        return render_json(metrics)
    raise ValueError("format must be 'md' or 'json'")
