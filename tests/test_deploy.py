from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
RENDERER = DEPLOY / "render_template.py"
INSTALLER = DEPLOY / "install.sh"
ENV_EXAMPLE = ROOT / ".env.example"
# install.sh is the macOS/Linux installer; deploy/windows/install.ps1 is tested in test_windows.py.
posix_installer = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell installer")


def test_env_example_has_no_inline_assignment_comments() -> None:
    assignments = [
        line
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    ]
    assert assignments
    assert all("#" not in line for line in assignments)


def render(platform: str, template: Path, output: Path, **values: str) -> str:
    args = [sys.executable, str(RENDERER), platform, str(template), str(output)]
    args.extend(f"{key}={value}" for key, value in values.items())
    subprocess.run(args, check=True, capture_output=True, text=True)
    return output.read_text(encoding="utf-8")


def test_launchd_template_escapes_paths_and_keeps_secrets_out(tmp_path: Path) -> None:
    content = render(
        "macos",
        DEPLOY / "macos/com.pmarb.collector.plist.template",
        tmp_path / "service.plist",
        REPO_DIR='/tmp/repo & "one"',
        DATA_DIR="/tmp/data <private>",
        LOG_DIR="/tmp/data <private>/logs",
        PYTHON_BIN="/tmp/repo/.venv/bin/python",
        ENV_FILE="/home/person/.config/pmarb/.env",
    )
    assert "@" not in content
    assert "/tmp/repo &amp; &quot;one&quot;" in content
    assert "/tmp/data &lt;private&gt;" in content
    assert "PMARB_ENV_FILE" in content
    assert "EnvironmentVariables" in content
    assert "<key>ExitTimeOut</key>\n  <integer>60</integer>" in content
    assert "KALSHI_API_KEY_ID" not in content
    assert "private-key-material" not in content


def test_systemd_template_quotes_paths_and_contains_hardening(tmp_path: Path) -> None:
    content = render(
        "linux",
        DEPLOY / "linux/pmarb-collector.service.template",
        tmp_path / "service.unit",
        REPO_DIR='/tmp/repo with "quote"',
        DATA_DIR="/tmp/data\\folder%slot/$HOME/@OPAQUE@",
        LOG_DIR="/tmp/data\\folder%slot/$HOME/@OPAQUE@/logs",
        DATA_ENV="PMARB_DATA_DIR=/tmp/data\\folder%slot/$HOME/@OPAQUE@",
        LOG_ENV="PMARB_LOG_DIR=/tmp/data\\folder%slot/$HOME/@OPAQUE@/logs",
        PYTHON_BIN="/tmp/repo with spaces/.venv/bin/python",
        ENV_FILE="/home/person/.config/pmarb/.env",
    )
    assert 'WorkingDirectory=/tmp/repo with "quote"' in content
    assert '"PMARB_DATA_DIR=/tmp/data\\\\folder%%slot/$HOME/@OPAQUE@"' in content
    assert '"PMARB_LOG_DIR=/tmp/data\\\\folder%%slot/$HOME/@OPAQUE@/logs"' in content
    assert "ExecStart=:/usr/bin/env" in content
    assert "EnvironmentFile=%h/.config/pmarb/.env" in content
    assert "Restart=on-failure" in content
    assert "RestartSec=60s" in content
    assert "TimeoutStopSec=60s" in content
    assert "KillSignal=SIGTERM" in content
    assert "NoNewPrivileges=yes" in content
    assert "PrivateTmp=yes" in content
    assert "ProtectSystem=strict" in content
    assert "StandardOutput=journal" in content
    assert "KALSHI_API_KEY_ID" not in content


