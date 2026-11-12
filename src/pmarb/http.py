"""Observation-only HTTP client: GET-only, host allowlist, capped bodies, rate-limited.

Retries
-------
A request is retried on 429, on 500/502/503/504, and on transport failures that
can be transient (timeouts, network errors, a server that hung up). Any other
status is returned to the caller. Any other exception propagates at once.

Backoff is shared per ``(venue, purpose)``, the same key as the token buckets.
A retryable failure starts a cooldown of ``Backoff.delay(level)``, or the
server's ``Retry-After`` when that is longer. Every request on the key waits the
cooldown out before its next attempt. The level rises with each failed round and
resets on the first success. So the jittered 1 s → 30 s backoff that the plan
(§4.1) and the Kalshi developer agreement ask for holds across requests: the
next poll cycle does not hit a venue again while it is still cooling down.

A caller can bound a request in time with :func:`request_deadline`. The client
then never sleeps, waits for a token, or runs an attempt past the deadline. It
raises :class:`RetriesExhaustedError` with ``stop="deadline"`` instead. The
collector sets a deadline for each book poll, so a failing venue gives up inside
the freshness window rather than stalling the cycle.

Every :class:`RetriesExhaustedError` says what failed last (status, or
exception class and message), how many attempts were made, how long it took,
the endpoint, and the venue and purpose. The last transport exception is kept
as ``__cause__``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
import ssl
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx

from pmarb import __version__
from pmarb.config import Settings

ALLOWED_HOSTS = frozenset(
    {
        "gateway.polymarket.us",
        "api.polymarket.us",
        "external-api.kalshi.com",
        "api.elections.kalshi.com",
        "assets.kalshi.com",
        "api.gemini.com",
    }
)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Transient transport failures. LocalProtocolError, ProxyError and
# UnsupportedProtocol are local configuration faults: another attempt cannot fix
# them. ``ssl.SSLError`` is listed because a TLS alert on an open connection
# (seen live: SSLV3_ALERT_BAD_RECORD_MAC from Polymarket US) reaches us raw:
# httpcore maps TLS errors only during the handshake. A new connection fixes it.
RETRY_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    ssl.SSLError,
)
MAX_REDIRECTS = 3
MAX_RETRY_AFTER_S = 120.0
ERROR_MESSAGE_CHARS = 300
# With a deadline, httpx's per-phase timeouts are clamped to the time left and
# normally fire first (so the error names the phase: ConnectTimeout, ReadTimeout,
# PoolTimeout). This overall cut-off is the backstop for a body that keeps
# trickling in.
ATTEMPT_CUTOFF_GRACE_S = 0.05

BucketKey = tuple[str, str]  # (venue, purpose)
Stop = Literal["attempts", "deadline"]
WaitingFor = Literal["cooldown", "token", "attempt"]


class ObservationHttpError(RuntimeError):
    """Base class for refusals and failures of the observation client."""


class ForbiddenMethodError(ObservationHttpError):
    """Only GET is allowed."""


class HostNotAllowedError(ObservationHttpError):
    """The URL (or a redirect target) is not on the allowlist or not HTTPS."""


class ResponseTooLargeError(ObservationHttpError):
    """The response body exceeded the cap."""


class AttemptDeadlineError(ObservationHttpError):
    """An attempt was still running when the caller's deadline passed."""


