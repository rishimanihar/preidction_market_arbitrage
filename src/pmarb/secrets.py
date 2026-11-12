"""Key-file policy: refuse unsafe key files, never log contents.

On POSIX a key file must have no group or other permission bits. On Windows
mode bits mean nothing, so the file must instead sit under
``%USERPROFILE%\\.config\\pmarb\\`` and its ACL (read with ``icacls``) must not
let Everyone, Users or Authenticated Users read it. See :mod:`pmarb.winacl`.
"""

import logging
import os
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path

from pmarb.osutil import is_windows
from pmarb.winacl import RESTRICT_HINT, CommandRunner, IcaclsParseError, check_acl

_log = logging.getLogger(__name__)


class SecretPolicyError(RuntimeError):
    """A key file or tracked file violates the secrets policy."""


def windows_config_dir() -> Path:
    """``%USERPROFILE%\\.config\\pmarb``, where Windows key and settings files live."""
    profile = os.environ.get("USERPROFILE")
    base = Path(profile) if profile else Path.home()
    return base / ".config" / "pmarb"


def check_key_file(
    path: Path | str,
    repo_root: Path | str,
    *,
    label: str = "key file",
    runner: CommandRunner | None = None,
    allowed_dir: Path | str | None = None,
    warnings: list[str] | None = None,
) -> Path:
    """Validate a key file; return its resolved path. Raise ``SecretPolicyError``.

    ``label`` names the file in messages (the doctor also checks the settings
    file with this function on Windows). ``runner``, ``allowed_dir`` and
    ``warnings`` apply on Windows only. ``runner`` executes ``icacls`` and
    ``allowed_dir`` defaults to :func:`windows_config_dir`. When ``icacls``
    cannot run, the ACL is unverified. That is logged as a warning and also
    appended to ``warnings`` when given. It never passes silently.
    """
    raw = Path(path).expanduser()
    if not raw.exists():
        raise SecretPolicyError(f"{label} not found: {raw}")
    resolved = raw.resolve()
    root = Path(repo_root).expanduser().resolve()
    if resolved == root or root in resolved.parents:
        raise SecretPolicyError(f"{label} must live outside the repository: {resolved}")
    st = resolved.stat()
    if not stat.S_ISREG(st.st_mode):
        raise SecretPolicyError(f"{label} is not a regular file: {resolved}")
    if is_windows():
        _check_windows_acl(resolved, label, runner, allowed_dir, warnings)
        return resolved
    if st.st_mode & 0o077:
        mode = stat.S_IMODE(st.st_mode)
        raise SecretPolicyError(
            f"{label} permissions {mode:04o} are too open (run: chmod 600 {resolved})"
        )
    return resolved


def _check_windows_acl(
    resolved: Path,
    label: str,
    runner: CommandRunner | None,
    allowed_dir: Path | str | None,
    warnings: list[str] | None,
) -> None:
    allowed = Path(allowed_dir) if allowed_dir is not None else windows_config_dir()
    allowed = allowed.expanduser().resolve()
    if allowed not in resolved.parents:
        raise SecretPolicyError(f"{label} must live under {allowed} on Windows: {resolved}")
    hint = RESTRICT_HINT.format(path=resolved)
    try:
        result = check_acl(resolved, runner)
    except IcaclsParseError as exc:
        raise SecretPolicyError(
            f"{label} ACL could not be verified ({exc}); refusing. Check it with: icacls "
            f'"{resolved}"'
        ) from None
    if result.status == "open":
        readers = ", ".join(result.broad_readers)
        raise SecretPolicyError(
            f"{label} is readable by {readers}; restrict it to your account (run: {hint})"
        )
    if result.status == "unavailable":
        message = (
            f"{label} ACL NOT verified: {result.detail}. Confirm that only your account can "
            f"read {resolved} (run: {hint})"
        )
        _log.warning(message)
        if warnings is not None:
            warnings.append(message)


def load_private_key(
    path: Path | str,
    repo_root: Path | str | None = None,
    *,
    runner: CommandRunner | None = None,
) -> bytes:
    """Read key bytes after the policy checks. ``repo_root`` defaults to the cwd's repo."""
    root = Path(repo_root) if repo_root is not None else _git_root(Path.cwd()) or Path.cwd()
    return check_key_file(path, root, runner=runner).read_bytes()


def _git_root(start: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip())


def assert_no_tracked_secrets(repo_root: Path | str, values: Iterable[str]) -> None:
    """Raise if any secret value appears in a git-tracked file. Skips non-git dirs.

    The error names the file only, never the value.
    """
    root = Path(repo_root)
    needles = [v.encode() for v in values if v]
    if not needles:
        return
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
        if any(n in data for n in needles):
            raise SecretPolicyError(f"a secret value appears in tracked file: {name}")
