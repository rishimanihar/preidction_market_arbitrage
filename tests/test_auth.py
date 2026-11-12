"""Offline signing vectors: all key material is generated at runtime."""

import base64
import logging
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from pmarb.logging import MASK, RedactingFilter
from pmarb.secrets import SecretPolicyError
from pmarb.venues.kalshi.auth import kalshi_auth_headers, load_kalshi_key, sign_kalshi
from pmarb.venues.polymarket_us.auth import load_polyus_key, polyus_auth_headers, sign_polyus

ROOT = Path(__file__).resolve().parents[1]
KALSHI_MESSAGE = b"1703123456789GET/trade-api/v2/portfolio/orders"
POLYUS_MESSAGE = b"1700000000000GET/v1/ws/markets"


def write_key(path: Path, data: bytes, mode: int = 0o600) -> Path:
    # Create owner-only before writing, even when the process umask is permissive.
    path.touch(mode=0o600)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def pem(key, fmt=serialization.PrivateFormat.PKCS8) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())


@pytest.fixture
def ed_key() -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def seed(ed_key) -> bytes:
    return ed_key.private_bytes_raw()


@pytest.mark.parametrize(
    "fmt", [serialization.PrivateFormat.PKCS8, serialization.PrivateFormat.TraditionalOpenSSL]
)
def test_kalshi_rsa_pem_and_vector(tmp_path, rsa_key, fmt):
    path = write_key(tmp_path / "rsa.pem", pem(rsa_key, fmt))
    assert ROOT not in path.resolve().parents
    key = load_kalshi_key(str(path))
    assert isinstance(key, rsa.RSAPrivateKey)
    headers = kalshi_auth_headers(
        "test-key-id",
        key,
        "get",
        "https://external-api.kalshi.com/trade-api/v2/portfolio/orders?limit=5",
        clock=lambda: 1703123456.789,
    )
    assert set(headers) == {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE",
    }
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"], validate=True)
    pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
    key.public_key().verify(signature, KALSHI_MESSAGE, pss, hashes.SHA256())
    with pytest.raises(InvalidSignature):
        key.public_key().verify(signature, KALSHI_MESSAGE + b"?limit=5", pss, hashes.SHA256())


def test_kalshi_ed25519_exact_vector(tmp_path, ed_key):
    key = load_kalshi_key(write_key(tmp_path / "ed.pem", pem(ed_key)))
    actual = sign_kalshi(key, 1703123456789, "gEt", "/trade-api/v2/portfolio/orders?limit=5")
    expected = base64.b64encode(ed_key.sign(KALSHI_MESSAGE)).decode("ascii")
    assert actual == expected
    assert sign_kalshi(key, 1703123456789, "GET", "/trade-api/v2/portfolio/orders") == actual
    key.public_key().verify(base64.b64decode(actual), KALSHI_MESSAGE)


@pytest.mark.parametrize("extended", [False, True])
def test_polyus_file_secret_vector(tmp_path, ed_key, seed, extended):
    raw = seed + ed_key.public_key().public_bytes_raw() if extended else seed
    encoded = base64.b64encode(raw)
    path = write_key(tmp_path / "polyus.key", encoded + b"\n")
    assert ROOT not in path.resolve().parents
    for source in (path, str(path)):
        key = load_polyus_key(source)
        headers = polyus_auth_headers(
            "test-key-id",
            key,
            "gEt",
            "wss://api.polymarket.us/v1/ws/markets?test=1",
            clock=lambda: 1700000000,
        )
        assert set(headers) == {"X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature"}
        assert headers["X-PM-Access-Key"] == "test-key-id"
        assert headers["X-PM-Timestamp"] == "1700000000000"
        expected = base64.b64encode(ed_key.sign(POLYUS_MESSAGE)).decode("ascii")
        assert headers["X-PM-Signature"] == expected
        signature = base64.b64decode(headers["X-PM-Signature"], validate=True)
        assert len(signature) == 64
        key.public_key().verify(signature, POLYUS_MESSAGE)
        assert sign_polyus(key, 1700000000000, "GET", "/v1/ws/markets") == expected
        with pytest.raises(InvalidSignature):
            key.public_key().verify(signature, POLYUS_MESSAGE + b"?test=1")


@pytest.mark.parametrize(
    ("headers_fn", "path", "message", "signature_name"),
    [
        (
            kalshi_auth_headers,
            "/trade-api/ws/v2",
            b"1700000000000GET/trade-api/ws/v2",
            "KALSHI-ACCESS-SIGNATURE",
        ),
        (polyus_auth_headers, "/v1/ws/markets", POLYUS_MESSAGE, "X-PM-Signature"),
    ],
)
def test_ws_headers_use_one_clock_read(ed_key, headers_fn, path, message, signature_name):
    calls = []

    def clock():
        calls.append(True)
        assert len(calls) == 1
        return 1700000000

    headers = headers_fn("test-key-id", ed_key, "get", f"wss://example.test{path}", clock)
    assert len(calls) == 1
    ed_key.public_key().verify(base64.b64decode(headers[signature_name]), message)


