"""Runtime settings (plan section 11 defaults).

Values come from the process environment (``PMARB_*`` plus the venue key
variables in ``.env.example``) and an optional env file outside the repo,
``~/.config/pmarb/.env``. Key ids are ``SecretStr`` so they never show in a
``repr``; private keys are referenced by path only.
"""

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ENV_FILE = Path("~/.config/pmarb/.env").expanduser()
ALLOWED_POLYUS_THETA = frozenset(Decimal(v) for v in ("0.095", "0.085", "0.075", "0.0695"))


def _alias(name: str) -> AliasChoices:
    return AliasChoices(name, f"PMARB_{name}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PMARB_",
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Path("./data")
    review_port: int = Field(default=8501, ge=1, le=65535)

    kalshi_api_key_id: SecretStr | None = Field(
        default=None, validation_alias=_alias("KALSHI_API_KEY_ID")
    )
    kalshi_private_key_path: Path | None = Field(
        default=None, validation_alias=_alias("KALSHI_PRIVATE_KEY_PATH")
    )
    polyus_api_key_id: SecretStr | None = Field(
        default=None, validation_alias=_alias("POLYUS_API_KEY_ID")
    )
    polyus_private_key_path: Path | None = Field(
        default=None, validation_alias=_alias("POLYUS_PRIVATE_KEY_PATH")
    )

    # Fee parameters. A configured value is the account's. Unset stays unknown
    # when fee_scenario is "none", and otherwise uses the conservative stand-in.
    kalshi_member_precision: Literal["0.0001", "0.01"] | None = None
    polyus_theta: Decimal | None = None
    fee_scenario: Literal["conservative", "none"] = "conservative"

    # Polling
    max_polled_groups: int = Field(default=8, ge=1)
    sample_interval_s: float = Field(default=60.0, gt=0)
    min_poll_cadence_s: float = Field(default=1.0, gt=0)
    polled_group_start_window_min: int = Field(default=30, ge=0)

    # Freshness and skew
    stream_max_age_s: float = Field(default=2.0, gt=0)
    poll_max_age_cadence_multiple: float = Field(default=2.0, gt=0)
    skew_indicative_s: float = Field(default=1.0, gt=0)
    skew_qualified_s: float = Field(default=0.25, gt=0)

    # Rate budgets (requests per second)
    kalshi_books_rps: float = Field(default=3.0, gt=0)
    kalshi_discovery_rps: float = Field(default=3.0, gt=0)
    polyus_books_rps: float = Field(default=4.0, gt=0)
    polyus_discovery_rps: float = Field(default=3.0, gt=0)

    # HTTP
    http_connect_timeout_s: float = Field(default=3.0, gt=0)
    http_read_timeout_s: float = Field(default=5.0, gt=0)
    http_max_body_bytes: int = Field(default=5 * 1024 * 1024, gt=0)

    # Book-poll time budget. One venue's fetch in a poll cycle (token waits,
    # attempts, and backoff included) gets fraction × the batch's freshness
    # window, capped at max. A failing venue then gives up, and its books go
    # invalid, before a healthy venue's books could age out.
    poll_fetch_budget_fraction: float = Field(default=0.75, gt=0, lt=1)
    poll_fetch_budget_max_s: float = Field(default=10.0, gt=0)

    # Raw log retention
    raw_log_max_days: int = Field(default=30, ge=1)
    raw_log_max_bytes: int = Field(default=20 * 1024**3, ge=1)

    min_touch_qty: int = Field(default=10, ge=0)
    display_tz: str = "America/Chicago"

    # D27: auto approves policy-v2 candidates and those groups stay indicative.
    review_mode: Literal["auto", "manual"] = "auto"
    # Catalog refresh. Adapters already spend this on the discovery rate bucket.
    discovery_interval_s: float = Field(default=900.0, gt=0)
    # Plan §6.1: re-fetch rules on watched markets. The catalog stays at 15 min.
    metadata_refresh_interval_s: float = Field(default=120.0, gt=0)

    @field_validator(
        "kalshi_member_precision",
        "kalshi_api_key_id",
        "polyus_api_key_id",
        "kalshi_private_key_path",
        "polyus_private_key_path",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("review_mode", mode="before")
    @classmethod
    def _review_mode(cls, v: object) -> object:
        if v is None or v == "":
            return "auto"
        return v

    @field_validator("fee_scenario", mode="before")
    @classmethod
    def _fee_scenario(cls, v: object) -> object:
        if v is None or v == "":
            return "conservative"
        return v

    @field_validator("polyus_theta", mode="before")
    @classmethod
    def _theta(cls, v: object) -> object:
        if v is None or v == "":
            return None
        try:
            theta = Decimal(str(v))
        except ArithmeticError as exc:
            raise ValueError("polyus_theta must be a decimal number") from exc
        if theta not in ALLOWED_POLYUS_THETA:
            allowed = ", ".join(sorted(str(t) for t in ALLOWED_POLYUS_THETA))
            raise ValueError(f"polyus_theta must be one of: {allowed}")
        return theta

    @field_validator("data_dir", "kalshi_private_key_path", "polyus_private_key_path")
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None

    def secret_values(self) -> list[str]:
        """Configured secret values, for ``assert_no_tracked_secrets``."""
        ids = (self.kalshi_api_key_id, self.polyus_api_key_id)
        return [s.get_secret_value() for s in ids if s is not None]


def load_settings(env_file: Path | str | None = None) -> Settings:
    """Load settings; an explicit path overrides ``PMARB_ENV_FILE`` and default."""
    selected = env_file if env_file is not None else os.environ.get("PMARB_ENV_FILE")
    if selected is None:
        selected = DEFAULT_ENV_FILE
    return Settings(_env_file=selected)
