-- Migration 002: Rebuild pending_deliveries with composite primary key.
--
-- The initial migration created pending_deliveries with dedup_key as the
-- sole primary key. Review R3#2 changed the ON CONFLICT target to
-- (dedup_key, chat_id) so multiple recipients can have independent retry
-- state, but the schema change was only applied to 001_initial.sql. This
-- migration rebuilds the table for existing databases that already have
-- 001 recorded.

CREATE TABLE IF NOT EXISTS pending_deliveries_new (
    dedup_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    text TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (dedup_key, chat_id)
);

-- Copy existing rows, defaulting chat_id to empty string if the old
-- schema had it as a non-key column (it did).
INSERT OR IGNORE INTO pending_deliveries_new (
    dedup_key, chat_id, session_id, text, attempt, next_retry_at, failed,
    created_at, updated_at
)
SELECT
    dedup_key,
    COALESCE(chat_id, ''),
    session_id,
    text,
    attempt,
    next_retry_at,
    failed,
    created_at,
    updated_at
FROM pending_deliveries;

DROP TABLE IF EXISTS pending_deliveries;
ALTER TABLE pending_deliveries_new RENAME TO pending_deliveries;

CREATE INDEX IF NOT EXISTS idx_pending_deliveries_session
    ON pending_deliveries (session_id);

-- Scheduler state snapshots: persisted at each completed assessment so
-- the next poll can compare current lifecycle/signal state against the
-- values at the last assessment, not the current values (R4#3).
CREATE TABLE IF NOT EXISTS scheduler_state (
    session_id TEXT PRIMARY KEY REFERENCES sessions (session_id),
    last_assessed_at TEXT,
    last_lifecycle_state TEXT,
    last_signal_fingerprint TEXT,
    last_material_progress_cursor INTEGER,
    in_progress INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
