"""Portable host checks used by ``pmarb doctor``."""

import platform
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

CommandRunner = Callable[..., object]
LOW_DISK_BYTES = 2 * 1024**3


def _run(runner: CommandRunner | None, command: list[str]) -> object:
    execute = runner or subprocess.run
    return execute(command, check=False, capture_output=True, text=True, timeout=5)


def _output(result: object) -> str:
    return str(getattr(result, "stdout", "") or "").strip()


def _returncode(result: object) -> int:
    return int(getattr(result, "returncode", 1))


def _clock_status(runner: CommandRunner | None, system: str | None = None) -> list[str]:
    system = system or platform.system()
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


def deployment_checks(
    data_dir: Path,
    env_file: Path,
    command_runner: CommandRunner | None = None,
    *,
    system: str | None = None,
) -> list[str]:
    """Return doctor findings; INFO/WARN entries are non-fatal to the CLI."""
    findings = _clock_status(command_runner, system)
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
