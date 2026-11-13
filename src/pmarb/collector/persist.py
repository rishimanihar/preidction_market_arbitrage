"""Hand SQLite ops to the single writer, one discovery cycle per batch."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from pmarb.storage.db import Write

Op = Callable[[sqlite3.Connection], None]


class WriteSink:
    """Enqueue one op, or fold a discovery cycle into a single queue item.

    The collector's writer commits the queue. A batch keeps a catalog refresh
    from pushing the queue over the backpressure limit: one cycle is one item,
    however many markets it upserts.
    """

    def __init__(self, queue: object | None) -> None:
        self._queue = queue
        self._batch: list[Op] | None = None

    def __call__(self, fn: Op) -> None:
        if self._batch is not None:
            self._batch.append(fn)
            return
        self._put(fn)

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Hold ops until the block exits, then enqueue them as one write."""
        if self._batch is not None:
            raise RuntimeError("write batch already open")
        self._batch = []
        try:
            yield
        finally:
            ops = self._batch
            self._batch = None
            if ops:
                captured = tuple(ops)
                self._put(lambda conn: _apply_all(conn, captured))

    def _put(self, fn: Op) -> None:
        if self._queue is None:
            return
        put = getattr(self._queue, "put_nowait", None)
        if put is None:
            raise TypeError("queue must provide put_nowait")
        put(Write(fn))


def _apply_all(conn: sqlite3.Connection, ops: tuple[Op, ...]) -> None:
    for op in ops:
        op(conn)
