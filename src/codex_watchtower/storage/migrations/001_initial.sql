-- Core Watchtower state store (spec section 6). Raw rollout payloads are
-- never written here; every text/summary column below holds redacted,
-- bounded, already-normalized data.

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    source TEXT,
    model TEXT,
    state TEXT NOT NULL DEFAULT 'unknown',
    status_epoch INTEGER NOT NULL DEFAULT 0,
    attention_epoch INTEGER NOT NULL DEFAULT 0,
    current_run_id TEXT,
    current_execution_epoch INTEGER,
    fatal_reason TEXT,
    fatal_detected_at TEXT,
    fatal_detail TEXT,
    goal_text TEXT,
    expected_paths TEXT NOT NULL DEFAULT '[]',
    forbidden_paths TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS executions (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    execution_epoch INTEGER NOT NULL,
    correlation_method TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, execution_epoch)
);

CREATE TABLE IF NOT EXISTS process_evidence (
    launch_id TEXT PRIMARY KEY,
    argv_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    pid INTEGER,
    started_at TEXT,
    workspace TEXT NOT NULL,
    -- Not a foreign key: the launcher may correlate to a session id before
    -- discovery has created that session's row (they can run as separate
    -- processes/times), so this must not be constrained to a pre-existing row.
    session_id TEXT,
    correlation_method TEXT,
    goal TEXT,
    expected_paths TEXT NOT NULL DEFAULT '[]',
    forbidden_paths TEXT NOT NULL DEFAULT '[]',
    exit_code INTEGER,
    exited_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Internal tailer cursor. Never returned by any API route or embedded in an
-- observation, assessment, or notification (spec section 5.2).
CREATE TABLE IF NOT EXISTS event_cursors (
    session_id TEXT PRIMARY KEY REFERENCES sessions (session_id),
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    record_ordinal INTEGER NOT NULL,
    checkpoint_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS normalized_events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    logical_event_id TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    path TEXT,
    exit_code INTEGER,
    source_type TEXT,
    run_id TEXT,
    execution_epoch INTEGER,
    source_device INTEGER,
    source_inode INTEGER,
    source_byte_offset INTEGER,
    source_record_length INTEGER,
    source_hash TEXT,
    source_original_byte_length INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, logical_event_id),
    UNIQUE (session_id, event_sequence)
);

CREATE INDEX IF NOT EXISTS idx_normalized_events_session_seq
    ON normalized_events (session_id, event_sequence);

CREATE TABLE IF NOT EXISTS rule_signals (
    signal_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    event_ids TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL,
    freshness TEXT NOT NULL DEFAULT 'current',
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (session_id, signal_id)
);

CREATE INDEX IF NOT EXISTS idx_rule_signals_active
    ON rule_signals (session_id, active);

CREATE TABLE IF NOT EXISTS assessments (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    schema_version TEXT NOT NULL DEFAULT '1.0',
    status TEXT NOT NULL,
    current_action TEXT NOT NULL,
    goal_alignment TEXT NOT NULL,
    evidence TEXT NOT NULL,
    basis_ids TEXT NOT NULL,
    concerns TEXT NOT NULL DEFAULT '[]',
    needs_attention INTEGER NOT NULL,
    recommended_human_action TEXT,
    confidence_percent INTEGER NOT NULL,
    assessed_by TEXT NOT NULL,
    escalation_reason TEXT,
    event_cursor INTEGER,
    assessed_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_assessments_session
    ON assessments (session_id, row_id DESC);

CREATE TABLE IF NOT EXISTS reports (
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    report_version INTEGER NOT NULL,
    run_id TEXT,
    execution_epoch INTEGER,
    provisional INTEGER NOT NULL,
    supersedes INTEGER,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (session_id, report_version)
);

CREATE TABLE IF NOT EXISTS deliveries (
    dedup_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    notification_status TEXT NOT NULL,
    last_cursor INTEGER,
    send_count INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_deliveries_session
    ON deliveries (session_id);

CREATE TABLE IF NOT EXISTS model_calls (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions (session_id),
    assessed_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    latency_ms INTEGER,
    input_characters INTEGER,
    success INTEGER NOT NULL DEFAULT 0,
    error_class TEXT,
    estimated_cost_cents INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_model_calls_session
    ON model_calls (session_id, row_id DESC);
