"""Command line tools for local review and diagnostics."""

import argparse
import asyncio
import importlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from pmarb import __version__
from pmarb.analysis.metrics import build_metrics
from pmarb.analysis.report import render_report
from pmarb.collector.stopfile import request_stop, wait_for_acknowledgement
from pmarb.config import DEFAULT_ENV_FILE, Settings, load_settings
from pmarb.diagnostics import deployment_checks
from pmarb.osutil import is_windows
from pmarb.secrets import SecretPolicyError, check_key_file
from pmarb.storage.db import open_reader, packaged_schema_version

# The packaged migrations' version; a fresh `open_writer` database has exactly this.
EXPECTED_SCHEMA_VERSION = packaged_schema_version()
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


def _same_database(output: Path, database: Path) -> bool:
    """True when ``output`` is the database file, including a hard link."""
    resolved = output.resolve()
    database_resolved = Path(database).resolve()
    if resolved == database_resolved:
        return True
    if resolved.exists() and database_resolved.exists():
        try:
            return resolved.samefile(database_resolved)
        except OSError:
            return False
    return False


def _checkout_for(output: Path, repo: Path | None) -> Path | None:
    """The nearest checkout that contains ``output``, or ``repo`` when none does."""
    resolved = output.resolve()
    for ancestor in (
        output.parent,
        *output.parent.parents,
        resolved.parent,
        *resolved.parent.parents,
    ):
        if (ancestor / ".git").exists():
            return ancestor
    return repo


def _under_docs(output: Path, repo: Path | None) -> bool:
    """True when ``output`` is under ``repo/docs``, lexically or via a symlink."""
    if repo is None:
        return False
    resolved = output.resolve()
    docs = (repo / "docs").resolve()
    return (
        output == repo / "docs"
        or repo / "docs" in output.parents
        or resolved == docs
        or docs in resolved.parents
    )


def _inside_directory(path: Path, root: Path) -> bool:
    """True when ``path`` is a file inside ``root`` on both the lexical and resolved paths."""
    lexical = Path(os.path.abspath(path))
    resolved = lexical.resolve()
    root_lexical = Path(os.path.abspath(root))
    root_resolved = root_lexical.resolve()
    return root_lexical in lexical.parents and root_resolved in resolved.parents


def _refuse_replay_output(
    output: Path,
    database: Path,
    data_dir: Path,
    parser: argparse.ArgumentParser,
    kind: str = "replay",
) -> None:
    """Refuse an output that overwrites the source, lands in docs/, or leaves the data dir."""
    output = Path(os.path.abspath(output))
    if _same_database(output, database):
        parser.error(f"{kind} output cannot overwrite the source database")
    if _under_docs(output, _checkout_for(output, _repo_root())):
        parser.error(
            f"{kind} output contains recorded data and cannot be written "
            "under the checkout docs/ directory"
        )
    if not _inside_directory(output, data_dir):
        parser.error(f"{kind} output must stay inside the data dir")


def _sim_report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from pmarb.sim.report import render_sim_report

    try:
        rendered = render_sim_report(args.db, args.format)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"FAIL sim report: {exc}", file=sys.stderr)
        return 1
    if args.out is None or args.out == "-":
        print(rendered, end="" if rendered.endswith("\n") else "\n")
        return 0
    output = Path(args.out).expanduser()
    try:
        settings = load_settings(args.env_file)
    except (ValidationError, OSError):
        print("FAIL config: settings are invalid; check the PMARB_* configuration", file=sys.stderr)
        return 1
    _refuse_replay_output(output, Path(args.db), settings.data_dir, parser, kind="sim report")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(output)
    return 0


def _sim_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from decimal import Decimal

    from pmarb.sim.engine import run_simulation

    try:
        settings = load_settings(args.env_file)
    except (ValidationError, OSError):
        print("FAIL config: settings are invalid; check the PMARB_* configuration", file=sys.stderr)
        return 1
    output = args.out.expanduser()
    _refuse_replay_output(output, args.db.expanduser(), settings.data_dir, parser, kind="sim")
    try:
        delays = _integer_list(args.delays, "delays")
        asymmetric = _delay_pairs(args.asymmetric)
        haircuts = [Decimal(value) for value in args.haircut.split(",")]
        result = run_simulation(
            args.raw.expanduser(),
            args.db.expanduser(),
            output,
            delays_ms=delays,
            asymmetric=asymmetric,
            haircuts=haircuts,
            order=args.order,
            jitter_ms=args.jitter_ms,
            seed=args.seed,
            cash_per_venue=Decimal(args.cash_per_venue),
            settings=settings,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError, ArithmeticError) as exc:
        print(f"FAIL sim: {exc}", file=sys.stderr)
        return 1
    print(
        f"opportunities={result['opportunities']} trades={result['trades']} "
        f"scenarios={result['scenarios']}\n{output}"
    )
    return 0


