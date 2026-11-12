"""Domain models for books, reviews, and assessments.

Prices, quantities, fees, and money are ``Decimal``. ``Book.received_monotonic``
is the one float: it stores ``time.monotonic()`` so later collectors can stamp
age and skew on the same clock.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET_US = "polymarket_us"


class Side(StrEnum):
    YES = "yes"
    NO = "no"


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise TypeError("float is not permitted; use Decimal")
    return value


class Level(BaseModel):
    """One price level. ``price`` is dollars per contract in (0, 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: Decimal
    size: Decimal

    @field_validator("price", "size", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("price")
    @classmethod
    def _price_in_open_unit_interval(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0 or value >= 1:
            raise ValueError("price must be finite and in (0, 1)")
        return value

    @field_validator("size")
    @classmethod
    def _size_positive(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= 0:
            raise ValueError("size must be finite and positive")
        return value


class Book(BaseModel):
    """Normalized YES/NO acquisition ladders for one venue market.

    ``yes_asks`` is the ladder to buy YES, ascending by price.
    ``no_asks`` is the ladder to buy NO, ascending by price.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Venue
    market_id: str
    yes_asks: list[Level]
    no_asks: list[Level]
    raw_native: dict[str, Any] | None = None
    source_ts: datetime | None = None
    received_utc: datetime
    received_monotonic: float
    connection_generation: int
    valid: bool
    invalid_reason: str | None = None


class Market(BaseModel):
    """Venue metadata for one binary contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Venue
    market_id: str
    title: str
    outcome_meaning: str
    status: str
    rules_text: str
    url: str
    expected_resolution_utc: datetime | None = None
    expected_payout_utc: datetime | None = None
    fee_metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class PairReview(BaseModel):
    """Human approval of one Kalshi / Polymarket US pair.

    ``outcome_mapping`` records the assumed alignment. The default research
    assumption is YES on Kalshi against YES on Polymarket US, stored as
    ``{"YES": "YES", "NO": "NO"}``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pair_id: str
    kalshi_market_id: str
    polymarket_market_id: str
    outcome_mapping: dict[str, str]
    approved: bool
    reviewer: str
    notes: str
    kalshi_rules_hash: str
    polymarket_rules_hash: str
    approved_utc: datetime | None = None


class LegFill(BaseModel):
    """Contracts acquired on one venue while walking its ladder.

    ``fee`` is ``None`` when that leg's fee model is unknown. A missing fee
    is not zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Venue
    side: Side
    levels_consumed: list[Level]
    quantity: Decimal
    cost: Decimal
    avg_price: Decimal
    fee: Decimal | None

    @field_validator("quantity", "cost", "avg_price", "fee", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)


class Assessment(BaseModel):
    """Profitability of one direction at a requested quantity.

    Direction ``A`` buys Kalshi YES and Polymarket US NO.
    Direction ``B`` buys Polymarket US YES and Kalshi NO.
    ``legs`` is ``(YES acquisition, NO acquisition)`` when the requested
    quantity can be walked on both books.

    ``net_profit`` is ``None`` when the requested quantity cannot be priced
    completely, including when the Polymarket US fee coefficient is unknown.
    An unknown fee is scenario-only: prices may still appear on the legs, and
    profit is withheld instead of treating the fee as zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pair_id: str
    direction: str
    requested_qty: Decimal
    legs: tuple[LegFill, LegFill] | None
    capital: Decimal | None
    net_profit: Decimal | None
    roc: Decimal | None
    extra_costs: Decimal
    qualified: bool
    rejection_reasons: list[str]
    max_profitable_qty: Decimal | None
    fee_model_version: str
    computed_utc: datetime

    @field_validator(
        "requested_qty",
        "capital",
        "net_profit",
        "roc",
        "extra_costs",
        "max_profitable_qty",
        mode="before",
    )
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)

    @model_validator(mode="after")
    def _qualified_requires_economics(self) -> Self:
        if self.direction not in {"A", "B"}:
            raise ValueError("direction must be 'A' or 'B'")
        priced = (
            self.legs is not None
            and self.capital is not None
            and self.net_profit is not None
            and self.roc is not None
        )
        if self.qualified and not priced:
            raise ValueError("a qualified assessment requires priced legs")
        if self.qualified and self.rejection_reasons:
            raise ValueError("a qualified assessment cannot include rejection reasons")
        return self
