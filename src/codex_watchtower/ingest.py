"""Orchestrate live Codex ingestion: discovery, tailing, normalization, lifecycle (Task 10).

``IngestionService.poll_once`` is the whole pipeline for one reconciliation
pass over every discovered session: it is deliberately the unit this module
is tested at, since filesystem-watch wake-ups (``watch_forever``) exist only
to call it promptly -- the correctness properties (exactly-once ingestion,
restart-safety) live in ``poll_once`` and do not depend on *what* woke it up.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codex_watchtower import domain
from codex_watchtower.codex import lifecycle
from codex_watchtower.codex.discovery import DiscoveredSession, discover_sessions
from codex_watchtower.codex.normalize import normalize_record
from codex_watchtower.codex.tailer import IdentityBroken, read_new_records, resolve_start
from codex_watchtower.storage.repository import Repository, SourceLocator


def _lifecycle_from_row(session_id: str, row: sqlite3.Row) -> lifecycle.LifecycleState:
    """Reconstruct LifecycleState from a `sessions` row."""
    get = row.__getitem__
    fatal = None
    if get("fatal_reason") is not None:
        fatal = lifecycle.FatalInfo(
            reason=domain.FatalReason(get("fatal_reason")),
            detected_at=get("fatal_detected_at") or "",
            detail=get("fatal_detail"),
        )
    pending_exit = None
    if get("pending_exit_run_id") is not None:
        pending_exit = lifecycle.PendingExit(
            run_id=get("pending_exit_run_id"),
            execution_epoch=get("pending_exit_execution_epoch"),
            exited_at=get("pending_exit_exited_at"),
        )
    return lifecycle.LifecycleState(
        session_id=session_id,
        state=domain.SessionState(get("state")),
        run_id=get("current_run_id"),
        execution_epoch=get("current_execution_epoch"),
        status_epoch=get("status_epoch"),
        last_event_at=get("last_event_at"),
        report_version=get("report_version"),
        pending_exit=pending_exit,
        fatal=fatal,
    )


def _persist_lifecycle(repo: Repository, state: lifecycle.LifecycleState) -> None:
    repo.update_session_lifecycle(
        state.session_id,
        state=state.state.value,
        run_id=state.run_id,
        execution_epoch=state.execution_epoch,
        status_epoch=state.status_epoch,
        last_event_at=state.last_event_at,
        report_version=state.report_version,
        pending_exit_run_id=state.pending_exit.run_id if state.pending_exit else None,
        pending_exit_execution_epoch=(
            state.pending_exit.execution_epoch if state.pending_exit else None
        ),
        pending_exit_exited_at=state.pending_exit.exited_at if state.pending_exit else None,
        fatal_reason=state.fatal.reason.value if state.fatal else None,
        fatal_detected_at=state.fatal.detected_at if state.fatal else None,
        fatal_detail=state.fatal.detail if state.fatal else None,
    )


class IngestionService:
    def __init__(
        self,
        sessions_root: Path,
        repo: Repository,
        *,
        quiet_grace_period: timedelta = lifecycle.DEFAULT_QUIET_GRACE_PERIOD,
        enforce_file_safety: bool = False,
        expected_uid: int | None = None,
    ) -> None:
        self.sessions_root = sessions_root
        self.repo = repo
        self.quiet_grace_period = quiet_grace_period
        self.enforce_file_safety = enforce_file_safety
        self.expected_uid = expected_uid

    def poll_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        for discovered in discover_sessions(self.sessions_root):
            self._ingest_session(discovered, now)

    def _ingest_session(self, discovered: DiscoveredSession, now: datetime) -> None:
        session_id = discovered.session_id
        path = discovered.path
        existing = self.repo.get_session(session_id)
        if existing is None:
            self.repo.upsert_session(
                session_id,
                workspace=discovered.workspace or "",
                started_at=discovered.started_at or now.isoformat(),
                state=domain.SessionState.unknown.value,
                source=discovered.source,
                model=discovered.model,
            )
            state = lifecycle.LifecycleState.initial(session_id)
        else:
            state = _lifecycle_from_row(session_id, existing)

        if state.fatal is not None:
            return  # ingestion permanently stopped for this session

        cursor = self.repo.get_cursor(session_id)
        resolved = resolve_start(path, cursor)
        if isinstance(resolved, IdentityBroken):
            state = lifecycle.mark_identity_broken(state, detail=resolved.detail, now=now)
            _persist_lifecycle(self.repo, state)
            return

        sessions_root = self.sessions_root if self.enforce_file_safety else None
        read_result = read_new_records(
            path, resolved, sessions_root=sessions_root, expected_uid=self.expected_uid
        )
        self.repo.save_cursor(session_id, read_result.cursor)

        new_events: list[domain.Event] = []
        for raw in read_result.records:
            normalized = normalize_record(session_id, raw, fallback_timestamp=now.isoformat())
            if normalized is None:
                continue
            locator = SourceLocator(
                device=raw.locator.device,
                inode=raw.locator.inode,
                byte_offset=raw.locator.byte_offset,
                record_length=raw.locator.record_length,
                source_hash=normalized.payload_hash,
            )
            inserted = self.repo.insert_event_if_new(
                session_id,
                normalized.event.id,
                kind=normalized.event.kind.value,
                timestamp=normalized.event.timestamp,
                summary=normalized.event.summary,
                path=normalized.event.path,
                exit_code=normalized.event.exit_code,
                source_type=normalized.event.source_type,
                run_id=normalized.event.run_id,
                execution_epoch=normalized.event.execution_epoch,
                locator=locator,
            )
            if inserted.inserted:
                new_events.append(normalized.event)

        if new_events:
            state = lifecycle.on_new_events(state, new_events, now)
        if state.fatal is None:
            state = lifecycle.tick(state, now=now, quiet_grace_period=self.quiet_grace_period)

        transition = lifecycle.report_for_terminal_or_idle(state)
        state = transition.state
        if transition.report is not None:
            self.repo.insert_report(
                session_id,
                report_version=transition.report.report_version,
                run_id=state.run_id,
                execution_epoch=state.execution_epoch,
                provisional=transition.report.provisional,
                supersedes=transition.report.supersedes,
                body={
                    "state": state.state.value,
                    "elapsed_seconds": None,
                    "session_id": session_id,
                },
            )

        _persist_lifecycle(self.repo, state)


def watch_forever(
    service: IngestionService, *, poll_interval: timedelta = timedelta(seconds=5)
) -> None:  # pragma: no cover - thin runtime loop, exercised via poll_once directly
    """Wake on filesystem changes (watchfiles) and reconcile periodically as a backstop.

    Not unit-tested directly: it is a thin wrapper whose only job is to
    call ``poll_once`` promptly. The ingestion correctness properties this
    module cares about live in ``poll_once`` and its tests.
    """
    from watchfiles import watch

    timeout_ms = int(poll_interval.total_seconds() * 1000)
    for _changes in watch(service.sessions_root, rust_timeout=timeout_ms, yield_on_timeout=True):
        service.poll_once()
