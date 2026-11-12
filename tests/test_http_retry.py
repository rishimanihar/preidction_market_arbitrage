"""Retry branches, Retry-After, deadlines, shared backoff, and connection-pool limits.

No network: ``httpx.MockTransport``, or a real httpcore pool over an in-memory
network backend for the pool tests. Time is fake unless a test says otherwise.
"""

from __future__ import annotations

import asyncio
import json
import math
import ssl
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from pmarb.config import Settings
from pmarb.http import (
    MAX_RETRY_AFTER_S,
    AttemptDeadlineError,
    Backoff,
    Deadline,
    ObservationHttpClient,
    RetriesExhaustedError,
    TokenBucket,
    UnexpectedStatusError,
    connection_limits,
    failure_cause,
    failure_detail,
    request_deadline,
)
from pmarb.protocols import PartialBooksError
from pmarb.venues.polymarket_us.rest import PolymarketUSAdapter

KALSHI_BOOKS = "https://external-api.kalshi.com/trade-api/v2/markets/orderbooks"
POLY_BOOK = "https://gateway.polymarket.us/v1/markets/slug-a/book"
WALL = datetime(2026, 11, 12, 20, tzinfo=UTC)
BOOK = json.loads((Path(__file__).parent / "fixtures" / "polymarket_us" / "book.json").read_text())


