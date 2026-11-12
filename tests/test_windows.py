"""Windows support, tested on any OS by simulating win32.

``sys.platform`` is monkeypatched to ``"win32"`` where the code under test
branches on it. Commands such as ``icacls`` and ``w32tm`` go through injected
runners that return sample outputs, so nothing here needs Windows. CI also
runs this file on ``windows-latest``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmarb import cli, diagnostics, osutil, winacl
from pmarb.app import decisions as app_decisions
from pmarb.collector.clock import FakeClock
from pmarb.collector.stopfile import (
    STOP_REQUEST_NAME,
    request_stop,
    stop_request_path,
    wait_for_acknowledgement,
)
from pmarb.config import Settings
from pmarb.models import Venue
from pmarb.review import spool
from pmarb.secrets import SecretPolicyError, check_key_file
from pmarb.storage.db import open_reader, open_writer
from pmarb.storage.rawlog import Envelope, RawLogWriter, iter_records, list_log_files
from tests.test_collector import T0, FakeVenue, Harness, build, pair, park

REPO = Path(__file__).resolve().parents[1]
WINDOWS_DEPLOY = REPO / "deploy" / "windows"
INSTALL_PS1 = WINDOWS_DEPLOY / "install.ps1"
STOP_PS1 = WINDOWS_DEPLOY / "stop.ps1"
RUNNER_PS1 = WINDOWS_DEPLOY / "run-collector.ps1"
TASK_TEMPLATE = WINDOWS_DEPLOY / "pmarb-collector.task.xml.template"
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith(("PMARB_", "KALSHI_", "POLYUS_")):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    # Never read the developer's real ~/.config/pmarb/.env.
    monkeypatch.setenv("PMARB_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr("pmarb.collector.runtime.in_maintenance", lambda _now: False)
    monkeypatch.setattr("pmarb.collector.freshness.in_maintenance", lambda _now: False)


@pytest.fixture
def win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")


def _runner(stdout: str, returncode: int = 0, calls: list[list[str]] | None = None):
    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if calls is not None:
            calls.append(list(command))
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    return run


def _missing_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
    raise FileNotFoundError(command[0])


# --- 1. Time zones --------------------------------------------------------------


def test_tzdata_is_pinned_exactly_for_windows() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [dep for dep in project["project"]["dependencies"] if dep.startswith("tzdata")]
    assert len(pins) == 1
    requirement, _, marker = pins[0].partition(";")
    assert "==" in requirement and requirement.split("==")[1].strip()[0].isdigit()
    assert "sys_platform" in marker and "win32" in marker


def test_timezone_check_passes_when_both_zones_load() -> None:
    loaded: list[str] = []
    assert diagnostics.timezone_findings(lambda name: loaded.append(name)) == []
    assert loaded == ["America/New_York", "America/Chicago"]
    assert diagnostics.timezone_findings() == []  # the real zoneinfo on this machine


def test_timezone_check_fails_without_tz_data() -> None:
    from zoneinfo import ZoneInfoNotFoundError

    def missing(name: str) -> object:
        raise ZoneInfoNotFoundError(f"No time zone found with key {name}")

    findings = diagnostics.timezone_findings(missing)
    assert len(findings) == 1
    assert findings[0].startswith("FAIL time zone data missing for America/New_York")
    assert "America/Chicago" in findings[0] and "tzdata" in findings[0]


def test_doctor_reports_missing_time_zones(tmp_path: Path, monkeypatch) -> None:
    def missing(name: str) -> object:
        raise KeyError(name)

    monkeypatch.setattr(diagnostics, "ZoneInfo", missing)
    findings = diagnostics.deployment_checks(
        tmp_path, tmp_path / "absent.env", _runner("yes"), system="Linux"
    )
    assert any(item.startswith("FAIL time zone data missing") for item in findings)


# --- 2. Signals and the stop-request file ------------------------------------------


async def test_win32_signal_fallback_schedules_stop_thread_safely(
    win32: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: dict[int, object] = {}
    sentinel = object()

    def fake_signal(sig: int, handler: object) -> object:
        previous = installed.get(int(sig), sentinel)
        installed[int(sig)] = handler
        return previous

    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    loop = asyncio.get_running_loop()

    def no_loop_handlers(*args: object) -> None:
        raise AssertionError("loop.add_signal_handler is not supported on Windows")

    monkeypatch.setattr(loop, "add_signal_handler", no_loop_handlers)
    monkeypatch.setattr(loop, "remove_signal_handler", no_loop_handlers)

    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
        install_signals=True,
    )
    async with Harness(collector) as task:
        await park(collector, clock, 1)
        assert set(installed) == {int(signal.SIGINT), 21}
        handler = installed[int(signal.SIGINT)]
        assert callable(handler)
        # Deliver from another thread: only call_soon_threadsafe may touch the loop.
        sender = threading.Thread(target=handler, args=(int(signal.SIGINT), None))
        sender.start()
        sender.join()
        await asyncio.wait_for(task, timeout=5)
        if collector._signal_task is not None:
            await collector._signal_task
    assert "collector_shutdown" in gaps
    assert collector.stopping
    # The handlers that were there before are restored.
    assert installed == {int(signal.SIGINT): sentinel, 21: sentinel}
    assert collector._signals == []


async def test_win32_signal_install_is_skipped_off_the_main_thread(
    win32: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        raise AssertionError("signal.signal must not be called off the main thread")

    monkeypatch.setattr(signal, "signal", fail)
    clock = FakeClock(wall=T0)
    collector, _gaps, _updates = build(
        clock, FakeVenue(Venue.KALSHI, []), FakeVenue(Venue.POLYMARKET_US, []), install_signals=True
    )
    loop = asyncio.get_running_loop()
    worker = threading.Thread(target=collector._install_win32_signals, args=(loop,))
    worker.start()
    worker.join()
    assert collector._signals == []


async def test_stop_file_triggers_clean_shutdown_and_is_acknowledged(tmp_path: Path) -> None:
    clock = FakeClock(wall=T0)
    log: list[tuple[str, str]] = []
    group, markets = pair("g", "KX", "pm", T0)
    stop_file = stop_request_path(tmp_path)
    collector, gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, log),
        FakeVenue(Venue.POLYMARKET_US, log),
        groups=(group,),
        markets=markets,
        stop_file=stop_file,
    )
    async with Harness(collector) as task:
        await park(collector, clock, 1)
        assert not collector.stopping
        request_stop(tmp_path)
        clock.advance(mono=1.0)  # the watcher checks once per (fake) second
        await asyncio.wait_for(task, timeout=5)
    assert "collector_shutdown" in gaps
    kinds = [event.kind for event in collector.events]
    assert "stop_requested" in kinds
    assert kinds.index("stop_requested") < kinds.index("collector_shutdown")
    assert not stop_file.exists()  # deleted after the drain: the acknowledgement


async def test_stale_stop_request_is_ignored_and_removed(tmp_path: Path) -> None:
    stop_file = request_stop(tmp_path)
    old = time.time() - 3600
    os.utime(stop_file, (old, old))
    clock = FakeClock(wall=T0)
    group, markets = pair("g", "KX", "pm", T0)
    collector, gaps, _updates = build(
        clock,
        FakeVenue(Venue.KALSHI, []),
        FakeVenue(Venue.POLYMARKET_US, []),
        groups=(group,),
        markets=markets,
        stop_file=stop_file,
    )
    async with Harness(collector):
        await park(collector, clock, 1)
        clock.advance(mono=1.0)
        for _ in range(50):
            await asyncio.sleep(0)
        assert not collector.stopping
        assert not stop_file.exists()
        assert "stop_request_stale" in [event.kind for event in collector.events]
    assert "collector_shutdown" in gaps


def test_stop_command_writes_request_without_waiting(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PMARB_DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as raised:
        cli.main(["stop"])
    assert raised.value.code == 0
    path = tmp_path / STOP_REQUEST_NAME
    assert path.is_file()
    assert "requested_utc" in json.loads(path.read_text(encoding="utf-8"))
    assert f"OK stop requested: {path}" in capsys.readouterr().out


def test_stop_command_reports_missing_data_dir(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PMARB_DATA_DIR", str(tmp_path / "absent"))
    with pytest.raises(SystemExit) as raised:
        cli.main(["stop"])
    assert raised.value.code == 1
    assert "data dir not found" in capsys.readouterr().err


def test_stop_command_wait_times_out_when_nothing_acknowledges(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PMARB_DATA_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as raised:
        cli.main(["stop", "--wait", "0.2"])
    assert raised.value.code == 1
    assert "no collector acknowledged" in capsys.readouterr().err


def test_wait_for_acknowledgement_sees_deletion(tmp_path: Path) -> None:
    path = request_stop(tmp_path)
    ticks = iter(range(100))

    def sleep(_seconds: float) -> None:
        path.unlink(missing_ok=True)

    assert wait_for_acknowledgement(path, 5, monotonic=lambda: float(next(ticks)), sleep=sleep)


def test_scan_keyboard_interrupt_exits_cleanly(monkeypatch, capsys) -> None:
    class Interrupted:
        async def run(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr("pmarb.collector.factory.build_collector", lambda settings: Interrupted())
    cli.main(["scan"])
    assert "interrupted; collector stopped" in capsys.readouterr().err


class _NoHttp:
    """Stands in for the HTTP client: the fake adapters never use it."""

    def __init__(self, settings: object) -> None:
        del settings

    async def aclose(self) -> None:
        return None


def test_pmarb_stop_shuts_down_scan_end_to_end(win32: None, tmp_path: Path, monkeypatch) -> None:
    """`pmarb scan` with fake venues; `pmarb stop` from another thread; drained exit."""
    from tests.test_pr6b import FakeKalshi, FakePoly

    monkeypatch.setattr("pmarb.collector.factory.KalshiAdapter", FakeKalshi)
    monkeypatch.setattr("pmarb.collector.factory.PolymarketUSAdapter", FakePoly)
    monkeypatch.setattr("pmarb.collector.factory.ObservationHttpClient", _NoHttp)
    # Raw logging pauses below 2 GiB free; a small CI disk must not change this test.
    monkeypatch.setattr("pmarb.collector.runtime.disk_free_bytes", lambda _path: 10**12)
    monkeypatch.setenv("PMARB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PMARB_KALSHI_MEMBER_PRECISION", "0.0001")
    monkeypatch.setenv("PMARB_POLYUS_THETA", "0.075")
    db_path = tmp_path / "pmarb.sqlite"
    stop_codes: list[object] = []
    scan_done = threading.Event()

    def episodes_open() -> bool:
        if not db_path.exists():
            return False
        try:
            with closing(open_reader(db_path)) as conn:
                return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] > 0
        except sqlite3.Error:
            return False

    def operator() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not episodes_open():
            time.sleep(0.1)
        try:
            cli.main(["stop"])
        except SystemExit as exc:
            stop_codes.append(exc.code)
        # Last resort so a broken stop path fails the test instead of hanging it.
        if not scan_done.wait(60):
            signal.raise_signal(signal.SIGINT)

    helper = threading.Thread(target=operator, daemon=True)
    helper.start()
    try:
        cli.main(["scan"])
    finally:
        scan_done.set()
    helper.join(timeout=5)

    assert stop_codes == [0]
    assert not (tmp_path / STOP_REQUEST_NAME).exists()
    with closing(open_reader(db_path)) as conn:
        events = [row[0] for row in conn.execute("SELECT kind FROM collector_events ORDER BY id")]
        reasons = [row[0] for row in conn.execute("SELECT termination_reason FROM episodes")]
        stop_detail = conn.execute(
            "SELECT detail_json FROM collector_events WHERE kind = 'stop_requested'"
        ).fetchone()
    assert "stop_requested" in events and "collector_shutdown" in events
    assert events.index("stop_requested") < events.index("collector_shutdown")
    assert json.loads(stop_detail[0]) == {"source": "stop_file"}
    assert reasons and None not in reasons
    assert "collector_shutdown" in reasons
    raw_files = list_log_files(tmp_path, "kalshi")
    assert raw_files, (events, sorted(p.as_posix() for p in tmp_path.rglob("*")))
    assert list(iter_records(raw_files))  # drained and decodable


# --- 3. Key-file and env-file ACLs (icacls) -------------------------------------------

PRIVATE_ACL = """{path} NT AUTHORITY\\SYSTEM:(F)
{pad}BUILTIN\\Administrators:(F)
{pad}DESKTOP-TEST\\someone:(F)

