"""Kalshi contract terms stored for the review app, without changing the rules hash."""

from __future__ import annotations

import json
from pathlib import Path

from pmarb.collector.clock import FakeClock
from pmarb.collector.discovery import Discovery
from pmarb.collector.registry import Registry
from pmarb.matching.rules import canonical_rules, rules_hash
from pmarb.models import Market, Venue
from pmarb.storage.db import open_writer
from pmarb.venues.kalshi.parse import contract_terms_text, parse_market

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_ingest_stores_kalshi_rules_as_terms_and_backfills_empty(tmp_path) -> None:
    series = _fixture("series_KXNFLGAME.json")["series"]
    raw = _fixture("markets_page_1.json")["markets"][0]
    event = {
        "occurrence_datetime": raw["occurrence_datetime"],
        "rules_primary": "Event rules for settlement.",
    }
    market = parse_market(raw, series, event)
    before = rules_hash(market)
    assert "terms" not in canonical_rules(market)
    text = contract_terms_text(market.raw)
    assert raw["rules_primary"] in text
    assert raw["rules_secondary"].strip() in text
    assert "Event rules for settlement." in text

    conn = open_writer(tmp_path / "pmarb.sqlite")
    try:
        discovery = Discovery(
            kalshi=object(),
            polyus=object(),
            registry=Registry(),
            review=object(),
            clock=FakeClock(),
            interval_s=lambda: 900.0,
            enqueue=lambda op: op(conn),
            episodes=object(),
        )
        stored = discovery._ingest(market)
        assert stored is not None
        assert rules_hash(stored) == before
        row = conn.execute(
            "SELECT rules_text, terms_text FROM rules_versions WHERE market_key = ?",
            (stored.market_key,),
        ).fetchone()
        assert row[0] == stored.rules_text
        assert row[1] == text

        conn.execute(
            "UPDATE rules_versions SET terms_text = '' WHERE market_key = ?",
            (stored.market_key,),
        )
        discovery._ingest(stored)
        again = conn.execute(
            "SELECT rules_text, terms_text FROM rules_versions WHERE market_key = ?",
            (stored.market_key,),
        ).fetchone()
        assert again == (stored.rules_text, text)

        poly = Market(
            venue=Venue.POLYMARKET_US,
            market_id="pm-terms",
            title="Synthetic poly market",
            market_type="moneyline",
            status="active",
            rules_text="Polymarket description",
            rules_hash="",
            raw={"description": "Polymarket description"},
        )
        stored_poly = discovery._ingest(poly)
        assert stored_poly is not None
        poly_terms = conn.execute(
            "SELECT terms_text FROM rules_versions WHERE market_key = ?",
            (stored_poly.market_key,),
        ).fetchone()
        assert poly_terms == ("",)
    finally:
        conn.close()
