"""Clocks for the collector.

Production uses :class:`SystemClock`. Tests use :class:`FakeClock`, whose
monotonic and wall clocks move only when the test says so, and independently
of each other. ``sleep`` wakes from monotonic time, never from a real delay.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as aware UTC. Naive datetimes are rejected."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 instant, including the collector's ``Z`` form."""
    if value is None or value.strip() == "":
        return None
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return ensure_utc(parsed)


class Clock(Protocol):
    """Time source for collector loops. Tests inject :class:`FakeClock`."""

    def monotonic(self) -> float:
        """Monotonic seconds. Paused while the machine is asleep."""

    def wall(self) -> datetime:
        """Aware UTC wall time."""

    async def sleep(self, seconds: float) -> None:
        """Wait until ``seconds`` of monotonic time have passed."""


class SystemClock:
    """The process clock: ``time.monotonic``, aware UTC, and ``asyncio.sleep``."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        await asyncio.sleep(seconds)


class FakeClock:
    """Manual clock. Monotonic and wall time advance only through :meth:`advance`.

    ``sleep`` completes when monotonic time reaches the deadline. Advancing the
    wall clock alone does not wake sleepers. That is what lets a test simulate
    "monotonic paused during sleep" without waiting.
    """

    def __init__(self, *, mono: float = 0.0, wall: datetime | None = None) -> None:
        self._mono = float(mono)
        if wall is None:
            wall = datetime(2026, 6, 3, 18, tzinfo=UTC)
        self._wall = ensure_utc(wall)
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._mono

    def wall(self) -> datetime:
        return self._wall

    @property
    def pending(self) -> int:
        """Sleeps not yet woken."""
        return sum(1 for _, fut in self._waiters if not fut.done())

    def advance(self, *, mono: float = 0.0, wall: timedelta | None = None) -> None:
        """Move monotonic time and/or wall time. Either argument may be omitted.

        ``mono`` is a non-negative delta. ``wall`` is a timedelta; ``None`` leaves
        the wall clock where it is. The two are not tied together.
        """
        if mono < 0:
            raise ValueError("mono advance must be >= 0")
        self._mono += mono
        if wall is not None:
            self._wall += wall
        self._wake()

    def set_wall(self, wall: datetime) -> None:
        """Replace the wall clock. Does not change monotonic time."""
        self._wall = ensure_utc(wall)
        self._wake()

    def _wake(self) -> None:
        still: list[tuple[float, asyncio.Future[None]]] = []
        for deadline, fut in self._waiters:
            if fut.done():
                continue
            if self._mono >= deadline:
                fut.set_result(None)
            else:
                still.append((deadline, fut))
        self._waiters = still

    def _discard(self, fut: asyncio.Future[None]) -> None:
        self._waiters = [
            (deadline, waiter) for deadline, waiter in self._waiters if waiter is not fut
        ]

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        self.sleeps.append(seconds)
        if seconds == 0:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append((self._mono + seconds, fut))
        try:
            await fut
        finally:
            if not fut.done():
                fut.cancel()
            self._discard(fut)
