-- Migration 004: Add owner_id (UUID) to model_calls and create
-- service_instances table for owner-aware reservation recovery.
--
-- PID alone is insufficient for owner identity because a restarted
-- process can receive the same PID (e.g., PID 1 in containers). This
-- migration adds a UUID-based owner_id and a service_instances table
-- that tracks which owner UUIDs are currently active (R7#1).

ALTER TABLE model_calls ADD COLUMN owner_id TEXT;

CREATE TABLE IF NOT EXISTS service_instances (
    owner_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_heartbeat TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
