"""Markdown and JSON renderers for simulator output."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path


def render_sim_report(path: Path | str, format: str = "md") -> str:
    """Render scenario-level simulator results from a completed database."""
    if format not in {"md", "json"}:
        raise ValueError("format must be md or json")
    with closing(sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT delay_yes_ms, delay_no_ms, haircut, order_type, opportunities,
                      both_filled, partial, any_filled, fill_rate, one_sided, one_sided_rate,
                      realized_edge, worst_exposure
               FROM sim_summary ORDER BY delay_yes_ms, delay_no_ms, haircut, order_type"""
            )
        ]
        if not rows:
            raise ValueError("simulation database has no summary rows")
    if format == "json":
        return json.dumps({"scenarios": rows}, indent=2, sort_keys=True) + "\n"
    lines = [
        "# Simulation report",
        "",
        "All values are from deterministic fills against recorded books; they are scenarios, not live fills.",
        "",
        "| YES delay (ms) | NO delay (ms) | Haircut | Order | Opportunities | Both filled | Partial | Fill rate | One-sided | One-sided rate | Realized edge | Worst exposure |",
        "|---:|---:|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        fill_rate = Decimal(row["fill_rate"])
        one_sided_rate = Decimal(row["one_sided_rate"])
        realized_edge = Decimal(row["realized_edge"])
        worst_exposure = Decimal(row["worst_exposure"])
        lines.append(
            f"| {row['delay_yes_ms']} | {row['delay_no_ms']} | {row['haircut']} | "
            f"{row['order_type']} | {row['opportunities']} | {row['both_filled']} | {row['partial']} | "
            f"{fill_rate * 100:.1f}% | {row['one_sided']} | {one_sided_rate * 100:.1f}% | "
            f"${realized_edge:.4f} | ${worst_exposure:.4f} |"
        )
    return "\n".join(lines) + "\n"
