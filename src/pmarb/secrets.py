"""Key-file policy: refuse unsafe key files, never log contents."""

import os
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path


class SecretPolicyError(RuntimeError):
    """A key file or tracked file violates the secrets policy."""


def check_key_file(path: Path | str, repo_root: Path | str) -> Path:
    """Validate a key file; return its resolved path. Raise ``SecretPolicyError``."""
    raw = Path(path).expanduser()
    if not raw.exists():
        raise SecretPolicyError(f"key file not found: {raw}")
    resolved = raw.resolve()
    root = Path(repo_root).expanduser().resolve()
    if resolved == root or root in resolved.parents:
        raise SecretPolicyError(f"key file must live outside the repository: {resolved}")
    st = resolved.stat()
    if not stat.S_ISREG(st.st_mode):
        raise SecretPolicyError(f"key file is not a regular file: {resolved}")
    if st.st_mode & 0o077:
        mode = stat.S_IMODE(st.st_mode)
        raise SecretPolicyError(
            f"key file permissions {mode:04o} are too open (run: chmod 600 {resolved})"
        )
    return resolved


def load_private_key(path: Path | str, repo_root: Path | str | None = None) -> bytes:
    """Read key bytes after the policy checks. ``repo_root`` defaults to the cwd's repo."""
    root = Path(repo_root) if repo_root is not None else _git_root(Path.cwd()) or Path.cwd()
    return check_key_file(path, root).read_bytes()


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