class UnexpectedStatusError(ObservationHttpError):
    """A venue answered with a status the adapter does not accept (and the client does not retry)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        venue: str | None = None,
        route: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.venue = venue
        self.route = route

    @property
    def cause(self) -> str:
        return f"http_{self.status_code}"


class RetriesExhaustedError(ObservationHttpError):
    """A retryable failure persisted past the attempt limit or the caller's deadline.

    ``stop`` is why the client gave up: ``attempts`` (``max_retries`` used up) or
    ``deadline`` (the next wait or attempt would have ended past the deadline).
    ``waiting_for`` says which wait did not fit: ``cooldown`` (shared backoff),
    ``token`` (rate bucket), or ``attempt`` (the attempt itself was cut off).
    ``attempts`` can be 0 when a deadline left no room to send anything.
    ``history`` lists each attempt's outcome, oldest first. :attr:`cause` is a
    short stable label for counting.
    """

    def __init__(
        self,
        message: str | None = None,
        status_code: int | None = None,
        *,
        venue: str | None = None,
        purpose: str | None = None,
        route: str | None = None,
        path: str | None = None,
        attempts: int = 0,
        elapsed_s: float = 0.0,
        error_type: str | None = None,
        error_message: str | None = None,
        retry_after_s: float | None = None,
        budget_s: float | None = None,
        stop: Stop = "attempts",
        waiting_for: WaitingFor | None = None,
        wait_s: float | None = None,
        remaining_s: float | None = None,
        history: tuple[str, ...] = (),
        last_cause: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.venue = venue
        self.purpose = purpose
        self.route = route
        self.path = path
        self.attempts = attempts
        self.elapsed_s = elapsed_s
        self.error_type = error_type
        self.error_message = error_message
        self.retry_after_s = retry_after_s
        self.budget_s = budget_s
        self.stop = stop
        self.waiting_for = waiting_for
        self.wait_s = wait_s
        self.remaining_s = remaining_s
        self.history = history
        self.last_cause = last_cause
        super().__init__(message if message is not None else self._summary())

    @property
    def cause(self) -> str:
        """``http_<status>``, the exception class, or ``cooldown``/``token_wait`` when nothing was sent."""
        if self.status_code is not None:
            return f"http_{self.status_code}"
        if self.error_type:
            return self.error_type
        if self.waiting_for == "cooldown":
            return "cooldown"
        if self.waiting_for == "token":
            return "token_wait"
        if self.history:
            return self.history[-1]
        return "retries_exhausted"

    def _last(self) -> str:
        if self.status_code is not None:
            return f"HTTP {self.status_code}"
        if self.error_type:
            return f"{self.error_type}({self.error_message or ''})"
        if self.waiting_for == "cooldown":
            after = f" after {self.last_cause}" if self.last_cause else ""
            return f"venue cooling down{after}"
        if self.waiting_for == "token":
            return "rate-limit token not available in time"
        return "no attempt completed"

    def _summary(self) -> str:
        who = "/".join(part for part in (self.venue, self.purpose) if part) or "request"
        route = f" GET {self.route}" if self.route else ""
        plural = "" if self.attempts == 1 else "s"
        text = (
            f"{who}{route} failed: {self._last()} after {self.attempts} attempt{plural} "
            f"in {self.elapsed_s:.2f}s"
        )
        if self.stop == "deadline":
            budget = f" of {self.budget_s:.2f}s budget" if self.budget_s is not None else ""
            if self.waiting_for == "attempt":
                text += f" (stopped: attempt cut off at the deadline{budget})"
            else:
                wait = f"{self.wait_s:.2f}s" if self.wait_s is not None else "?"
                left = f"{max(self.remaining_s or 0.0, 0.0):.2f}s"
                text += f" (stopped: {self.waiting_for} wait {wait} > {left} left{budget})"
        if self.retry_after_s is not None:
            text += f" retry_after={self.retry_after_s:.1f}s"
        if self.history:
            text += f" [{','.join(self.history)}]"
        return text

    def as_detail(self) -> dict[str, Any]:
        """JSON-ready fields for a collector event."""
        return {
            "cause": self.cause,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 4),
            "http_venue": self.venue,
            "purpose": self.purpose,
            "endpoint": self.route,
            "path": self.path,
            "last_error_type": self.error_type,
            "last_error": self.error_message,
            "retry_after_s": self.retry_after_s,
            "budget_s": self.budget_s,
            "stop": self.stop,
            "waiting_for": self.waiting_for,
            "history": list(self.history),
            "last_cause": self.last_cause,
        }


def failure_cause(exc: BaseException) -> str:
    """Short stable label for counting failures: ``http_503``, ``ReadTimeout``, ``cooldown``…"""
    cause = getattr(exc, "cause", None)
    if isinstance(cause, str) and cause:
        return cause
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"http_{status}"
    return type(exc).__name__


def failure_detail(exc: BaseException) -> dict[str, Any]:
    """Event fields for any adapter failure; richer for :class:`RetriesExhaustedError`."""
    status = getattr(exc, "status_code", None)
    detail: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error": str(exc)[:500],
        "cause": failure_cause(exc),
        "status_code": status if isinstance(status, int) and not isinstance(status, bool) else None,
    }
    if isinstance(exc, RetriesExhaustedError):
        detail.update(exc.as_detail())
    elif isinstance(exc, UnexpectedStatusError):
        detail.update({"http_venue": exc.venue, "endpoint": exc.route})
    underlying = exc.__cause__
    if underlying is not None:
        detail["underlying"] = f"{type(underlying).__name__}: {str(underlying)[:200]}"
    return detail


@dataclass(frozen=True, slots=True)
class Deadline:
    """An absolute limit ``at`` on ``clock`` (monotonic seconds)."""

    at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(cls, seconds: float, clock: Callable[[], float] = time.monotonic) -> Deadline:
        return cls(clock() + seconds, clock)

    def remaining(self) -> float:
        return self.at - self.clock()


@dataclass(frozen=True, slots=True)
class _Bound:
    deadline: Deadline
    budget_s: float | None


_DEADLINE: ContextVar[_Bound | None] = ContextVar("pmarb_http_deadline", default=None)


@contextlib.contextmanager
def request_deadline(deadline: Deadline | None, *, budget_s: float | None = None) -> Iterator[None]:
    """Bound every request made in this context (and tasks started from it) by ``deadline``.

    Tasks copy the context when they are created, so an adapter's
    ``asyncio.gather`` inherits it. An inner call replaces an outer one.
    ``budget_s`` is only reported in errors.
    """
    token = _DEADLINE.set(None if deadline is None else _Bound(deadline, budget_s))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


class TokenWaitExceededError(ObservationHttpError):
    """The next token would arrive after the caller's deadline."""

    def __init__(self, wait_s: float, remaining_s: float) -> None:
        super().__init__(f"token wait {wait_s:.3f}s exceeds {remaining_s:.3f}s left")
        self.wait_s = wait_s
        self.remaining_s = remaining_s


