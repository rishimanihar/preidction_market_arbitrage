"""Offline Kalshi RSA-PSS and Ed25519 request signing; never log results."""

import base64
import time
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from pmarb.venues._auth import key_file_bytes, signing_message

KalshiKey = rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


def load_kalshi_key(path: Path | str) -> KalshiKey:
    """Load PKCS#1/PKCS#8 PEM after enforcing the repository's key-file policy."""
    key = serialization.load_pem_private_key(key_file_bytes(path), password=None)
    if not isinstance(key, rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey):
        raise TypeError("Kalshi requires an RSA or Ed25519 private key")
    return key


def sign_kalshi(key: KalshiKey, timestamp_ms: int, method: str, path: str) -> str:
    """Sign timestamp + uppercase verb + /trade-api/... path, excluding the query.

    https://docs.kalshi.com/getting_started/api_keys.md specifies:
    "MGF1 with SHA-256, salt length equal to the digest length".
    """
    message = signing_message(timestamp_ms, method, path, "/trade-api/")
    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    elif isinstance(key, rsa.RSAPrivateKey):
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    else:
        raise TypeError("Kalshi requires an RSA or Ed25519 private key")
    return base64.b64encode(signature).decode("ascii")


def kalshi_auth_headers(
    key_id: str,
    key: KalshiKey,
    method: str,
    url: str,
    clock: Callable[[], float] = time.time,
) -> dict[str, str]:
    """Build sensitive headers; clock returns Unix seconds, read exactly once.

    Pass the complete API URL or rooted path, including /trade-api/. The WebSocket
    handshake uses GET and /trade-api/ws/v2. Queries never enter the signature.
    """
    timestamp_ms = int(clock() * 1000)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "KALSHI-ACCESS-SIGNATURE": sign_kalshi(key, timestamp_ms, method, url),
    }
