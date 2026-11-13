"""Clock driven by a raw-log envelope's receipt timestamps.

The collector's monotonic and wall clocks are not reconstructed from the
machine that recorded the log. Each envelope already stores both:
``recv_mono_ns`` and ``recv_utc``. Replay decisions read those values, and
the 250 ms freshness timer advances both by the same interval between
envelopes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from pmarb.collector.clock import ensure_utc


class LookaheadError(RuntimeError):
    """A decision used information from after the decision time."""


class ReplayClock:
    """Monotonic and wall time taken from the envelope being applied.

    ``monotonic`` is ``recv_mono_ns / 1e9``. ``wall`` is ``recv_utc``.
    :meth:`advance_to` moves both clocks by the same number of nanoseconds,
    which is how a freshness tick stays on the recording's timeline without
    sleeping. The clock never moves backwards.
    """

    def __init__(self) -> None:
        self._mono_ns: int | None = None
        self._wall: datetime | None = None

    @property
    def started(self) -> bool:
        return self._mono_ns is not None

    @property
    def mono_ns(self) -> int:
        if self._mono_ns is None:
            raise RuntimeError("replay clock has not started")
        return self._mono_ns

    def monotonic(self) -> float:
        return self.mono_ns / 1_000_000_000

    def wall(self) -> datetime:
        if self._wall is None:
            raise RuntimeError("replay clock has not started")
        return self._wall

    def seek(self, mono_ns: int, wall: datetime) -> None:
        """Place the clock on one envelope. Equal timestamps are allowed."""
        if isinstance(mono_ns, bool) or not isinstance(mono_ns, int):
            raise TypeError("recv_mono_ns must be an int")
        if mono_ns < 0:
            raise ValueError("recv_mono_ns must be >= 0")
        if self._mono_ns is not None and mono_ns < self._mono_ns:
            raise LookaheadError(f"replay clock moved backwards from {self._mono_ns} to {mono_ns}")
        self._mono_ns = mono_ns
        self._wall = ensure_utc(wall)

    def advance_to(self, mono_ns: int) -> None:
        """Move to ``mono_ns``, carrying the wall clock by the same interval.

        The wall step is whole microseconds. A step that is not a multiple of
        1 µs drops the remainder rather than moving the wall early.
        """
        current = self.mono_ns
        if isinstance(mono_ns, bool) or not isinstance(mono_ns, int):
            raise TypeError("mono_ns must be an int")
        if mono_ns < current:
            raise LookaheadError(f"replay clock moved backwards from {current} to {mono_ns}")
        self._mono_ns = mono_ns
        self._wall = self.wall() + timedelta(microseconds=(mono_ns - current) // 1000)

    async def sleep(self, seconds: float) -> None:
        """Advance replay time by ``seconds`` and return. Does not wait."""
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        self.advance_to(self.mono_ns + round(seconds * 1_000_000_000))
        await asyncio.sleep(0)
