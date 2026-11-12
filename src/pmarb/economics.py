"""N-venue complementary economics.

``assess`` prices one YES leg against one NO leg. ``assess_group`` prices every
ordered pair of members in one equivalence group. Both are pure: ``Decimal``
money, no clock reads, and no I/O. ``computed_utc`` is the caller's ``now_wall``.

Books and fee models are keyed by ``Market.market_key`` (``venue:market_id``).

A group's single outcome is what "happens". For a member whose
``native_side_for_outcome`` is YES, happening is a YES buy and not happening is
a NO buy. ``native_side_for_outcome`` NO flips both of those. The YES leg stored
on ``OpportunityAssessment`` is the "happens" purchase only when that member's
native side is YES. When both members are native NO, the happens purchase is
the NO leg and the YES leg is the complement on the other member.

``OpportunityAssessment`` requires one native YES and one native NO. A pair that
would buy the same native side on both members is not representable, so
``assess_group`` skips it. Plan section 3.4 rejects those groups at review time:
the flip is valid only when the venue's two outcomes are exact complements.
Pairs of members on the same venue are skipped as well.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from pmarb.ladder import InsufficientDepth, InvalidLadder, available_size, walk
from pmarb.models import (
    AssessmentClass,
    Book,
    EquivalenceGroup,
    FeeQuote,
    GroupMember,
    Leg,
    LegResult,
    Level,
    OpportunityAssessment,
    Side,
    Venue,
)
from pmarb.protocols import FeeModel
from pmarb.safety import assert_no_order_client

_REASON_PREFIXES = (
    "book_invalid",
    "book_stale",
    "book_skew",
    "insufficient_depth",
    "dust",
    "missing_book",
    "fee_unknown",
    "invalid_capital",
    "not_profitable",
    "below_min_profit",
    "below_min_roc",
)
_BLOCKING_PREFIXES = ("book_invalid_", "book_stale_", "dust_")
_MS = Decimal(1000)
_ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class LegInput:
    """One side to buy, the book it walks, and the fee model for that market.

    ``leg.side`` YES walks ``book.yes_asks``. NO walks ``book.no_asks``.
    """

    leg: Leg
    book: Book
    fee_model: FeeModel


@dataclass(frozen=True, slots=True)
class Limits:
    """Thresholds from plan sections 6.2 and 11."""

    max_book_age_s: Decimal = Decimal(2)
    max_skew_indicative_s: Decimal = Decimal(1)
    max_skew_qualified_s: Decimal = Decimal("0.25")
    min_profit: Decimal = Decimal("0.50")
    min_roc: Decimal = Decimal("0.005")
    min_touch_qty: Decimal = Decimal(10)
    scan_limit: Decimal = Decimal(1000)

    def __post_init__(self) -> None:
        for name in (
            "max_book_age_s",
            "max_skew_indicative_s",
            "max_skew_qualified_s",
            "min_profit",
            "min_roc",
            "min_touch_qty",
            "scan_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class _Clock:
    mono: Decimal | None
    mono_age_s: Decimal | None
    wall_age_s: Decimal | None

    @property
    def usable(self) -> bool:
        return self.mono_age_s is not None and self.wall_age_s is not None

    @property
    def age_s(self) -> Decimal | None:
        if self.mono_age_s is None or self.wall_age_s is None:
            return None
        return max(self.mono_age_s, self.wall_age_s)


@dataclass(frozen=True, slots=True)
class _MissingFeeModel:
    """Stand-in when a market has no registered fee model."""

    market_key: str

    def taker_fee(self, levels: Sequence[Level], side: Side) -> FeeQuote:
        return FeeQuote(amount=None, provenance=f"no fee model for {self.market_key}")


def assess(
    group_id: str,
    yes: LegInput,
    no: LegInput,
    qty: Decimal,
    *,
    now_monotonic: float,
    now_wall: datetime,
    books_atomic: bool,
    exception_risk: bool,
    model_version: str,
    limits: Limits = Limits(),  # noqa: B008
) -> OpportunityAssessment:
    """Price ``qty`` contracts of YES against the same quantity of NO.

    ``model_version`` is required and keyword-only. It sits before ``limits``
    so the defaulted limits argument stays legal Python.

    A reason rejects the result. With no reasons, atomic books are qualified
    and polled books are indicative. Fee amounts of ``None`` clear capital,
    net, and ROC. ``max_profitable_qty`` is the largest whole number of
    contracts at or under the scan limit and both depths that passes the same
    checks. Freshness, skew, and dust do not depend on size. Every other size
    recomputes the walk and both fees, because cent rounding is not monotone.
    """
    _require_group_id(group_id)
    _require_leg_input("yes", yes, Side.YES)
    _require_leg_input("no", no, Side.NO)
    if yes.leg.venue == no.leg.venue:
        raise ValueError("yes and no legs must be on different venues")
    qty = _require_qty(qty)
    now_monotonic = _require_now_monotonic(now_monotonic)
    now_wall = _require_now_wall(now_wall)
    _require_bool(books_atomic, "books_atomic")
    _require_bool(exception_risk, "exception_risk")
    _require_model_version(model_version)
    if not isinstance(limits, Limits):
        raise TypeError("limits must be Limits")

    reasons: set[str] = set()
    yes_clock = _clock(yes.book, now_monotonic, now_wall)
    no_clock = _clock(no.book, now_monotonic, now_wall)
    _note_book(yes.book, yes_clock, limits, reasons)
    _note_book(no.book, no_clock, limits, reasons)
    _note_skew(yes_clock, no_clock, books_atomic, limits, reasons)

    yes_levels = _asks(yes.book, yes.leg.side)
    no_levels = _asks(no.book, no.leg.side)
    yes_walk = _note_walk(yes_levels, qty, yes.leg.venue.value, limits, reasons)
    no_walk = _note_walk(no_levels, qty, no.leg.venue.value, limits, reasons)

    yes_result: LegResult | None = None
    no_result: LegResult | None = None
    gross_edge: Decimal | None = None
    capital: Decimal | None = None
    net_profit: Decimal | None = None
    roc: Decimal | None = None
    yes_amount: Decimal | None = None
    no_amount: Decimal | None = None
    if yes_walk is not None:
        yes_used, yes_cost = yes_walk
        yes_quote = _quote(yes.fee_model, yes_used, yes.leg.side)
        yes_amount = _amount(yes_quote)
        yes_result = _leg_result(yes.leg, yes_used, yes_cost, yes_quote, yes_clock)
        if yes_amount is None:
            reasons.add(f"fee_unknown_{yes.leg.venue.value}")
    if no_walk is not None:
        no_used, no_cost = no_walk
        no_quote = _quote(no.fee_model, no_used, no.leg.side)
        no_amount = _amount(no_quote)
        no_result = _leg_result(no.leg, no_used, no_cost, no_quote, no_clock)
        if no_amount is None:
            reasons.add(f"fee_unknown_{no.leg.venue.value}")
    if yes_walk is not None and no_walk is not None:
        gross_edge = qty - (yes_walk[1] + no_walk[1])
        if yes_amount is not None and no_amount is not None:
            raw_capital = yes_walk[1] + no_walk[1] + yes_amount + no_amount
            if raw_capital <= 0:
                reasons.add("invalid_capital")
            else:
                capital = raw_capital
                net_profit = qty - capital
                roc = net_profit / capital
                reasons.update(_threshold_reasons(net_profit, roc, limits))

    return _assessment(
        group_id=group_id,
        yes_leg=yes.leg,
        no_leg=no.leg,
        qty=qty,
        yes_result=yes_result,
        no_result=no_result,
        capital=capital,
        gross_edge=gross_edge,
        net_profit=net_profit,
        roc=roc,
        max_profitable_qty=_max_profitable_qty(
            yes_levels,
            no_levels,
            yes.fee_model,
            no.fee_model,
            yes.leg.side,
            no.leg.side,
            limits,
            reasons,
        ),
        skew_ms=_skew_ms(yes_clock, no_clock),
        books_atomic=books_atomic,
        exception_risk=exception_risk,
        reasons=reasons,
        model_version=model_version,
        now_wall=now_wall,
    )


def assess_group(
    group: EquivalenceGroup,
    books: Mapping[str, Book],
    fee_models: Mapping[str, FeeModel],
    qty: Decimal,
    *,
    now_monotonic: float,
    now_wall: datetime,
    books_atomic: bool,
    model_version: str,
    limits: Limits = Limits(),  # noqa: B008
    exception_risk: bool | None = None,
) -> list[OpportunityAssessment]:
    """Assess every representable ordered pair of ``group.members``.

    ``exception_risk`` defaults to the group's own flag. A missing book rejects
    that pair with ``missing_book_<venue>``. A missing fee model is an unknown
    fee, not a missing book.
    """
    if not isinstance(group, EquivalenceGroup):
        raise TypeError("group must be EquivalenceGroup")
    if not isinstance(books, Mapping) or not isinstance(fee_models, Mapping):
        raise TypeError("books and fee_models must be mappings")
    _require_qty(qty)
    _require_now_monotonic(now_monotonic)
    _require_now_wall(now_wall)
    _require_bool(books_atomic, "books_atomic")
    _require_model_version(model_version)
    if not isinstance(limits, Limits):
        raise TypeError("limits must be Limits")
    risk = group.exception_risk if exception_risk is None else exception_risk
    _require_bool(risk, "exception_risk")
    results: list[OpportunityAssessment] = []
    members = tuple(group.members)
    for index, happens in enumerate(members):
        for other_index, complement in enumerate(members):
            if index == other_index:
                continue
            legs = _legs_for_pair(happens, complement)
            if legs is None:
                continue
            yes_leg, no_leg = legs
            results.append(
                _assess_legs(
                    group.group_id,
                    yes_leg,
                    no_leg,
                    qty,
                    books,
                    fee_models,
                    now_monotonic=now_monotonic,
                    now_wall=now_wall,
                    books_atomic=books_atomic,
                    exception_risk=risk,
                    model_version=model_version,
                    limits=limits,
                )
            )
    return results


def _assess_legs(
    group_id: str,
    yes_leg: Leg,
    no_leg: Leg,
    qty: Decimal,
    books: Mapping[str, Book],
    fee_models: Mapping[str, FeeModel],
    *,
    now_monotonic: float,
    now_wall: datetime,
    books_atomic: bool,
    exception_risk: bool,
    model_version: str,
    limits: Limits,
) -> OpportunityAssessment:
    yes_key = _market_key(yes_leg.venue, yes_leg.market_id)
    no_key = _market_key(no_leg.venue, no_leg.market_id)
    yes_book = books.get(yes_key)
    no_book = books.get(no_key)
    missing: set[str] = set()
    if yes_book is None:
        missing.add(f"missing_book_{yes_leg.venue.value}")
    if no_book is None:
        missing.add(f"missing_book_{no_leg.venue.value}")
    if yes_book is None or no_book is None:
        checked_wall = _require_now_wall(now_wall)
        _require_now_monotonic(now_monotonic)
        _require_qty(qty)
        _require_bool(books_atomic, "books_atomic")
        _require_bool(exception_risk, "exception_risk")
        _require_model_version(model_version)
        return _assessment(
            group_id=group_id,
            yes_leg=yes_leg,
            no_leg=no_leg,
            qty=qty,
            yes_result=None,
            no_result=None,
            capital=None,
            gross_edge=None,
            net_profit=None,
            roc=None,
            max_profitable_qty=None,
            skew_ms=None,
            books_atomic=books_atomic,
            exception_risk=exception_risk,
            reasons=missing,
            model_version=model_version,
            now_wall=checked_wall,
        )
    _require_book(yes_key, yes_book, yes_leg)
    _require_book(no_key, no_book, no_leg)
    yes_fee = fee_models.get(yes_key, _MissingFeeModel(yes_key))
    no_fee = fee_models.get(no_key, _MissingFeeModel(no_key))
    return assess(
        group_id,
        LegInput(yes_leg, yes_book, yes_fee),
        LegInput(no_leg, no_book, no_fee),
        qty,
        now_monotonic=now_monotonic,
        now_wall=now_wall,
        books_atomic=books_atomic,
        exception_risk=exception_risk,
        model_version=model_version,
        limits=limits,
    )


def _legs_for_pair(happens: GroupMember, complement: GroupMember) -> tuple[Leg, Leg] | None:
    if happens.venue == complement.venue:
        return None
    buy_happens = _buy_side(happens, happens=True)
    buy_complement = _buy_side(complement, happens=False)
    if buy_happens is Side.YES and buy_complement is Side.NO:
        return (
            Leg(venue=happens.venue, market_id=happens.market_id, side=Side.YES),
            Leg(venue=complement.venue, market_id=complement.market_id, side=Side.NO),
        )
    if buy_happens is Side.NO and buy_complement is Side.YES:
        return (
            Leg(venue=complement.venue, market_id=complement.market_id, side=Side.YES),
            Leg(venue=happens.venue, market_id=happens.market_id, side=Side.NO),
        )
    return None


def _buy_side(member: GroupMember, *, happens: bool) -> Side:
    native_yes = member.native_side_for_outcome is Side.YES
    if happens:
        return Side.YES if native_yes else Side.NO
    return Side.NO if native_yes else Side.YES


def _market_key(venue: Venue, market_id: str) -> str:
    return f"{venue.value}:{market_id}"


def _note_book(book: Book, clock: _Clock, limits: Limits, reasons: set[str]) -> None:
    if not book.valid or not clock.usable:
        reasons.add(f"book_invalid_{book.venue.value}")
    if _is_stale(clock, limits):
        reasons.add(f"book_stale_{book.venue.value}")


def _is_stale(clock: _Clock, limits: Limits) -> bool:
    if clock.mono_age_s is None or clock.wall_age_s is None:
        return False
    return clock.mono_age_s > limits.max_book_age_s or clock.wall_age_s > limits.max_book_age_s


def _note_skew(
    yes_clock: _Clock,
    no_clock: _Clock,
    books_atomic: bool,
    limits: Limits,
    reasons: set[str],
) -> None:
    skew_s = _skew_s(yes_clock, no_clock)
    if skew_s is None:
        return
    indicative = skew_s > limits.max_skew_indicative_s
    qualified = books_atomic and skew_s > limits.max_skew_qualified_s
    if indicative or qualified:
        reasons.add("book_skew")


def _note_walk(
    levels: list[Level],
    qty: Decimal,
    venue: str,
    limits: Limits,
    reasons: set[str],
) -> tuple[list[Level], Decimal] | None:
    try:
        used, cost = walk(levels, qty)
    except InvalidLadder:
        reasons.add(f"book_invalid_{venue}")
        return None
    except InsufficientDepth:
        reasons.add(f"insufficient_depth_{venue}")
        _note_dust(levels, venue, limits, reasons)
        return None
    _note_dust(levels, venue, limits, reasons)
    return used, cost


def _note_dust(levels: Sequence[Level], venue: str, limits: Limits, reasons: set[str]) -> None:
    if levels and levels[0].size < limits.min_touch_qty:
        reasons.add(f"dust_{venue}")


def _asks(book: Book, side: Side) -> list[Level]:
    return book.yes_asks if side is Side.YES else book.no_asks


def _quote(model: FeeModel, levels: Sequence[Level], side: Side) -> FeeQuote:
    quote = model.taker_fee(levels, side)
    if not isinstance(quote, FeeQuote):
        raise TypeError("taker_fee must return FeeQuote")
    return quote


def _amount(quote: FeeQuote) -> Decimal | None:
    amount = quote.amount
    if amount is None:
        return None
    if isinstance(amount, bool) or not isinstance(amount, Decimal):
        raise TypeError("fee amount must be Decimal or None")
    if not amount.is_finite():
        raise ValueError("fee amount must be finite")
    return amount


def _threshold_reasons(net: Decimal, roc: Decimal, limits: Limits) -> tuple[str, ...]:
    found: list[str] = []
    if net <= 0:
        found.append("not_profitable")
    if net < limits.min_profit:
        found.append("below_min_profit")
    if roc < limits.min_roc:
        found.append("below_min_roc")
    return tuple(found)


def _leg_result(
    leg: Leg,
    levels: list[Level],
    cost: Decimal,
    fee: FeeQuote,
    clock: _Clock,
) -> LegResult:
    quantity = sum((level.size for level in levels), Decimal(0))
    return LegResult(
        leg=leg,
        levels_consumed=list(levels),
        quantity=quantity,
        cost=cost,
        avg_price=cost / quantity,
        fee=fee,
        book_age_ms=_ms(clock.age_s),
    )


def _max_profitable_qty(
    yes_levels: list[Level],
    no_levels: list[Level],
    yes_model: FeeModel,
    no_model: FeeModel,
    yes_side: Side,
    no_side: Side,
    limits: Limits,
    reasons: set[str],
) -> Decimal | None:
    # Freshness, skew, and dust fail every size. Invalid ladders have no depth.
    if _blocks_all_sizes(reasons):
        return None
    try:
        depth_yes = available_size(yes_levels)
        depth_no = available_size(no_levels)
    except InvalidLadder:
        return None
    upper = min(limits.scan_limit, depth_yes, depth_no).to_integral_value(rounding=ROUND_DOWN)
    best: Decimal | None = None
    qty = _ONE
    while qty <= upper:
        if _size_is_clear(
            qty,
            yes_levels,
            no_levels,
            yes_model,
            no_model,
            yes_side,
            no_side,
            limits,
        ):
            best = qty
        qty += _ONE
    return best


def _blocks_all_sizes(reasons: set[str]) -> bool:
    for reason in reasons:
        if reason == "book_skew" or reason.startswith(_BLOCKING_PREFIXES):
            return True
    return False


def _size_is_clear(
    qty: Decimal,
    yes_levels: list[Level],
    no_levels: list[Level],
    yes_model: FeeModel,
    no_model: FeeModel,
    yes_side: Side,
    no_side: Side,
    limits: Limits,
) -> bool:
    try:
        yes_used, yes_cost = walk(yes_levels, qty)
        no_used, no_cost = walk(no_levels, qty)
    except (InsufficientDepth, InvalidLadder):
        return False
    yes_amount = _amount(_quote(yes_model, yes_used, yes_side))
    no_amount = _amount(_quote(no_model, no_used, no_side))
    if yes_amount is None or no_amount is None:
        return False
    capital = yes_cost + no_cost + yes_amount + no_amount
    if capital <= 0:
        return False
    net = qty - capital
    roc = net / capital
    return not _threshold_reasons(net, roc, limits)


def _assessment(
    *,
    group_id: str,
    yes_leg: Leg,
    no_leg: Leg,
    qty: Decimal,
    yes_result: LegResult | None,
    no_result: LegResult | None,
    capital: Decimal | None,
    gross_edge: Decimal | None,
    net_profit: Decimal | None,
    roc: Decimal | None,
    max_profitable_qty: Decimal | None,
    skew_ms: int | None,
    books_atomic: bool,
    exception_risk: bool,
    reasons: set[str],
    model_version: str,
    now_wall: datetime,
) -> OpportunityAssessment:
    ordered = _ordered_reasons(reasons, yes_leg.venue.value, no_leg.venue.value)
    if ordered:
        result_class = AssessmentClass.REJECTED
    elif books_atomic:
        result_class = AssessmentClass.QUALIFIED
    else:
        result_class = AssessmentClass.INDICATIVE
    return OpportunityAssessment(
        group_id=group_id,
        yes_leg=yes_leg,
        no_leg=no_leg,
        requested_qty=qty,
        yes_result=yes_result,
        no_result=no_result,
        capital=capital,
        gross_edge=gross_edge,
        net_profit=net_profit,
        roc=roc,
        max_profitable_qty=max_profitable_qty,
        skew_ms=skew_ms,
        books_atomic=books_atomic,
        exception_risk=exception_risk,
        result_class=result_class,
        reasons=ordered,
        model_version=model_version,
        computed_utc=now_wall,
    )


def _ordered_reasons(reasons: set[str], yes_venue: str, no_venue: str) -> tuple[str, ...]:
    def rank(reason: str) -> tuple[int, int, str]:
        for index, prefix in enumerate(_REASON_PREFIXES):
            if reason == prefix or reason.startswith(prefix + "_"):
                if reason.endswith("_" + yes_venue):
                    venue_rank = 0
                elif reason.endswith("_" + no_venue):
                    venue_rank = 1
                else:
                    venue_rank = 2
                return (index, venue_rank, reason)
        return (len(_REASON_PREFIXES), 3, reason)

    return tuple(sorted(reasons, key=rank))


def _clock(book: Book, now_monotonic: float, now_wall: datetime) -> _Clock:
    mono_age = _mono_age(now_monotonic, book.received_monotonic)
    wall_age = _wall_age(now_wall, book.received_utc)
    mono = None if mono_age is None else Decimal(str(book.received_monotonic))
    return _Clock(mono=mono, mono_age_s=mono_age, wall_age_s=wall_age)


def _mono_age(now_monotonic: float, received: float) -> Decimal | None:
    if isinstance(received, bool) or not isinstance(received, (int, float)):
        return None
    if not math.isfinite(received) or received > now_monotonic:
        return None
    return Decimal(str(now_monotonic)) - Decimal(str(received))


def _wall_age(now_wall: datetime, received: datetime) -> Decimal | None:
    if not isinstance(received, datetime):
        return None
    if received.tzinfo is None or received.tzinfo.utcoffset(received) is None:
        return None
    try:
        delta = now_wall - received
    except (TypeError, OverflowError):
        return None
    age = (
        Decimal(delta.days) * Decimal(86400)
        + Decimal(delta.seconds)
        + (Decimal(delta.microseconds) / Decimal(1_000_000))
    )
    if age < 0:
        return None
    return age


def _skew_s(yes_clock: _Clock, no_clock: _Clock) -> Decimal | None:
    if (
        yes_clock.mono is None
        or no_clock.mono is None
        or not yes_clock.usable
        or not no_clock.usable
    ):
        return None
    return abs(yes_clock.mono - no_clock.mono)


def _skew_ms(yes_clock: _Clock, no_clock: _Clock) -> int | None:
    skew_s = _skew_s(yes_clock, no_clock)
    if skew_s is None:
        return None
    return _ms(skew_s)


def _ms(age_s: Decimal | None) -> int | None:
    if age_s is None:
        return None
    return int((age_s * _MS).to_integral_value(rounding=ROUND_HALF_EVEN))


def _require_leg_input(name: str, item: LegInput, side: Side) -> None:
    if not isinstance(item, LegInput):
        raise TypeError(f"{name} must be LegInput")
    if item.leg.side is not side:
        raise ValueError(f"{name} leg side must be {side.value}")
    if item.book.venue != item.leg.venue or item.book.market_id != item.leg.market_id:
        raise ValueError(f"{name} book does not match its leg")


def _require_book(key: str, book: Book, leg: Leg) -> None:
    if not isinstance(book, Book):
        raise TypeError(f"book for {key} must be Book")
    if book.venue != leg.venue or book.market_id != leg.market_id:
        raise ValueError(f"book for {key} does not match that market")


def _require_group_id(group_id: str) -> None:
    if not isinstance(group_id, str):
        raise TypeError("group_id must be str")


def _require_qty(qty: Decimal) -> Decimal:
    if isinstance(qty, bool) or not isinstance(qty, Decimal):
        raise TypeError("qty must be Decimal")
    if not qty.is_finite() or qty <= 0:
        raise ValueError("qty must be positive and finite")
    return qty


def _require_now_monotonic(now_monotonic: float) -> float:
    if isinstance(now_monotonic, bool) or not isinstance(now_monotonic, (int, float)):
        raise TypeError("now_monotonic must be a real number")
    if not math.isfinite(now_monotonic):
        raise ValueError("now_monotonic must be finite")
    return float(now_monotonic)


def _require_now_wall(now_wall: datetime) -> datetime:
    if not isinstance(now_wall, datetime):
        raise TypeError("now_wall must be a datetime")
    if now_wall.tzinfo is None or now_wall.tzinfo.utcoffset(now_wall) is None:
        raise ValueError("now_wall must be timezone-aware")
    return now_wall


def _require_bool(value: bool, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")


def _require_model_version(model_version: str) -> None:
    if not isinstance(model_version, str):
        raise TypeError("model_version must be str")


assert_no_order_client()
