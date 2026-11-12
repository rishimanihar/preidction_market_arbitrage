from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from pmarb.config import Settings, load_settings

ENV_KEYS = [
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "POLYUS_API_KEY_ID",
    "POLYUS_PRIVATE_KEY_PATH",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith("PMARB_") or name in ENV_KEYS:
            monkeypatch.delenv(name)


def test_defaults_match_plan() -> None:
    s = Settings(_env_file=None)
    assert s.data_dir == Path("./data")
    assert s.kalshi_api_key_id is None and s.polyus_theta is None
    assert s.kalshi_member_precision is None
    assert (s.kalshi_books_rps, s.kalshi_discovery_rps) == (3.0, 1.0)
    assert (s.polyus_books_rps, s.polyus_discovery_rps) == (4.0, 1.0)
    assert (s.http_connect_timeout_s, s.http_read_timeout_s) == (3.0, 5.0)
    assert s.raw_log_max_days == 30 and s.raw_log_max_bytes == 20 * 1024**3
    assert s.min_touch_qty == 10 and s.display_tz == "America/Chicago"
    assert s.max_polled_groups == 8 and s.stream_max_age_s == 2.0
    assert s.skew_indicative_s == 1.0 and s.skew_qualified_s == 0.25
    assert s.review_mode == "auto"
    assert s.discovery_interval_s == 900.0


def test_env_vars_prefixed_and_venue_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PMARB_DATA_DIR", "/tmp/x")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid-123")
    monkeypatch.setenv("POLYUS_PRIVATE_KEY_PATH", "/keys/p.pem")
    monkeypatch.setenv("PMARB_POLYUS_THETA", "0.075")
    monkeypatch.setenv("PMARB_KALSHI_MEMBER_PRECISION", "0.01")
    s = Settings(_env_file=None)
    assert s.data_dir == Path("/tmp/x")
    assert s.kalshi_api_key_id is not None and s.kalshi_api_key_id.get_secret_value() == "kid-123"
    assert s.polyus_private_key_path == Path("/keys/p.pem")
    assert s.polyus_theta == Decimal("0.075")
    assert s.kalshi_member_precision == "0.01"


def test_env_file_with_inline_comment_and_blank_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "PMARB_DATA_DIR=./data   # local only\nKALSHI_API_KEY_ID=\nKALSHI_PRIVATE_KEY_PATH=~/k.pem\n"
    )
    s = load_settings(env)
    assert s.data_dir == Path("./data")
    assert s.kalshi_api_key_id is None
    assert s.kalshi_private_key_path == Path("~/k.pem").expanduser()


@pytest.mark.parametrize("theta", ["0.095", "0.085", "0.075", "0.0695"])
def test_theta_allowed(theta: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PMARB_POLYUS_THETA", theta)
    assert Settings(_env_file=None).polyus_theta == Decimal(theta)


@pytest.mark.parametrize("theta", ["0.07", "0.1", "abc"])
def test_theta_rejected(theta: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PMARB_POLYUS_THETA", theta)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_precision_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PMARB_KALSHI_MEMBER_PRECISION", "0.001")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_repr_never_shows_key_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_API_KEY_ID", "super-secret-id")
    monkeypatch.setenv("POLYUS_API_KEY_ID", "other-secret-id")
    s = Settings(_env_file=None)
    text = repr(s) + str(s) + s.model_dump_json()
    assert "super-secret-id" not in text and "other-secret-id" not in text
    assert s.secret_values() == ["super-secret-id", "other-secret-id"]
