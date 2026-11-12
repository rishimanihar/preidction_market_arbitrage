"""Per-venue book source: WebSocket stream or REST poll, and stream health.

Mode is chosen once per venue at build time (:func:`select_venue_modes`):

- ``stream`` when that venue's key id and key file are both configured, the
  key file passes :func:`pmarb.secrets.check_key_file`, and the key loads;
- ``poll`` otherwise, with a reason (``keys_missing``, ``key_policy``,
  ``key_invalid``) the collector records as a ``stream_mode`` event.

Each venue is decided on its own, so one venue can stream while the other
polls. A streaming venue still falls back to REST while its stream is
unhealthy (:class:`StreamHealth`) and returns to the stream once it is healthy.

The factories only build market-data streams (Kalshi ``orderbook_delta`` and
``market_lifecycle_v2``; Polymarket US ``SUBSCRIPTION_TYPE_MARKET_DATA``).
Nothing here can place, amend, or cancel an order.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import UnsupportedAlgorithm

from pmarb.collector.clock import Clock
from pmarb.config import Settings
from pmarb.models import Venue
from pmarb.protocols import BookEvent
from pmarb.secrets import SecretPolicyError, check_key_file
from pmarb.venues.kalshi.auth import load_kalshi_key
from pmarb.venues.kalshi.ws import KalshiOrderbookStream
from pmarb.venues.polymarket_us.auth import load_polyus_key
from pmarb.venues.polymarket_us.ws import PolyUSMarketStream

STREAM = "stream"
POLL = "poll"

KEYS_CONFIGURED = "keys_configured"
KEYS_MISSING = "keys_missing"
KEY_POLICY = "key_policy"
KEY_INVALID = "key_invalid"
NO_STREAM = "no_stream"

# Health transitions, also the collector_events kinds.
STREAM_FALLBACK = "stream_fallback"
STREAM_RECOVERED = "stream_recovered"

# Fallback reasons.
DISCONNECTED = "disconnected"
SCHEMA_FAILURES = "schema_failures"
SEQUENCE_GAPS = "sequence_gaps"

# How a stream's invalidate reason counts toward health. Anything else
# (crossed_book, market_closed, ...) is about one market, not the connection.
_DISCONNECT_REASONS = frozenset({"disconnect", "disconnected", "stream_ended", "stream_error"})
_GAP_REASONS = frozenset({"sequence_gap"})
_SCHEMA_REASONS = frozenset({"schema_failure", "malformed_frame", "parse_failure"})
_MAINTENANCE_REASONS = frozenset({"maintenance"})

# src/pmarb/collector/streams.py -> repository root, the same anchor the
# venue signing modules use for the key-file policy.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_KEY_LOAD_ERRORS = (SecretPolicyError, OSError, TypeError, ValueError, UnsupportedAlgorithm)

ModeName = Literal["stream", "poll"]
StreamFactory = Callable[[Sequence[str]], AsyncIterator[BookEvent]]
IncidentKind = Literal["disconnect", "gap", "schema", "maintenance", "market"]


@dataclass(frozen=True, slots=True)
class VenueMode:
    """How one venue gets its books, and why. ``factory`` is set only for streams."""

    venue: Venue
    mode: ModeName
    reason: str
    detail: str | None = None
    factory: StreamFactory | None = None

    def __post_init__(self) -> None:
        if self.mode not in (STREAM, POLL):
            raise ValueError("mode must be 'stream' or 'poll'")
        if (self.mode == STREAM) != (self.factory is not None):
            raise ValueError("a stream mode needs a factory and a poll mode must not have one")

    def describe(self) -> dict[str, Any]:
        """The ``stream_mode`` event detail. Never includes key material."""
        return {"mode": self.mode, "reason": self.reason, "detail": self.detail}


def poll_mode(venue: Venue, reason: str = NO_STREAM, detail: str | None = None) -> VenueMode:
    return VenueMode(venue=venue, mode=POLL, reason=reason, detail=detail)


def stream_mode(venue: Venue, factory: StreamFactory, detail: str | None = None) -> VenueMode:
    return VenueMode(
        venue=venue, mode=STREAM, reason=KEYS_CONFIGURED, detail=detail, factory=factory
    )


def select_venue_modes(
    settings: Settings,
    clock: Clock,
    *,
    repo_root: Path | None = None,
    connect: dict[Venue, Callable[..., Any]] | None = None,
) -> dict[Venue, VenueMode]:
    """Decide stream or poll for each venue from the configured keys.

    ``connect`` replaces the WebSocket connector per venue (tests only). Key
    files are checked against ``repo_root`` (default: this checkout).
    """
    root = repo_root if repo_root is not None else _REPO_ROOT
    connectors = connect or {}
    return {
        Venue.KALSHI: _kalshi_mode(settings, clock, root, connectors.get(Venue.KALSHI)),
        Venue.POLYMARKET_US: _polyus_mode(
            settings, clock, root, connectors.get(Venue.POLYMARKET_US)
        ),
    }


def _key_check(
    venue: Venue, key_id: object, key_path: Path | None, repo_root: Path
) -> VenueMode | Path:
    missing = [
        name
        for name, value in (("key id", key_id), ("key path", key_path))
        if value is None or value == ""
    ]
    if missing:
        return poll_mode(venue, KEYS_MISSING, " and ".join(missing) + " not configured")
    assert key_path is not None
    try:
        return check_key_file(key_path, repo_root)
    except SecretPolicyError as exc:
        return poll_mode(venue, KEY_POLICY, str(exc))
    except OSError as exc:
        return poll_mode(venue, KEY_POLICY, f"key file check failed: {type(exc).__name__}")


def _kalshi_mode(
    settings: Settings, clock: Clock, repo_root: Path, connect: Callable[..., Any] | None
) -> VenueMode:
    venue = Venue.KALSHI
    key_id = settings.kalshi_api_key_id
    checked = _key_check(venue, key_id, settings.kalshi_private_key_path, repo_root)
    if isinstance(checked, VenueMode):
        return checked
    try:
        key = load_kalshi_key(checked)
    except _KEY_LOAD_ERRORS as exc:
        # The exception type only: a parse error must not echo file contents.
        return poll_mode(venue, KEY_INVALID, f"key did not load: {type(exc).__name__}")
    assert key_id is not None
    secret_id = key_id.get_secret_value()
    extra: dict[str, Any] = {} if connect is None else {"connect": connect}

    def factory(market_ids: Sequence[str]) -> AsyncIterator[BookEvent]:
        # Signing keeps the real Unix clock (the venue checks it). Book stamps
        # and reconnect sleeps use the collector clock so ages are comparable.
        return KalshiOrderbookStream(
            secret_id,
            key,
            market_ids,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            clock_utc=clock.wall,
            **extra,
        )

    return stream_mode(venue, factory)


class _PolyClock:
    """The ``utcnow``/``monotonic``/``sleep`` shape PolyUSMarketStream reads."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def utcnow(self) -> Any:
        return self._clock.wall()

    def monotonic(self) -> float:
        return self._clock.monotonic()

    async def sleep(self, seconds: float) -> None:
        await self._clock.sleep(seconds)


