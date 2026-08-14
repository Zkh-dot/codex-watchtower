"""Typed repository over the Watchtower SQLite state store.

Every insert here operates on already-redacted, already-bounded values;
this module never accepts or stores a raw rollout payload. Event
deduplication and monotonic per-session event-sequence assignment
(spec section 5.2) happen in a single atomic INSERT OR IGNORE statement so
a replayed logical event id collides with its existing row and keeps that
row's original sequence, rather than racing a second writer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_watchtower import domain


@dataclass(frozen=True, slots=True)
class CursorState:
    device: int
    inode: int
    byte_offset: int
    record_ordinal: int
    checkpoint_hash: str


@dataclass(frozen=True, slots=True)
class SourceLocator:
    device: int | None = None
    inode: int | None = None
    byte_offset: int | None = None
    record_length: int | None = None
    source_hash: str | None = None
    original_byte_length: int | None = None


@dataclass(frozen=True, slots=True)
class InsertedEvent:
    event_sequence: int
    inserted: bool


@dataclass(frozen=True, slots=True)
class DeliveryState:
    dedup_key: str
    session_id: str
    notification_status: str
    last_cursor: int | None
    send_count: int
    last_sent_at: str | None


@dataclass(frozen=True, slots=True)
class ProcessEvidenceRecord:
    launch_id: str
    argv_hash: str
    state: str  # "pending" | "running" | "exited"
    pid: int | None
    started_at: str | None
    workspace: str
    session_id: str | None
    correlation_method: str | None
    goal: str | None
    expected_paths: list[str]
    forbidden_paths: list[str]
    exit_code: int | None
    exited_at: str | None
    consumed: bool = False


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    def begin_transaction(self) -> None:
        """Begin a transaction. Serializes on the shared connection (R8#3)."""
        self._lock.acquire()
        self._conn.execute("BEGIN")

    def commit_transaction(self) -> None:
        self._conn.execute("COMMIT")
        self._lock.release()

    def rollback_transaction(self) -> None:
        self._conn.execute("ROLLBACK")
        self._lock.release()

    @contextmanager
    def transaction(self) -> Any:
        """Context manager: commits on success, rolls back on any exception.

        Serializes access to the shared connection so concurrent threads
        (API handler + scheduled Luna) cannot overlap transactions (R8#3).
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # --- sessions ---------------------------------------------------

    def upsert_session(
        self,
        session_id: str,
        *,
        workspace: str,
        started_at: str,
        state: str,
        source: str | None = None,
        model: str | None = None,
        goal_text: str | None = None,
        expected_paths: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions (
                session_id, workspace, started_at, source, model, state,
                goal_text, expected_paths, forbidden_paths
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                state = excluded.state,
                model = COALESCE(excluded.model, sessions.model),
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                session_id,
                workspace,
                started_at,
                source,
                model,
                state,
                goal_text,
                json.dumps(expected_paths or []),
                json.dumps(forbidden_paths or []),
            ),
        )

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row

    def list_sessions(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM sessions ORDER BY started_at DESC"))

    def update_session_lifecycle(
        self,
        session_id: str,
        *,
        state: str,
        run_id: str | None,
        execution_epoch: int | None,
        status_epoch: int,
        last_event_at: str | None,
        report_version: int,
        pending_exit_run_id: str | None,
        pending_exit_execution_epoch: int | None,
        pending_exit_exited_at: str | None,
        fatal_reason: str | None,
        fatal_detected_at: str | None,
        fatal_detail: str | None,
    ) -> None:
        """Persist the deterministic lifecycle fields codex.lifecycle.LifecycleState owns.

        Repository stays unaware of the lifecycle module's types by design
        (storage is a lower layer than codex/ingestion); the caller
        translates LifecycleState to/from these primitive fields.
        """
        self._conn.execute(
            """
            UPDATE sessions SET
                state = ?, current_run_id = ?, current_execution_epoch = ?,
                status_epoch = ?, last_event_at = ?, report_version = ?,
                pending_exit_run_id = ?, pending_exit_execution_epoch = ?,
                pending_exit_exited_at = ?, fatal_reason = ?, fatal_detected_at = ?,
                fatal_detail = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE session_id = ?
            """,
            (
                state,
                run_id,
                execution_epoch,
                status_epoch,
                last_event_at,
                report_version,
                pending_exit_run_id,
                pending_exit_execution_epoch,
                pending_exit_exited_at,
                fatal_reason,
                fatal_detected_at,
                fatal_detail,
                session_id,
            ),
        )

    # --- reports -------------------------------------------------------

    def insert_report(
        self,
        session_id: str,
        *,
        report_version: int,
        run_id: str | None,
        execution_epoch: int | None,
        provisional: bool,
        supersedes: int | None,
        body: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO reports (
                session_id, report_version, run_id, execution_epoch, provisional,
                supersedes, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                report_version,
                run_id,
                execution_epoch,
                1 if provisional else 0,
                supersedes,
                json.dumps(body),
            ),
        )

    def get_report(self, session_id: str, report_version: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM reports WHERE session_id = ? AND report_version = ?",
            (session_id, report_version),
        ).fetchone()
        return row

    def get_all_reports(self, session_id: str) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT * FROM reports WHERE session_id = ? ORDER BY report_version ASC",
            (session_id,),
        ).fetchall()
        return list(rows)

    # --- tailer cursor (internal; never leaves the process) ---------

    def get_cursor(self, session_id: str) -> CursorState | None:
        row = self._conn.execute(
            "SELECT device, inode, byte_offset, record_ordinal, checkpoint_hash "
            "FROM event_cursors WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return CursorState(
            device=row["device"],
            inode=row["inode"],
            byte_offset=row["byte_offset"],
            record_ordinal=row["record_ordinal"],
            checkpoint_hash=row["checkpoint_hash"],
        )

    def save_cursor(self, session_id: str, cursor: CursorState) -> None:
        self._conn.execute(
            """
            INSERT INTO event_cursors (
                session_id, device, inode, byte_offset, record_ordinal, checkpoint_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                device = excluded.device,
                inode = excluded.inode,
                byte_offset = excluded.byte_offset,
                record_ordinal = excluded.record_ordinal,
                checkpoint_hash = excluded.checkpoint_hash,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                session_id,
                cursor.device,
                cursor.inode,
                cursor.byte_offset,
                cursor.record_ordinal,
                cursor.checkpoint_hash,
            ),
        )

    # --- normalized events -------------------------------------------

    def insert_event_if_new(
        self,
        session_id: str,
        logical_event_id: str,
        *,
        kind: str,
        timestamp: str,
        summary: str,
        path: str | None = None,
        exit_code: int | None = None,
        source_type: str | None = None,
        run_id: str | None = None,
        execution_epoch: int | None = None,
        locator: SourceLocator | None = None,
    ) -> InsertedEvent:
        locator = locator or SourceLocator()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO normalized_events (
                session_id, logical_event_id, event_sequence, kind, timestamp, summary,
                path, exit_code, source_type, run_id, execution_epoch,
                source_device, source_inode, source_byte_offset, source_record_length,
                source_hash, source_original_byte_length
            ) VALUES (
                ?, ?,
                (SELECT COALESCE(MAX(event_sequence), -1) + 1
                 FROM normalized_events WHERE session_id = ?),
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                session_id,
                logical_event_id,
                session_id,
                kind,
                timestamp,
                summary,
                path,
                exit_code,
                source_type,
                run_id,
                execution_epoch,
                locator.device,
                locator.inode,
                locator.byte_offset,
                locator.record_length,
                locator.source_hash,
                locator.original_byte_length,
            ),
        )
        inserted = cursor.rowcount == 1
        row = self._conn.execute(
            "SELECT event_sequence FROM normalized_events "
            "WHERE session_id = ? AND logical_event_id = ?",
            (session_id, logical_event_id),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive; UNIQUE guarantees presence
            raise RuntimeError("event insert/ignore left no row behind")
        return InsertedEvent(event_sequence=row["event_sequence"], inserted=inserted)

    def get_events_since(self, session_id: str, after_sequence: int | None) -> list[sqlite3.Row]:
        if after_sequence is None:
            return list(
                self._conn.execute(
                    "SELECT * FROM normalized_events WHERE session_id = ? "
                    "ORDER BY event_sequence ASC",
                    (session_id,),
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM normalized_events WHERE session_id = ? AND event_sequence > ? "
                "ORDER BY event_sequence ASC",
                (session_id, after_sequence),
            )
        )

    def max_event_sequence(self, session_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(event_sequence) AS m FROM normalized_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else row["m"]

    def get_recent_domain_events(self, session_id: str, *, limit: int = 500) -> list[domain.Event]:
        """Recent events as domain.Event, for feeding the rule engine (spec 5.5).

        Rule functions filter by timestamp window internally, so a bounded
        recent tail is sufficient input; ``limit`` only guards against
        reconstructing an unbounded number of rows for a very long session.
        """
        rows = self._conn.execute(
            "SELECT * FROM normalized_events WHERE session_id = ? "
            "ORDER BY event_sequence DESC LIMIT ?",
            (session_id, limit),
        )
        return [row_to_domain_event(row) for row in reversed(list(rows))]

    # --- rule signals --------------------------------------------------

    def upsert_signal(self, session_id: str, signal: domain.Signal, *, active: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO rule_signals (
                session_id, signal_id, kind, severity, source, event_ids,
                observed_at, freshness, summary, payload, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id, signal_id) DO UPDATE SET
                severity = excluded.severity,
                event_ids = excluded.event_ids,
                observed_at = excluded.observed_at,
                freshness = excluded.freshness,
                summary = excluded.summary,
                payload = excluded.payload,
                active = excluded.active
            """,
            (
                session_id,
                signal.id,
                signal.kind.value,
                signal.severity.value,
                signal.source.value,
                json.dumps(signal.event_ids),
                signal.observed_at,
                signal.freshness.value,
                signal.summary,
                json.dumps(signal.payload),
                1 if active else 0,
            ),
        )

    def get_active_signals(self, session_id: str) -> list[domain.Signal]:
        rows = self._conn.execute(
            "SELECT * FROM rule_signals WHERE session_id = ? AND active = 1", (session_id,)
        )
        return [
            domain.Signal(
                id=row["signal_id"],
                kind=domain.SignalKind(row["kind"]),
                severity=domain.Severity(row["severity"]),
                source=domain.SignalSource(row["source"]),
                event_ids=json.loads(row["event_ids"]),
                observed_at=row["observed_at"],
                freshness=domain.Freshness(row["freshness"]),
                summary=row["summary"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    def deactivate_all_signals(self, session_id: str) -> None:
        self._conn.execute("UPDATE rule_signals SET active = 0 WHERE session_id = ?", (session_id,))

    # --- reconciled assessments (spec 5.8) --------------------------

    def save_reconciled(
        self, reconciled: domain.ReconciledAssessment, *, lifecycle_status_epoch: int
    ) -> None:
        """Persist the latest reconciled result for its session.

        Validates against the authoritative JSON Schema at the persistence
        boundary so combinations the Pydantic model might accept but the
        schema forbids (fatal/identity_broken coupling, notification_status
        matching) cannot enter storage (G1).

        ``lifecycle_status_epoch`` is recorded alongside the exposed
        ``status_epoch`` (which can differ from it once rule-narrowing
        increments have been layered on top -- see assess/policy.py's
        module docstring) so the next reconciliation can compute its delta
        against ``lifecycle_state.status_epoch`` correctly.
        """
        reconciled.validate_against_schema()
        self._conn.execute(
            """
            INSERT INTO reconciled_assessments (
                session_id, notification_status, status_epoch, lifecycle_status_epoch,
                attention_epoch, needs_attention, body, reconciled_at, updated_seq
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?,
                (SELECT COALESCE(MAX(updated_seq), 0) + 1 FROM reconciled_assessments)
            )
            ON CONFLICT (session_id) DO UPDATE SET
                notification_status = excluded.notification_status,
                status_epoch = excluded.status_epoch,
                lifecycle_status_epoch = excluded.lifecycle_status_epoch,
                attention_epoch = excluded.attention_epoch,
                needs_attention = excluded.needs_attention,
                body = excluded.body,
                reconciled_at = excluded.reconciled_at,
                updated_seq = excluded.updated_seq
            """,
            (
                reconciled.session_id,
                reconciled.notification_status.value,
                reconciled.status_epoch,
                lifecycle_status_epoch,
                reconciled.attention_epoch,
                1 if reconciled.needs_attention else 0,
                json.dumps(reconciled.model_dump(mode="json")),
                reconciled.reconciled_at,
            ),
        )

    def get_reconciled_since(self, updated_seq: int | None) -> list[sqlite3.Row]:
        """Reconciled assessments across all sessions whose updated_seq exceeds the cursor.

        Backs the SSE endpoint's reconnect-cursor support: a client that
        disconnects at updated_seq N resumes with ``after=N`` and misses
        nothing, regardless of which sessions changed while it was away.
        """
        if updated_seq is None:
            return list(
                self._conn.execute("SELECT * FROM reconciled_assessments ORDER BY updated_seq ASC")
            )
        return list(
            self._conn.execute(
                "SELECT * FROM reconciled_assessments WHERE updated_seq > ? "
                "ORDER BY updated_seq ASC",
                (updated_seq,),
            )
        )

    def get_latest_reconciled(self, session_id: str) -> domain.ReconciledAssessment | None:
        row = self._conn.execute(
            "SELECT body FROM reconciled_assessments WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return domain.ReconciledAssessment.model_validate(json.loads(row["body"]))

    def get_reconciled_row(self, session_id: str) -> sqlite3.Row | None:
        """Raw row access for the epoch bookkeeping columns, not just the JSON body."""
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM reconciled_assessments WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row

    # --- assessments -----------------------------------------------

    def insert_assessment(self, session_id: str, assessment: domain.Assessment) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO assessments (
                session_id, schema_version, status, current_action, goal_alignment,
                evidence, basis_ids, concerns, needs_attention, recommended_human_action,
                confidence_percent, assessed_by, escalation_reason, event_cursor, assessed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                assessment.schema_version,
                assessment.status.value,
                assessment.current_action,
                assessment.goal_alignment.value,
                json.dumps([e.model_dump(mode="json") for e in assessment.evidence]),
                json.dumps(assessment.basis_ids),
                json.dumps([c.model_dump(mode="json") for c in assessment.concerns]),
                1 if assessment.needs_attention else 0,
                assessment.recommended_human_action,
                assessment.confidence_percent,
                assessment.assessed_by.value,
                assessment.escalation_reason,
                assessment.event_cursor,
                assessment.assessed_at,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def get_latest_assessment(self, session_id: str) -> domain.Assessment | None:
        row = self._conn.execute(
            "SELECT * FROM assessments WHERE session_id = ? ORDER BY row_id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return domain.Assessment.model_validate(
            {
                "schema_version": row["schema_version"],
                "status": row["status"],
                "current_action": row["current_action"],
                "goal_alignment": row["goal_alignment"],
                "evidence": json.loads(row["evidence"]),
                "basis_ids": json.loads(row["basis_ids"]),
                "concerns": json.loads(row["concerns"]),
                "needs_attention": bool(row["needs_attention"]),
                "recommended_human_action": row["recommended_human_action"],
                "confidence_percent": row["confidence_percent"],
                "assessed_by": row["assessed_by"],
                "escalation_reason": row["escalation_reason"],
                "event_cursor": row["event_cursor"],
                "assessed_at": row["assessed_at"],
            }
        )

    # --- deliveries (notification dedup) -----------------------------

    def get_delivery(self, dedup_key: str) -> DeliveryState | None:
        row = self._conn.execute(
            "SELECT * FROM deliveries WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if row is None:
            return None
        return DeliveryState(
            dedup_key=row["dedup_key"],
            session_id=row["session_id"],
            notification_status=row["notification_status"],
            last_cursor=row["last_cursor"],
            send_count=row["send_count"],
            last_sent_at=row["last_sent_at"],
        )

    def record_delivery(
        self,
        dedup_key: str,
        session_id: str,
        notification_status: str,
        *,
        cursor: int | None,
        sent_at: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO deliveries (
                dedup_key, session_id, notification_status, last_cursor, send_count, last_sent_at
            ) VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT (dedup_key) DO UPDATE SET
                last_cursor = excluded.last_cursor,
                send_count = deliveries.send_count + 1,
                last_sent_at = excluded.last_sent_at,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (dedup_key, session_id, notification_status, cursor, sent_at),
        )

    def pending_deliveries(self, session_id: str) -> list[DeliveryState]:
        rows = self._conn.execute(
            "SELECT * FROM deliveries WHERE session_id = ? ORDER BY updated_at DESC",
            (session_id,),
        )
        return [
            DeliveryState(
                dedup_key=row["dedup_key"],
                session_id=row["session_id"],
                notification_status=row["notification_status"],
                last_cursor=row["last_cursor"],
                send_count=row["send_count"],
                last_sent_at=row["last_sent_at"],
            )
            for row in rows
        ]

    # --- pending notification deliveries (durable retry) -------------

    def upsert_pending_delivery(
        self,
        dedup_key: str,
        session_id: str,
        chat_id: str,
        text: str,
        *,
        attempt: int,
        next_retry_at: str | None,
        failed: bool = False,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO pending_deliveries (
                dedup_key, chat_id, session_id, text, attempt, next_retry_at, failed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (dedup_key, chat_id) DO UPDATE SET
                text = excluded.text,
                attempt = excluded.attempt,
                next_retry_at = excluded.next_retry_at,
                failed = excluded.failed,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                dedup_key,
                chat_id,
                session_id,
                text,
                attempt,
                next_retry_at,
                1 if failed else 0,
            ),
        )

    def list_due_pending_deliveries(self, now_iso: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM pending_deliveries
            WHERE failed = 0 AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY created_at ASC
            """,
            (now_iso,),
        )
        return [dict(row) for row in rows]

    def has_pending_delivery(self, dedup_key: str, chat_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM pending_deliveries WHERE dedup_key = ? AND chat_id = ? AND failed = 0",
            (dedup_key, chat_id),
        ).fetchone()
        return row is not None

    def delete_pending_delivery(self, dedup_key: str, chat_id: str) -> None:
        self._conn.execute(
            "DELETE FROM pending_deliveries WHERE dedup_key = ? AND chat_id = ?",
            (dedup_key, chat_id),
        )

    # --- model call observability ------------------------------------

    def insert_model_call(
        self,
        session_id: str | None,
        assessed_by: str,
        *,
        started_at: str,
        finished_at: str | None,
        latency_ms: int | None,
        input_characters: int | None,
        success: bool,
        error_class: str | None = None,
        estimated_cost_cents: int | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO model_calls (
                session_id, assessed_by, started_at, finished_at, latency_ms,
                input_characters, success, error_class, estimated_cost_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                assessed_by,
                started_at,
                finished_at,
                latency_ms,
                input_characters,
                1 if success else 0,
                error_class,
                estimated_cost_cents,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def reserve_model_call(
        self,
        session_id: str,
        assessed_by: str,
        *,
        started_at: str,
        input_characters: int | None,
        estimated_cost_cents: int | None = None,
        owner_id: str | None = None,
    ) -> int:
        """Atomically reserve a model-call slot by inserting a pending row.

        The reservation counts toward the ceiling immediately so
        concurrent callers see it (R3#3). The estimated cost is stored
        in the row so ``sum_daily_cost_cents`` includes it (R4#2).
        ``owner_id`` is a UUID identifying the serve process that made
        the reservation so recovery can verify the owner is dead via
        the service_instances table before reclaiming (R7#1).
        """
        cursor = self._conn.execute(
            """
            INSERT INTO model_calls (
                session_id, assessed_by, started_at, finished_at, latency_ms,
                input_characters, success, error_class, estimated_cost_cents, owner_id
            ) VALUES (?, ?, ?, NULL, NULL, ?, 0, NULL, ?, ?)
            """,
            (
                session_id,
                assessed_by,
                started_at,
                input_characters,
                estimated_cost_cents,
                owner_id,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def finalize_model_call(
        self,
        row_id: int,
        *,
        success: bool,
        latency_ms: int | None,
        finished_at: str,
        error_class: str | None = None,
        estimated_cost_cents: int | None = None,
    ) -> None:
        """Update a reserved model-call row with the actual outcome."""
        self._conn.execute(
            """
            UPDATE model_calls SET
                success = ?,
                latency_ms = ?,
                finished_at = ?,
                error_class = ?,
                estimated_cost_cents = ?
            WHERE row_id = ?
            """,
            (
                1 if success else 0,
                latency_ms,
                finished_at,
                error_class,
                estimated_cost_cents,
                row_id,
            ),
        )

    def cancel_model_call(self, row_id: int) -> None:
        """Delete a reserved model-call row when the budget is exhausted."""
        self._conn.execute("DELETE FROM model_calls WHERE row_id = ?", (row_id,))

    def register_service_instance(self, owner_id: str, pid: int) -> None:
        """Register a serve process in the service_instances table (R7#1).

        Each serve instance generates a UUID at startup and registers
        itself here. The heartbeat is updated periodically so recovery
        can distinguish live owners from dead ones.
        """
        self._conn.execute(
            """
            INSERT INTO service_instances (owner_id, pid, started_at, last_heartbeat)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(owner_id) DO UPDATE SET
                last_heartbeat = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (owner_id, pid),
        )

    def update_heartbeat(self, owner_id: str) -> None:
        """Refresh the heartbeat for a registered service instance (R7#1)."""
        self._conn.execute(
            "UPDATE service_instances"
            " SET last_heartbeat = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE owner_id = ?",
            (owner_id,),
        )

    def deregister_service_instance(self, owner_id: str) -> None:
        """Remove a service instance on clean shutdown (R7#1)."""
        self._conn.execute(
            "DELETE FROM service_instances WHERE owner_id = ?",
            (owner_id,),
        )

    def recover_abandoned_reservations(
        self,
        *,
        lease_dir: Path | None = None,
        lease_seconds: int = 300,
    ) -> int:
        """Delete model_calls rows with finished_at=NULL whose owner is provably dead.

        A crash, SIGKILL, or power loss after reserve_model_call() but
        before finalize_model_call() leaves a stale row that permanently
        consumes a ceiling slot and reserved cost. This method removes
        those rows so the slots and budget are released (R5#1).

        Owner liveness uses a two-phase fencing protocol (R12#1):
        1. Quick filter: check the lease file's mtime. If recent, skip.
        2. Fencing: try to acquire a non-blocking exclusive flock on the
           lease file. If the owner is alive, it holds the lock and the
           attempt fails (BlockingIOError). If the owner is dead (crash/
           SIGKILL), the OS has released the lock and the attempt
           succeeds, proving the owner is gone before any DELETE.

        This eliminates the TOCTOU race where recovery reads a stale
        mtime and then deletes the reservation while the live owner
        renews the lease in between.

        This method must only be called from the ``serve`` startup, not
        from ``open_database()``, so ``inspect``/``doctor`` cannot
        reclaim a live serve process's reservations (R6#1).

        Returns the number of abandoned reservations recovered.
        """
        import fcntl
        import os
        from pathlib import Path as _Path

        if lease_dir is None:
            cursor = self._conn.execute(
                """
                DELETE FROM model_calls
                WHERE finished_at IS NULL
                  AND started_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
                """,
                (f"-{lease_seconds} seconds",),
            )
            return cursor.rowcount or 0

        # Select all unfinished reservations. For rows with an owner_id,
        # the flock is the authoritative liveness check and is tried
        # immediately, regardless of age (R14#1). For legacy ownerless
        # rows (owner_id IS NULL), the age gate is retained as a
        # conservative fallback.
        stale_rows = self._conn.execute(
            """
            SELECT row_id, owner_id FROM model_calls
            WHERE finished_at IS NULL
              AND (
                    owner_id IS NOT NULL
                    OR started_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
                  )
            """,
            (f"-{lease_seconds} seconds",),
        ).fetchall()

        recovered = 0
        for row in stale_rows:
            owner_id = row["owner_id"]
            if owner_id is not None:
                lease_file = _Path(lease_dir) / f"lease.{owner_id}"
                if lease_file.exists():
                    # The flock is the authoritative liveness check (R13#1).
                    # A process may have refreshed its heartbeat (mtime)
                    # and then crashed, leaving a fresh mtime but no live
                    # flock. Therefore we always attempt the non-blocking
                    # flock regardless of mtime.
                    try:
                        fd = os.open(str(lease_file), os.O_RDWR)
                    except OSError:
                        # File was removed between exists() and open() —
                        # owner is gone.
                        pass
                    else:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            # Owner is alive (holds the lock) — skip.
                            os.close(fd)
                            continue
                        else:
                            # Lock acquired — owner is dead. Clean up.
                            os.close(fd)
            self._conn.execute("DELETE FROM model_calls WHERE row_id = ?", (row["row_id"],))
            recovered += 1
        return recovered

    # --- scheduler state (R4#3) --------------------------------------

    def get_scheduler_state(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM scheduler_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def save_scheduler_state(
        self,
        session_id: str,
        *,
        last_assessed_at: str | None,
        last_lifecycle_state: str | None,
        last_signal_fingerprint: str | None,
        last_material_progress_cursor: int | None = None,
        in_progress: bool = False,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO scheduler_state (
                session_id, last_assessed_at, last_lifecycle_state,
                last_signal_fingerprint, last_material_progress_cursor, in_progress
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (session_id) DO UPDATE SET
                last_assessed_at = excluded.last_assessed_at,
                last_lifecycle_state = excluded.last_lifecycle_state,
                last_signal_fingerprint = excluded.last_signal_fingerprint,
                last_material_progress_cursor = excluded.last_material_progress_cursor,
                in_progress = excluded.in_progress,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                session_id,
                last_assessed_at,
                last_lifecycle_state,
                last_signal_fingerprint,
                last_material_progress_cursor,
                1 if in_progress else 0,
            ),
        )

    def create_process_evidence(
        self,
        launch_id: str,
        *,
        argv_hash: str,
        workspace: str,
        goal: str | None = None,
        expected_paths: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
    ) -> None:
        """Create the record *before* spawn: state=pending, pid=null.

        No PID can exist before spawn, so nothing may require one here.
        """
        self._conn.execute(
            """
            INSERT INTO process_evidence (
                launch_id, argv_hash, state, workspace, goal, expected_paths, forbidden_paths
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                launch_id,
                argv_hash,
                workspace,
                goal,
                json.dumps(expected_paths or []),
                json.dumps(forbidden_paths or []),
            ),
        )

    def mark_process_running(self, launch_id: str, *, pid: int, started_at: str) -> None:
        self._conn.execute(
            "UPDATE process_evidence SET state = 'running', pid = ?, started_at = ? "
            "WHERE launch_id = ?",
            (pid, started_at, launch_id),
        )

    def mark_process_exited(self, launch_id: str, *, exit_code: int, exited_at: str) -> None:
        self._conn.execute(
            "UPDATE process_evidence SET state = 'exited', exit_code = ?, exited_at = ? "
            "WHERE launch_id = ?",
            (exit_code, exited_at, launch_id),
        )

    def bind_process_session(
        self, launch_id: str, *, session_id: str, correlation_method: str
    ) -> None:
        self._conn.execute(
            "UPDATE process_evidence SET session_id = ?, correlation_method = ? "
            "WHERE launch_id = ?",
            (session_id, correlation_method, launch_id),
        )

    def get_process_evidence(self, launch_id: str) -> ProcessEvidenceRecord | None:
        row = self._conn.execute(
            "SELECT * FROM process_evidence WHERE launch_id = ?", (launch_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_process_evidence(row)

    def list_process_evidence_for_session(self, session_id: str) -> list[ProcessEvidenceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM process_evidence WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        return [_row_to_process_evidence(row) for row in rows]

    def list_unconsumed_process_evidence(self) -> list[ProcessEvidenceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM process_evidence WHERE state = 'exited' AND session_id IS NOT NULL "
            "AND consumed = 0 ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_process_evidence(row) for row in rows]

    def mark_process_evidence_consumed(self, launch_id: str) -> None:
        self._conn.execute(
            "UPDATE process_evidence SET consumed = 1 WHERE launch_id = ?", (launch_id,)
        )

    def count_model_calls_for_session(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM model_calls WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["cnt"] if row else 0

    def latest_model_call_started_at(self, session_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT started_at FROM model_calls WHERE session_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row["started_at"] if row else None

    def sum_daily_cost_cents(self, day: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(estimated_cost_cents), 0) as total FROM model_calls "
            "WHERE started_at LIKE ?",
            (f"{day}%",),
        ).fetchone()
        return row["total"] if row else 0


def row_to_domain_event(row: sqlite3.Row) -> domain.Event:
    return domain.Event(
        id=row["logical_event_id"],
        timestamp=row["timestamp"],
        kind=domain.EventKind(row["kind"]),
        summary=row["summary"],
        path=row["path"],
        exit_code=row["exit_code"],
        source_type=row["source_type"],
        run_id=row["run_id"],
        execution_epoch=row["execution_epoch"],
    )


def _row_to_process_evidence(row: sqlite3.Row) -> ProcessEvidenceRecord:
    return ProcessEvidenceRecord(
        launch_id=row["launch_id"],
        argv_hash=row["argv_hash"],
        state=row["state"],
        pid=row["pid"],
        started_at=row["started_at"],
        workspace=row["workspace"],
        session_id=row["session_id"],
        correlation_method=row["correlation_method"],
        goal=row["goal"],
        expected_paths=json.loads(row["expected_paths"]),
        forbidden_paths=json.loads(row["forbidden_paths"]),
        exit_code=row["exit_code"],
        exited_at=row["exited_at"],
        consumed=bool(row["consumed"]) if "consumed" in row.keys() else False,
    )
