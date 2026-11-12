"""Change-only persistence for live game state."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from pmarb.storage.db import Write, insert_game_state
from pmarb.storage.rawlog import Envelope, RawLogWriter


def _clock_seconds(clock: str | None) -> int | None:
    if clock is None:
        return None
    pieces = clock.split(":")
    try:
        values = [int(part) for part in pieces]
    except ValueError:
        return None
    if len(values) == 2 and 0 <= values[1] < 60 and values[0] >= 0:
        return values[0] * 60 + values[1]
    if len(values) == 3 and 0 <= values[1] < 60 and 0 <= values[2] < 60:
        return values[0] * 3600 + values[1] * 60 + values[2]
    return None


class GameStateRecorder:
    """Poll live states and enqueue a row only when its meaningful state changes.

    ``leagues`` maps event tickers to leagues; ``clock`` and ``monotonic`` are
    wired into the Kalshi client for deterministic response timestamps. The
    supplied writer's task must be running while the recorder enqueues writes.
    """

    def __init__(
        self,
        client: Any,
        event_tickers: Sequence[str],
        leagues: Mapping[str, str],
        writer: Any,
        raw_log: RawLogWriter,
        *,
        poll_interval_s: float = 10.0,
        clock_bucket_s: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if poll_interval_s <= 0 or clock_bucket_s <= 0:
            raise ValueError("poll interval and clock bucket must be positive")
        if not event_tickers:
            raise ValueError("at least one event ticker is required")
        self.client = client
        self.event_tickers = tuple(event_tickers)
        self.leagues = dict(leagues)
        self.writer = writer
        self.raw_log = raw_log
        self.poll_interval_s = poll_interval_s
        self.clock_bucket_s = clock_bucket_s
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self._previous: dict[str, tuple[object, ...]] = {}
        self._final: set[str] = set()
        if hasattr(client, "leagues"):
            client.leagues = self.leagues
        if hasattr(client, "_clock_utc"):
            client._clock_utc = clock
        if hasattr(client, "_monotonic"):
            client._monotonic = monotonic
        # The client logs each complete HTTP response, even if every row is unchanged.
        self.client.raw_log = self._log_response

    def _log_response(
        self,
        body_bytes: bytes,
        payload: dict[str, Any],
        received_utc: datetime,
        received_mono: float,
        endpoint: str,
        status_code: int,
        cache_headers: dict[str, Any] | None = None,
    ) -> None:
        self.raw_log.append(
            Envelope(
                recv_utc=received_utc.isoformat().replace("+00:00", "Z"),
                recv_mono_ns=int(received_mono * 1_000_000_000),
                venue="kalshi",
                kind="gamestate",
                conn_gen=0,
                endpoint=endpoint,
                status=status_code,
                cache_headers=cache_headers or {},
                body=body_bytes.decode("utf-8", errors="replace"),
            )
        )

    def _change_key(self, state: Any) -> tuple[object, ...]:
        seconds = _clock_seconds(state.clock)
        bucket = None if seconds is None else seconds // self.clock_bucket_s
        return (
            state.home_points,
            state.away_points,
            state.period,
            bucket,
            state.status,
        )

    async def run(self) -> None:
        """Poll until every watched game reports a final status."""
        mapping = await self.client.resolve_milestones(self.event_tickers)
        active = {
            ticker: milestone for ticker, milestone in mapping.items() if ticker not in self._final
        }
        while active:
            states = await self.client.fetch_live(list(dict.fromkeys(active.values())))
            for fetched in states:
                tickers = [ticker for ticker, mid in active.items() if mid == fetched.milestone_id]
                for ticker in tickers:
                    state = replace(
                        fetched,
                        event_ticker=ticker,
                        league=self.leagues.get(ticker, fetched.league),
                    )
                    key = self._change_key(state)
                    if self._previous.get(ticker) != key:
                        await self.writer.queue.put(
                            Write(lambda conn, state=state: insert_game_state(conn, state))
                        )
                        self._previous[ticker] = key
                    if _is_final(state.status):
                        self._final.add(ticker)
                        active.pop(ticker, None)
            self.raw_log.flush()
            if active:
                await self.sleep(self.poll_interval_s)


def _is_final(status: str | None) -> bool:
    return status is not None and status.casefold() in {
        "final",
        "complete",
        "completed",
        "finished",
        "closed",
        "ended",
    }
