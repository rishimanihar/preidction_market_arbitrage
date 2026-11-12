import httpx
import pytest

from pmarb import __version__
from pmarb.config import Settings
from pmarb.http import (
    ALLOWED_HOSTS,
    Backoff,
    ForbiddenMethodError,
    HostNotAllowedError,
    ObservationHttpClient,
    ResponseTooLargeError,
    RetriesExhaustedError,
    TokenBucket,
)

KALSHI = "https://external-api.kalshi.com/trade-api/v2/markets"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def make_client(handler, *, sleeps: list[float] | None = None, **kw) -> ObservationHttpClient:
    rec = sleeps if sleeps is not None else []

    async def sleep(s: float) -> None:
        rec.append(s)

    return ObservationHttpClient(
        Settings(_env_file=None),
        transport=httpx.MockTransport(handler),
        backoff=Backoff(rng=lambda: 0.0),
        sleep=sleep,
        **kw,
    )


async def test_token_bucket_burst_then_rate() -> None:
    clock = FakeClock()
    bucket = TokenBucket(2.0, 2, clock=clock, sleep=clock.sleep)
    await bucket.acquire()
    await bucket.acquire()
    assert clock.sleeps == []
    await bucket.acquire()
    assert clock.sleeps == [pytest.approx(0.5)]
    clock.now += 10  # refill is capped at burst
    await bucket.acquire()
    await bucket.acquire()
    assert len(clock.sleeps) == 1


def test_bucket_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, 1)


def test_backoff_exponential_jitter_and_cap() -> None:
    lo = Backoff(rng=lambda: 0.0)
    hi = Backoff(rng=lambda: 1.0)
    assert [hi.delay(n) for n in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    assert lo.delay(0) == 0.5 and lo.delay(10) == 15.0


async def test_429_then_200_honors_retry_after_and_user_agent() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True}, headers={"Cache-Control": "no-cache"})

    sleeps: list[float] = []
    async with make_client(handler, sleeps=sleeps) as client:
        result = await client.get(KALSHI, venue="kalshi", purpose="discovery", params={"limit": 1})
    assert result.status_code == 200 and result.json() == {"ok": True}
    assert result.headers["cache-control"] == "no-cache"
    assert sleeps == [7.0]
    assert calls[0].headers["user-agent"] == f"pmarb/{__version__}"
    assert calls[0].url.params["limit"] == "1"
    assert client.request_metrics("kalshi", "discovery") == {"requests": 1, "attempts": 2}


async def test_503_backoff_then_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    sleeps: list[float] = []
    async with make_client(handler, sleeps=sleeps, max_retries=3) as client:
        with pytest.raises(RetriesExhaustedError) as info:
            await client.get(KALSHI, venue="kalshi", purpose="books")
    assert info.value.status_code == 503
    assert sleeps == [0.5, 1.0, 2.0]


async def test_transport_error_retried() -> None:
    n = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        if n == 1:
            raise httpx.ConnectTimeout("t", request=request)
        return httpx.Response(200, json=[])

    async with make_client(handler) as client:
        assert (await client.get(KALSHI, venue="kalshi", purpose="books")).json() == []


async def test_non_retry_status_returned() -> None:
    async with make_client(lambda r: httpx.Response(404)) as client:
        assert (await client.get(KALSHI, venue="kalshi", purpose="books")).status_code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def test_non_get_refused(method: str) -> None:
    hit = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hit
        hit = True
        return httpx.Response(200)

    async with make_client(handler) as client:
        with pytest.raises(ForbiddenMethodError):
            await client.request(method, KALSHI, venue="kalshi", purpose="books")
    assert not hit


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/x",
        "http://external-api.kalshi.com/x",
        "https://kalshi.com.evil.io/",
    ],
)
async def test_off_allowlist_refused(url: str) -> None:
    async with make_client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(HostNotAllowedError):
            await client.get(url, venue="kalshi", purpose="books")


async def test_allowlisted_redirect_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "external-api.kalshi.com":
            return httpx.Response(302, headers={"Location": "https://api.elections.kalshi.com/m"})
        return httpx.Response(200, json={"hop": request.url.host})

    async with make_client(handler) as client:
        result = await client.get(KALSHI, venue="kalshi", purpose="books")
    assert result.json() == {"hop": "api.elections.kalshi.com"}


async def test_off_allowlist_redirect_refused() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://evil.example.com/steal"})

    async with make_client(handler) as client:
        with pytest.raises(HostNotAllowedError):
            await client.get(KALSHI, venue="kalshi", purpose="books")
    assert seen == ["external-api.kalshi.com"]


async def test_redirect_loop_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": KALSHI})

    async with make_client(handler) as client:
        with pytest.raises(HostNotAllowedError, match="too many"):
            await client.get(KALSHI, venue="kalshi", purpose="books")


async def test_oversize_body_refused() -> None:
    big = b"x" * 2000
    async with make_client(lambda r: httpx.Response(200, content=big)) as client:
        client.max_body_bytes = 1000
        with pytest.raises(ResponseTooLargeError):
            await client.get(KALSHI, venue="kalshi", purpose="books")


async def test_oversize_streamed_without_content_length() -> None:
    async def gen():
        for _ in range(5):
            yield b"y" * 300

    async with make_client(lambda r: httpx.Response(200, content=gen())) as client:
        client.max_body_bytes = 1000
        with pytest.raises(ResponseTooLargeError):
            await client.get(KALSHI, venue="kalshi", purpose="books")


async def test_default_cap_is_5mb_and_timeouts_from_settings() -> None:
    async with make_client(lambda r: httpx.Response(200)) as client:
        assert client.max_body_bytes == 5 * 1024 * 1024
        timeout = client._client.timeout
        assert (timeout.connect, timeout.read) == (3.0, 5.0)


async def test_separate_buckets_per_venue_and_purpose() -> None:
    async with make_client(lambda r: httpx.Response(200)) as client:
        b = client._buckets
        assert len({id(v) for v in b.values()}) == 5
        assert b[("kalshi", "books")].rate == 3.0
        assert b[("kalshi", "discovery")].rate == 3.0
        assert b[("kalshi", "gamestate")].rate == 1.0
        assert b[("kalshi", "gamestate")].burst == 1.0
        assert b[("polyus", "books")].rate == 4.0
        assert b[("polyus", "discovery")].rate == 3.0
        with pytest.raises(ValueError, match="no rate bucket"):
            await client.get(KALSHI, venue="gemini", purpose="books")


def test_allowlist_contents() -> None:
    assert ALLOWED_HOSTS == {
        "gateway.polymarket.us",
        "api.polymarket.us",
        "external-api.kalshi.com",
        "api.elections.kalshi.com",
        "assets.kalshi.com",
        "api.gemini.com",
    }
