import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmarb import diagnostics

# POSIX permission bits do not exist on Windows; tests/test_windows.py covers the ACL checks.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def test_macos_sntp_offset_is_reported_and_warns_above_threshold() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="-0.251000 +/- 0.001000 time.apple.com")

    result = diagnostics._clock_status(runner, "Darwin")
    assert calls == [["sntp", "-t", "3", "time.apple.com"]]
    assert result == ["WARN clock offset -251.0 ms (sntp) exceeds 250 ms"]


def test_macos_network_time_fallback_reports_unknown_offset() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[0] == "sntp":
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout="Network Time: On")

    assert diagnostics._clock_status(runner, "Darwin") == [
        "WARN clock offset unknown; network time is enabled"
    ]
    assert calls[-1] == ["systemsetup", "-getusingnetworktime"]


def test_linux_ntp_and_offset_status_are_reported() -> None:
    seen: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(command)
        return SimpleNamespace(returncode=0, stdout="yes\n")

    assert diagnostics._clock_status(runner, "Linux") == [
        "INFO clock synchronized: yes (timedatectl)",
        "WARN clock offset unknown",
    ]
    assert seen == [["timedatectl", "show", "-p", "NTPSynchronized", "--value"]]


def test_linux_synchronized_clock_has_no_clock_warning() -> None:
    findings = diagnostics._clock_status(
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="yes"), "Linux"
    )
    assert findings[0] == "INFO clock synchronized: yes (timedatectl)"
    assert "WARN system clock is not synchronized" not in " ".join(findings)


def test_missing_or_timed_out_clock_command_is_unknown() -> None:
    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("timedatectl")

    def timed_out(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("timedatectl", 5)

    assert diagnostics._clock_status(missing, "Linux") == [
        "WARN clock synchronization unknown; timedatectl unavailable"
    ]
    assert diagnostics._clock_status(timed_out, "Linux") == [
        "WARN clock synchronization unknown; timedatectl unavailable"
    ]


def test_deployment_checks_require_logs_and_exact_env_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("synthetic=true\n", encoding="utf-8")
    env_file.chmod(0o640)

    findings = diagnostics.deployment_checks(
        data_dir,
        env_file,
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
        system="Unknown",
    )
    assert any(item.startswith("FAIL logs directory not found") for item in findings)
    assert any(item.startswith("FAIL env file must have exact mode 600") for item in findings)
    assert any(item.startswith("WARN clock") for item in findings)


@posix_only
def test_deployment_checks_accept_logs_and_env_mode_600(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    env_file = tmp_path / ".env"
    env_file.write_text("synthetic=true\n", encoding="utf-8")
    env_file.chmod(0o600)
    findings = diagnostics.deployment_checks(
        data_dir,
        env_file,
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="yes"),
        system="Linux",
    )
    assert not any(item.startswith("FAIL") for item in findings)
    assert any("disk headroom" in item for item in findings)


def test_low_disk_headroom_fails_using_logs_directory(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    seen: list[Path] = []

    def disk_usage(path: Path) -> SimpleNamespace:
        seen.append(path)
        return SimpleNamespace(free=1024)

    monkeypatch.setattr(diagnostics.shutil, "disk_usage", disk_usage)
    findings = diagnostics.deployment_checks(
        data_dir,
        tmp_path / "absent.env",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="yes"),
        system="Linux",
    )
    assert seen == [logs]
    assert any(item.startswith("FAIL disk headroom below 2 GiB") for item in findings)
