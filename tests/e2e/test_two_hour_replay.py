"""Two-hour accelerated replay: incremental behavior, restart safety, notification dedup.

Replays timestamped events under a fake clock to verify the full pipeline
end-to-end: discovery, tailing, normalization, lifecycle transitions
(idle/reopen, process exit/resume), report version chains, exactly-once
ingestion across restarts, and notification deduplication.

The "two hours" is simulated wall-clock time driven by the ``now`` parameter
to ``poll_once``; no real time is spent waiting.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codex_watchtower import domain
from codex_watchtower.codex import lifecycle
from codex_watchtower.ingest import IngestionService, _persist_lifecycle
from codex_watchtower.notify import policy as notify_policy
from codex_watchtower.storage import db
from codex_watchtower.storage.repository import Repository

GRACE = timedelta(minutes=10)
T0 = datetime(2026, 8, 14, 10, 0, 0, tzinfo=UTC)


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _append(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _meta(session_id: str, ts: str) -> dict[str, object]:
    return {"type": "session_meta", "timestamp": ts, "payload": {"id": session_id, "cwd": "/w"}}


def _turn_started(ts: str) -> dict[str, object]:
    return {"type": "turn_started", "timestamp": ts, "payload": {}}


def _agent_message(ts: str, text: str) -> dict[str, object]:
    return {"type": "agent_message", "timestamp": ts, "payload": {"text": text}}


def _ts(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# Scenario 1: idle timeout -> provisional report -> reopen -> supersede
# ---------------------------------------------------------------------------


def test_idle_reopen_provisional_report_and_supersede(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "14" / "rollout-idle-reopen.jsonl"
    db_path = tmp_path / "state.db"

    _write(
        rollout,
        [
            _meta("idle-reopen", _ts(0)),
            _turn_started(_ts(1)),
            _agent_message(_ts(2), "Starting work"),
        ],
    )

    conn = db.open_database(db_path)
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo, quiet_grace_period=GRACE)

    # Ingest the first burst.
    service.poll_once(now=T0 + timedelta(minutes=2))
    session = repo.get_session("idle-reopen")
    assert session is not None
    assert session["state"] == domain.SessionState.active_turn.value

    # No new bytes; clock advances past the grace period -> idle.
    service.poll_once(now=T0 + timedelta(minutes=15))
    session = repo.get_session("idle-reopen")
    assert session["state"] == domain.SessionState.idle.value

    # A provisional report (version 1) should have been emitted.
    reports = repo.get_all_reports("idle-reopen")
    assert len(reports) == 1
    assert reports[0]["report_version"] == 1
    assert bool(reports[0]["provisional"]) is True

    # New turn appended -> session reopens to active_turn.
    _append(rollout, [_turn_started(_ts(20)), _agent_message(_ts(21), "Resuming after idle")])
    service.poll_once(now=T0 + timedelta(minutes=21))
    session = repo.get_session("idle-reopen")
    assert session["state"] == domain.SessionState.active_turn.value

    # No new bytes; grace period expires again -> second idle report (version 2),
    # which supersedes version 1.
    service.poll_once(now=T0 + timedelta(minutes=35))
    reports = repo.get_all_reports("idle-reopen")
    assert len(reports) == 2
    assert reports[1]["report_version"] == 2
    assert bool(reports[1]["provisional"]) is True
    assert reports[1]["supersedes"] == 1

    # Every normalized event appears exactly once.
    events = repo.get_events_since("idle-reopen", after_sequence=None)
    assert len(events) == 4  # 2 turn_started + 2 agent_message
    assert len({e["logical_event_id"] for e in events}) == 4

    conn.close()


# ---------------------------------------------------------------------------
# Scenario 2: process exit -> terminal_completed -> resume -> supersede
# ---------------------------------------------------------------------------


def test_resume_after_terminal_supersedes_report(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "14" / "rollout-resume.jsonl"
    db_path = tmp_path / "state.db"

    _write(
        rollout,
        [_meta("resume", _ts(0)), _turn_started(_ts(1)), _agent_message(_ts(2), "Working on task")],
    )

    conn = db.open_database(db_path)
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo, quiet_grace_period=GRACE)

    # Ingest initial events.
    service.poll_once(now=T0 + timedelta(minutes=2))

    # Bind the first execution, then simulate a zero-exit process_lifecycle event.
    session_row = repo.get_session("resume")
    assert session_row is not None
    from codex_watchtower.ingest import _lifecycle_from_row

    state = _lifecycle_from_row("resume", session_row)
    bound = lifecycle.bind_execution(
        state, run_id="run-1", now=T0 + timedelta(minutes=3), first_binding=True
    )
    _persist_lifecycle(repo, bound)

    exited = lifecycle.on_process_exit(
        bound, run_id="run-1", execution_epoch=0, exit_code=0, now=T0 + timedelta(minutes=10)
    )
    _persist_lifecycle(repo, exited)

    # Grace period elapses -> terminal_completed, final report (version 1).
    service.poll_once(now=T0 + timedelta(minutes=21))
    session = repo.get_session("resume")
    assert session["state"] == domain.SessionState.terminal_completed.value
    reports = repo.get_all_reports("resume")
    assert len(reports) == 1
    assert reports[0]["report_version"] == 1
    assert bool(reports[0]["provisional"]) is False

    # Re-read state from DB so we don't clobber report_version saved by poll_once.
    session_row = repo.get_session("resume")
    assert session_row is not None
    state_after_report = _lifecycle_from_row("resume", session_row)

    # Resume: bind a new execution (codex exec resume <same-session-id>).
    resumed = lifecycle.bind_execution(
        state_after_report, run_id="run-2", now=T0 + timedelta(minutes=25), first_binding=False
    )
    _persist_lifecycle(repo, resumed)

    # Append new events from the resumed session.
    _append(rollout, [_turn_started(_ts(26)), _agent_message(_ts(27), "Resumed session work")])

    service.poll_once(now=T0 + timedelta(minutes=27))
    session = repo.get_session("resume")
    assert session["state"] == domain.SessionState.active_turn.value
    assert session["current_execution_epoch"] == 1

    # The earlier report is superseded by version 2 (provisional: active_turn is not terminal/idle,
    # so no new report is emitted yet -- but the version chain is intact).
    reports = repo.get_all_reports("resume")
    assert len(reports) == 1  # still version 1; no new report until idle/terminal

    # Advance to idle to trigger report version 2.
    service.poll_once(now=T0 + timedelta(minutes=40))
    reports = repo.get_all_reports("resume")
    assert len(reports) == 2
    assert reports[1]["report_version"] == 2
    assert reports[1]["supersedes"] == 1

    # No event lost or duplicated.
    events = repo.get_events_since("resume", after_sequence=None)
    assert len(events) == 4
    assert len({e["logical_event_id"] for e in events}) == 4

    conn.close()


# ---------------------------------------------------------------------------
# Restart safety: new connection over the same state.db and file
# ---------------------------------------------------------------------------


def test_restart_midway_does_not_duplicate_events_or_reports(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "14" / "rollout-restart.jsonl"
    db_path = tmp_path / "state.db"

    _write(
        rollout,
        [_meta("restart", _ts(0)), _turn_started(_ts(1)), _agent_message(_ts(2), "Before restart")],
    )

    # First instance ingests the initial burst.
    conn1 = db.open_database(db_path)
    repo1 = Repository(conn1)
    IngestionService(sessions_root, repo1, quiet_grace_period=GRACE).poll_once(
        now=T0 + timedelta(minutes=2)
    )
    conn1.close()

    # Restart: brand-new connection, same file and db.
    conn2 = db.open_database(db_path)
    repo2 = Repository(conn2)
    service2 = IngestionService(sessions_root, repo2, quiet_grace_period=GRACE)
    service2.poll_once(now=T0 + timedelta(minutes=3))  # no new bytes

    events = repo2.get_events_since("restart", after_sequence=None)
    assert len(events) == 2
    assert len({e["logical_event_id"] for e in events}) == 2

    # Append after restart.
    _append(rollout, [_agent_message(_ts(5), "After restart")])
    service2.poll_once(now=T0 + timedelta(minutes=5))

    events = repo2.get_events_since("restart", after_sequence=None)
    assert len(events) == 3
    assert len({e["logical_event_id"] for e in events}) == 3

    # Advance to idle to check report count.
    service2.poll_once(now=T0 + timedelta(minutes=20))
    reports = repo2.get_all_reports("restart")
    assert len(reports) == 1  # exactly one report, not duplicated by restart

    conn2.close()


# ---------------------------------------------------------------------------
# Notification dedup: stale idle dedup entry suppresses no alert in reopened episode
# ---------------------------------------------------------------------------


def test_notification_dedup_across_reopen(tmp_path: Path) -> None:
    """Stale idle dedup entry must not suppress the alert when the session reopens.

    The dedup key is session_id + status_epoch + attention_epoch +
    notification_status + signal_fingerprint. When the session reopens,
    status_epoch advances, producing a new dedup key -- so the second idle
    alert is sent, not suppressed by the first.
    """
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "14" / "rollout-dedup.jsonl"
    db_path = tmp_path / "state.db"

    _write(rollout, [_meta("dedup", _ts(0)), _turn_started(_ts(1)), _agent_message(_ts(2), "Work")])

    conn = db.open_database(db_path)
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo, quiet_grace_period=GRACE)

    # First idle.
    service.poll_once(now=T0 + timedelta(minutes=2))
    service.poll_once(now=T0 + timedelta(minutes=15))

    reconciled = repo.get_latest_reconciled("dedup")
    assert reconciled is not None
    assert reconciled.state == domain.SessionState.idle

    key1 = notify_policy.deduplication_key(reconciled)
    decision1 = notify_policy.should_send(
        reconciled, last_delivery=None, now=T0 + timedelta(minutes=15)
    )
    assert decision1.should_send
    repo.record_delivery(
        key1, "dedup", reconciled.notification_status.value, cursor=0, sent_at=_ts(15)
    )

    # Reopen.
    _append(rollout, [_turn_started(_ts(20)), _agent_message(_ts(21), "Resumed")])
    service.poll_once(now=T0 + timedelta(minutes=21))
    service.poll_once(now=T0 + timedelta(minutes=35))

    reconciled2 = repo.get_latest_reconciled("dedup")
    assert reconciled2 is not None
    assert reconciled2.state == domain.SessionState.idle

    key2 = notify_policy.deduplication_key(reconciled2)
    assert key1 != key2, "status_epoch must advance on reopen, producing a new dedup key"

    # The second idle alert is NOT suppressed by the first delivery.
    prior_delivery = repo.get_delivery(key2)
    assert prior_delivery is None, "no prior delivery under the new key"
    decision2 = notify_policy.should_send(
        reconciled2, last_delivery=None, now=T0 + timedelta(minutes=35)
    )
    assert decision2.should_send

    conn.close()
