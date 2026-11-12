"""Command line tools for local review and diagnostics."""

import argparse
import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from pmarb import __version__
from pmarb.config import DEFAULT_ENV_FILE, Settings, load_settings
from pmarb.diagnostics import deployment_checks
from pmarb.secrets import SecretPolicyError, check_key_file
from pmarb.storage.db import open_reader

EXPECTED_SCHEMA_VERSION = 1
DEFAULT_REVIEW_PORT = 8501


def _repo_root() -> Path | None:
    """Return this checkout's root, or ``None`` when running outside a checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(result.stdout.strip()).resolve()


def _database_path(data_dir: Path) -> Path:
    """Return the canonical local database path."""
    return data_dir / "pmarb.sqlite"


def _check_data_dir(data_dir: Path, repo_root: Path | None) -> list[str]:
    errors: list[str] = []
    lexical = Path(os.path.abspath(data_dir.expanduser()))
    resolved = lexical.resolve()
    # The configured directory may belong to a different checkout than cwd.
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            repo_root = parent
            break
    in_repo = repo_root is not None and (
        lexical == repo_root
        or repo_root in lexical.parents
        or resolved == repo_root
        or repo_root in resolved.parents
    )
    if in_repo:
        assert repo_root is not None
        required_lexical = repo_root / "data"
        if lexical != required_lexical and required_lexical not in lexical.parents:
            errors.append(f"data dir inside repository must be under gitignored {required_lexical}")
        else:
            relative = lexical.relative_to(required_lexical)
            cursor = required_lexical
            if cursor.is_symlink():
                errors.append(f"data dir must not use a symlink under repository data/: {cursor}")
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    errors.append(
                        f"data dir must not use a symlink under repository data/: {cursor}"
                    )
                    break
            if resolved != lexical and not errors:
                errors.append(f"data dir resolves through a symlink: {lexical}")
            try:
                ignored = subprocess.run(
                    ["git", "check-ignore", "-q", str(lexical) + "/"],
                    cwd=repo_root,
                    check=False,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                errors.append("could not verify that the data dir is gitignored")
            else:
                if ignored.returncode != 0:
                    errors.append(f"data dir is not gitignored: {lexical}")
    if errors:
        return errors
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".pmarb-doctor-", dir=resolved):
            pass
    except OSError as exc:
        errors.append(f"data dir is not writable: {exc}")
    try:
        usage = shutil.disk_usage(resolved)
    except OSError as exc:
        errors.append(f"could not check free disk space: {exc}")
    else:
        if usage.free <= 0:
            errors.append("no free disk space")
    return errors


def _doctor(
    settings: Settings,
    repo_root: Path | None,
    command_runner: object | None = None,
    env_file: Path = DEFAULT_ENV_FILE,
) -> list[str]:
    errors: list[str] = []
    errors.extend(_check_data_dir(settings.data_dir, repo_root))
    for label, key_path in (
        ("Kalshi", settings.kalshi_private_key_path),
        ("Polymarket US", settings.polyus_private_key_path),
    ):
        if key_path is not None:
            try:
                key_repo_root = repo_root or Path(__file__).resolve().parents[2]
                check_key_file(key_path, key_repo_root)
            except (OSError, SecretPolicyError) as exc:
                errors.append(f"{label} key file check failed: {exc}")

    database = _database_path(settings.data_dir)
    if not database.is_file():
        errors.append(f"database not found: {database}")
    else:
        try:
            with closing(open_reader(database)) as conn:
                rows = conn.execute("SELECT version FROM schema_version").fetchall()
                if len(rows) != 1 or rows[0][0] != EXPECTED_SCHEMA_VERSION:
                    errors.append(f"database schema version must be {EXPECTED_SCHEMA_VERSION}")
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"database schema check failed: {exc}")

    try:
        importlib.import_module("streamlit")
    except ImportError as exc:
        errors.append(f"Streamlit import failed: {exc}")
    errors.extend(deployment_checks(settings.data_dir, env_file, command_runner))
    return errors


def _load_review_app() -> Path:
    path = Path(__file__).resolve().parent / "app" / "main.py"
    if not path.is_file():
        raise FileNotFoundError(f"review app entry point not found: {path}")
    return path


def _run_review(port: int, env_file: Path | None = None) -> int:
    try:
        app = _load_review_app()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
    ]
    child_env = None
    if env_file is not None:
        child_env = os.environ.copy()
        child_env["PMARB_ENV_FILE"] = str(env_file.expanduser())
    try:
        return subprocess.run(
            command, check=False, **({"env": child_env} if child_env else {})
        ).returncode
    except OSError as exc:
        print(f"could not launch Streamlit: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pmarb")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", type=Path, default=None, help="settings file path")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="check local configuration and runtime readiness")
    doctor.add_argument(
        "--env-file", type=Path, default=argparse.SUPPRESS, help="settings file path"
    )
    review = commands.add_parser("review", help="launch the local review app")
    review.add_argument("--port", type=int, default=None)
    review.add_argument(
        "--env-file", type=Path, default=argparse.SUPPRESS, help="settings file path"
    )
    commands.add_parser("scan", help="scan markets (collector arrives in PR-6)")
    commands.add_parser("replay", help="replay recorded data")
    args = parser.parse_args(argv)

    if args.command == "scan":
        print("collector arrives in PR-6")
        raise SystemExit(2)
    if args.command == "replay":
        print("not implemented yet (phase 2)")
        raise SystemExit(2)
    if args.command == "review":
        try:
            settings = load_settings(args.env_file)
        except (ValidationError, OSError):
            print(
                "FAIL config: settings are invalid; check the PMARB_* configuration",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        port = (
            args.port
            if args.port is not None
            else getattr(settings, "review_port", DEFAULT_REVIEW_PORT)
        )
        if not 1 <= port <= 65535:
            parser.error("review port must be between 1 and 65535")
        raise SystemExit(_run_review(port, args.env_file))
    if args.command == "doctor":
        try:
            settings = load_settings(args.env_file)
        except (ValidationError, OSError):
            print(
                "FAIL config: settings are invalid; check the PMARB_* configuration",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        configured_env_file = Path(
            args.env_file or os.environ.get("PMARB_ENV_FILE", DEFAULT_ENV_FILE)
        ).expanduser()
        problems = _doctor(settings, _repo_root(), subprocess.run, configured_env_file)
        failures = [problem for problem in problems if not problem.startswith(("INFO ", "WARN "))]
        for finding in problems:
            if finding.startswith("WARN "):
                print(finding, file=sys.stderr)
            elif finding.startswith("INFO "):
                print(finding)
            elif finding.startswith("FAIL "):
                print(finding, file=sys.stderr)
            else:
                print(f"FAIL {finding}", file=sys.stderr)
        if failures:
            raise SystemExit(1)
        print("OK config, data dir, disk, keys, database schema, Streamlit, logs, and env file")
        return
    raise SystemExit(2)


if __name__ == "__main__":
    main()
