"""Render deployment templates with platform-specific path quoting."""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path


def systemd_quote(value: str) -> str:
    if any(char in value for char in "\r\n\0"):
        raise ValueError("paths cannot contain line breaks or NUL bytes")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def systemd_literal_path(value: str) -> str:
    """Escape unit specifiers while preserving a raw path directive value."""
    if any(char in value for char in "\r\n\0"):
        raise ValueError("paths cannot contain line breaks or NUL bytes")
    return value.replace("%", "%%")


def render(template: Path, output: Path, platform: str, values: dict[str, str]) -> None:
    content = template.read_text(encoding="utf-8")
    replacements: dict[str, str] = {}
    for key, value in values.items():
        if any(char in value for char in "\r\n\0"):
            raise ValueError("paths cannot contain line breaks or NUL bytes")
        if platform == "macos":
            replacements[key] = html.escape(value, quote=True)
        elif key == "REPO_DIR":
            # WorkingDirectory is parsed as a raw path by systemd, not as a
            # quoted command-line argument. Quotes would become path bytes.
            replacements[key] = systemd_literal_path(value)
        elif key in {"DATA_ENV", "LOG_ENV"}:
            replacements[key] = systemd_quote(value)
        else:
            replacements[key] = systemd_quote(value)

    def replace_placeholder(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in replacements:
            raise ValueError(f"unknown template placeholder: {key}")
        return replacements[key]

    content = re.sub(r"@([A-Z_]+)@", replace_placeholder, content)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    output.chmod(0o600)


def main() -> int:
    if len(sys.argv) < 5:
        print("usage: render_template.py PLATFORM TEMPLATE OUTPUT NAME=VALUE...", file=sys.stderr)
        return 2
    platform, template_name, output_name, *raw_values = sys.argv[1:]
    template, output = Path(template_name), Path(output_name)
    values = dict(item.split("=", 1) for item in raw_values)
    render(template, output, platform, values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
