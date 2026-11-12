"""Structured JSON-ish logging with a redacting filter."""

import json
import logging as _logging
import re
import sys
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
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = _redact_value(record.args)
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


def configure_logging(level: int | str = "INFO", stream: Any = None) -> _logging.Logger:
    """Install a JSON handler with the redacting filter on the root logger."""
    handler = _logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    handler.set_name("pmarb")
    root = _logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == "pmarb"]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    return root
