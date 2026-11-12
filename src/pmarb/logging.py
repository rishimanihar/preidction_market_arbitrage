"""Structured JSON-ish logging with a redacting filter."""

import json
import logging as _logging
import os
import re
import sys
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


def redact(text: str) -> str:
    text = _PEM_RE.sub(MASK, text)
    text = _HEADER_RE.sub(lambda m: f"{m['name']}{m['sep']}{MASK}", text)
    return _B64_RE.sub(MASK, text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_pair(k, v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return redact(str(value))


def _redact_pair(key: Any, value: Any) -> Any:
    if isinstance(key, str) and _HEADER_NAME_RE.fullmatch(key):
        return MASK
    return _redact_value(value)


class RedactingFilter(_logging.Filter):
    """Mask secrets in the message, args and exception text of every record."""

    def filter(self, record: _logging.LogRecord) -> bool:
        if record.args:
            try:
                record.msg = redact(record.getMessage())
                record.args = ()
            except (TypeError, ValueError):
                if isinstance(record.msg, str):
                    record.msg = redact(record.msg)
                record.args = _redact_value(record.args)
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.exc_info and record.exc_info[1] is not None:
            formatted = _logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(formatted)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


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
    if log_dir is None:
        handler: _logging.Handler = _logging.StreamHandler(stream or sys.stderr)
    else:
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "pmarb.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    handler.set_name("pmarb")
    root = _logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == "pmarb"]:
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    root.setLevel(level)
    return root