@posix_installer
@pytest.mark.parametrize(
    ("system", "service_suffix"),
    [
        ("Darwin", "Library/LaunchAgents/com.pmarb.collector.plist"),
        ("Linux", ".config/systemd/user/pmarb-collector.service"),
    ],
)
def test_installer_dry_run_selects_os_and_makes_no_changes(
    tmp_path: Path, system: str, service_suffix: str
) -> None:
    home = tmp_path / "home with spaces"
    repo = tmp_path / "repo with spaces"
    bindir = tmp_path / "mock-bin"
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / ".env.example").write_text("PMARB_DATA_DIR=./data\n", encoding="utf-8")
    for binary in ("pmarb", "python"):
        executable = repo / ".venv/bin" / binary
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    bindir.mkdir()
    uname = bindir / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{system}'\n", encoding="utf-8")
    uname.chmod(0o700)
    data_dir = tmp_path / "data with spaces"
    env = os.environ.copy()
    env.update(HOME=str(home), PATH=f"{bindir}{os.pathsep}{env['PATH']}")

    result = subprocess.run(
        ["sh", str(INSTALLER), "--repo", str(repo), "--data-dir", str(data_dir), "--dry-run"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert f"OS: {'macos' if system == 'Darwin' else 'linux'}" in result.stdout
    assert str(home / service_suffix) in result.stdout
    assert str(data_dir) in result.stdout
    assert "Would run: PMARB_DATA_DIR=" in result.stdout
    assert not home.exists()
    assert not data_dir.exists()


def test_renderer_rejects_newlines_to_prevent_directive_injection(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "linux",
            str(DEPLOY / "linux/pmarb-collector.service.template"),
            str(tmp_path / "unit"),
            "REPO_DIR=/tmp/repo\nExecStart=/bin/evil",
            "DATA_DIR=/tmp/data",
            "LOG_DIR=/tmp/data/logs",
            "PYTHON_BIN=/tmp/python",
            "ENV_FILE=/tmp/env",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert not (tmp_path / "unit").exists()


@posix_installer
def test_linux_install_uses_systemd_user_unit_dir_and_preserves_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    bindir = tmp_path / "mock-bin"
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / ".env.example").write_text("PMARB_DATA_DIR=./data\n", encoding="utf-8")
    pmarb = repo / ".venv/bin/pmarb"
    pmarb.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pmarb.chmod(0o700)
    (repo / ".venv/bin/python").symlink_to(sys.executable)
    bindir.mkdir()
    uname = bindir / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Linux\\n'\n", encoding="utf-8")
    uname.chmod(0o700)
    systemctl = bindir / "systemctl"
    call_log = tmp_path / "systemctl.log"
    systemctl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{call_log}'\n", encoding="utf-8")
    systemctl.chmod(0o700)
    env_dir = home / ".config/pmarb"
    env_dir.mkdir(parents=True)
    env_file = env_dir / ".env"
    env_file.write_text("PMARB_DATA_DIR=/private/existing\n", encoding="utf-8")
    env_file.chmod(0o600)
    env = os.environ.copy()
    env.update(HOME=str(home), PATH=f"{bindir}{os.pathsep}{env['PATH']}")

    for _ in range(2):
        subprocess.run(
            [
                "sh",
                str(INSTALLER),
                "--repo",
                str(repo),
                "--data-dir",
                str(tmp_path / "recordings"),
                "--start",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    unit = home / ".config/systemd/user/pmarb-collector.service"
    content = unit.read_text(encoding="utf-8")
    assert unit.is_file()
    assert f'"PMARB_DATA_DIR={tmp_path}/recordings"' in content
    assert f'"PMARB_LOG_DIR={tmp_path}/recordings/logs"' in content
    assert "/private/existing" not in content
    assert env_file.read_text(encoding="utf-8") == "PMARB_DATA_DIR=/private/existing\n"
    assert "daemon-reload" in call_log.read_text(encoding="utf-8")
    assert "enable --now pmarb-collector.service" in call_log.read_text(encoding="utf-8")

    shutil.rmtree(repo / ".venv")
    subprocess.run(
        [
            "sh",
            str(INSTALLER),
            "--repo",
            str(repo),
            "--data-dir",
            str(tmp_path / "recordings"),
            "--uninstall",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not unit.exists()
    assert "disable --now pmarb-collector.service" in call_log.read_text(encoding="utf-8")


@posix_installer
def test_install_script_is_shellcheck_clean_when_available() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    subprocess.run([shellcheck, str(INSTALLER)], check=True, capture_output=True, text=True)
