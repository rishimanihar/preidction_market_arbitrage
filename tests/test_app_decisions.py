import json

import pytest

from pmarb.app.decisions import build_decision, write_decision
from pmarb.osutil import portable_filename

STAMP = "2026-11-12T05:07:01Z"
TABLE = [
    {
        "outcome_case": "cancelled",
        "payouts": {"kalshi:ABC": "0.5", "polymarket_us:xyz": "0.5"},
        "preserves_unit_payout": True,
    }
]


def _decision(action="approve", acknowledged=True):
    return build_decision(
        group_id="group-1",
        action=action,
        reviewer="reviewer",
        shown_hashes={"kalshi:ABC": "hash-k", "polymarket_us:xyz": "hash-p"},
        exception_table=TABLE,
        acknowledged_exceptions=acknowledged,
        notes="synthetic review",
        created_utc=STAMP,
    )


def test_approve_requires_explicit_acknowledgment():
    with pytest.raises(ValueError, match="acknowledgment"):
        _decision(acknowledged=False)


def test_decision_is_atomic_json_with_exact_schema(tmp_path):
    decision = _decision()
    target = write_decision(tmp_path, decision)
    name = portable_filename(f"{STAMP}-group-1-approve.json")
    assert target == tmp_path / "spool" / "reviews" / name
    assert json.loads(target.read_text()) == decision
    assert set(json.loads(target.read_text())) == {
        "group_id",
        "action",
        "reviewer",
        "shown_hashes",
        "exception_table",
        "acknowledged_exceptions",
        "notes",
        "created_utc",
    }
    assert list(target.parent.iterdir()) == [target]


def test_write_decision_rejects_extra_keys(tmp_path):
    decision = _decision("reject")
    decision["unexpected"] = "no"
    with pytest.raises(ValueError, match="schema"):
        write_decision(tmp_path, decision)


def test_invalid_timestamp_and_filename_components_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="aware"):
        build_decision(
            group_id="group-1",
            action="reject",
            reviewer="reviewer",
            shown_hashes={},
            exception_table=[],
            acknowledged_exceptions=False,
            created_utc="2026-11-12T05:07:01",
        )
    unsafe = _decision("reject")
    unsafe["group_id"] = "../escape"
    with pytest.raises(ValueError, match="filename"):
        write_decision(tmp_path, unsafe)


def test_writer_revalidates_manual_payload_types(tmp_path):
    decision = _decision("reject")
    decision["exception_table"] = [
        {"outcome_case": "bad", "payouts": {}, "preserves_unit_payout": 1}
    ]
    with pytest.raises(TypeError, match="bool"):
        write_decision(tmp_path, decision)


def test_fsync_happens_before_rename(tmp_path, monkeypatch):
    import os

    from pmarb.review import spool

    calls = []
    real_fsync = os.fsync
    real_replace = os.replace

    def record_fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def record_replace(source, target):
        calls.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(spool.os, "fsync", record_fsync)
    monkeypatch.setattr(spool.os, "replace", record_replace)
    write_decision(tmp_path, _decision())
    assert calls.index("fsync") < calls.index("replace")


def test_rename_failure_leaves_no_decision_or_temp_file(tmp_path, monkeypatch):
    from pmarb.review import spool

    def fail_replace(_source, _target):
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(spool.os, "replace", fail_replace)
    with pytest.raises(OSError, match="rename"):
        write_decision(tmp_path, _decision())
    spool = tmp_path / "spool" / "reviews"
    assert list(spool.iterdir()) == []


def test_app_modules_never_import_or_call_open_writer(monkeypatch):
    import importlib

    from pmarb.app import decisions, queries
    from pmarb.storage import db

    def forbidden(*_args, **_kwargs):
        raise AssertionError("app must not open a writable database")

    monkeypatch.setattr(db, "open_writer", forbidden)
    importlib.reload(queries)
    importlib.reload(decisions)
    assert "open_writer" not in queries.__dict__
    assert "open_writer" not in decisions.__dict__
