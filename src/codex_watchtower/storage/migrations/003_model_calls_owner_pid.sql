-- Migration 003: Add owner_pid to model_calls for owner-aware reservation recovery.
--
-- The reservation recovery (R5#1) must not reclaim a reservation that
-- still has a live owner process. This migration adds an owner_pid column
-- so recovery can check whether the process that made the reservation is
-- still alive before deleting it (R6#1).

ALTER TABLE model_calls ADD COLUMN owner_pid INTEGER;
