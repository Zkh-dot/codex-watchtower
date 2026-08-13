from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from codex_watchtower import domain
from codex_watchtower.storage import db
from codex_watchtower.storage.repository import CursorState, Repository, SourceLocator


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    conn = db.open_database(tmp_path / "state.db")
    return Repository(conn)


def _seed_session(repo: Repository, session_id: str = "sess-1") -> None:
    repo.upsert_session(
        session_id,
        workspace="/home/user/project",
        started_at="2026-08-13T10:00:00Z",
        state="active_turn",
    )


# --- migrations ------------------------------------------------------


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn1 = db.open_database(path)
    conn1.execute(
        "INSERT INTO sessions (session_id, workspace, started_at, state) "
        "VALUES ('s', 'w', '2026-08-13T10:00:00Z', 'active_turn')"
    )
    conn1.close()

    conn2 = db.open_database(path)  # re-running migrate() must not error or wipe data
    row = conn2.execute("SELECT session_id FROM sessions WHERE session_id = 's'").fetchone()
    assert row is not None
    conn2.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    conn = db.open_database(tmp_path / "state.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# --- events: dedup, redaction boundary, sequence assignment --------


def test_insert_event_assigns_sequence_and_cursor_update_is_atomic(repo: Repository) -> None:
    _seed_session(repo)
    result = repo.insert_event_if_new(
        "sess-1",
        "logical-1",
        kind="command",
        timestamp="2026-08-13T10:00:30Z",
        summary="ran pytest",  # redacted/bounded summary, never a raw payload
        locator=SourceLocator(device=1, inode=2, byte_offset=0, record_length=120),
    )
    assert result.inserted is True
    assert result.event_sequence == 0

    repo.save_cursor(
        "sess-1",
        CursorState(
            device=1, inode=2, byte_offset=120, record_ordinal=1, checkpoint_hash="deadbeef"
        ),
    )
    cursor = repo.get_cursor("sess-1")
    assert cursor is not None
    assert cursor.byte_offset == 120


def test_normalized_events_table_has_no_raw_payload_column(repo: Repository) -> None:
    columns = {
        row["name"] for row in repo.connection.execute("PRAGMA table_info(normalized_events)")
    }
    assert "raw_payload" not in columns
    assert "payload" not in columns
    assert {"summary", "source_hash", "source_original_byte_length"} <= columns


def test_replayed_logical_event_id_is_ignored_and_keeps_original_sequence(
    repo: Repository,
) -> None:
    _seed_session(repo)
    first = repo.insert_event_if_new(
        "sess-1", "logical-1", kind="command", timestamp="2026-08-13T10:00:30Z", summary="a"
    )
    second = repo.insert_event_if_new(
        "sess-1",
        "logical-1",
        kind="command",
        timestamp="2026-08-13T10:00:30Z",
        summary="a-different-summary-must-not-matter",
    )
    assert first.inserted is True
    assert second.inserted is False
    assert first.event_sequence == second.event_sequence

    rows = repo.get_events_since("sess-1", after_sequence=None)
    assert len(rows) == 1


def test_event_sequence_is_monotonic_per_session(repo: Repository) -> None:
    _seed_session(repo)
    seqs = [
        repo.insert_event_if_new(
            "sess-1",
            f"logical-{i}",
            kind="command",
            timestamp="2026-08-13T10:00:30Z",
            summary=f"cmd {i}",
        ).event_sequence
        for i in range(5)
    ]
    assert seqs == sorted(seqs)
    assert seqs == list(range(5))


def test_concurrent_insert_of_same_logical_id_deduplicates_exactly_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    setup_conn = db.open_database(path)
    Repository(setup_conn).upsert_session(
        "sess-1", workspace="/w", started_at="2026-08-13T10:00:00Z", state="active_turn"
    )
    setup_conn.close()

    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = db.connect(path)
        try:
            r = Repository(conn).insert_event_if_new(
                "sess-1",
                "logical-race",
                kind="command",
                timestamp="2026-08-13T10:00:30Z",
                summary="racey",
            )
            with lock:
                results.append(r.inserted)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == 7

    verify_conn = db.connect(path)
    rows = verify_conn.execute(
        "SELECT COUNT(*) FROM normalized_events WHERE session_id = 'sess-1'"
    ).fetchone()
    assert rows[0] == 1
    verify_conn.close()


def test_get_events_since_returns_only_newer_events(repo: Repository) -> None:
    _seed_session(repo)
    for i in range(3):
        repo.insert_event_if_new(
            "sess-1",
            f"logical-{i}",
            kind="command",
            timestamp="2026-08-13T10:00:30Z",
            summary=f"cmd {i}",
        )
    since = repo.get_events_since("sess-1", after_sequence=0)
    assert [r["event_sequence"] for r in since] == [1, 2]


# --- latest assessment and pending deliveries -----------------------


def _sample_assessment(event_cursor: int = 3) -> domain.Assessment:
    return domain.Assessment(
        status=domain.AssessmentStatus.progressing,
        current_action="Refactoring the parser.",
        goal_alignment=domain.GoalAlignment.aligned,
        evidence=[domain.Evidence(ref_type=domain.RefType.event, ref_id="evt:1", claim="did x")],
        basis_ids=["evt:1"],
        needs_attention=False,
        confidence_percent=80,
        assessed_by=domain.AssessedBy.luna,
        event_cursor=event_cursor,
        assessed_at="2026-08-13T10:02:00Z",
    )


def test_latest_assessment_query_returns_most_recent(repo: Repository) -> None:
    _seed_session(repo)
    repo.insert_assessment("sess-1", _sample_assessment(event_cursor=1))
    repo.insert_assessment("sess-1", _sample_assessment(event_cursor=5))
    latest = repo.get_latest_assessment("sess-1")
    assert latest is not None
    assert latest.event_cursor == 5


def test_latest_assessment_query_none_when_absent(repo: Repository) -> None:
    _seed_session(repo)
    assert repo.get_latest_assessment("sess-1") is None


def test_pending_delivery_queries(repo: Repository) -> None:
    _seed_session(repo)
    repo.record_delivery(
        "sess-1:1:0:progressing:fp1",
        "sess-1",
        "progressing",
        cursor=3,
        sent_at="2026-08-13T10:02:00Z",
    )
    pending = repo.pending_deliveries("sess-1")
    assert len(pending) == 1
    assert pending[0].send_count == 1

    repo.record_delivery(
        "sess-1:1:0:progressing:fp1",
        "sess-1",
        "progressing",
        cursor=6,
        sent_at="2026-08-13T10:12:00Z",
    )
    updated = repo.get_delivery("sess-1:1:0:progressing:fp1")
    assert updated is not None
    assert updated.send_count == 2
    assert updated.last_cursor == 6


# --- repository tests run twice against the same temporary database --


def test_repository_suite_runs_twice_against_same_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    for _ in range(2):
        conn = db.open_database(path)
        repo = Repository(conn)
        repo.upsert_session(
            "sess-x", workspace="/w", started_at="2026-08-13T10:00:00Z", state="active_turn"
        )
        repo.insert_event_if_new(
            "sess-x", "logical-only", kind="command", timestamp="2026-08-13T10:00:30Z", summary="x"
        )
        conn.close()

    conn = sqlite3.connect(path)
    count = conn.execute("SELECT COUNT(*) FROM normalized_events").fetchone()[0]
    conn.close()
    assert count == 1  # second pass re-inserts the same logical id; it must not duplicate
