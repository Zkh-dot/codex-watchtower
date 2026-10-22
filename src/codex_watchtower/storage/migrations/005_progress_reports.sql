CREATE TABLE progress_sources (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    source_path TEXT NOT NULL,
    last_attempt_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    bootstrap_target INTEGER,
    bootstrap_bytes INTEGER NOT NULL,
    malformed_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE progress_reports (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_cursor INTEGER NOT NULL,
    assessed_at TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE(session_id, event_cursor)
);
