"""The PR-0 SQLite migration matches plan section 6.4 and is package data."""

import sqlite3
from collections.abc import Iterator
from importlib.resources import files

import pytest

# (name, declared type, primary-key position; 0 means not part of the key)
TABLES: dict[str, list[tuple[str, str, int]]] = {
    "markets": [
        ("market_key", "TEXT", 1),
        ("venue", "TEXT", 0),
        ("venue_market_id", "TEXT", 0),
        ("title", "TEXT", 0),
        ("league", "TEXT", 0),
        ("participants_json", "TEXT", 0),
        ("yes_participant", "TEXT", 0),
        ("market_type", "TEXT", 0),
        ("line", "TEXT", 0),
        ("event_start_utc", "TEXT", 0),
        ("expected_resolution_utc", "TEXT", 0),
        ("expected_resolution_src", "TEXT", 0),
        ("expected_payout_utc", "TEXT", 0),
        ("status", "TEXT", 0),
        ("rules_hash", "TEXT", 0),
        ("fee_meta_json", "TEXT", 0),
        ("url", "TEXT", 0),
        ("first_seen_utc", "TEXT", 0),
        ("last_seen_utc", "TEXT", 0),
    ],
    "rules_versions": [
        ("market_key", "TEXT", 1),
        ("rules_hash", "TEXT", 2),
        ("rules_text", "TEXT", 0),
        ("terms_text", "TEXT", 0),
        ("captured_utc", "TEXT", 0),
    ],
    "groups": [
        ("group_id", "TEXT", 1),
        ("status", "TEXT", 0),
        ("exception_risk", "INTEGER", 0),
        ("exception_table_json", "TEXT", 0),
        ("reviewer", "TEXT", 0),
        ("decided_utc", "TEXT", 0),
        ("notes", "TEXT", 0),
    ],
    "group_members": [
        ("group_id", "TEXT", 1),
        ("market_key", "TEXT", 2),
        ("yes_maps_to", "TEXT", 0),
        ("approved_rules_hash", "TEXT", 0),
    ],
    "review_log": [
        ("id", "INTEGER", 1),
        ("group_id", "TEXT", 0),
        ("action", "TEXT", 0),
        ("reviewer", "TEXT", 0),
        ("shown_hashes_json", "TEXT", 0),
        ("result", "TEXT", 0),
        ("spool_file", "TEXT", 0),
        ("applied_utc", "TEXT", 0),
    ],
    "assessments": [
        ("id", "INTEGER", 1),
        ("group_id", "TEXT", 0),
        ("yes_market_key", "TEXT", 0),
        ("no_market_key", "TEXT", 0),
        ("qty", "TEXT", 0),
        ("qualified", "INTEGER", 0),
        ("reasons_json", "TEXT", 0),
        ("gross_edge", "TEXT", 0),
        ("net_profit", "TEXT", 0),
        ("roc", "TEXT", 0),
        ("capital", "TEXT", 0),
        ("fee_yes", "TEXT", 0),
        ("fee_no", "TEXT", 0),
        ("fee_provenance_json", "TEXT", 0),
        ("avg_yes", "TEXT", 0),
        ("avg_no", "TEXT", 0),
        ("max_profitable_qty", "TEXT", 0),
        ("age_yes_ms", "INTEGER", 0),
        ("age_no_ms", "INTEGER", 0),
        ("skew_ms", "INTEGER", 0),
        ("books_atomic", "INTEGER", 0),
        ("class", "TEXT", 0),
        ("exception_risk", "INTEGER", 0),
        ("model_version", "TEXT", 0),
        ("computed_utc", "TEXT", 0),
    ],
    "episodes": [
        ("id", "INTEGER", 1),
        ("group_id", "TEXT", 0),
        ("yes_market_key", "TEXT", 0),
        ("no_market_key", "TEXT", 0),
        ("qty", "TEXT", 0),
        ("class", "TEXT", 0),
        ("model_version", "TEXT", 0),
        ("first_seen_utc", "TEXT", 0),
        ("last_seen_utc", "TEXT", 0),
        ("observations", "INTEGER", 0),
        ("peak_net", "TEXT", 0),
        ("min_size", "TEXT", 0),
        ("max_size", "TEXT", 0),
        ("termination_reason", "TEXT", 0),
    ],
    "collector_events": [
        ("id", "INTEGER", 1),
        ("utc", "TEXT", 0),
        ("venue", "TEXT", 0),
        ("kind", "TEXT", 0),
        ("detail_json", "TEXT", 0),
    ],
    "schema_version": [
        ("version", "INTEGER", 0),
    ],
}


def _migration_sql() -> str:
    resource = files("pmarb.storage").joinpath("migrations", "0001_initial.sql")
    assert resource.is_file()
    text = resource.read_text(encoding="utf-8")
    assert "CREATE TABLE markets" in text
    assert "CREATE TABLE schema_version" in text
    return text


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_migration_sql())
    try:
        yield connection
    finally:
        connection.close()


def test_migration_is_package_data_and_creates_plan_tables(db: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == set(TABLES)
    for table, columns in TABLES.items():
        info = db.execute(f"PRAGMA table_info({table})").fetchall()
        assert [(row[1], row[2], row[5]) for row in info] == columns
    version = db.execute("PRAGMA table_info(schema_version)").fetchone()
    assert version is not None
    assert version[3] == 1
    assert db.execute("SELECT version FROM schema_version").fetchone() == (1,)
    assert db.execute("SELECT COUNT(*) FROM schema_version").fetchone() == (1,)


def test_indexes_cover_group_time_and_collector_utc(db: sqlite3.Connection) -> None:
    assert ("group_id", "computed_utc") in _index_columns(db, "assessments")
    assert ("group_id", "first_seen_utc") in _index_columns(db, "episodes")
    assert ("utc",) in _index_columns(db, "collector_events")


def test_check_constraints_reject_bad_status_result_and_class(db: sqlite3.Connection) -> None:
    for status in ("candidate", "approved", "revoked", "rejected"):
        db.execute("INSERT INTO groups(group_id, status) VALUES (?, ?)", (status, status))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO groups(group_id, status) VALUES ('bad', 'nope')")

    for result in ("applied", "stale_review", "invalid"):
        db.execute("INSERT INTO review_log(group_id, result) VALUES ('g', ?)", (result,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO review_log(group_id, result) VALUES ('g', 'nope')")

    for result_class in ("qualified", "indicative", "rejected"):
        db.execute(
            "INSERT INTO assessments(group_id, class) VALUES ('g', ?)",
            (result_class,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO assessments(group_id, class) VALUES ('g', 'nope')")


def _index_columns(connection: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for row in connection.execute(f"PRAGMA index_list({table})"):
        index_name = row[1]
        columns = tuple(
            column[2] for column in connection.execute(f"PRAGMA index_info({index_name})")
        )
        found.add(columns)
    return found
