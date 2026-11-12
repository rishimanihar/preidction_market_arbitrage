"""Domain models for books, reviews, groups, and assessments.

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
    # Lets a group hold three members. Equivalence groups require distinct venues.
    GEMINI = "gemini"


class Side(StrEnum):
    YES = "yes"
    NO = "no"


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise TypeError("float is not permitted; use Decimal")
    return value


def _require_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("datetime must be timezone-aware")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware")
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
    """One binary contract on a venue.

    ``expected_payout_utc`` is ``None`` when the payout time is unknown.
    ``participants`` are canonical team ids. ``market_key`` is ``venue:market_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Venue
    market_id: str
    title: str
    league: str | None = None
    participants: tuple[str, ...] = ()
    yes_participant: str | None = None
    market_type: str
    line: Decimal | None = None
    event_start_utc: datetime | None = None
    expected_resolution_utc: datetime | None = None
    expected_resolution_src: str | None = None
    expected_payout_utc: datetime | None = None
    status: str
    rules_text: str
    rules_hash: str
    fee_meta: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("line", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("event_start_utc", "expected_resolution_utc", "expected_payout_utc")
    @classmethod
    def _aware_datetimes(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @property
    def market_key(self) -> str:
        return f"{self.venue}:{self.market_id}"


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


class AssessmentClass(StrEnum):
    QUALIFIED = "qualified"
    INDICATIVE = "indicative"
    REJECTED = "rejected"


class GroupStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REVOKED = "revoked"
    REJECTED = "rejected"


class EpisodeTermination(StrEnum):
    DISQUALIFIED = "disqualified"
    DATA_GAP = "data_gap"
    COLLECTOR_RESTART = "collector_restart"
    COLLECTOR_SHUTDOWN = "collector_shutdown"
    CLOCK_JUMP = "clock_jump"
    MAINTENANCE = "maintenance"
    RULES_CHANGED = "rules_changed"
    MARKET_CLOSED = "market_closed"


class Leg(BaseModel):
    """The contract we would buy: one side of one venue market."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Venue
    market_id: str
    side: Side


class GroupMember(BaseModel):
    """A market that expresses the group's single outcome.

    ``native_side_for_outcome`` is YES when that venue's YES is the outcome,
    and NO when the venue's YES is the complement. ``approved_rules_hash`` is
    the hash the reviewer was shown; it stays ``None`` until approval.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: Venue
    market_id: str
    native_side_for_outcome: Side
    approved_rules_hash: str | None = None


class ExceptionRow(BaseModel):
    """One settlement case in a group's exception table.

    ``payouts`` maps ``venue:market_id`` to what that member pays.
    ``preserves_unit_payout`` is true when a YES/NO pair still pays $1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome_case: str
    payouts: dict[str, str]
    preserves_unit_payout: bool


class EquivalenceGroup(BaseModel):
    """Markets on different venues that share one real-world outcome.

    A group holds exactly one outcome, so YES on one member and NO on another
    pay $1 between them. Two members may not share a venue.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: str
    outcome: str
    members: tuple[GroupMember, ...]
    status: GroupStatus
    hash_version: int = 1
    exception_risk: bool = False
    exception_table: tuple[ExceptionRow, ...] = ()
    reviewer: str | None = None
    decided_utc: datetime | None = None
    notes: str = ""

    @field_validator("outcome")
    @classmethod
    def _outcome_non_empty(cls, value: str) -> str:
        if value == "":
            raise ValueError("outcome must be non-empty")
        return value

    @field_validator("decided_utc")
    @classmethod
    def _aware_decided_utc(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @model_validator(mode="after")
    def _group_rules(self) -> Self:
        if len(self.members) < 2:
            raise ValueError("an equivalence group needs at least 2 members")
        venues = [member.venue for member in self.members]
        if len(set(venues)) != len(venues):
            raise ValueError("member venues must be distinct")
        if self.status is GroupStatus.APPROVED:
            if self.reviewer is None or self.reviewer == "":
                raise ValueError("an approved group requires reviewer")
            if self.decided_utc is None:
                raise ValueError("an approved group requires decided_utc")
            if any(not member.approved_rules_hash for member in self.members):
                raise ValueError("an approved group requires approved_rules_hash on every member")
        return self


class FeeQuote(BaseModel):
    """Taker fee for one leg.

    ``amount`` is ``None`` when the fee is unknown. Unknown is not zero.
    ``conservative_bound`` is true when ``amount`` is an upper bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal | None = None
    provenance: str
    conservative_bound: bool = False

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("provenance")
    @classmethod
    def _provenance_non_empty(cls, value: str) -> str:
        if value == "":
            raise ValueError("provenance must be non-empty")
        return value


class LegResult(BaseModel):
    """Contracts acquired while walking one leg, plus that leg's fee quote."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg: Leg
    levels_consumed: list[Level]
    quantity: Decimal
    cost: Decimal
    avg_price: Decimal
    fee: FeeQuote
    book_age_ms: int | None = None

    @field_validator("quantity", "cost", "avg_price", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)


class OpportunityAssessment(BaseModel):
    """One direction of one group at a requested quantity.

    ``result_class`` is ``qualified`` only for atomic books with no reasons.
    A rejected result must say why. ``None`` economics mean the figure is unknown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: str
    yes_leg: Leg
    no_leg: Leg
    requested_qty: Decimal
    yes_result: LegResult | None
    no_result: LegResult | None
    capital: Decimal | None
    gross_edge: Decimal | None
    net_profit: Decimal | None
    roc: Decimal | None
    max_profitable_qty: Decimal | None
    skew_ms: int | None
    books_atomic: bool
    exception_risk: bool
    result_class: AssessmentClass
    reasons: tuple[str, ...]
    model_version: str
    computed_utc: datetime

    @field_validator(
        "requested_qty",
        "capital",
        "gross_edge",
        "net_profit",
        "roc",
        "max_profitable_qty",
        mode="before",
    )
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("computed_utc")
    @classmethod
    def _aware_computed_utc(cls, value: datetime) -> datetime:
        checked = _require_aware(value)
        if checked is None:
            raise ValueError("datetime must be timezone-aware")
        return checked

    @model_validator(mode="after")
    def _assessment_rules(self) -> Self:
        if self.yes_leg.side is not Side.YES:
            raise ValueError("yes_leg.side must be YES")
        if self.no_leg.side is not Side.NO:
            raise ValueError("no_leg.side must be NO")
        if self.yes_leg.venue == self.no_leg.venue:
            raise ValueError("yes_leg and no_leg must be on different venues")
        if self.result_class is AssessmentClass.QUALIFIED:
            if not self.books_atomic:
                raise ValueError("a qualified assessment requires books_atomic")
            if self.reasons:
                raise ValueError("a qualified assessment cannot include reasons")
        if self.result_class is AssessmentClass.REJECTED and not self.reasons:
            raise ValueError("a rejected assessment requires reasons")
        return self


class Episode(BaseModel):
    """A run of qualified or indicative assessments for one direction and size.

    Indicative and qualified episodes are separate rows. ``result_class``
    cannot be ``rejected``. ``last_seen_utc`` is at or after ``first_seen_utc``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int | None = None
    group_id: str
    yes_leg: Leg
    no_leg: Leg
    qty: Decimal
    result_class: AssessmentClass
    model_version: str
    first_seen_utc: datetime
    last_seen_utc: datetime
    observations: int
    peak_net: Decimal
    min_size: Decimal
    max_size: Decimal
    termination: EpisodeTermination | None = None
    termination_detail: str | None = None

    @field_validator("qty", "peak_net", "min_size", "max_size", mode="before")
    @classmethod
    def _no_float(cls, value: object) -> object:
        return _reject_float(value)

    @field_validator("first_seen_utc", "last_seen_utc")
    @classmethod
    def _aware_seen(cls, value: datetime) -> datetime:
        checked = _require_aware(value)
        if checked is None:
            raise ValueError("datetime must be timezone-aware")
        return checked

    @field_validator("observations")
    @classmethod
    def _observations_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("observations must be >= 1")
        return value

    @model_validator(mode="after")
    def _episode_rules(self) -> Self:
        if self.result_class is AssessmentClass.REJECTED:
            raise ValueError("episode result_class must be qualified or indicative")
        if self.last_seen_utc < self.first_seen_utc:
            raise ValueError("last_seen_utc must be >= first_seen_utc")
        return self
