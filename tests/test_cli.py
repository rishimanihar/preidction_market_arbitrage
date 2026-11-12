"""CLI behavior, with no server or network processes started."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmarb import cli
from pmarb.config import Settings
from pmarb.storage.db import open_writer


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("PMARB_", "KALSHI_", "POLYUS_")):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _mock_streamlit_import(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = cli.importlib.import_module

    def mock_import(name: str, package: str | None = None) -> object:
        if name == "streamlit":
            return object()
        return real_import(name, package)

    monkeypatch.setattr(cli.importlib, "import_module", mock_import)


def _script() -> Path:
    return Path(sys.executable).with_name("pmarb")


def test_version() -> None:
    completed = subprocess.run(
        [_script(), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"pmarb {version('pmarb')}"


def test_replay_is_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["replay"])
    assert raised.value.code == 2
    assert capsys.readouterr().out.strip() == "not implemented yet (phase 2)"


def _make_database(data_dir: Path, version: int = 1) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "pmarb.sqlite"
    with closing(open_writer(db_path)) as conn:
        conn.execute("UPDATE schema_version SET version = ?", (version,))


def test_doctor_happy_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "outside" / "data"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _make_database(data_dir)
    assert cli._doctor(Settings(data_dir=data_dir), repo_root) == []


def test_doctor_command_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "external-data"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _make_database(data_dir)
    monkeypatch.setenv("PMARB_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cli, "_repo_root", lambda: repo_root)
    cli.main(["doctor"])
    assert capsys.readouterr().out.startswith("OK config")


def test_doctor_command_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setenv("PMARB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "_repo_root", lambda: repo_root)
    with pytest.raises(SystemExit) as raised:
        cli.main(["doctor"])
    assert raised.value.code == 1
    assert "database not found" in capsys.readouterr().err


def test_doctor_reports_missing_database_and_key(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    data_dir = tmp_path / "external-data"
    bad_key = tmp_path / "bad.pem"
    bad_key.write_text("never print key material", encoding="utf-8")
    errors = cli._doctor(Settings(data_dir=data_dir, kalshi_private_key_path=bad_key), repo_root)
    joined = " ".join(errors)
    assert "database not found" in joined
    assert "key file check failed" in joined
    assert "never print key material" not in joined


def test_doctor_rejects_wrong_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _make_database(data_dir, version=9)
    assert any(
        "schema version must be 1" in e for e in cli._doctor(Settings(data_dir=data_dir), repo_root)
    )


@pytest.mark.parametrize("rows", [[], [1, 1]])
def test_doctor_rejects_missing_or_extra_schema_rows(tmp_path: Path, rows: list[int]) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with closing(sqlite3.connect(data_dir / "pmarb.sqlite")) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.executemany("INSERT INTO schema_version VALUES (?)", [(row,) for row in rows])
    errors = cli._doctor(Settings(data_dir=data_dir), tmp_path / "repo")
    assert any("schema version must be 1" in error for error in errors)


@pytest.mark.parametrize("ddl", [None, "CREATE TABLE schema_version (wrong_column INTEGER)"])
def test_doctor_rejects_missing_or_malformed_schema_table(tmp_path: Path, ddl: str | None) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with closing(sqlite3.connect(data_dir / "pmarb.sqlite")) as conn:
        if ddl is not None:
            conn.execute(ddl)
    errors = cli._doctor(Settings(data_dir=data_dir), tmp_path / "repo")
    assert any("database schema check failed" in error for error in errors)


def _git_repo_with_data_ignore(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("data/\n", encoding="utf-8")
    return repo


def test_data_dir_policy_accepts_gitignored_repo_data(tmp_path: Path) -> None:
    repo = _git_repo_with_data_ignore(tmp_path)
    assert cli._check_data_dir(repo / "data" / "nested", repo) == []


def test_data_dir_policy_detects_target_repository_when_cwd_is_elsewhere(tmp_path: Path) -> None:
    repo = _git_repo_with_data_ignore(tmp_path)
    assert cli._check_data_dir(repo / "data", None) == []
    assert cli._check_data_dir(repo / "src" / "runtime", tmp_path / "different-repo")
    assert not (repo / "src").exists()


def test_data_dir_policy_rejects_nonignored_repo_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    data_dir = repo / "data"
    errors = cli._check_data_dir(data_dir, repo)
    assert any("not gitignored" in error for error in errors)
    assert not data_dir.exists()


def test_data_dir_policy_rejects_symlink_escape(tmp_path: Path) -> None:
    repo = _git_repo_with_data_ignore(tmp_path)
    (repo / "data").symlink_to(repo / "src", target_is_directory=True)
    errors = cli._check_data_dir(repo / "data" / "nested", repo)
    assert any("symlink" in error for error in errors)
    assert not (repo / "src").exists()


def test_data_dir_policy_rejects_external_alias_into_repo(tmp_path: Path) -> None:
    repo = _git_repo_with_data_ignore(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(repo / "src", target_is_directory=True)
    assert cli._check_data_dir(alias / "nested", repo)
    assert not (repo / "src").exists()


def test_data_dir_policy_reports_unwritable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "external-data"

    def fail_write(*args: object, **kwargs: object) -> None:
        raise PermissionError("no write access")

    monkeypatch.setattr(cli.tempfile, "NamedTemporaryFile", fail_write)
    assert any(
        "not writable" in error for error in cli._check_data_dir(data_dir, tmp_path / "repo")
    )


def test_data_dir_policy_reports_no_free_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert any(
        "no free disk space" in error for error in cli._check_data_dir(tmp_path / "data", None)
    )


def test_doctor_reports_unavailable_streamlit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _make_database(data_dir)
    real_import = cli.importlib.import_module

    def unavailable(name: str, package: str | None = None) -> object:
        if name == "streamlit":
            raise ModuleNotFoundError("not installed")
        return real_import(name, package)

    monkeypatch.setattr(cli.importlib, "import_module", unavailable)
    assert any(
        "Streamlit import failed" in error
        for error in cli._doctor(Settings(data_dir=data_dir), tmp_path / "repo")
    )


def test_doctor_config_error_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PMARB_POLYUS_THETA", "sensitive-value")
    monkeypatch.setenv("PMARB_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit) as raised:
        cli.main(["doctor"])
    assert raised.value.code == 1
    assert "sensitive-value" not in capsys.readouterr().err


def test_review_uses_loopback_and_requested_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = tmp_path / "main.py"
    entry.touch()
    monkeypatch.setattr(cli, "_load_review_app", lambda: entry)
    called: dict[str, object] = {}

    def fake_run(command: list[str], *, check: bool) -> SimpleNamespace:
        called["command"] = command
        called["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._run_review(9123) == 0
    assert called["command"] == [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(entry),
        "--server.address=127.0.0.1",
        "--server.port=9123",
    ]
    assert called["check"] is False


def test_review_port_argument_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(review_port=8123))
    monkeypatch.setattr(cli, "_run_review", lambda port: port)
    with pytest.raises(SystemExit) as raised:
        cli.main(["review", "--port", "9124"])
    assert raised.value.code == 9124


def test_review_uses_configured_port_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(review_port=8123))
    monkeypatch.setattr(cli, "_run_review", lambda port: port)
    with pytest.raises(SystemExit) as raised:
        cli.main(["review"])
    assert raised.value.code == 8123


def test_review_reports_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = tmp_path / "main.py"
    entry.touch()
    monkeypatch.setattr(cli, "_load_review_app", lambda: entry)

    def fail_run(*args: object, **kwargs: object) -> object:
        raise OSError("launch unavailable")

    monkeypatch.setattr(cli.subprocess, "run", fail_run)
    assert cli._run_review(8501) == 1
    assert "could not launch Streamlit" in capsys.readouterr().err