def _integer_list(raw: str, label: str) -> list[int]:
    try:
        values = [int(value) for value in raw.split(",")]
    except ValueError as exc:
        raise ValueError(f"--{label} must be comma-separated integers") from exc
    if not values or any(value < 0 for value in values):
        raise ValueError(f"--{label} values must be non-negative")
    return values


def _delay_pairs(raw: str) -> list[tuple[int, int]]:
    if not raw:
        return []
    pairs = []
    try:
        for item in raw.split(","):
            left, right = item.split(":", maxsplit=1)
            pair = (int(left), int(right))
            if min(pair) < 0:
                raise ValueError
            pairs.append(pair)
    except ValueError as exc:
        raise ValueError("--asymmetric must use comma-separated YES:NO delay pairs") from exc
    return pairs


def _report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        settings = load_settings()
    except (ValidationError, OSError):
        print("FAIL config: settings are invalid; check the PMARB_* configuration", file=sys.stderr)
        return 1
    database = args.db.expanduser() if args.db else _database_path(settings.data_dir)
    try:
        with closing(open_reader(database)) as conn:
            metrics = build_metrics(conn, since=args.since, until=args.until)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"FAIL report input: {exc}", file=sys.stderr)
        return 1
    try:
        rendered = render_report(metrics, args.format)
    except (TypeError, ValueError) as exc:
        print(f"FAIL report rendering: {exc}", file=sys.stderr)
        return 1
    if args.out == "-":
        sys.stdout.write(rendered)
        return 0

    repo = _repo_root()
    if args.out is None:
        output = (
            settings.data_dir
            / "reports"
            / (f"m1-report-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.{args.format}")
        )
        problems = _check_data_dir(output.parent, repo)
        if problems:
            for problem in problems:
                print(f"FAIL {problem}", file=sys.stderr)
            return 1
    else:
        output = Path(args.out).expanduser()
    output = Path(os.path.abspath(output))
    if _same_database(output, Path(database)):
        parser.error("report output cannot overwrite the input database")
    repo = _checkout_for(output, repo)
    if _under_docs(output, repo):
        parser.error(
            "reports contain live data and cannot be written under the checkout docs/ directory"
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        print(f"FAIL could not write report {output}: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


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
            # A repo-relative POSIX path works with Git on every platform; an
            # absolute Windows path with a trailing "/" mixes separators.
            pathspec = (Path("data") / relative).as_posix().rstrip("/") + "/"
            try:
                ignored = subprocess.run(
                    ["git", "check-ignore", "-q", pathspec],
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
            warnings: list[str] = []
            try:
                key_repo_root = repo_root or Path(__file__).resolve().parents[2]
                check_key_file(
                    key_path,
                    key_repo_root,
                    runner=command_runner,
                    warnings=warnings,
                )
            except (OSError, SecretPolicyError) as exc:
                errors.append(f"{label} key file check failed: {exc}")
            errors.extend(f"WARN {label} {warning}" for warning in warnings)

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
    errors.extend(
        deployment_checks(
            settings.data_dir,
            env_file,
            command_runner,
            repo_root=repo_root,
        )
    )
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


def _stop(args: argparse.Namespace) -> int:
    """Ask a running collector to stop through the stop-request file."""
    try:
        settings = load_settings(args.env_file)
    except (ValidationError, OSError):
        print("FAIL config: settings are invalid; check the PMARB_* configuration", file=sys.stderr)
        return 1
    if args.wait < 0:
        print("FAIL --wait must be zero or more seconds", file=sys.stderr)
        return 2
    try:
        path = request_stop(settings.data_dir)
    except OSError as exc:
        print(f"FAIL could not write the stop request: {exc}", file=sys.stderr)
        return 1
    print(f"OK stop requested: {path}")
    if args.wait == 0:
        print("The collector checks once per second, then drains its writes and exits.")
        return 0
    if wait_for_acknowledgement(path, args.wait):
        print("OK collector stopped: writes drained and open episodes closed")
        return 0
    print(
        f"WARN no collector acknowledged the stop request within {args.wait:g} s. "
        "Check that it is running and uses this data dir "
        f"({settings.data_dir}). A later collector start ignores this stale request.",
        file=sys.stderr,
    )
    return 1


def _scan(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run the collector until a signal or ``pmarb stop``, or for one pass with ``--once``."""
    try:
        settings = load_settings()
    except (ValidationError, OSError):
        print(
            "FAIL config: settings are invalid; check the PMARB_* configuration",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    updates: dict[str, object] = {}
    if args.max_groups is not None:
        if args.max_groups < 1:
            parser.error("--max-groups must be at least 1")
        updates["max_polled_groups"] = args.max_groups
    if args.review_mode is not None:
        updates["review_mode"] = args.review_mode
    if updates:
        settings = settings.model_copy(update=updates)
    # Imported here so a test can replace the adapter classes on the factory.
    from pmarb.collector.factory import build_collector

    collector = build_collector(settings)
    try:
        if args.once:
            started = time.monotonic()
            asyncio.run(collector.run_once())
            phases = []
            for event in collector.events:
                if event.kind != "timing":
                    continue
                phase = dict(event.detail)
                if event.venue is not None:
                    phase.setdefault("venue", event.venue)
                phases.append(phase)
            fields = [f"total={time.monotonic() - started:.3f}s"]
            for phase in phases:
                name = str(phase.get("phase", "unknown"))
                values = [f"{name}={float(phase.get('duration_s', 0.0)):.3f}s"]
                for key in (
                    "venue",
                    "requests",
                    "retries",
                    "pages",
                    "markets",
                    "requested",
                    "returned",
                    "groups",
                    "batches",
                    "ops",
                    "flushes",
                    "bytes",
                ):
                    if key in phase:
                        values.append(f"{key}={phase[key]}")
                fields.append(",".join(values))
            print("TIMING " + " ".join(fields))
        else:
            asyncio.run(collector.run())
    except KeyboardInterrupt:
        # Ctrl+C before the collector's own handlers are installed (startup).
        # Inside asyncio.run, asyncio cancels the collector first, and the
        # cancellation runs the same drain as `pmarb stop`.
        print("interrupted; collector stopped", file=sys.stderr)
        return


def _replay(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Replay a raw log, or compare that replay with assessments already stored."""
    from pmarb.replay.clock import LookaheadError
    from pmarb.replay.engine import ReplayEngine, ReplayError, format_verify_report, verify_replay

    try:
        settings = load_settings(args.env_file)
    except (ValidationError, OSError):
        print("FAIL config: settings are invalid; check the PMARB_* configuration", file=sys.stderr)
        return 1
    raw = Path(args.raw).expanduser()
    database = Path(args.db).expanduser()
    if args.replay_command == "run":
        output = Path(args.out).expanduser()
        _refuse_replay_output(output, database, settings.data_dir, parser)
    try:
        if args.replay_command == "verify":
            report = verify_replay(
                raw,
                database,
                since=args.since,
                until=args.until,
                settings=settings,
            )
        else:
            result = ReplayEngine(
                raw,
                database,
                output,
                since=args.since,
                until=args.until,
                settings=settings,
            ).run()
    except (ReplayError, LookaheadError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"FAIL replay: {exc}", file=sys.stderr)
        return 1
    if args.replay_command == "verify":
        sys.stdout.write(format_verify_report(report))
        return 0 if report.ok else 1
    print(
        f"envelopes={result.envelopes} gaps={len(result.gaps)} "
        f"assessments={result.assessments} episodes={result.episodes}"
    )
    print(result.out_db)
    return 0


def _never_crash_on_console_encoding() -> None:
    """Replace characters the Windows code page cannot encode instead of raising.

    A piped stdout on Windows (for example, an assistant capturing a command's
    output) uses the ANSI code page, and reports contain characters such as
    "Θ" that it cannot encode. POSIX streams are left alone.
    """
    if not is_windows():
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            continue


def main(argv: list[str] | None = None) -> None:
    _never_crash_on_console_encoding()
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
    scan = commands.add_parser(
        "scan",
        help="discover markets, review groups, poll books, and evaluate",
    )
    scan.add_argument(
        "--once",
        action="store_true",
        help="discover, drain writes, fetch fresh hot groups (or soonest samples), evaluate, and exit",
    )
    scan.add_argument("--max-groups", type=int, default=None)
    scan.add_argument("--review-mode", choices=("auto", "manual"), default=None)
    report = commands.add_parser("report", help="summarize stored M1 soak data")
    report.add_argument(
        "--db", type=Path, default=None, help="SQLite database (default: data dir database)"
    )
    report.add_argument("--since", default=None, help="inclusive ISO timestamp lower bound")
    report.add_argument("--until", default=None, help="inclusive ISO timestamp upper bound")
    report.add_argument("--format", choices=("md", "json"), default="md")
    report.add_argument("--out", default=None, help="output path; use - for stdout")
    stop = commands.add_parser(
        "stop",
        help="ask a running collector to drain its writes and exit (works without signals)",
    )
    stop.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="wait up to SECONDS for the collector to finish draining (default: do not wait)",
    )
    stop.add_argument("--env-file", type=Path, default=argparse.SUPPRESS, help="settings file path")
    replay = commands.add_parser("replay", help="replay a recorded raw log")
    replay_commands = replay.add_subparsers(dest="replay_command", required=True)
    replay_run = replay_commands.add_parser("run", help="replay into a new database")
    replay_verify = replay_commands.add_parser(
        "verify",
        help="compare a replay with assessments stored in the source database",
    )
    for sub in (replay_run, replay_verify):
        sub.add_argument("--raw", type=Path, required=True, help="raw log root (contains raw/)")
        sub.add_argument("--db", type=Path, required=True, help="source database, opened read-only")
        sub.add_argument("--since", default=None, help="inclusive ISO-8601 lower bound on recv_utc")
        sub.add_argument("--until", default=None, help="inclusive ISO-8601 upper bound on recv_utc")
    replay_run.add_argument("--out", type=Path, required=True, help="new SQLite database to write")
    sim = commands.add_parser("sim", help="simulate delayed fills against recorded books")
    sim_commands = sim.add_subparsers(dest="sim_command", required=True)
    sim_run = sim_commands.add_parser("run", help="run the latency and fill simulator")
    sim_run.add_argument("--raw", type=Path, required=True, help="raw log root (contains raw/)")
    sim_run.add_argument("--db", type=Path, required=True, help="source database, opened read-only")
    sim_run.add_argument("--out", type=Path, required=True, help="new simulation SQLite database")
    sim_run.add_argument("--delays", default="100,250,500,1000,2000")
    sim_run.add_argument("--asymmetric", default="", help="optional YES:NO pairs, e.g. 100:250")
    sim_run.add_argument("--haircut", default="0,0.25,0.5")
    sim_run.add_argument("--order", choices=("fok", "ioc"), default="fok")
    sim_run.add_argument("--jitter-ms", type=int, default=0)
    sim_run.add_argument("--seed", type=int, default=0)
    sim_run.add_argument("--cash-per-venue", default="10000")
    sim_run.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    sim_report = sim_commands.add_parser("report", help="render simulation results")
    sim_report.add_argument("--db", type=Path, required=True, help="simulation database")
    sim_report.add_argument("--format", choices=("md", "json"), default="md")
    sim_report.add_argument("--out", default=None, help="output path; use - for stdout")
    sim_report.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command == "scan":
        _scan(args, scan)
        return
    if args.command == "stop":
        raise SystemExit(_stop(args))
    if args.command == "replay":
        active = replay_run if args.replay_command == "run" else replay_verify
        raise SystemExit(_replay(args, active))
    if args.command == "sim":
        if args.sim_command == "run":
            raise SystemExit(_sim_run(args, sim_run))
        raise SystemExit(_sim_report(args, sim_report))
    if args.command == "report":
        raise SystemExit(_report(args, parser))
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
        print(
            "OK config, data dir, disk, keys, database schema, Streamlit, time zones, logs, "
            "and env file"
        )
        return
    raise SystemExit(2)


if __name__ == "__main__":
    main()