def _polyus_mode(
    settings: Settings, clock: Clock, repo_root: Path, connect: Callable[..., Any] | None
) -> VenueMode:
    venue = Venue.POLYMARKET_US
    key_id = settings.polyus_api_key_id
    checked = _key_check(venue, key_id, settings.polyus_private_key_path, repo_root)
    if isinstance(checked, VenueMode):
        return checked
    try:
        key = load_polyus_key(checked)
    except _KEY_LOAD_ERRORS as exc:
        return poll_mode(venue, KEY_INVALID, f"key did not load: {type(exc).__name__}")
    assert key_id is not None
    secret_id = key_id.get_secret_value()
    depth_confirmed = settings.polyus_ws_depth_confirmed
    poly_clock = _PolyClock(clock)

    def factory(market_ids: Sequence[str]) -> AsyncIterator[BookEvent]:
        return PolyUSMarketStream(
            secret_id,
            key,
            market_ids,
            connect=connect,
            clock=poly_clock,
            depth_confirmed=depth_confirmed,
        )

    detail = None if depth_confirmed else "depth_unconfirmed: results stay indicative"
    return stream_mode(venue, factory, detail)


def classify(reason: str | None) -> IncidentKind:
    """How an invalidate reason counts toward the connection's health."""
    if reason in _DISCONNECT_REASONS:
        return "disconnect"
    if reason in _GAP_REASONS:
        return "gap"
    if reason in _SCHEMA_REASONS:
        return "schema"
    if reason in _MAINTENANCE_REASONS:
        return "maintenance"
    return "market"


@dataclass(frozen=True, slots=True)
class HealthLimits:
    """When a stream is unhealthy enough to fall back to REST, and when it is back."""

    fallback_after_s: float = 5.0
    recovery_s: float = 10.0
    window_s: float = 60.0
    max_schema_failures: int = 3
    max_gaps: int = 3

    @classmethod
    def from_settings(cls, settings: Settings) -> HealthLimits:
        return cls(
            fallback_after_s=settings.stream_fallback_after_s,
            recovery_s=settings.stream_recovery_s,
            window_s=settings.stream_health_window_s,
            max_schema_failures=settings.stream_max_schema_failures,
            max_gaps=settings.stream_max_gaps,
        )


@dataclass(frozen=True, slots=True)
class Transition:
    """``stream_fallback`` or ``stream_recovered``, with the detail to record."""

    kind: str
    reason: str
    detail: dict[str, Any]


