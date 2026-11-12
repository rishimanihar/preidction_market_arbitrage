"""Observation-only HTTP client: GET-only, host allowlist, capped bodies, rate-limited."""

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Self
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
MAX_REDIRECTS = 3
MAX_RETRY_AFTER_S = 120.0

BucketKey = tuple[str, str]  # (venue, purpose)


class ObservationHttpError(RuntimeError):
    """Base class for refusals and failures of the observation client."""


class ForbiddenMethodError(ObservationHttpError):
    """Only GET is allowed."""


class HostNotAllowedError(ObservationHttpError):
    """The URL (or a redirect target) is not on the allowlist or not HTTPS."""


class ResponseTooLargeError(ObservationHttpError):
    """The response body exceeded the cap."""


class RetriesExhaustedError(ObservationHttpError):
    """Retryable failures persisted past the retry budget."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TokenBucket:
    """Async token bucket: ``rate_per_s`` refill, ``burst`` capacity."""

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

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            while self._tokens < 1:
                await self._sleep((1 - self._tokens) / self.rate)
                self._refill()
            self._tokens -= 1


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


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER_S))
    except ValueError:
        return None  # HTTP-date form: fall back to the backoff delay


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
        max_retries: int = 4,
    ) -> None:
        self.settings = settings or Settings(_env_file=None)
        s = self.settings
        self.max_body_bytes = s.http_max_body_bytes
        self.max_retries = max_retries
        self.user_agent = f"pmarb/{__version__}"
        self._backoff = backoff or Backoff()
        self._sleep = sleep
        self._buckets: dict[BucketKey, TokenBucket] = (
            dict(buckets) if buckets is not None else self._default_buckets(s)
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=s.http_connect_timeout_s,
                read=s.http_read_timeout_s,
                write=s.http_read_timeout_s,
                pool=s.http_connect_timeout_s,
            ),
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
    ) -> HttpResult:
        return await self.request("GET", url, venue=venue, purpose=purpose, params=params)

    async def request(
        self,
        method: str,
        url: str,
        *,
        venue: str,
        purpose: str,
        params: Mapping[str, Any] | None = None,
    ) -> HttpResult:
        if method.upper() != "GET":
            raise ForbiddenMethodError(f"observation client is GET-only, got {method}")
        _check_url(url)
        bucket = self._buckets.get((venue, purpose))
        if bucket is None:
            raise ValueError(f"no rate bucket for ({venue}, {purpose})")

        last_status: int | None = None
        for attempt in range(self.max_retries + 1):
            await bucket.acquire()
            try:
                response, body = await self._fetch(url, params)
            except httpx.TransportError as exc:
                last_status = None
                reason = f"transport error: {type(exc).__name__}"
                retry_after = None
            else:
                if response.status_code not in RETRY_STATUSES:
                    return HttpResult(
                        response.status_code, dict(response.headers), body, str(response.url)
                    )
                last_status = response.status_code
                reason = f"HTTP {response.status_code}"
                retry_after = _retry_after(response)
            if attempt == self.max_retries:
                break
            delay = self._backoff.delay(attempt)
            if retry_after is not None:
                delay = max(delay, retry_after)
            await self._sleep(delay)
        raise RetriesExhaustedError(
            f"{reason} after {self.max_retries + 1} attempts", status_code=last_status
        )

    async def _fetch(
        self, url: str, params: Mapping[str, Any] | None
    ) -> tuple[httpx.Response, bytes]:
        current = url
        query = params
        for _ in range(MAX_REDIRECTS + 1):
            _check_url(current)
            request = self._client.build_request("GET", current, params=query)
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
