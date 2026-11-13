"""Structured JSON-ish logging with a redacting filter."""

import json
import logging as _logging
import os
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

MASK = "[REDACTED]"

_HEADER_NAMES = r"(?:KALSHI-ACCESS-(?:KEY|SIGNATURE|TIMESTAMP)|X-PM-[A-Za-z0-9_-]+|Authorization)"
# Matches `Name: value`, `Name=value`, `'Name': 'value'` and `"Name": "value"`.
_HEADER_RE = re.compile(
    rf"(?P<name>{_HEADER_NAMES})(?P<sep>[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>(?:Bearer|Basic)\s+[^\s\"',}]+|[^\s\"',}]+)",
    re.IGNORECASE,
)
_HEADER_NAME_RE = re.compile(_HEADER_NAMES, re.IGNORECASE)
_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL)
_B64_RE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    sentinel = "\x00"
    ordered = sorted(
        (item for item in secrets if item and sentinel not in item),
        key=len,
        reverse=True,
    )
    for secret in ordered:
        text = text.replace(secret, sentinel)
    text = text.replace(sentinel, MASK)
    text = _PEM_RE.sub(MASK, text)
    text = _HEADER_RE.sub(lambda m: f"{m['name']}{m['sep']}{MASK}", text)
    return _B64_RE.sub(MASK, text)


def _redact_value(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: _redact_pair(k, v, secrets) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_value(v, secrets) for v in value)
    if isinstance(value, list):
        return [_redact_value(v, secrets) for v in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return redact(str(value), secrets)


def _redact_pair(key: Any, value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(key, str) and _HEADER_NAME_RE.fullmatch(key):
        return MASK
    return _redact_value(value, secrets)


class RedactingFilter(_logging.Filter):
    """Mask secrets in the message, args and exception text of every record."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = tuple(item for item in secrets if item)

    def filter(self, record: _logging.LogRecord) -> bool:
        secrets = self.secrets
        if record.args:
            try:
                record.msg = redact(record.getMessage(), secrets)
                record.args = ()
            except (TypeError, ValueError):
                if isinstance(record.msg, str):
                    record.msg = redact(record.msg, secrets)
                record.args = _redact_value(record.args, secrets)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg, secrets)
        if record.exc_info and record.exc_info[1] is not None:
            formatted = _logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted, secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, secrets)
        return True


def _configured_secrets() -> tuple[str, ...]:
    """Exact secret strings from settings, for the redacting filter."""
    try:
        from pmarb.config import load_settings

        values = load_settings().secret_values()
    except Exception:  # noqa: BLE001 - logging must start when settings cannot be read
        try:
            from pmarb.config import Settings

            values = Settings(_env_file=None).secret_values()
        except Exception:  # noqa: BLE001 - logging must start when settings cannot be read
            return ()
    return tuple(value for value in values if value)


def _make_private_dir(directory: Path) -> None:
    """Create ``directory`` as mode 0o700, ignoring the process umask."""
    previous = os.umask(0o077)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    finally:
        os.umask(previous)
    os.chmod(directory, 0o700)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating file handler that creates the log at mode 0o600."""

    def _open(self) -> Any:
        previous = os.umask(0o077)
        try:
            stream = super()._open()
        finally:
            os.umask(previous)
        os.chmod(self.baseFilename, 0o600)
        return stream


def _prepare_handler(handler: _logging.Handler, secrets: tuple[str, ...]) -> _logging.Handler:
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(secrets))
    handler.set_name("pmarb")
    return handler


class JsonFormatter(_logging.Formatter):
    def format(self, record: _logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def configure_logging(
    level: int | str = "INFO",
    stream: Any = None,
    *,
    log_dir: Path | str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> _logging.Logger:
    """Install redacted JSON logging, rotating in ``log_dir`` when supplied.

    File mode is intentionally file-only so launchd and other service managers
    do not accumulate an unbounded stdout/stderr log. Defaults cap the active
    file at 10 MiB and retain five rotated files.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if backup_count < 1:
        raise ValueError("backup_count must be positive")
    if log_dir is None:
        log_dir = os.environ.get("PMARB_LOG_DIR")
    secrets = _configured_secrets()
    if log_dir is None:
        handler: _logging.Handler = _logging.StreamHandler(stream or sys.stderr)
    else:
        directory = Path(log_dir).expanduser()
        _make_private_dir(directory)
        handler = _PrivateRotatingFileHandler(
            directory / "pmarb.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    _prepare_handler(handler, secrets)
    root = _logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == "pmarb"]:
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    root.setLevel(level)
    return root
