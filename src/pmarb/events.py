"""Event kinds shared by the collector and the soak report.

The collector is the producer. ``pmarb report`` is the consumer. Both import
these names so a health row, an adapter failure, a recording stall, or a
restart is classified once.
"""

from __future__ import annotations

from pmarb.models import EpisodeTermination

ADAPTER_ERROR = "adapter_error"
# Older report fixtures used this kind. The collector writes ADAPTER_ERROR.
ERROR = "error"
RECORDING_BACKPRESSURE = "recording_backpressure"
BACKPRESSURE = "backpressure"
COLLECTOR_RESTART = EpisodeTermination.COLLECTOR_RESTART.value
RESTART = "restart"
LOOP_CRASH = "loop_crash"
HEALTH = "health"
GAP = "gap"
MAINTENANCE = EpisodeTermination.MAINTENANCE.value
CLOCK_JUMP = EpisodeTermination.CLOCK_JUMP.value
DATA_GAP = EpisodeTermination.DATA_GAP.value
COLLECTOR_SHUTDOWN = EpisodeTermination.COLLECTOR_SHUTDOWN.value
ADAPTER_RECOVERED = "adapter_recovered"
STOP_REQUESTED = "stop_requested"
STOP_REQUEST_STALE = "stop_request_stale"
TIMING = "timing"
DISCOVERY = "discovery"
DISCOVERY_ERROR = "discovery_error"
METADATA_REFRESH = "metadata_refresh"
METADATA_ERROR = "metadata_error"
EVALUATION_ERROR = "evaluation_error"

# Health detail object. One collector row covers every venue.
VENUES = "venues"
FRESH = "fresh"
VALID = "valid"

# Buckets the report publishes under coverage.gaps_by_kind.
GAP_KINDS = (MAINTENANCE, CLOCK_JUMP, DATA_GAP, BACKPRESSURE, RESTART)

# Event kind, or the reason inside a ``gap`` envelope, mapped to a bucket.
GAP_ALIASES = {
    RECORDING_BACKPRESSURE: BACKPRESSURE,
    BACKPRESSURE: BACKPRESSURE,
    COLLECTOR_RESTART: RESTART,
    RESTART: RESTART,
    MAINTENANCE: MAINTENANCE,
    CLOCK_JUMP: CLOCK_JUMP,
    DATA_GAP: DATA_GAP,
}

ERROR_KINDS = frozenset({ADAPTER_ERROR, ERROR})


def is_error_event(kind: object) -> bool:
    """True for a collector adapter failure or a legacy ``error`` row."""
    return kind in ERROR_KINDS


def gap_bucket(kind: object, detail: object) -> str | None:
    """Return the report gap bucket for one event, or ``None`` when it is not a gap.

    A ``gap`` row carries the bucket in ``detail["kind"]`` or ``detail["reason"]``.
    The collector also emits the bucket as the event kind itself
    (``recording_backpressure``, ``collector_restart``, ``clock_jump``,
    ``maintenance``).
    """
    if not isinstance(kind, str):
        return None
    if kind == GAP:
        if not isinstance(detail, dict):
            return None
        reason = detail.get("kind", detail.get("reason"))
        if not isinstance(reason, str):
            return None
        mapped = GAP_ALIASES.get(reason, reason)
        return mapped if mapped in GAP_KINDS else None
    mapped = GAP_ALIASES.get(kind)
    if mapped in GAP_KINDS:
        return mapped
    return None
