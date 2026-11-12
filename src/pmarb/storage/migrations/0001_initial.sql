CREATE TABLE markets (
    market_key TEXT PRIMARY KEY,
    venue TEXT,
    venue_market_id TEXT,
    title TEXT,
    league TEXT,
    participants_json TEXT,
    yes_participant TEXT,
    market_type TEXT,
    line TEXT,
    event_start_utc TEXT,
    expected_resolution_utc TEXT,
    expected_resolution_src TEXT,
    expected_payout_utc TEXT,
    status TEXT,
    rules_hash TEXT,
    fee_meta_json TEXT,
    url TEXT,
    first_seen_utc TEXT,
    last_seen_utc TEXT
);

CREATE TABLE rules_versions (
    market_key TEXT,
    rules_hash TEXT,
    rules_text TEXT,
    terms_text TEXT,
    captured_utc TEXT,
    PRIMARY KEY (market_key, rules_hash)
);

CREATE TABLE groups (
    group_id TEXT PRIMARY KEY,
    status TEXT CHECK(status IN ('candidate','approved','revoked','rejected')),
    exception_risk INTEGER,
    exception_table_json TEXT,
    reviewer TEXT,
    decided_utc TEXT,
    notes TEXT
);

CREATE TABLE group_members (
    group_id TEXT,
    market_key TEXT,
    yes_maps_to TEXT,
    approved_rules_hash TEXT,
    PRIMARY KEY (group_id, market_key)
);

CREATE TABLE review_log (
    id INTEGER PRIMARY KEY,
    group_id TEXT,
    action TEXT,
    reviewer TEXT,
    shown_hashes_json TEXT,
    result TEXT CHECK(result IN ('applied','stale_review','invalid')),
    spool_file TEXT,
    applied_utc TEXT
);

CREATE TABLE assessments (
    id INTEGER PRIMARY KEY,
    group_id TEXT,
    yes_market_key TEXT,
    no_market_key TEXT,
    qty TEXT,
    qualified INTEGER,
    reasons_json TEXT,
    gross_edge TEXT,
    net_profit TEXT,
    roc TEXT,
    capital TEXT,
    fee_yes TEXT,
    fee_no TEXT,
    fee_provenance_json TEXT,
    avg_yes TEXT,
    avg_no TEXT,
    max_profitable_qty TEXT,
    age_yes_ms INTEGER,
    age_no_ms INTEGER,
    skew_ms INTEGER,
    books_atomic INTEGER,
    class TEXT CHECK(class IN ('qualified','indicative','rejected')),
    exception_risk INTEGER,
    model_version TEXT,
    computed_utc TEXT
);

CREATE TABLE episodes (
    id INTEGER PRIMARY KEY,
    group_id TEXT,
    yes_market_key TEXT,
    no_market_key TEXT,
    qty TEXT,
    class TEXT,
    model_version TEXT,
    first_seen_utc TEXT,
    last_seen_utc TEXT,
    observations INTEGER,
    peak_net TEXT,
    min_size TEXT,
    max_size TEXT,
    termination_reason TEXT
);

CREATE TABLE collector_events (
    id INTEGER PRIMARY KEY,
    utc TEXT,
    venue TEXT,
    kind TEXT,
    detail_json TEXT
);

CREATE TABLE schema_version (
    version INTEGER NOT NULL
);

INSERT INTO schema_version VALUES (1);

CREATE INDEX assessments_group_id_computed_utc ON assessments (group_id, computed_utc);
CREATE INDEX episodes_group_id_first_seen_utc ON episodes (group_id, first_seen_utc);
CREATE INDEX collector_events_utc ON collector_events (utc);