def test_wrong_key_types(tmp_path, rsa_key):
    other_key = ec.generate_private_key(ec.SECP256R1())
    path = write_key(tmp_path / "ec.pem", pem(other_key))
    with pytest.raises(TypeError, match="RSA or Ed25519"):
        load_kalshi_key(path)
    with pytest.raises(TypeError, match="RSA or Ed25519"):
        sign_kalshi(other_key, 1700000000000, "GET", "/trade-api/ws/v2")
    with pytest.raises(TypeError, match="Ed25519"):
        sign_polyus(rsa_key, 1700000000000, "GET", "/v1/ws/markets")
    with pytest.raises(ValueError, match="base64"):
        load_polyus_key(path)


@pytest.mark.parametrize("length", [0, 16, 31, 33, 63, 65])
def test_polyus_wrong_seed_length(tmp_path, length):
    path = write_key(tmp_path / "polyus.key", base64.b64encode(bytes(length)))
    with pytest.raises(ValueError, match="32 or 64"):
        load_polyus_key(path)


@pytest.mark.parametrize("secret", ["not base64!", "\N{SNOWMAN}"])
def test_polyus_malformed_secret_is_not_in_error(tmp_path, secret):
    path = write_key(tmp_path / "polyus.key", secret.encode())
    with pytest.raises(ValueError, match="base64") as caught:
        load_polyus_key(path)
    assert secret not in str(caught.value)


def test_polyus_raw_secret_is_not_loaded(seed):
    with pytest.raises(SecretPolicyError):
        load_polyus_key(base64.b64encode(seed).decode("ascii"))


@pytest.mark.parametrize("loader", [load_kalshi_key, load_polyus_key])
def test_key_inside_repo_refused(tmp_path, monkeypatch, loader):
    # No key bytes are written into the real checkout: model its containment boundary.
    import pmarb.venues._auth as shared

    fake_module = tmp_path / "repo/src/pmarb/venues/_auth.py"
    monkeypatch.setattr(shared, "__file__", str(fake_module))
    fake_module.parent.mkdir(parents=True)
    key = write_key(tmp_path / "repo/key", b"unparsed-test-placeholder")
    with pytest.raises(SecretPolicyError, match="outside the repository"):
        loader(key)


@pytest.mark.parametrize("loader", [load_kalshi_key, load_polyus_key])
def test_real_repo_path_refused_without_writing_key(loader):
    with pytest.raises(SecretPolicyError, match="outside the repository"):
        loader(ROOT / "pyproject.toml")


@pytest.mark.parametrize("loader", [load_kalshi_key, load_polyus_key])
def test_loose_permissions_refused(tmp_path, loader):
    key = write_key(tmp_path / "key", b"unparsed-test-placeholder", 0o644)
    with pytest.raises(SecretPolicyError, match="too open"):
        loader(key)


def test_repo_policy_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SecretPolicyError, match="outside the repository"):
        load_kalshi_key(ROOT / "pyproject.toml")


@pytest.mark.parametrize(
    ("sign", "path"), [(sign_kalshi, "/trade-api/ws/v2"), (sign_polyus, "/v1/ws/markets")]
)
def test_encoded_path_is_preserved(ed_key, sign, path):
    url = f"https://example.test{path}/a%2Fb?query=/different#fragment"
    signature = base64.b64decode(sign(ed_key, 1700000000000, "get", url))
    ed_key.public_key().verify(signature, f"1700000000000GET{path}/a%2Fb".encode())


@pytest.mark.parametrize(
    ("sign", "path"), [(sign_kalshi, "/trade-api/ws/v2"), (sign_polyus, "/v1/ws/markets")]
)
def test_invalid_signing_inputs(ed_key, sign, path):
    for timestamp in (-1, 1.5, True, "1700000000000"):
        with pytest.raises(ValueError, match="timestamp_ms"):
            sign(ed_key, timestamp, "GET", path)
    for method in ("", " GET", "get\n", "g\N{SNOWMAN}t"):
        with pytest.raises(ValueError, match="method"):
            sign(ed_key, 1700000000000, method, path)
    for wrong_path in ("orders", "/portfolio/orders", "/"):
        with pytest.raises(ValueError, match="path"):
            sign(ed_key, 1700000000000, "GET", wrong_path)


@pytest.mark.parametrize(
    ("headers_fn", "path"),
    [(kalshi_auth_headers, "/trade-api/ws/v2"), (polyus_auth_headers, "/v1/ws/markets")],
)
def test_all_generated_headers_redacted_and_no_signer_logs(ed_key, headers_fn, path, caplog):
    with caplog.at_level(logging.DEBUG):
        headers = headers_fn("test-key-id", ed_key, "GET", path, lambda: 1700000000)
    assert not caplog.records
    redactor = RedactingFilter()
    for name, value in headers.items():
        for message in (f"{name}: {value}", str({name: value})):
            record = logging.makeLogRecord({"msg": message, "args": ()})
            assert redactor.filter(record)
            assert value not in record.getMessage()
            assert MASK in record.getMessage()
    record = logging.makeLogRecord({"msg": "headers=%s", "args": (headers,)})
    assert redactor.filter(record)
    assert all(value not in record.getMessage() for value in headers.values())
