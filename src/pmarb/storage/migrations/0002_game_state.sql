CREATE TABLE game_state (
    id INTEGER PRIMARY KEY,
    source TEXT,
    event_ticker TEXT,
    milestone_id TEXT,
    league TEXT,
    status TEXT,
    period TEXT,
    clock TEXT,
    home_points INTEGER,
    away_points INTEGER,
    possession TEXT,
    last_play TEXT,
    result_pending INTEGER,
    delayed INTEGER,
    received_utc TEXT
);

CREATE INDEX game_state_event_ticker_received_utc
    ON game_state (event_ticker, received_utc);

UPDATE schema_version SET version = 2;
