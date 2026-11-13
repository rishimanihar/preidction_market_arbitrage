"""Small portability helpers for Windows and POSIX.

Each check reads ``sys.platform`` when it is called, so tests can simulate
Windows with ``monkeypatch.setattr(sys, "platform", "win32")``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_windows() -> bool:
    """True on native Windows (``sys.platform == "win32"``)."""
    return sys.platform == "win32"


def fsync_directory(path: Path | str) -> None:
    """Make a create, rename, or unlink in ``path`` durable.

    POSIX needs an ``fsync`` on the directory itself before a new or renamed
    entry survives a crash. Windows cannot open a directory with ``os.open``
    (it raises ``PermissionError``), and Python has no portable way to flush a
    directory handle there. On Windows this step is skipped. NTFS records the
    metadata change in its journal, but a power cut in the next few seconds can
    still lose the newest entry. Callers still ``fsync`` file contents on every
    platform.
    """
    if is_windows():
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def portable_filename(name: str) -> str:
    """Return ``name`` as a valid file name on this platform.

    ``:`` is reserved on Windows, where NTFS reads it as an alternate data stream.
    Timestamps such as ``2026-11-11T18:00:00Z`` therefore become
    ``2026-11-11T18-00-00Z``. POSIX names are unchanged. Readers take the
    timestamp from the file body, never from the name.
    """
    if is_windows():
        return name.replace(":", "-")
    return name
