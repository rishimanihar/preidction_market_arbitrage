"""Collector: poll approved groups, track freshness, shut down cleanly."""

from pmarb.collector.books import BookMeta, BookState, BookStore
from pmarb.collector.clock import Clock, FakeClock, SystemClock
from pmarb.collector.freshness import ClockJumpGuard, is_fresh, max_age_for
from pmarb.collector.runtime import Collector, CollectorEvent
from pmarb.collector.scheduler import LIVE_WINDOW, PollPlan, ScheduledGroup, build_poll_plan
from pmarb.collector.supervisor import supervise

__all__ = [
    "LIVE_WINDOW",
    "BookMeta",
    "BookState",
    "BookStore",
    "Clock",
    "ClockJumpGuard",
    "Collector",
    "CollectorEvent",
    "FakeClock",
    "PollPlan",
    "ScheduledGroup",
    "SystemClock",
    "build_poll_plan",
    "is_fresh",
    "max_age_for",
    "supervise",
]
