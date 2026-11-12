"""Shared request canonicalization for offline venue signing."""

from pathlib import Path
from urllib.parse import urlsplit

from pmarb.secrets import load_private_key


def key_file_bytes(path: Path | str) -> bytes:
    """Anchor the key policy to this checkout even when called from another cwd."""
    return load_private_key(path, repo_root=Path(__file__).resolve().parents[3])


def signing_message(timestamp_ms: int, method: str, url: str, prefix: str) -> bytes:
    """Preserve the encoded path, excluding query and fragment; normalize the verb."""
    if type(timestamp_ms) is not int or timestamp_ms < 0:
        raise ValueError("timestamp_ms must be a nonnegative integer")
    verb = method.upper()
    if not verb or not verb.isascii() or not verb.isalpha():
        raise ValueError("method must be an ASCII HTTP verb")
    path = urlsplit(url).path
    if not path.startswith(prefix):
        raise ValueError(f"signing path must start with {prefix}")
    return f"{timestamp_ms}{verb}{path}".encode()
