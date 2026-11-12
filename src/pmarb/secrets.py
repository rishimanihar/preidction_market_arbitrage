"""Key-file policy: refuse unsafe key files, never log contents."""

import errno
import hashlib
import os
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

# src/pmarb/secrets.py -> parents[2] is the repository root. Never use the cwd.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
# Built at runtime so the source does not contain a private-key header.
_PRIVATE_KEY_MARK = ("-----BEGIN" + " PRIVATE KEY-----").encode()
_MAX_KEY_BYTES = 1024 * 1024


class SecretPolicyError(RuntimeError):
    """A key file or tracked file violates the secrets policy."""


class _KeyFingerprint(NamedTuple):
    body: bytes
    digest: bytes


def check_key_file(path: Path | str, repo_root: Path | str) -> Path:
    """Validate a key file; return its resolved path. Raise ``SecretPolicyError``."""
    raw, resolved = _locate(path, repo_root)
    fd = _open_checked(raw, resolved)
    os.close(fd)
    return resolved


def load_private_key(path: Path | str, repo_root: Path | str | None = None) -> bytes:
    """Read key bytes after the policy checks.

    ``repo_root`` defaults to this package's repository. The process working
    directory is not used. The file is opened with ``O_NOFOLLOW``, checked with
    ``fstat`` on that descriptor, and read from the same descriptor.
    """
    root = _PACKAGE_ROOT if repo_root is None else repo_root
    raw, resolved = _locate(path, root)
    fd = _open_checked(raw, resolved)
    try:
        return _read_fd(fd)
    finally:
        os.close(fd)


def _locate(path: Path | str, repo_root: Path | str) -> tuple[Path, Path]:
    raw = Path(path).expanduser()
    if not raw.exists():
        raise SecretPolicyError(f"key file not found: {raw}")
    resolved = raw.resolve()
    root = Path(repo_root).expanduser().resolve()
    if resolved == root or root in resolved.parents:
        raise SecretPolicyError(f"key file must live outside the repository: {resolved}")
    return raw, resolved


def _open_checked(raw: Path, display: Path) -> int:
    try:
        fd = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise SecretPolicyError(f"key file not found: {display}") from exc
        raise SecretPolicyError(f"key file is not a regular file: {display}") from exc
    try:
        _require_key_stat(os.fstat(fd), display)
    except Exception:
        os.close(fd)
        raise
    return fd


def _require_key_stat(info: os.stat_result, display: Path) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise SecretPolicyError(f"key file is not a regular file: {display}")
    if info.st_mode & 0o077:
        mode = stat.S_IMODE(info.st_mode)
        raise SecretPolicyError(
            f"key file permissions {mode:04o} are too open (run: chmod 600 {display})"
        )
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise SecretPolicyError(f"key file is not owned by the current user: {display}")


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


def _configured_key_paths() -> list[Path]:
    try:
        from pmarb.config import load_settings

        settings = load_settings()
    except Exception:  # noqa: BLE001 - a missing config must not hide tracked secrets
        return []
    paths = (settings.kalshi_private_key_path, settings.polyus_private_key_path)
    return [path for path in paths if path is not None]


def _fingerprint(path: Path) -> _KeyFingerprint | None:
    """SHA-256 of a configured key body. Unreadable paths are skipped."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _MAX_KEY_BYTES:
            return None
        body = _read_fd(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if not body:
        return None
    return _KeyFingerprint(body, hashlib.sha256(body).hexdigest().encode())


def assert_no_tracked_secrets(
    repo_root: Path | str,
    values: Iterable[str],
    key_paths: Iterable[Path | str] | None = None,
) -> None:
    """Raise if a tracked file contains a secret, a private-key block, or a key fingerprint.

    The error names the file only, never the secret or the key bytes.
    ``key_paths`` defaults to the private-key paths configured on ``Settings``.
    A non-git directory is skipped.
    """
    root = Path(repo_root)
    needles = [value.encode() for value in values if value]
    selected = _configured_key_paths() if key_paths is None else [Path(path) for path in key_paths]
    fingerprints = [item for item in (_fingerprint(path) for path in selected) if item is not None]
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return  # not a git repo (or no git): nothing is tracked
    for rel in out.stdout.split(b"\0"):
        if not rel:
            continue
        name = os.fsdecode(rel)
        file = root / name
        try:
            if file.is_symlink() or not file.is_file():
                continue
            data = file.read_bytes()
        except OSError:
            continue
        if _PRIVATE_KEY_MARK in data:
            raise SecretPolicyError(f"a private key block appears in tracked file: {name}")
        file_digest = hashlib.sha256(data).hexdigest().encode()
        for item in fingerprints:
            embedded = len(item.body) >= 16 and item.body in data
            if item.digest in data or file_digest == item.digest or embedded:
                raise SecretPolicyError(
                    f"a configured key fingerprint appears in tracked file: {name}"
                )
        if any(needle in data for needle in needles):
            raise SecretPolicyError(f"a secret value appears in tracked file: {name}")
