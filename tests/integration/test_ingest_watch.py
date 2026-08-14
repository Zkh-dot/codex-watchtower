from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from codex_watchtower import domain
from codex_watchtower.ingest import IngestionService
from codex_watchtower.storage import db
from codex_watchtower.storage.repository import Repository

NOW = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _append(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("a") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def test_events_reach_sqlite_exactly_once(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "13" / "rollout-1.jsonl"
    _write(
        rollout,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-13T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/w"},
            },
            {"type": "turn_started", "timestamp": "2026-08-13T10:00:05Z", "payload": {}},
            {
                "type": "agent_message",
                "timestamp": "2026-08-13T10:00:10Z",
                "payload": {"text": "hello"},
            },
        ],
    )

    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo)
    service.poll_once(now=NOW)

    events = repo.get_events_since("sess-1", after_sequence=None)
    assert len(events) == 2  # turn_started + agent_message; session_meta is not an event

    session = repo.get_session("sess-1")
    assert session is not None
    assert session["state"] == domain.SessionState.active_turn.value

    # Poll again with no new bytes: no duplicate events.
    service.poll_once(now=NOW)
    events_again = repo.get_events_since("sess-1", after_sequence=None)
    assert len(events_again) == 2


def test_restart_does_not_duplicate_events(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "13" / "rollout-1.jsonl"
    db_path = tmp_path / "state.db"
    _write(
        rollout,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-13T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/w"},
            },
            {"type": "turn_started", "timestamp": "2026-08-13T10:00:05Z", "payload": {}},
        ],
    )

    conn1 = db.open_database(db_path)
    repo1 = Repository(conn1)
    IngestionService(sessions_root, repo1).poll_once(now=NOW)
    conn1.close()

    # "Restart": brand-new connection and service instance over the same file.
    conn2 = db.open_database(db_path)
    repo2 = Repository(conn2)
    service2 = IngestionService(sessions_root, repo2)
    service2.poll_once(now=NOW)  # no new bytes; must not duplicate

    _append(
        rollout,
        [
            {
                "type": "agent_message",
                "timestamp": "2026-08-13T10:00:20Z",
                "payload": {"text": "more"},
            }
        ],
    )
    service2.poll_once(now=NOW)

    events = repo2.get_events_since("sess-1", after_sequence=None)
    assert len(events) == 2  # turn_started (from before restart) + agent_message (after)
    assert len({e["logical_event_id"] for e in events}) == 2


def test_poll_once_persists_a_reconciled_assessment(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "13" / "rollout-1.jsonl"
    _write(
        rollout,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-13T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/w"},
            },
            {"type": "turn_started", "timestamp": "2026-08-13T10:00:05Z", "payload": {}},
        ],
    )
    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    IngestionService(sessions_root, repo).poll_once(now=NOW)

    reconciled = repo.get_latest_reconciled("sess-1")
    assert reconciled is not None
    assert reconciled.session_id == "sess-1"
    assert reconciled.state == domain.SessionState.active_turn
    assert reconciled.model_assessment is None
    reconciled.validate_against_schema()


def test_forbidden_path_change_produces_active_signal_and_needs_attention(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "13" / "rollout-1.jsonl"
    _write(
        rollout,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-13T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": str(tmp_path)},
            },
            {
                "type": "file_change",
                "timestamp": "2026-08-13T10:00:05Z",
                "payload": {"path": "secrets/key.pem", "change": "modified"},
            },
        ],
    )
    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    repo.upsert_session(
        "sess-1",
        workspace=str(tmp_path),
        started_at="2026-08-13T10:00:00Z",
        state="unknown",
        forbidden_paths=["secrets/"],
    )
    IngestionService(sessions_root, repo).poll_once(now=NOW)

    reconciled = repo.get_latest_reconciled("sess-1")
    assert reconciled is not None
    assert reconciled.needs_attention is True
    assert any(s.kind == domain.SignalKind.forbidden_path for s in reconciled.active_signals)


def test_terminal_completion_emits_a_report(tmp_path: Path) -> None:
    from datetime import timedelta

    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "13" / "rollout-1.jsonl"
    _write(
        rollout,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-13T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/w"},
            },
        ],
    )
    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo)
    service.poll_once(now=NOW)

    from codex_watchtower.codex import lifecycle as lifecycle_module

    bound = lifecycle_module.bind_execution(
        lifecycle_module.LifecycleState.initial("sess-1"),
        run_id="run-1",
        now=NOW,
        first_binding=True,
    )
    exited = lifecycle_module.on_process_exit(
        bound, run_id="run-1", execution_epoch=0, exit_code=0, now=NOW
    )
    from codex_watchtower.ingest import _persist_lifecycle

    _persist_lifecycle(repo, exited)

    service.poll_once(now=NOW + timedelta(minutes=11))

    session = repo.get_session("sess-1")
    assert session is not None
    assert session["state"] == domain.SessionState.terminal_completed.value
    report = repo.get_report("sess-1", 1)
    assert report is not None
    assert bool(report["provisional"]) is False