class TokenBucket:
    """Async token bucket: ``rate_per_s`` refill, ``burst`` capacity. FIFO under contention."""

    def __init__(
        self,
        rate_per_s: float,
        burst: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("rate_per_s must be > 0 and burst >= 1")
        self.rate = rate_per_s
        self.burst = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self, deadline: Deadline | None = None) -> float:
        """Take one token and return the seconds spent waiting for it.

        With a ``deadline``, raise :class:`TokenWaitExceededError` instead of
        sleeping past it. No token is consumed in that case.
        """
        started = self._clock()
        async with self._lock:
            self._refill()
            while self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                if deadline is not None:
                    remaining = deadline.remaining()
                    if wait >= remaining:
                        raise TokenWaitExceededError(wait, remaining)
                await self._sleep(wait)
                self._refill()
            self._tokens -= 1
        return max(0.0, self._clock() - started)


class Backoff:
    """Jittered exponential backoff: 1 s doubling to a 30 s cap (half fixed, half random)."""

    def __init__(
        self,
        base_s: float = 1.0,
        cap_s: float = 30.0,
        *,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.base_s = base_s
        self.cap_s = cap_s
        self._rng = rng

    def delay(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (0-based)."""
        ceiling = min(self.cap_s, self.base_s * (2 ** max(attempt, 0)))
        return ceiling / 2 + self._rng() * ceiling / 2


@dataclass(slots=True)
class _Cooldown:
    """Shared backoff for one ``(venue, purpose)``."""

    until: float = -math.inf
    level: int = 0
    # Bumped on every escalation. An attempt remembers the round it was sent in,
    # so failures of attempts that were already in flight count as one round.
    round: int = 0
    last_cause: str | None = None


@dataclass(slots=True)
class _Metrics:
    requests: int = 0
    attempts: int = 0
    failures: int = 0
    http_429: int = 0
    http_5xx: int = 0
    transport_errors: int = 0
    deadline_stops: int = 0
    cooldown_waits: int = 0
    token_wait_s: float = 0.0
    token_wait_max_s: float = 0.0
    causes: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "attempts": self.attempts,
            "retries": max(0, self.attempts - self.requests),
            "failures": self.failures,
            "http_429": self.http_429,
            "http_5xx": self.http_5xx,
            "transport_errors": self.transport_errors,
            "deadline_stops": self.deadline_stops,
            "cooldown_waits": self.cooldown_waits,
            "token_wait_s": round(self.token_wait_s, 4),
            "token_wait_max_s": round(self.token_wait_max_s, 4),
            "attempt_failures_by_cause": dict(self.causes),
        }


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str

    def json(self) -> Any:
        return json.loads(self.content)


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise HostNotAllowedError(f"only https URLs are allowed: {parts.scheme or '(none)'}")
    if (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise HostNotAllowedError(f"host not on allowlist: {parts.hostname}")


def _retry_after(response: httpx.Response, now: datetime | None = None) -> float | None:
    """Seconds from ``Retry-After`` (delta-seconds or HTTP-date), capped; ``None`` if absent or bad."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    text = raw.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if not math.isfinite(seconds):
        return MAX_RETRY_AFTER_S if seconds > 0 else None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_S))