class FakeTime:
    """Monotonic clock that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def make_client(
    handler: Any,
    clock: FakeTime,
    *,
    max_retries: int = 4,
    rate: float = 1000.0,
    burst: float = 100.0,
) -> ObservationHttpClient:
    buckets = {
        key: TokenBucket(rate, burst, clock=clock, sleep=clock.sleep)
        for key in (("kalshi", "books"), ("polyus", "books"), ("kalshi", "discovery"))
    }
    return ObservationHttpClient(
        Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        buckets=buckets,
        backoff=Backoff(rng=lambda: 0.0),
        sleep=clock.sleep,
        clock=clock,
        wall=lambda: WALL,
        max_retries=max_retries,
    )


def deadline_in(clock: FakeTime, budget: float):
    return request_deadline(Deadline(clock() + budget, clock), budget_s=budget)


def failing_then_ok(first: Any) -> tuple[Any, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            if isinstance(first, int):
                return httpx.Response(first)
            raise first(request)
        return httpx.Response(200, json={"ok": True})

    return handler, seen


# --- which failures are retried --------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retryable_status_is_retried(status: int) -> None:
    clock = FakeTime()
    handler, seen = failing_then_ok(status)
    async with make_client(handler, clock) as client:
        result = await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
    assert result.status_code == 200
    assert len(seen) == 2
    assert clock.sleeps == [0.5]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501])
async def test_non_retryable_status_is_returned_after_one_attempt(status: int) -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    async with make_client(handler, clock) as client:
        result = await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        assert client.metrics()["kalshi/books"]["failures"] == 0
    assert result.status_code == status
    assert len(seen) == 1
    assert clock.sleeps == []


def _transport_error(kind: type[Exception]) -> Any:
    def raise_it(request: httpx.Request) -> Exception:
        if kind is ssl.SSLError:
            return ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac")
        return kind("simulated", request=request)

    return raise_it


@pytest.mark.parametrize(
    "kind",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
        ssl.SSLError,
    ],
)
async def test_transient_transport_error_is_retried(kind: type[Exception]) -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []
    make_error = _transport_error(kind)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            raise make_error(request)
        return httpx.Response(200, json=[])

    async with make_client(handler, clock) as client:
        result = await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        metrics = client.metrics()["kalshi/books"]
    assert result.status_code == 200 and len(seen) == 2
    assert metrics["transport_errors"] == 1
    assert metrics["attempt_failures_by_cause"] == {kind.__name__: 1}


@pytest.mark.parametrize(
    "kind", [httpx.LocalProtocolError, httpx.UnsupportedProtocol, httpx.ProxyError]
)
async def test_configuration_transport_error_is_not_retried(kind: type[Exception]) -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise kind("misconfigured", request=request)

    async with make_client(handler, clock) as client:
        with pytest.raises(kind):
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
    assert len(seen) == 1
    assert clock.sleeps == []


# --- what an exhausted error says -------------------------------------------


async def test_exhausted_transport_error_keeps_cause_and_every_detail() -> None:
    clock = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    async with make_client(handler, clock, max_retries=2) as client:
        with pytest.raises(RetriesExhaustedError) as info:
            await client.get(
                KALSHI_BOOKS,
                venue="kalshi",
                purpose="books",
                params={"tickers": ["SECRET-LOOKING-VALUE"]},
                route="/markets/orderbooks",
            )
    err = info.value
    assert isinstance(err.__cause__, httpx.ConnectTimeout)
    assert (err.venue, err.purpose) == ("kalshi", "books")
    assert err.route == "/markets/orderbooks"
    assert err.path == "/trade-api/v2/markets/orderbooks"
    assert err.attempts == 3
    assert err.elapsed_s == pytest.approx(1.5)
    assert err.status_code is None
    assert (err.error_type, err.error_message) == ("ConnectTimeout", "connect timed out")
    assert err.cause == "ConnectTimeout"
    assert err.stop == "attempts"
    assert err.history == ("ConnectTimeout",) * 3
    text = str(err)
    for piece in (
        "kalshi/books",
        "GET /markets/orderbooks",
        "ConnectTimeout(connect timed out)",
        "after 3 attempts",
        "in 1.50s",
    ):
        assert piece in text
    assert "SECRET-LOOKING-VALUE" not in text  # no query values
    detail = failure_detail(err)
    assert detail["cause"] == "ConnectTimeout"
    assert detail["attempts"] == 3 and detail["elapsed_s"] == pytest.approx(1.5)
    assert detail["endpoint"] == "/markets/orderbooks"
    assert (detail["http_venue"], detail["purpose"]) == ("kalshi", "books")
    assert detail["underlying"] == "ConnectTimeout: connect timed out"


async def test_exhausted_status_error_names_the_status() -> None:
    clock = FakeTime()
    async with make_client(lambda r: httpx.Response(503), clock, max_retries=3) as client:
        with pytest.raises(RetriesExhaustedError) as info:
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        metrics = client.metrics()["kalshi/books"]
    err = info.value
    assert err.status_code == 503 and err.cause == "http_503"
    assert err.attempts == 4 and err.elapsed_s == pytest.approx(3.5)
    assert clock.sleeps == [0.5, 1.0, 2.0]
    assert "HTTP 503 after 4 attempts in 3.50s" in str(err)
    assert metrics["http_5xx"] == 4 and metrics["failures"] == 1 and metrics["retries"] == 3


def test_legacy_constructor_and_failure_labels() -> None:
    legacy = RetriesExhaustedError("HTTP 429 after 5 attempts", status_code=429)
    assert str(legacy) == "HTTP 429 after 5 attempts" and legacy.cause == "http_429"
    unexpected = UnexpectedStatusError("nope", status_code=404, venue="polyus", route="/x")
    assert failure_cause(unexpected) == "http_404"
    assert failure_detail(unexpected)["endpoint"] == "/x"
    assert failure_cause(ValueError("bad payload")) == "ValueError"


# --- Retry-After -------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        (format_datetime(WALL + timedelta(seconds=10), usegmt=True), 10.0),
        ("100000", MAX_RETRY_AFTER_S),
        ("soon", 0.5),  # unparseable: the backoff delay
        ("-5", 0.5),  # negative: the backoff delay still applies
        ("nan", 0.5),
    ],
)
async def test_retry_after_forms(header: str, expected: float) -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": header})
        return httpx.Response(200)

    async with make_client(handler, clock) as client:
        await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
    assert clock.sleeps == [pytest.approx(expected)]


async def test_retry_after_past_the_deadline_gives_up_without_sleeping() -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})

    async with make_client(handler, clock) as client:
        with deadline_in(clock, 1.5), pytest.raises(RetriesExhaustedError) as info:
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        # The next request honours the same Retry-After instead of hitting the venue.
        with deadline_in(clock, 1.5), pytest.raises(RetriesExhaustedError) as again:
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
    err = info.value
    assert len(seen) == 1
    assert clock.sleeps == []
    assert (err.stop, err.waiting_for, err.attempts) == ("deadline", "cooldown", 1)
    assert err.retry_after_s == 30.0 and err.cause == "http_429"
    assert "retry_after=30.0s" in str(err)
    assert again.value.attempts == 0 and again.value.cause == "cooldown"
    assert again.value.last_cause == "http_429"


# --- shared backoff ----------------------------------------------------------


async def test_backoff_is_shared_per_key_and_reset_by_success() -> None:
    clock = FakeTime()
    status = {"code": 503}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status["code"])

    async with make_client(handler, clock, max_retries=1) as client:
        with pytest.raises(RetriesExhaustedError):
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        assert clock.sleeps == [0.5]  # attempt 1 failed; attempt 2 failed and set a 1.0 s cooldown
        status["code"] = 200
        await client.get(POLY_BOOK, venue="polyus", purpose="books")  # other key: no wait
        assert clock.sleeps == [0.5]
        await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        assert clock.sleeps == [0.5, 1.0]  # waited out the shared cooldown first
        await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        assert clock.sleeps == [0.5, 1.0]  # success reset it
        assert client.metrics()["kalshi/books"]["backoff_level"] == 0


async def test_concurrent_failures_escalate_the_backoff_once() -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []
    all_in_flight = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) <= 4:
            if len(seen) == 4:
                all_in_flight.set()
            await all_in_flight.wait()  # the first four are on the wire together
            return httpx.Response(503)
        return httpx.Response(200)

    async with make_client(handler, clock) as client:
        await asyncio.gather(
            *(client.get(KALSHI_BOOKS, venue="kalshi", purpose="books") for _ in range(4))
        )
    assert len(seen) == 8
    # One round of four failures: level 0 delay only, never 1, 2, 4 s.
    assert max(clock.sleeps) == pytest.approx(0.5)
    assert clock.now <= 0.5 + 1e-9


# --- deadlines ---------------------------------------------------------------


async def test_deadline_stops_before_a_backoff_that_would_pass_it() -> None:
    clock = FakeTime()
    async with make_client(lambda r: httpx.Response(503), clock) as client:
        with deadline_in(clock, 1.5), pytest.raises(RetriesExhaustedError) as info:
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        assert client.metrics()["kalshi/books"]["deadline_stops"] == 1
    err = info.value
    # Attempt at 0 s, wait 0.5, attempt at 0.5 s; the next wait (1.0 s) would end at the deadline.
    assert err.attempts == 2 and clock.now == pytest.approx(0.5)
    assert (err.stop, err.waiting_for, err.budget_s) == ("deadline", "cooldown", 1.5)
    assert err.history == ("http_503", "http_503")
    assert "cooldown wait 1.00s > 1.00s left of 1.50s budget" in str(err)


async def test_token_wait_past_the_deadline_sends_nothing() -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with make_client(handler, clock, rate=1.0, burst=1.0) as client:
        await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        with deadline_in(clock, 0.5), pytest.raises(RetriesExhaustedError) as info:
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
    assert len(seen) == 1
    err = info.value
    assert (err.attempts, err.cause, err.waiting_for) == (0, "token_wait", "token")
    assert clock.sleeps == []


async def test_attempt_still_running_at_the_deadline_is_cut_off() -> None:
    """Real time, kept short: the handler hangs and the deadline is 50 ms away."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    client = ObservationHttpClient(Settings(_env_file=None), transport=httpx.MockTransport(handler))
    started = time.monotonic()
    async with client:
        with (
            request_deadline(Deadline.after(0.05), budget_s=0.05),
            pytest.raises(RetriesExhaustedError) as info,
        ):
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
    assert time.monotonic() - started < 1.0
    err = info.value
    assert isinstance(err.__cause__, AttemptDeadlineError)
    assert (err.stop, err.waiting_for, err.cause) == ("deadline", "attempt", "AttemptDeadlineError")
    assert err.attempts == 1


