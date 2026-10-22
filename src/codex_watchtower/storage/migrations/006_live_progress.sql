CREATE TABLE progress_live_reports (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_cursor INTEGER NOT NULL,
    assessed_at TEXT NOT NULL,
    body TEXT NOT NULL,
    coverage TEXT NOT NULL,
    UNIQUE(session_id, event_cursor)
);
ALTER TABLE progress_sources ADD COLUMN live_attempt_at REAL NOT NULL DEFAULT 0;
ALTER TABLE progress_sources ADD COLUMN live_error TEXT;
