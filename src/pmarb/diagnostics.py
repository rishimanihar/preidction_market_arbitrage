"""Portable host checks used by ``pmarb doctor``."""

import platform
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from pmarb.osutil import is_windows
from pmarb.secrets import SecretPolicyError, check_key_file

CommandRunner = Callable[..., object]
LOW_DISK_BYTES = 2 * 1024**3
# Zones the collector and review app use. Windows has no IANA database, so
# these load only when the pinned ``tzdata`` package is installed.
REQUIRED_TIMEZONES = ("America/New_York", "America/Chicago")
# w32tm sources that mean "not synchronized to any time server".
_W32TM_UNSYNCED_SOURCES = ("local cmos clock", "free-running system clock")


def _system_name(system: str | None) -> str:
    if system:
        return system
    return "Windows" if is_windows() else platform.system()


def _run(runner: CommandRunner | None, command: list[str]) -> object:
    execute = runner or subprocess.run
    return execute(command, check=False, capture_output=True, text=True, timeout=5)


def _output(result: object) -> str:
    return str(getattr(result, "stdout", "") or "").strip()


def _returncode(result: object) -> int:
    return int(getattr(result, "returncode", 1))


def parse_w32tm_status(text: str) -> dict[str, str]:
    """Parse ``w32tm /query /status`` into lower-case ``label -> value``.

    Lines look like ``Source: time.windows.com,0x9``. The first ``:`` splits
    the label, so values such as ``1:23:45 PM`` stay whole. Labels are English;
    on a localized Windows they will not match and the clock reads as unknown.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        key = label.strip().lower()
        if sep and key and key not in fields:
            fields[key] = value.strip()
    return fields


def _windows_clock_status(runner: CommandRunner | None) -> list[str]:
    try:
        result = _run(runner, ["w32tm", "/query", "/status"])
    except (OSError, subprocess.SubprocessError):
        return ["WARN clock synchronization unknown; w32tm unavailable"]
    text = _output(result)
    fields = parse_w32tm_status(text)
    source = fields.get("source", "")
    if _returncode(result) != 0 or not source:
        first_line = text.splitlines()[0][:160] if text else "no output"
        return [f"WARN clock synchronization unknown; w32tm /query /status failed: {first_line}"]
    last_sync = fields.get("last successful sync time", "unknown")
    leap = fields.get("leap indicator", "")
    if (
        source.lower().startswith(_W32TM_UNSYNCED_SOURCES)
        or leap.startswith("3")
        or last_sync.lower() == "unspecified"
    ):
        return [f"WARN system clock is not synchronized (w32tm source: {source}); offset unknown"]
    return [
        f"INFO clock source {source}, last sync {last_sync} (w32tm)",
        "WARN clock offset unknown",
    ]


def _clock_status(runner: CommandRunner | None, system: str | None = None) -> list[str]:
    system = _system_name(system)
    if system == "Windows":
        return _windows_clock_status(runner)
    if system == "Darwin":
        try:
            result = _run(runner, ["sntp", "-t", "3", "time.apple.com"])
            match = re.search(r"(?<![\w.])([+-]?\d+(?:\.\d+)?)\s*(?:\+/-|\+−)", _output(result))
            if _returncode(result) == 0 and match:
                offset_ms = float(match.group(1)) * 1000
                status = f"INFO clock offset {offset_ms:.1f} ms (sntp)"
                if abs(offset_ms) > 250:
                    status = "WARN " + status.removeprefix("INFO ") + " exceeds 250 ms"
                return [status]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        try:
            result = _run(runner, ["systemsetup", "-getusingnetworktime"])
            output = _output(result).lower()
            if _returncode(result) == 0 and re.search(r"^network time:\s*on$", output):
                return ["WARN clock offset unknown; network time is enabled"]
            if _returncode(result) == 0 and re.search(r"^network time:\s*off$", output):
                return ["WARN network time is disabled; clock offset unknown"]
        except (OSError, subprocess.SubprocessError):
            pass
        return ["WARN clock synchronization or offset unknown"]
    if system == "Linux":
        try:
            result = _run(runner, ["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        except (OSError, subprocess.SubprocessError):
            return ["WARN clock synchronization unknown; timedatectl unavailable"]
        value = _output(result).lower()
        if _returncode(result) == 0 and value in {"yes", "no"}:
            if value == "yes":
                return ["INFO clock synchronized: yes (timedatectl)", "WARN clock offset unknown"]
            return ["WARN system clock is not synchronized; offset unknown"]
        return ["WARN clock synchronization unknown; offset unknown"]
    return [f"WARN clock synchronization and offset unknown on {system}"]


def timezone_findings(loader: Callable[[str], object] | None = None) -> list[str]:
    """FAIL unless every zone in :data:`REQUIRED_TIMEZONES` loads."""
    load = loader if loader is not None else ZoneInfo
    missing: list[str] = []
    for name in REQUIRED_TIMEZONES:
        try:
            load(name)
        except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
            missing.append(name)
    if not missing:
        return []
    zones = ", ".join(missing)
    hint = "reinstall with `python -m pip install -e .` (Windows needs the pinned tzdata package)"
    return [f"FAIL time zone data missing for {zones}; {hint}"]


def _windows_env_file_findings(
    env_file: Path, runner: CommandRunner | None, repo_root: Path | None
) -> list[str]:
    """The Windows stand-in for "mode 600": location and ACL, like key files."""
    if not env_file.exists():
        return []
    warnings: list[str] = []
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        check_key_file(env_file, root, label="env file", runner=runner, warnings=warnings)
    except SecretPolicyError as exc:
        return [f"FAIL {exc}"]
    except OSError as exc:
        return [f"FAIL env file permission check failed: {exc}"]
    return [f"WARN {warning}" for warning in warnings]


def deployment_checks(
    data_dir: Path,
    env_file: Path,
    command_runner: CommandRunner | None = None,
    *,
    system: str | None = None,
    repo_root: Path | None = None,
) -> list[str]:
    """Return doctor findings; INFO/WARN entries are non-fatal to the CLI."""
    system = _system_name(system)
    findings = _clock_status(command_runner, system)
    findings.extend(timezone_findings())
    log_dir = data_dir / "logs"
    logs_present = log_dir.is_dir()
    if not logs_present:
        findings.append(f"FAIL logs directory not found: {log_dir}")

    try:
        free = shutil.disk_usage(log_dir if logs_present else data_dir).free
        findings.append(f"INFO disk headroom {free / 1024**3:.2f} GiB")
        if free < LOW_DISK_BYTES:
            findings.append(f"FAIL disk headroom below 2 GiB: {free} bytes free")
    except OSError as exc:
        findings.append(f"FAIL could not check disk headroom: {exc}")

    if system == "Windows":
        findings.extend(_windows_env_file_findings(env_file, command_runner, repo_root))
        return findings
    try:
        env_stat = env_file.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        findings.append(f"FAIL env file permission check failed: {exc}")
    else:
        if stat.S_IMODE(env_stat.st_mode) != 0o600:
            findings.append(f"FAIL env file must have exact mode 600: {env_file}")
    return findings