async def test_attempt_timeouts_are_clamped_to_the_time_left() -> None:
    clock = FakeTime()
    seen: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(200)

    async with make_client(handler, clock) as client:
        await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
        with deadline_in(clock, 1.2):
            await client.get(KALSHI_BOOKS, venue="kalshi", purpose="books")
    assert seen[0] == {"connect": 3.0, "read": 5.0, "write": 5.0, "pool": 3.0}
    assert seen[1] == pytest.approx({"connect": 1.2, "read": 1.2, "write": 1.2, "pool": 1.2})


async def test_deadline_reaches_tasks_started_inside_it() -> None:
    clock = FakeTime()
    async with make_client(lambda r: httpx.Response(503), clock) as client:
        with deadline_in(clock, 0.75):
            tasks = [
                asyncio.ensure_future(client.get(POLY_BOOK, venue="polyus", purpose="books"))
                for _ in range(3)
            ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, RetriesExhaustedError) for r in results)
    assert all(r.stop == "deadline" for r in results)
    assert clock.now <= 0.75


# --- adapter partial results --------------------------------------------------


async def test_polyus_failed_market_keeps_the_others_and_leaves_no_request_running() -> None:
    clock = FakeTime()
    active = 0
    finished: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active
        slug = request.url.path.split("/")[-2]
        active += 1
        await asyncio.sleep(0)
        active -= 1
        finished.append(slug)
        if slug == "slug-bad":
            return httpx.Response(503)
        body = json.loads(json.dumps(BOOK))
        body["marketData"]["marketSlug"] = slug
        return httpx.Response(200, json=body)

    async with make_client(handler, clock, max_retries=1) as client:
        adapter = PolymarketUSAdapter(client)
        with pytest.raises(PartialBooksError) as info:
            await adapter.get_books(["slug-a", "slug-bad", "slug-c"])
        assert active == 0  # nothing left retrying in the background
    err = info.value
    assert [book.market_id for book in err.books] == ["slug-a", "slug-c"]
    assert list(err.failures) == ["slug-bad"]
    (only,) = err.errors
    assert isinstance(only, RetriesExhaustedError)
    assert only.route == "/v1/markets/{slug}/book" and only.path == "/v1/markets/slug-bad/book"
    assert finished.count("slug-bad") == 2


