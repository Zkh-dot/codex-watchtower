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
from dataclasses import dataclass

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


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

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

    # --- process evidence (launcher, Task 9A) -------------------------

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
    )
