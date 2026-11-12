"""Offline Polymarket US Ed25519 signing; never log secrets or returned headers.

WARNING: This is a TRADING CREDENTIAL, even when used only for market data.
Keep it in its own external, owner-only file / Pane secret; never mount it on a
shared machine. A read-only retail key is not documented (plan section 8).
"""

import base64
import binascii
import time
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pmarb.venues._auth import key_file_bytes, signing_message


def load_polyus_key(path_or_secret: Path | str) -> Ed25519PrivateKey:
    """Load a base64 secret from an external, policy-checked file path.

    Both str and Path arguments name files; raw secret strings are not accepted.
    PEM is not supported:
    https://docs.polymarket.us/api-reference/authentication.md decodes base64 and
    takes [:32]. Accept a 32-byte seed or a 64-byte secret (first 32 bytes).
    """
    secret = key_file_bytes(path_or_secret)
    try:
        decoded = base64.b64decode(secret.strip(), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Polymarket US requires a base64 Ed25519 secret") from None
    if len(decoded) not in (32, 64):
        raise ValueError("Polymarket US secret must decode to 32 or 64 bytes")
    return Ed25519PrivateKey.from_private_bytes(decoded[:32])


def sign_polyus(key: Ed25519PrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    """Sign timestamp + uppercase verb + /v1/... path, with no query or body."""
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Polymarket US requires an Ed25519 private key")
    message = signing_message(timestamp_ms, method, path, "/v1/")
    return base64.b64encode(key.sign(message)).decode("ascii")


def polyus_auth_headers(
    key_id: str,
    key: Ed25519PrivateKey,
    method: str,
    url: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, str]:
    """Build sensitive headers using one Unix-seconds clock reading.

    The market-data WebSocket uses GET and /v1/ws/markets. The timestamp must be
    within 30 seconds of server time; the caller maintains clock synchronization.
    """
    timestamp_ms = int(clock() * 1000)
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": str(timestamp_ms),
        "X-PM-Signature": sign_polyus(key, timestamp_ms, method, url),
    }
