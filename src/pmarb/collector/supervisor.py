"""Restart a loop after a crash. Never swallow cancellation.

``CancelledError`` is not an ``Exception``. It is re-raised so shutdown and
timeouts propagate. Any other exception is reported through ``on_crash`` and
the loop starts again after ``backoff.delay(attempt)`` monotonic seconds.
Pass :class:`pmarb.http.Backoff` in production so the wait is jittered; tests
pass a deterministic policy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from pmarb.collector.clock import Clock


class RestartBackoff(Protocol):
    """Delay before restart attempt ``attempt`` (0-based)."""

    def delay(self, attempt: int) -> float:
        """Seconds to wait. Jitter belongs inside the implementation."""


async def supervise(
    name: str,
    coro_factory: Callable[[], Awaitable[object]],
    *,
    backoff: RestartBackoff,
    clock: Clock,
    on_crash: Callable[[str, BaseException], None],
) -> None:
    """Run ``coro_factory`` until it returns or this task is cancelled.

    ``coro_factory`` is called again after each crash, so it must return a new
    awaitable each time. A clean return stops the supervisor. ``on_crash`` is
    how the crash is recorded; this function does not write storage itself.
    """
    attempt = 0
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_crash(name, exc)
            delay = backoff.delay(attempt)
            attempt += 1
            if delay < 0:
                raise ValueError("backoff delay must be >= 0") from exc
            await clock.sleep(delay)
        else:
            return
