"""Stop-request file: a clean shutdown that needs no signals.

``pmarb stop`` writes ``<data_dir>/collector.stop``. A running collector checks
for it once a second and then shuts down as it does for SIGINT: it stops the
pollers, closes open episodes with ``collector_shutdown``, and drains the raw
log and the database. When the drain is done it deletes the file. Deletion is
the acknowledgement that ``pmarb stop --wait`` and ``deploy/windows/stop.ps1``
wait for.

A request older than the collector process is stale. Nobody consumed it, for
example because ``pmarb stop`` ran while no collector was running. The
collector deletes a stale request and keeps running.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

STOP_REQUEST_NAME = "collector.stop"


def stop_request_path(data_dir: Path | str) -> Path:
    """Where ``pmarb stop`` writes its request for this data directory."""
    return Path(data_dir) / STOP_REQUEST_NAME


def request_stop(data_dir: Path | str) -> Path:
    """Write the stop request atomically and return its path.

    The data directory must already exist. A missing directory usually means
    the wrong ``PMARB_DATA_DIR``, and no collector can be running on it.
    """
    directory = Path(data_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"data dir not found: {directory}")
    path = stop_request_path(directory)
    payload = json.dumps(
        {
            "requested_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "requested_by_pid": os.getpid(),
        }
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def wait_for_acknowledgement(
    path: Path,
    timeout_s: float,
    *,
    poll_s: float = 0.5,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> bool:
    """Wait until the collector deletes ``path``. Return False on timeout."""
    deadline = monotonic() + max(0.0, timeout_s)
    while True:
        if not path.exists():
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(poll_s, remaining))
