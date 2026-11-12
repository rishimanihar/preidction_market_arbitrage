"""Shared book builders for calculator tests."""

from datetime import UTC, datetime
from decimal import Decimal

from pmarb.fees import FeeConfig
from pmarb.models import Book, Level, Venue

NOW = 10_000.0
RECEIVED = datetime(2026, 11, 11, 18, tzinfo=UTC)


def make_book(
    venue: Venue,
    market_id: str,
    yes_asks: list[Level],
    no_asks: list[Level],
    *,
    received_monotonic: float,
    valid: bool = True,
    invalid_reason: str | None = None,
    source_ts: datetime | None = None,
) -> Book:
    return Book(
        venue=venue,
        market_id=market_id,
        yes_asks=yes_asks,
        no_asks=no_asks,
        raw_native=None,
        source_ts=source_ts,
        received_utc=RECEIVED,
        received_monotonic=received_monotonic,
        connection_generation=1,
        valid=valid,
        invalid_reason=invalid_reason,
    )


def standard_fees(theta: Decimal | None = Decimal("0.095")) -> FeeConfig:
    return FeeConfig(
        kalshi_multiplier=Decimal(1),
        kalshi_rounding="direct_member",
        polyus_theta=theta,
        version="phase1-test",
    )