def connection_limits(buckets: Mapping[BucketKey, TokenBucket], attempt_s: float) -> httpx.Limits:
    """Pool limits that cover every request the buckets can have in flight.

    A bucket admits ``burst`` requests at once and then ``rate`` per second.
    Each request holds a connection for at most one attempt (``attempt_s``,
    connect plus read timeout), so ``burst + rate × attempt_s`` per bucket is the
    worst case. Keep-alive covers one second of traffic so steady polling reuses
    connections instead of opening new ones.
    """
    in_flight = sum(math.ceil(b.burst + b.rate * attempt_s) for b in buckets.values())
    keepalive = sum(math.ceil(b.burst + b.rate) for b in buckets.values())
    return httpx.Limits(
        max_connections=max(1, in_flight), max_keepalive_connections=max(1, keepalive)
    )


class ObservationHttpClient:
    """GET-only client for venue read APIs. Use as an async context manager."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        buckets: Mapping[BucketKey, TokenBucket] | None = None,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_retries: int = 4,
    ) -> None:
        self.settings = settings or Settings(_env_file=None)
        s = self.settings
        self.max_body_bytes = s.http_max_body_bytes
        self.max_retries = max_retries
        self.user_agent = f"pmarb/{__version__}"
        self._backoff = backoff or Backoff()
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._buckets: dict[BucketKey, TokenBucket] = (
            dict(buckets) if buckets is not None else self._default_buckets(s)
        )
        self._metrics: dict[BucketKey, _Metrics] = {}
        self._cooldowns: dict[BucketKey, _Cooldown] = {}
        self.timeout = httpx.Timeout(
            connect=s.http_connect_timeout_s,
            read=s.http_read_timeout_s,
            write=s.http_read_timeout_s,
            pool=s.http_connect_timeout_s,
        )
        self.limits = connection_limits(
            self._buckets, s.http_connect_timeout_s + s.http_read_timeout_s
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=self.timeout,
            limits=self.limits,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            follow_redirects=False,  # redirects are followed manually, host-checked
        )

    @staticmethod
    def _default_buckets(s: Settings) -> dict[BucketKey, TokenBucket]:
        def bucket(rps: float) -> TokenBucket:
            return TokenBucket(rps, max(1.0, rps))

        return {
            ("kalshi", "books"): bucket(s.kalshi_books_rps),
            ("kalshi", "discovery"): bucket(s.kalshi_discovery_rps),
            ("kalshi", "gamestate"): bucket(1.0),
            ("polyus", "books"): bucket(s.polyus_books_rps),
            ("polyus", "discovery"): bucket(s.polyus_discovery_rps),
        }

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        venue: str,
        purpose: str,
        params: Mapping[str, Any] | None = None,
        route: str | None = None,
    ) -> HttpResult:
        return await self.request(
            "GET", url, venue=venue, purpose=purpose, params=params, route=route
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        venue: str,
        purpose: str,
        params: Mapping[str, Any] | None = None,
        route: str | None = None,
    ) -> HttpResult:
        """GET ``url`` with retries. ``route`` labels the endpoint in errors (default: the path)."""
        if method.upper() != "GET":
            raise ForbiddenMethodError(f"observation client is GET-only, got {method}")
        _check_url(url)
        key = (venue, purpose)
        bucket = self._buckets.get(key)
        if bucket is None:
            raise ValueError(f"no rate bucket for ({venue}, {purpose})")

        metrics = self._metrics.setdefault(key, _Metrics())
        cooldown = self._cooldowns.setdefault(key, _Cooldown())
        metrics.requests += 1
        bound = _DEADLINE.get()
        deadline = None if bound is None else bound.deadline
        budget_s = None if bound is None else bound.budget_s
        started = self._clock()
        path = urlsplit(url).path
        history: list[str] = []
        attempts = 0
        last_status: int | None = None
        last_exc: BaseException | None = None
        retry_after: float | None = None

        def give_up(
            stop: Stop,
            waiting_for: WaitingFor | None = None,
            wait_s: float | None = None,
            remaining_s: float | None = None,
        ) -> RetriesExhaustedError:
            metrics.failures += 1
            if stop == "deadline":
                metrics.deadline_stops += 1
            return RetriesExhaustedError(
                status_code=last_status,
                venue=venue,
                purpose=purpose,
                route=route or path,
                path=path,
                attempts=attempts,
                elapsed_s=max(0.0, self._clock() - started),
                error_type=type(last_exc).__name__ if last_exc is not None else None,
                error_message=(
                    str(last_exc)[:ERROR_MESSAGE_CHARS] if last_exc is not None else None
                ),
                retry_after_s=retry_after,
                budget_s=budget_s,
                stop=stop,
                waiting_for=waiting_for,
                wait_s=wait_s,
                remaining_s=remaining_s,
                history=tuple(history),
                last_cause=cooldown.last_cause,
            )

        pending = cooldown.until - self._clock()
        while True:
            if pending > 0:
                metrics.cooldown_waits += 1
                if deadline is not None:
                    remaining = deadline.remaining()
                    if pending >= remaining:
                        raise give_up("deadline", "cooldown", pending, remaining) from last_exc
                await self._sleep(pending)
            seen_until = cooldown.until
            try:
                waited = await bucket.acquire(deadline)
            except TokenWaitExceededError as exc:
                raise give_up("deadline", "token", exc.wait_s, exc.remaining_s) from last_exc
            metrics.token_wait_s += waited
            metrics.token_wait_max_s = max(metrics.token_wait_max_s, waited)
            if cooldown.until > seen_until:
                # Another request on this key failed while this one queued for a token.
                pending = cooldown.until - self._clock()
                if pending > 0:
                    continue
            if deadline is not None and deadline.remaining() <= 0:
                raise give_up("deadline", "attempt", 0.0, deadline.remaining()) from last_exc
            attempts += 1
            metrics.attempts += 1
            issued = cooldown.round
            try:
                response, body = await self._attempt(url, params, deadline)
            except AttemptDeadlineError as exc:
                last_exc, last_status, retry_after = exc, None, None
                history.append(type(exc).__name__)
                metrics.causes[type(exc).__name__] = metrics.causes.get(type(exc).__name__, 0) + 1
                raise give_up("deadline", "attempt") from exc
            except RETRY_TRANSPORT_ERRORS as exc:
                last_exc, last_status, retry_after = exc, None, None
                outcome = type(exc).__name__
                metrics.transport_errors += 1
            else:
                if response.status_code not in RETRY_STATUSES:
                    if issued == cooldown.round:
                        cooldown.level = 0
                        cooldown.until = -math.inf
                    return HttpResult(
                        response.status_code, dict(response.headers), body, str(response.url)
                    )
                last_exc, last_status = None, response.status_code
                retry_after = _retry_after(response, self._wall())
                outcome = f"http_{response.status_code}"
                if response.status_code == 429:
                    metrics.http_429 += 1
                else:
                    metrics.http_5xx += 1
            history.append(outcome)
            metrics.causes[outcome] = metrics.causes.get(outcome, 0) + 1
            pending = self._escalate(cooldown, outcome, issued, retry_after)
            if attempts > self.max_retries:
                raise give_up("attempts") from last_exc

    def _escalate(
        self, cooldown: _Cooldown, outcome: str, issued: int, retry_after: float | None
    ) -> float:
        """Record a failed attempt; return how long this request should wait before retrying.

        Only a failure of an attempt sent after the last escalation raises the
        level (``issued`` is the round it was sent in), so a burst of
        concurrent failures counts as one round.
        """
        now = self._clock()
        cooldown.last_cause = outcome
        if issued == cooldown.round:
            delay = self._backoff.delay(cooldown.level)
            cooldown.level += 1
            cooldown.round += 1
        else:
            delay = max(0.0, cooldown.until - now)
        if retry_after is not None:
            delay = max(delay, retry_after)
        cooldown.until = max(cooldown.until, now + delay)
        return delay

    def request_metrics(self, venue: str, purpose: str) -> dict[str, int]:
        """Return a copy of logical request and HTTP attempt counts."""
        metrics = self._metrics.get((venue, purpose))
        if metrics is None:
            return {"requests": 0, "attempts": 0}
        return {"requests": metrics.requests, "attempts": metrics.attempts}

    def metrics(self) -> dict[str, dict[str, Any]]:
        """Every bucket's counters, keyed ``venue/purpose``, including failures by cause."""
        out: dict[str, dict[str, Any]] = {}
        for (venue, purpose), metrics in sorted(self._metrics.items()):
            row = metrics.snapshot()
            cooldown = self._cooldowns.get((venue, purpose))
            row["backoff_level"] = cooldown.level if cooldown is not None else 0
            out[f"{venue}/{purpose}"] = row
        return out

    async def _attempt(
        self, url: str, params: Mapping[str, Any] | None, deadline: Deadline | None
    ) -> tuple[httpx.Response, bytes]:
        if deadline is None:
            return await self._fetch(url, params, httpx.USE_CLIENT_DEFAULT)
        remaining = max(deadline.remaining(), 0.001)
        timeout = httpx.Timeout(
            connect=min(self.timeout.connect or remaining, remaining),
            read=min(self.timeout.read or remaining, remaining),
            write=min(self.timeout.write or remaining, remaining),
            pool=min(self.timeout.pool or remaining, remaining),
        )
        try:
            async with asyncio.timeout(remaining + ATTEMPT_CUTOFF_GRACE_S):
                return await self._fetch(url, params, timeout)
        except (
            TimeoutError
        ) as exc:  # asyncio.timeout's own expiry; httpx timeouts are not TimeoutError
            raise AttemptDeadlineError(
                f"no complete response within the {remaining:.3f}s left before the deadline"
            ) from exc

    async def _fetch(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        timeout: httpx.Timeout | Any,
    ) -> tuple[httpx.Response, bytes]:
        current = url
        query = params
        for _ in range(MAX_REDIRECTS + 1):
            _check_url(current)
            request = self._client.build_request("GET", current, params=query, timeout=timeout)
            response = await self._client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise HostNotAllowedError("redirect without Location header")
                    current = str(response.url.join(location))
                    query = None
                    continue
                return response, await self._read_capped(response)
            finally:
                await response.aclose()
        raise HostNotAllowedError(f"too many redirects (> {MAX_REDIRECTS})")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_body_bytes:
            raise ResponseTooLargeError(
                f"Content-Length {declared} exceeds cap {self.max_body_bytes}"
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self.max_body_bytes:
                raise ResponseTooLargeError(f"response body exceeds cap {self.max_body_bytes}")
            chunks.append(chunk)
        return b"".join(chunks)
