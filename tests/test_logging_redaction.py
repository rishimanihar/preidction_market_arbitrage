import io
import json
import logging

import pytest

from pmarb.logging import MASK, RedactingFilter, configure_logging, redact

# Built at runtime so secret scanners do not flag the test source.
_DASHES = "-" * 5
_KIND = "PRIV" + "ATE KEY"
PEM = (
    f"{_DASHES}BEGIN {_KIND}{_DASHES}\nMC4CAQAwBQYDK2VwBCIEIFAKEFAKEFAKE\nline2abc\n"
    f"{_DASHES}END {_KIND}{_DASHES}"
)
B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5YWJjZGVm"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("KALSHI-ACCESS-KEY: key-id-1234", "key-id-1234"),
        ("KALSHI-ACCESS-SIGNATURE: sig/abc+def=", "sig/abc"),
        ("kalshi-access-timestamp=1731360000000", "1731360000000"),
        ("X-PM-Api-Key: pmkey99", "pmkey99"),
        ("X-PM-Signature: pmsig99", "pmsig99"),
        ("Authorization: Bearer tok.en-123", "tok.en-123"),
        ("headers={'KALSHI-ACCESS-KEY': 'kid777', 'Accept': 'x'}", "kid777"),
        ('{"X-PM-Timestamp": "1731360000"}', "1731360000"),
        (f"loaded {PEM} done", "FAKEFAKEFAKE"),
        (f"blob {B64} end", B64),
    ],
)
def test_redact_patterns(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert MASK in out


def test_redact_keeps_ordinary_text() -> None:
    assert redact("fetched 12 markets in 0.4s") == "fetched 12 markets in 0.4s"
    assert redact("Accept: application/json") == "Accept: application/json"


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger("pmarb.test")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_filter_msg_args_and_exc() -> None:
    logger, stream = make_logger()
    logger.info("hdr KALSHI-ACCESS-SIGNATURE: topsecretsig")
    logger.info("args %s and %s", "Authorization: Bearer abc123", {"X-PM-Api-Key": "pmk"})
    logger.info("tuple %s", ("x", f"blob {B64}"))
    try:
        raise ValueError(f"bad {PEM} and X-PM-Signature: excsig")
    except ValueError:
        logger.exception("failed")
    out = stream.getvalue()
    for leaked in ("topsecretsig", "abc123", "pmk", B64, "FAKEFAKEFAKE", "excsig"):
        assert leaked not in out, leaked
    assert "Traceback" in out and "failed" in out


def test_non_string_args_preserved_for_numeric_format() -> None:
    logger, stream = make_logger()
    logger.info("count=%d ratio=%.2f", 3, 0.5)
    assert "count=3 ratio=0.50" in stream.getvalue()


def test_configure_logging_emits_redacted_json() -> None:
    stream = io.StringIO()
    root = configure_logging("INFO", stream=stream)
    try:
        logging.getLogger("pmarb.cfg").info("Authorization: Bearer verysecret")
        try:
            raise RuntimeError("KALSHI-ACCESS-KEY: leakedkey")
        except RuntimeError:
            logging.getLogger("pmarb.cfg").exception("boom")
    finally:
        for h in [h for h in root.handlers if h.get_name() == "pmarb"]:
            root.removeHandler(h)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["level"] == "INFO" and "verysecret" not in lines[0]["msg"]
    assert "leakedkey" not in lines[1]["exc"]