Successfully processed 1 files; Failed processing 0 files
"""

INHERITED_USERS_ACL = """{path} NT AUTHORITY\\SYSTEM:(I)(F)
{pad}BUILTIN\\Administrators:(I)(F)
{pad}BUILTIN\\Users:(I)(RX)
{pad}DESKTOP-TEST\\someone:(I)(F)

Successfully processed 1 files; Failed processing 0 files
"""


def _acl(template: str, path: Path | str) -> str:
    return template.format(path=path, pad=" " * (len(str(path)) + 1))


def test_icacls_parser_reads_every_entry() -> None:
    path = "C:\\Users\\someone\\.config\\pmarb\\.env"
    entries = winacl.parse_icacls(_acl(INHERITED_USERS_ACL, path), path)
    assert [entry.principal for entry in entries] == [
        "NT AUTHORITY\\SYSTEM",
        "BUILTIN\\Administrators",
        "BUILTIN\\Users",
        "DESKTOP-TEST\\someone",
    ]
    users = entries[2]
    assert users.inherited and users.rights == {"RX"} and users.grants_read
    assert winacl.broad_readers(entries) == ("BUILTIN\\Users",)


def test_icacls_parser_handles_spaces_in_the_path() -> None:
    path = "C:\\Users\\Some One\\.config\\pmarb\\kalshi key.pem"
    entries = winacl.parse_icacls(_acl(PRIVATE_ACL, path), path)
    assert len(entries) == 3
    assert winacl.broad_readers(entries) == ()


@pytest.mark.parametrize(
    "ace",
    [
        "Everyone:(R)",
        "Everyone:(F)",
        "BUILTIN\\Users:(I)(RX)",
        "Users:(M)",
        "NT AUTHORITY\\Authenticated Users:(I)(M)",
        "Authenticated Users:(RD)",
        "*S-1-1-0:(GR)",
        "S-1-5-32-545:(RD,RA)",
        "S-1-5-11:(GA)",
    ],
)
def test_broad_read_access_is_refused(ace: str) -> None:
    path = "C:\\k.pem"
    output = (
        f"{path} {ace}\n           NT AUTHORITY\\SYSTEM:(F)\n\nSuccessfully processed 1 files\n"
    )
    readers = winacl.broad_readers(winacl.parse_icacls(output, path))
    assert len(readers) == 1


@pytest.mark.parametrize(
    "ace",
    [
        "Everyone:(DENY)(R)",  # a deny entry grants nothing
        "BUILTIN\\Users:(OI)(CI)(IO)(RX)",  # inherit-only: does not apply to the file
        "Everyone:(W)",  # write without read
        "Authenticated Users:(RA,REA,RC)",  # attributes and permissions, not contents
        "NT AUTHORITY\\SYSTEM:(F)",
        "BUILTIN\\Administrators:(F)",
    ],
)
def test_narrow_or_non_read_entries_are_allowed(ace: str) -> None:
    path = "C:\\k.pem"
    output = f"{path} {ace}\n\nSuccessfully processed 1 files\n"
    assert winacl.broad_readers(winacl.parse_icacls(output, path)) == ()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Successfully processed 0 files; Failed processing 1 files\n",
        "C:\\other\\file.txt NT AUTHORITY\\SYSTEM:(F)\n",
        "C:\\k.pem NT AUTHORITY\\SYSTEM:(F)\n        this line is not an ACE\n",
    ],
)
def test_unexpected_icacls_output_fails_closed(output: str) -> None:
    with pytest.raises(winacl.IcaclsParseError):
        winacl.parse_icacls(output, "C:\\k.pem")


def test_icacls_path_echo_falls_back_to_the_file_name() -> None:
    # A console code page can mangle a non-ASCII profile name in the echoed path.
    path = "C:\\Users\\Jos\u00e9\\.config\\pmarb\\.env"
    output = "C:\\Users\\Jos\u201a\\.config\\pmarb\\.env Everyone:(R)\n\nSuccessfully processed 1\n"
    assert winacl.broad_readers(winacl.parse_icacls(output, path)) == ("Everyone",)


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "profile"
    config = home / ".config" / "pmarb"
    config.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    return config


def _key(directory: Path, name: str = "kalshi.pem") -> Path:
    key = directory / name
    key.write_bytes(b"synthetic-key-bytes")
    return key


def test_windows_key_file_with_private_acl_is_accepted(
    win32: None, tmp_path: Path, profile: Path
) -> None:
    key = _key(profile)
    calls: list[list[str]] = []
    runner = _runner(_acl(PRIVATE_ACL, key.resolve()), calls=calls)
    assert check_key_file(key, tmp_path / "repo", runner=runner) == key.resolve()
    assert calls == [["icacls", str(key.resolve())]]


def test_windows_key_file_readable_by_users_is_refused(
    win32: None, tmp_path: Path, profile: Path
) -> None:
    key = _key(profile)
    runner = _runner(_acl(INHERITED_USERS_ACL, key.resolve()))
    with pytest.raises(SecretPolicyError, match="readable by BUILTIN\\\\Users") as info:
        check_key_file(key, tmp_path / "repo", runner=runner)
    assert "icacls" in str(info.value) and "/inheritance:r" in str(info.value)
    assert "synthetic-key-bytes" not in str(info.value)


def test_windows_key_file_outside_the_config_dir_is_refused(win32: None, tmp_path: Path) -> None:
    elsewhere = tmp_path / "Downloads"
    elsewhere.mkdir()
    key = _key(elsewhere)
    runner = _runner(_acl(PRIVATE_ACL, key.resolve()))
    with pytest.raises(SecretPolicyError, match="must live under"):
        check_key_file(key, tmp_path / "repo", runner=runner, allowed_dir=tmp_path / "keys")


def test_windows_key_file_keeps_repo_and_regular_file_checks(
    win32: None, tmp_path: Path, profile: Path
) -> None:
    repo = profile / "checkout"
    repo.mkdir()
    runner = _runner("unused")
    with pytest.raises(SecretPolicyError, match="outside the repository"):
        check_key_file(_key(repo), repo, runner=runner)
    with pytest.raises(SecretPolicyError, match="not a regular file"):
        check_key_file(profile, tmp_path / "repo", runner=runner)


def test_windows_key_file_without_icacls_warns_loudly(
    win32: None, tmp_path: Path, profile: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = _key(profile)
    warnings: list[str] = []
    with caplog.at_level("WARNING", logger="pmarb.secrets"):
        check_key_file(key, tmp_path / "repo", runner=_missing_runner, warnings=warnings)
    assert len(warnings) == 1 and "ACL NOT verified" in warnings[0]
    assert "icacls was not found" in warnings[0]
    assert any("ACL NOT verified" in record.getMessage() for record in caplog.records)


def test_windows_key_file_with_failing_icacls_is_refused(
    win32: None, tmp_path: Path, profile: Path
) -> None:
    key = _key(profile)
    with pytest.raises(SecretPolicyError, match="could not be verified"):
        check_key_file(key, tmp_path / "repo", runner=_runner("Access is denied.", 5))


def test_posix_key_check_is_unchanged_off_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    key = _key(tmp_path)
    if os.name == "nt":
        pytest.skip("POSIX mode bits cannot be set on Windows")
    key.chmod(0o644)

    def runner(*args: object, **kwargs: object) -> object:
        raise AssertionError("icacls must not run off Windows")

    with pytest.raises(SecretPolicyError, match="too open"):
        check_key_file(key, tmp_path / "repo", runner=runner)


def test_doctor_env_file_check_on_windows(tmp_path: Path, profile: Path, win32: None) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    env_file = profile / ".env"
    env_file.write_text("PMARB_DATA_DIR=synthetic\n", encoding="utf-8")
    resolved = env_file.resolve()

    def runner_for(acl: str | None):
        def run(command: list[str], **kwargs: object) -> SimpleNamespace:
            if command[0] == "w32tm":
                return SimpleNamespace(returncode=0, stdout=W32TM_SYNCED)
            if command[0] == "icacls":
                if acl is None:
                    raise FileNotFoundError("icacls")
                return SimpleNamespace(returncode=0, stdout=_acl(acl, resolved))
            raise AssertionError(command)

        return run

    def check(runner) -> list[str]:
        return diagnostics.deployment_checks(
            data_dir, env_file, runner, repo_root=tmp_path / "repo"
        )

    ok = check(runner_for(PRIVATE_ACL))
    assert not any(item.startswith(("FAIL", "WARN env")) for item in ok)
    assert not any("mode 600" in item for item in ok)

    loose = check(runner_for(INHERITED_USERS_ACL))
    assert any(item.startswith("FAIL env file is readable by BUILTIN\\Users") for item in loose)

    unverified = check(runner_for(None))
    assert any(item.startswith("WARN env file ACL NOT verified") for item in unverified)
    assert not any(item.startswith("FAIL") for item in unverified)

    outside = tmp_path / "elsewhere.env"
    outside.write_text("x=1\n", encoding="utf-8")
    findings = diagnostics.deployment_checks(
        data_dir, outside, runner_for(PRIVATE_ACL), repo_root=tmp_path / "repo"
    )
    assert any("FAIL env file must live under" in item for item in findings)


def test_doctor_key_warning_is_reported_not_fatal(
    tmp_path: Path, profile: Path, win32: None
) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "logs").mkdir(parents=True)
    open_writer(data_dir / "pmarb.sqlite").close()
    key = _key(profile)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[0] == "icacls":
            raise FileNotFoundError("icacls")
        if command[0] == "w32tm":
            return SimpleNamespace(returncode=0, stdout=W32TM_SYNCED)
        return SimpleNamespace(returncode=1, stdout="")

    settings = Settings(data_dir=data_dir, kalshi_private_key_path=key)
    findings = cli._doctor(settings, tmp_path / "repo", run, env_file=tmp_path / "absent.env")
    assert any(item.startswith("WARN Kalshi key file ACL NOT verified") for item in findings)
    assert not any(item.startswith("FAIL") for item in findings), findings
    assert not any(item.startswith("Kalshi key file check failed") for item in findings)


# --- 4. Durability: directory fsync is skipped on Windows ------------------------------


@pytest.fixture
def no_directory_open(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Fail on os.open of a directory (Windows raises); count file fsyncs."""
    real_open = os.open
    real_fsync = os.fsync
    fsyncs: list[int] = []

    def guarded_open(path, flags, *args, **kwargs):
        if Path(path).is_dir():
            raise PermissionError(13, "Permission denied (simulated Windows)", str(path))
        return real_open(path, flags, *args, **kwargs)

    def counting_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "fsync", counting_fsync)
    return fsyncs


