"""CLI output routing and JSON contract for the local M1 report."""

import json
import os
from pathlib import Path

import pytest

from pmarb import cli
from pmarb.config import Settings
from pmarb.storage.db import open_writer


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


def _empty_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    open_writer(path).close()


def test_json_cli_writes_only_to_explicit_path_and_has_stable_schema(tmp_path, monkeypatch):
    data_dir = tmp_path / "configured-data"
    db_path = tmp_path / "input.sqlite"
    output = tmp_path / "chosen" / "report.json"
    _empty_db(db_path)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=data_dir))
    monkeypatch.setattr(cli, "_repo_root", lambda: None)

    with pytest.raises(SystemExit) as result:
        cli.main(["report", "--db", str(db_path), "--format", "json", "--out", str(output)])

    assert result.value.code == 0
    assert output.is_file()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) == {
        "methodology",
        "window",
        "coverage",
        "opportunities",
        "episodes",
        "fees",
        "exception_risk",
        "verdict_inputs",
    }
    assert not data_dir.exists()
    assert not list(data_dir.rglob("*")) if data_dir.exists() else True
    assert sorted(
        p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.name == "report.json"
    ) == [Path("chosen/report.json")]


def test_default_output_is_under_data_reports(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    _empty_db(settings.data_dir / "pmarb.sqlite")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "_repo_root", lambda: None)

    with pytest.raises(SystemExit) as result:
        cli.main(["report", "--format", "json"])

    assert result.value.code == 0
    printed = capsys.readouterr().out.strip()
    output = Path(printed)
    assert output.parent == settings.data_dir / "reports"
    assert output.suffix == ".json"
    assert (
        json.loads(output.read_text(encoding="utf-8"))["opportunities"]["overall"]["assessments"]
        == 0
    )
    assert list(settings.data_dir.rglob("m1-report-*.json")) == [output]


def test_stdout_json_does_not_create_an_output_file(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "input.sqlite"
    _empty_db(db_path)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=tmp_path / "unused"))
    monkeypatch.setattr(cli, "_repo_root", lambda: None)

    with pytest.raises(SystemExit) as result:
        cli.main(["report", "--db", str(db_path), "--format", "json", "--out", "-"])

    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["opportunities"]["overall"]["assessments"] == 0
    assert not (tmp_path / "unused").exists()
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith(("-shm", "-wal"))) == [
        "input.sqlite"
    ]


@pytest.mark.parametrize("through_symlink", [False, True])
def test_explicit_output_cannot_target_checkout_docs_even_through_symlink(
    tmp_path, monkeypatch, through_symlink
):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    docs = checkout / "docs"
    docs.mkdir()
    output_dir = checkout / "alias"
    if through_symlink:
        output_dir.symlink_to(docs, target_is_directory=True)
    else:
        output_dir = docs
    db_path = tmp_path / "input.sqlite"
    _empty_db(db_path)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=tmp_path / "data"))
    monkeypatch.setattr(cli, "_repo_root", lambda: checkout)

    with pytest.raises(SystemExit) as result:
        cli.main(
            [
                "report",
                "--db",
                str(db_path),
                "--out",
                str(output_dir / "report.md"),
            ]
        )

    assert result.value.code == 2
    assert not (docs / "report.md").exists()


def test_default_output_rejects_reports_symlink_into_tracked_source(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "src").mkdir()
    data_dir = checkout / "data"
    data_dir.mkdir()
    _empty_db(data_dir / "pmarb.sqlite")
    (data_dir / "reports").symlink_to(checkout / "src", target_is_directory=True)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=data_dir))
    monkeypatch.setattr(cli, "_repo_root", lambda: checkout)

    with pytest.raises(SystemExit) as result:
        cli.main(["report", "--format", "json"])

    assert result.value.code == 1
    assert list((checkout / "src").iterdir()) == []


def test_markdown_limits_table_width_and_keeps_a_five_line_verdict():
    from decimal import Decimal

    from pmarb.analysis.report import render_json, render_markdown

    metrics = {
        "verdict_inputs": {
            "live_net_positive_qty10_fraction": Decimal("0.1234567890123456789"),
            "median_episode_duration_s": Decimal(30),
            "median_depth": Decimal(20),
            "skew_gt_250ms_share": Decimal("0.25"),
        },
        "window": {"duration_seconds": Decimal("0.1234567890123456789")},
        "opportunities": {
            "by_class": {"indicative": {f"metric_{index}": Decimal(index) for index in range(12)}}
        },
    }
    markdown = render_markdown(metrics)
    tables = [line for line in markdown.splitlines() if line.startswith("|")]
    assert all(line.count("|") - 1 <= 8 for line in tables)
    assert markdown.splitlines()[0:2] == ["# M1 Soak Analysis", ""]
    assert len([line for line in markdown.splitlines() if line.startswith("- What ")]) == 4
    assert "0.12345679" in markdown
    assert (
        json.loads(render_json(metrics))["verdict_inputs"]["live_net_positive_qty10_fraction"]
        == "0.1234567890123456789"
    )


@pytest.mark.parametrize("hard_link", [False, True])
def test_cli_refuses_output_that_is_input_database_or_hard_link(tmp_path, monkeypatch, hard_link):
    db_path = tmp_path / "input.sqlite"
    _empty_db(db_path)
    original = db_path.read_bytes()
    output = tmp_path / "output.sqlite" if hard_link else db_path
    if hard_link:
        os.link(db_path, output)
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=tmp_path / "unused"))
    monkeypatch.setattr(cli, "_repo_root", lambda: None)

    with pytest.raises(SystemExit) as result:
        cli.main(["report", "--db", str(db_path), "--format", "json", "--out", str(output)])

    assert result.value.code == 2
    assert db_path.read_bytes() == original
    if hard_link:
        assert output.read_bytes() == original
    assert not (tmp_path / "unused").exists()
