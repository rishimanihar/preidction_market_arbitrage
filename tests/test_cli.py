"""Console script placeholders."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest


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


@pytest.mark.parametrize("command", ["scan", "review", "replay"])
def test_commands_are_not_implemented(command: str) -> None:
    completed = subprocess.run(
        [_script(), command],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stdout.strip() == "not implemented yet (phase 2)"