def test_directory_fsync_is_skipped_on_windows(win32: None, no_directory_open, tmp_path) -> None:
    osutil.fsync_directory(tmp_path)  # would raise PermissionError if attempted
    assert no_directory_open == []


def test_directory_fsync_still_runs_on_posix(monkeypatch, tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("a real directory fsync is impossible on Windows")
    monkeypatch.setattr(sys, "platform", "linux")
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd)))
    osutil.fsync_directory(tmp_path)
    assert len(calls) == 1


def test_writers_skip_directory_fsync_but_fsync_files_on_windows(
    win32: None, no_directory_open, tmp_path: Path
) -> None:
    # Raw log: a new hour file in a new directory.
    writer = RawLogWriter(
        tmp_path / "raw-root", clock=lambda: datetime(2026, 11, 11, 18, tzinfo=UTC)
    )
    writer.append(
        Envelope(
            recv_utc="2026-11-11T18:30:00+00:00",
            recv_mono_ns=1,
            venue="kalshi",
            kind="rest_book",
            conn_gen=1,
            endpoint="/book",
            status=200,
        )
    )
    assert writer.flush() > 0
    after_rawlog = len(no_directory_open)
    assert after_rawlog >= 1

    # Database: the parent directory is created and would be fsynced on POSIX.
    open_writer(tmp_path / "db" / "nested" / "pmarb.sqlite").close()

    # Review spool (atomic write with os.replace) and the review app's writer.
    decision = spool.ReviewDecision(
        group_id="g1",
        action="reject",
        reviewer="synthetic",
        shown_hashes={"kalshi:K": "h1"},
        notes="",
        created_utc="2026-11-11T18:00:00Z",
        acknowledged_exceptions=False,
    )
    spooled = spool.write_decision(tmp_path / "spool", decision)
    app_payload = app_decisions.build_decision(
        group_id="g2",
        action="reject",
        reviewer="synthetic",
        shown_hashes={},
        exception_table=[],
        acknowledged_exceptions=False,
        created_utc="2026-11-11T18:00:00+00:00",
    )
    app_file = app_decisions.write_decision(tmp_path / "app", app_payload)
    assert len(no_directory_open) >= after_rawlog + 2  # both spool writers fsynced files

    # ":" is reserved on Windows; names use "-" while the body keeps the exact time.
    for path in (spooled, app_file):
        assert ":" not in path.name
        assert path.name.startswith("2026-11-11T18-00-00Z-")
    assert json.loads(spooled.read_text(encoding="utf-8"))["created_utc"] == "2026-11-11T18:00:00Z"
    assert json.loads(app_file.read_text(encoding="utf-8"))["created_utc"] == "2026-11-11T18:00:00Z"