# --- connection pool ---------------------------------------------------------


class _Stream(httpcore.AsyncNetworkStream):
    """One in-memory HTTP/1.1 connection. Answers each request when ``gate`` opens."""

    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.pending: list[bytes] = []
        self.buffer = b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.buffer += buffer
        if b"\r\n\r\n" in self.buffer:
            head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
            path = head.split(b" ", 2)[1].decode()
            self.pending.append(self.backend.respond(path))

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        await self.backend.gate.wait()
        if not self.pending:
            await asyncio.Event().wait()
        return self.pending.pop(0)

    async def aclose(self) -> None:
        self.backend.open -= 1

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class _Backend(httpcore.AsyncNetworkBackend):
    """Opens the gate once ``needed`` connections are open at the same time."""

    def __init__(self, needed: int) -> None:
        self.needed = needed
        self.open = 0
        self.peak = 0
        self.gate = asyncio.Event()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.open += 1
        self.peak = max(self.peak, self.open)
        if self.open >= self.needed:
            self.gate.set()
        return _Stream(self)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def respond(self, path: str) -> bytes:
        body = json.loads(json.dumps(BOOK))
        body["marketData"]["marketSlug"] = path.split("/")[-2]
        payload = json.dumps(body).encode()
        return (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )


def _pooled_client(limits: httpx.Limits | None, backend: _Backend) -> ObservationHttpClient:
    """A client over a real httpcore pool; only the socket layer is in memory."""
    if limits is None:
        client = ObservationHttpClient(Settings(_env_file=None))
        transport = client._client._transport
    else:
        transport = httpx.AsyncHTTPTransport(limits=limits)
        client = ObservationHttpClient(Settings(_env_file=None), transport=transport)
    transport._pool._network_backend = backend  # type: ignore[attr-defined]
    return client


async def test_pool_exhaustion_is_a_pooltimeout_and_is_named_in_the_error() -> None:
    """Eight concurrent book calls through a 2-connection pool: the rest time out waiting."""
    backend = _Backend(needed=8)  # the venue answers only once all eight are in flight
    async with _pooled_client(httpx.Limits(max_connections=2), backend) as client:
        adapter = PolymarketUSAdapter(client)
        with (
            request_deadline(Deadline.after(0.3), budget_s=0.3),
            pytest.raises(PartialBooksError) as info,
        ):
            await adapter.get_books([f"slug-{n}" for n in range(8)])
    causes = {failure_cause(err) for err in info.value.errors}
    assert "PoolTimeout" in causes
    assert backend.peak == 2


async def test_default_limits_cover_concurrent_book_calls() -> None:
    backend = _Backend(needed=8)
    async with _pooled_client(None, backend) as client:
        assert client.limits.max_connections is not None
        assert client.limits.max_connections >= 8
        adapter = PolymarketUSAdapter(client)
        with request_deadline(Deadline.after(2.0), budget_s=2.0):
            books = await adapter.get_books([f"slug-{n}" for n in range(8)])
    assert [book.market_id for book in books] == [f"slug-{n}" for n in range(8)]
    assert backend.peak == 8


def test_connection_limits_cover_every_bucket_in_flight() -> None:
    client = ObservationHttpClient(Settings(_env_file=None))
    attempt_s = 3.0 + 5.0
    expected = sum(math.ceil(b.burst + b.rate * attempt_s) for b in client._buckets.values())
    assert client.limits.max_connections == expected == 126
    assert client.limits.max_keepalive_connections == 28
    assert connection_limits({}, 8.0).max_connections == 1