class StreamHealth:
    """One venue's stream health, folded from its events on the collector clock.

    Unhealthy (fall back to REST) when any of these hold:

    - disconnected or reconnecting for more than ``fallback_after_s``
      (a stream that never delivers a first snapshot counts from its start);
    - ``max_schema_failures`` schema/parse incidents within ``window_s``;
    - ``max_gaps`` sequence-gap incidents within ``window_s``.

    An incident is one problem until the next snapshot: the burst of per-market
    invalidations a single gap or disconnect produces counts once.

    Healthy again after ``recovery_s`` of snapshots with no new problem, on a
    live connection outside maintenance. A Polymarket US maintenance window is
    not a failure (REST is down too), so it pauses the disconnect timer.
    """

    def __init__(self, venue: Venue, limits: HealthLimits, now_mono: float) -> None:
        self.venue = venue
        self.limits = limits
        self.connected = False
        self.maintenance = False
        self.fallback = False
        self.fallback_reason: str | None = None
        self.fallback_since: float | None = None
        self.down_since: float | None = now_mono
        self.stable_since: float | None = None
        self.last_problem: float | None = None
        self.snapshots = 0
        self._schema: deque[float] = deque()
        self._gaps: deque[float] = deque()
        self._open: set[IncidentKind] = set()

    @property
    def healthy(self) -> bool:
        """True when the stream's books may be used as stream books."""
        return self.connected and not self.fallback and not self.maintenance

    @property
    def state(self) -> str:
        if self.fallback:
            return "fallback"
        if self.maintenance:
            return "maintenance"
        if self.connected:
            return "healthy"
        if self.snapshots == 0:
            return "connecting"
        return "reconnecting"

    def restart(self, now_mono: float) -> None:
        """A new stream instance (resubscribe or crash restart): not connected yet."""
        self.connected = False
        self.stable_since = None
        if self.down_since is None and not self.maintenance:
            self.down_since = now_mono
        self._open.clear()

    def idle(self) -> None:
        """No market to stream: not connected, and the idle time is not an outage."""
        self.connected = False
        self.down_since = None
        self.stable_since = None
        self._open.clear()

    def on_snapshot(self, now_mono: float) -> None:
        self.connected = True
        self.maintenance = False
        self.down_since = None
        self.snapshots += 1
        if self.stable_since is None:
            self.stable_since = now_mono
        self._open.clear()

    def on_invalidate(self, now_mono: float, reason: str | None) -> IncidentKind:
        kind = classify(reason)
        if kind == "market":
            return kind
        if kind == "maintenance":
            self.connected = False
            self.maintenance = True
            self.down_since = None
            self.stable_since = None
            return kind
        self.last_problem = now_mono
        self.stable_since = None
        if kind == "disconnect":
            self.connected = False
            self.maintenance = False
            if self.down_since is None:
                self.down_since = now_mono
        if kind not in self._open:
            self._open.add(kind)
            if kind == "gap":
                self._gaps.append(now_mono)
            elif kind == "schema":
                self._schema.append(now_mono)
        return kind

    def check(self, now_mono: float) -> Transition | None:
        """Return a transition when the health class changes at ``now_mono``."""
        self._prune(now_mono)
        if not self.fallback:
            reason = self._failure(now_mono)
            if reason is None:
                return None
            detail = self.describe(now_mono)
            self.fallback = True
            self.fallback_reason = reason
            self.fallback_since = now_mono
            self._schema.clear()
            self._gaps.clear()
            detail["reason"] = reason
            return Transition(STREAM_FALLBACK, reason, detail)
        if (
            self.connected
            and not self.maintenance
            and self.stable_since is not None
            and now_mono - self.stable_since >= self.limits.recovery_s
        ):
            reason = self.fallback_reason or ""
            since = self.fallback_since if self.fallback_since is not None else now_mono
            self.fallback = False
            self.fallback_reason = None
            self.fallback_since = None
            detail = self.describe(now_mono)
            detail["recovered_from"] = reason
            detail["fallback_s"] = max(0.0, now_mono - since)
            return Transition(STREAM_RECOVERED, reason, detail)
        return None

    def describe(self, now_mono: float) -> dict[str, Any]:
        down = None if self.down_since is None else max(0.0, now_mono - self.down_since)
        return {
            "state": self.state,
            "connected": self.connected,
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "down_s": down,
            "schema_failures": len(self._schema),
            "gaps": len(self._gaps),
            "snapshots": self.snapshots,
        }

    def _failure(self, now_mono: float) -> str | None:
        if (
            self.down_since is not None
            and now_mono - self.down_since > self.limits.fallback_after_s
        ):
            return DISCONNECTED
        if len(self._schema) >= self.limits.max_schema_failures:
            return SCHEMA_FAILURES
        if len(self._gaps) >= self.limits.max_gaps:
            return SEQUENCE_GAPS
        return None

    def _prune(self, now_mono: float) -> None:
        horizon = now_mono - self.limits.window_s
        for items in (self._schema, self._gaps):
            while items and items[0] < horizon:
                items.popleft()