def test_portable_filename_is_unchanged_on_posix(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert osutil.portable_filename("2026-11-11T18:00:00Z-g-a.json") == (
        "2026-11-11T18:00:00Z-g-a.json"
    )


# --- 5. Clock check (w32tm) ----------------------------------------------------------

W32TM_SYNCED = """Leap Indicator: 0(no warning)
Stratum: 4 (secondary reference - syncd by (S)NTP)
Precision: -23 (119.209ns per tick)
Root Delay: 0.0312500s
Root Dispersion: 7.7756457s
ReferenceId: 0x14650039 (source IP:  192.0.2.10)
Last Successful Sync Time: 11/12/2026 1:23:45 PM
Source: time.windows.com,0x9
Poll Interval: 10 (1024s)
"""

W32TM_CMOS = """Leap Indicator: 3(not synchronized)
Stratum: 0 (unspecified)
Precision: -23 (119.209ns per tick)
Root Delay: 0.0000000s
Root Dispersion: 0.0000000s
ReferenceId: 0x00000000 (unspecified)
Last Successful Sync Time: unspecified
Source: Local CMOS Clock
Poll Interval: 6 (64s)
"""

W32TM_STOPPED = "The following error occurred: The service has not been started. (0x80070426)\n"


def test_w32tm_parser_keeps_values_with_colons() -> None:
    fields = diagnostics.parse_w32tm_status(W32TM_SYNCED)
    assert fields["source"] == "time.windows.com,0x9"
    assert fields["last successful sync time"] == "11/12/2026 1:23:45 PM"
    assert fields["leap indicator"] == "0(no warning)"
    assert fields["stratum"].startswith("4")


def test_windows_clock_synced() -> None:
    calls: list[list[str]] = []
    findings = diagnostics._clock_status(_runner(W32TM_SYNCED, calls=calls), "Windows")
    assert calls == [["w32tm", "/query", "/status"]]
    assert findings == [
        "INFO clock source time.windows.com,0x9, last sync 11/12/2026 1:23:45 PM (w32tm)",
        "WARN clock offset unknown",
    ]


def test_windows_clock_unsynchronized() -> None:
    findings = diagnostics._clock_status(_runner(W32TM_CMOS), "Windows")
    assert findings == [
        "WARN system clock is not synchronized (w32tm source: Local CMOS Clock); offset unknown"
    ]


def test_windows_clock_service_stopped_or_missing() -> None:
    stopped = diagnostics._clock_status(_runner(W32TM_STOPPED, returncode=1), "Windows")
    assert stopped[0].startswith("WARN clock synchronization unknown; w32tm /query /status failed")
    assert "service has not been started" in stopped[0]
    missing = diagnostics._clock_status(_missing_runner, "Windows")
    assert missing == ["WARN clock synchronization unknown; w32tm unavailable"]


def test_simulated_win32_selects_the_w32tm_check(win32: None) -> None:
    calls: list[list[str]] = []
    diagnostics._clock_status(_runner(W32TM_SYNCED, calls=calls))
    assert calls == [["w32tm", "/query", "/status"]]


# --- 6. deploy/windows: static checks and task XML rendering -------------------------


def _script_text(path: Path) -> str:
    data = path.read_bytes()
    data.decode("ascii")  # PowerShell 5 reads BOM-less scripts in the ANSI code page
    return data.decode("ascii")


@pytest.mark.parametrize("script", [INSTALL_PS1, STOP_PS1, RUNNER_PS1])
def test_powershell_scripts_have_no_secrets_or_personal_paths(script: Path) -> None:
    text = _script_text(script)
    lowered = text.lower()
    for forbidden in (
        "c:\\users\\",
        "/users/",
        "/home/",
        "begin private key",
        "begin rsa",
        "begin openssh",
        "--no-verify",
        "powercfg",
        "set-executionpolicy",
        "invoke-webrequest",
        "invoke-expression",
        "convertto-securestring",
    ):
        assert forbidden not in lowered, f"{script.name} contains {forbidden!r}"
    assert "#requires -version 5.1" in lowered
    assert "set-strictmode" in lowered
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith(("$", "[")) and "=" in stripped:
            key = stripped.partition("=")[0]
            assert not any(word in key for word in ("password", "secret", "apikey", "api_key"))


def test_install_ps1_parameters_and_steps() -> None:
    text = _script_text(INSTALL_PS1)
    param_block = text[text.index("param(") : text.index(")", text.index("param("))]
    for name in ("$RepoPath", "$DataDir", "$Start", "$Uninstall", "$DryRun"):
        assert name in param_block
    for switch in ("[switch]$Start", "[switch]$Uninstall", "[switch]$DryRun"):
        assert switch in param_block
    for step in (
        "py -3.12 --version",
        "py -3.12 -m venv",
        "pip install -e '.[dev]'",
        "'.config\\pmarb'",
        ".env.example",
        "Test-Path -LiteralPath $envFile",
        "/inheritance:r /grant:r",
        "& $pmarb doctor",
        "Register-ScheduledTask",
        "pmarb-collector",
        "Start-ScheduledTask",
        "Unregister-ScheduledTask",
        "render_template.py",
    ):
        assert step in text, step
    # The settings file is copied only when missing, and doctor gates registration.
    assert text.index("Test-Path -LiteralPath $envFile") < text.index(
        "copy .env.example to $envFile"
    )
    assert text.index("& $pmarb doctor") < text.index("Register-ScheduledTask -TaskName")
    assert "$env:USERPROFILE" in text  # the profile path is never hardcoded


def test_stop_ps1_uses_pmarb_stop_and_waits_90_seconds() -> None:
    text = _script_text(STOP_PS1)
    assert "[int]$TimeoutSeconds = 90" in text
    assert "& $pmarb stop --wait $TimeoutSeconds" in text
    assert "Stop-ScheduledTask" not in text.replace("end the task: Stop-ScheduledTask", "")


def test_runner_restarts_after_crash_but_not_after_clean_stop() -> None:
    text = _script_text(RUNNER_PS1)
    assert "[int]$RestartDelaySeconds = 60" in text
    assert "'-m', 'pmarb.service'" in text
    assert "if ($code -eq 0)" in text
    assert "collector.stop" in text  # a stop during the restart wait still ends it
    assert "$env:PMARB_LOG_DIR = $logDir" in text


def _render_task(tmp_path: Path, values: dict[str, str]) -> tuple[str, ET.Element]:
    sys.path.insert(0, str(REPO / "deploy"))
    try:
        import render_template
    finally:
        sys.path.pop(0)
    output = tmp_path / "pmarb-collector.task.xml"
    render_template.render(TASK_TEMPLATE, output, "windows", values)
    raw = output.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 with BOM, as declared
    text = raw.decode("utf-16")
    return text, ET.fromstring(text.encode("utf-16"))


def test_task_xml_renders_escaped_and_valid(tmp_path: Path) -> None:
    repo = "C:\\src\\pmarb & co"
    values = {
        "TASK_USER": "DESKTOP-TEST\\someone",
        "RUNNER_SCRIPT": repo + "\\deploy\\windows\\run-collector.ps1",
        "REPO_DIR": repo,
        "DATA_DIR": "D:\\pmarb-data <soak>",
        "ENV_FILE": "C:\\profile\\.config\\pmarb\\.env",
    }
    text, root = _render_task(tmp_path, values)
    assert not re.search(r"@[A-Z_]+@", text)  # every placeholder was filled
    assert "&amp; co" in text and "&lt;soak&gt;" in text

    def find(path: str) -> str:
        node = root.find(path, TASK_NS)
        assert node is not None, path
        return node.text or ""

    assert find("t:RegistrationInfo/t:URI") == "\\pmarb-collector"
    assert find("t:Triggers/t:LogonTrigger/t:UserId") == values["TASK_USER"]
    assert find("t:Principals/t:Principal/t:UserId") == values["TASK_USER"]
    assert find("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert find("t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert find("t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert find("t:Settings/t:RestartOnFailure/t:Interval") == "PT1M"
    assert find("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert find("t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert find("t:Actions/t:Exec/t:WorkingDirectory") == repo
    arguments = find("t:Actions/t:Exec/t:Arguments")
    assert f'-File "{values["RUNNER_SCRIPT"]}"' in arguments
    assert f'-RepoPath "{repo}"' in arguments
    assert f'-DataDir "{values["DATA_DIR"]}"' in arguments
    assert f'-EnvFile "{values["ENV_FILE"]}"' in arguments
    assert "-ExecutionPolicy RemoteSigned" in arguments
    assert find("t:Actions/t:Exec/t:Command").endswith("powershell.exe")


def test_task_xml_rejects_unknown_placeholders_and_line_breaks(tmp_path: Path) -> None:
    base = {
        "TASK_USER": "PC\\someone",
        "RUNNER_SCRIPT": "C:\\r\\run.ps1",
        "REPO_DIR": "C:\\r",
        "DATA_DIR": "C:\\r\\data",
        "ENV_FILE": "C:\\p\\.env",
    }
    with pytest.raises(ValueError, match="unknown template placeholder"):
        _render_task(tmp_path, {k: v for k, v in base.items() if k != "ENV_FILE"})
    with pytest.raises(ValueError, match="line breaks"):
        _render_task(tmp_path, {**base, "DATA_DIR": "C:\\r\\data\nevil"})


def test_cli_output_never_crashes_on_a_windows_code_page(win32: None, monkeypatch) -> None:
    import io

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    cli._never_crash_on_console_encoding()
    print("by Θ: —")
    stream.flush()
    assert buffer.getvalue().decode("cp1252").strip() == "by ?: —"


# --- 7. The runbook stays runnable ---------------------------------------------------

RUNBOOK = REPO / "docs" / "RUN_ON_WINDOWS.md"


def test_runbook_health_query_runs_read_only_on_a_fresh_database(tmp_path: Path) -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    pattern = r"@'\n(.*?)\n'@ \| \.\\\.venv\\Scripts\\python\.exe -"
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None
    script = match.group(1)
    script.encode("ascii")  # PowerShell 5 pipes text to python as ASCII
    open_writer(tmp_path / "pmarb.sqlite").close()
    env = {
        **os.environ,
        "PMARB_DATA_DIR": str(tmp_path),
        "PMARB_ENV_FILE": str(tmp_path / "absent.env"),
    }
    done = subprocess.run(
        [sys.executable, "-"], input=script, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "collector_events 0" in done.stdout and "now_utc" in done.stdout


def test_runbook_covers_the_checklist_and_rules() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    steps = re.findall(r"^## (\d+)\. ", text, re.MULTILINE)
    assert steps == [str(n) for n in range(1, len(steps) + 1)] and len(steps) >= 14
    for needle in (
        "Set-ExecutionPolicy -Scope Process",
        "py -3.12 --version",
        "git checkout m1/windows",
        "install.ps1 -DryRun",
        "PMARB_FEE_SCENARIO",
        "pmarb.exe doctor",
        "Start-ScheduledTask -TaskName pmarb-collector",
        "pmarb.exe report --since",
        "pmarb.exe review",
        "stop.ps1",
        "git pull --ff-only",
        "powercfg /change standby-timeout-ac 0",
        "Data is PRIVATE",
        "No API keys are needed for M1",
        "## Rules for the AI assistant",
        "--no-verify",
        "git push",
    ):
        assert needle in text, needle
    assert text.index("## Rules for the AI assistant") > text.index("## 14.")
