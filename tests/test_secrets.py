import os
import subprocess
from pathlib import Path

import pytest

from pmarb.secrets import (
    SecretPolicyError,
    assert_no_tracked_secrets,
    check_key_file,
    load_private_key,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    d = tmp_path / "keys"
    d.mkdir()
    return d


def make_key(directory: Path, mode: int = 0o600) -> Path:
    key = directory / "k.pem"
    key.write_bytes(b"synthetic-key-bytes")
    key.chmod(mode)
    return key


def test_valid_key_loads(repo: Path, outside: Path) -> None:
    key = make_key(outside)
    assert load_private_key(key, repo) == b"synthetic-key-bytes"
    assert check_key_file(key, repo) == key.resolve()


def test_missing(repo: Path, outside: Path) -> None:
    with pytest.raises(SecretPolicyError, match="not found"):
        check_key_file(outside / "nope.pem", repo)


def test_inside_repo(repo: Path) -> None:
    key = make_key(repo)
    with pytest.raises(SecretPolicyError, match="outside the repository"):
        check_key_file(key, repo)


def test_symlink_into_repo_refused(repo: Path, outside: Path) -> None:
    real = make_key(repo)
    link = outside / "link.pem"
    link.symlink_to(real)
    with pytest.raises(SecretPolicyError, match="outside the repository"):
        check_key_file(link, repo)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o666])
def test_loose_permissions(repo: Path, outside: Path, mode: int) -> None:
    key = make_key(outside, mode)
    with pytest.raises(SecretPolicyError, match="too open"):
        check_key_file(key, repo)


def test_owner_only_variants_ok(repo: Path, outside: Path) -> None:
    check_key_file(make_key(outside, 0o400), repo)


def test_not_regular_file(repo: Path, outside: Path) -> None:
    with pytest.raises(SecretPolicyError, match="not a regular file"):
        check_key_file(outside, repo)


def test_error_never_contains_contents(repo: Path, outside: Path) -> None:
    key = make_key(outside, 0o644)
    with pytest.raises(SecretPolicyError) as info:
        check_key_file(key, repo)
    assert "synthetic-key-bytes" not in str(info.value)


def git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def test_tracked_secret_detected(repo: Path) -> None:
    git(repo, "init", "-q")
    (repo / "config.txt").write_text("value is abc-SECRET-123 here\n")
    (repo / "clean.txt").write_text("nothing\n")
    git(repo, "add", ".")
    with pytest.raises(SecretPolicyError, match="config.txt") as info:
        assert_no_tracked_secrets(repo, ["abc-SECRET-123"])
    assert "abc-SECRET-123" not in str(info.value)


def test_untracked_secret_ignored(repo: Path) -> None:
    git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("ok\n")
    git(repo, "add", ".")
    (repo / "untracked.env").write_text("abc-SECRET-123\n")
    assert_no_tracked_secrets(repo, ["abc-SECRET-123"])


def test_no_values_is_noop(repo: Path) -> None:
    assert_no_tracked_secrets(repo, [])
    assert_no_tracked_secrets(repo, [""])


def test_not_a_git_repo_skipped(repo: Path) -> None:
    (repo / "a.txt").write_text("abc-SECRET-123")
    assert_no_tracked_secrets(repo, ["abc-SECRET-123"])
