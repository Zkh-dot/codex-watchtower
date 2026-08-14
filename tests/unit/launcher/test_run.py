from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from types import FrameType

import pytest

from codex_watchtower.launcher import run as run_module
from codex_watchtower.storage import db
from codex_watchtower.storage.repository import Repository


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    conn = db.open_database(tmp_path / "state.db")
    return Repository(conn)


def _script(tmp_path: Path, body: str) -> list[str]:
    script_path = tmp_path / "fake_codex.py"
    script_path.write_text(textwrap.dedent(body))
    return [sys.executable, str(script_path)]


# --- exit code forwarding, stdio passthrough ------------------------


def test_forwards_child_exit_code(tmp_path: Path, repo: Repository) -> None:
    argv = _script(tmp_path, "import sys; sys.exit(7)")
    sessions_root = tmp_path / "sessions"
    result = run_module.run_wrapped(
        argv, repo=repo, sessions_root=sessions_root, stdout_stream=io.BytesIO()
    )
    assert result.exit_code == 7


def test_stdout_forwarded_byte_for_byte(tmp_path: Path, repo: Repository) -> None:
    payload = "hello watchtower\nsecond line\n"
    argv = _script(tmp_path, f"import sys; sys.stdout.write({payload!r}); sys.stdout.flush()")
    sessions_root = tmp_path / "sessions"
    captured = io.BytesIO()
    run_module.run_wrapped(argv, repo=repo, sessions_root=sessions_root, stdout_stream=captured)
    assert captured.getvalue() == payload.encode()


def test_launcher_never_opens_a_pipe_to_child_stdin(
    tmp_path: Path, repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_kwargs: dict[str, object] = {}
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen_kwargs.update(kwargs)
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    argv = _script(tmp_path, "pass")
    run_module.run_wrapped(
        argv, repo=repo, sessions_root=tmp_path / "sessions", stdout_stream=io.BytesIO()
    )
    assert seen_kwargs.get("stdin") != subprocess.PIPE


# --- record lifecycle: pending -> running -> exited ----------------------


def test_record_lifecycle_pending_running_exited(tmp_path: Path, repo: Repository) -> None:
    argv = _script(tmp_path, "import sys; sys.exit(0)")
    result = run_module.run_wrapped(
        argv, repo=repo, sessions_root=tmp_path / "sessions", stdout_stream=io.BytesIO()
    )
    record = repo.get_process_evidence(result.launch_id)
    assert record is not None
    assert record.state == "exited"
    assert record.exit_code == 0
    assert record.pid is not None
    assert record.started_at is not None
    assert record.exited_at is not None


def test_record_lifecycle_nonzero_exit(tmp_path: Path, repo: Repository) -> None:
    argv = _script(tmp_path, "import sys; sys.exit(3)")
    result = run_module.run_wrapped(
        argv, repo=repo, sessions_root=tmp_path / "sessions", stdout_stream=io.BytesIO()
    )
    record = repo.get_process_evidence(result.launch_id)
    assert record is not None
    assert record.state == "exited"
    assert record.exit_code == 3


def test_record_lifecycle_signal_termination(tmp_path: Path, repo: Repository) -> None:
    # Real OS signal *delivery* from the launcher to the child is exercised
    # separately (unit-level, no OS signal) in
    # test_signal_forwarding_calls_send_signal_on_sigint_and_sigterm to
    # avoid sandbox signal-delivery flakiness. Here we only need a process
    # that terminates itself via signal, to verify exit-evidence recording
    # for the signal-termination path.
    self_term_argv = _script(tmp_path, "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    result = run_module.run_wrapped(
        self_term_argv, repo=repo, sessions_root=tmp_path / "sessions", stdout_stream=io.BytesIO()
    )
    record = repo.get_process_evidence(result.launch_id)
    assert record is not None
    assert record.state == "exited"
    assert record.exit_code == -signal_number()


def signal_number() -> int:
    import signal

    return int(signal.SIGTERM)


# --- failed spawn ----------------------------------------------------


def test_failed_spawn_leaves_record_pending_and_binds_no_session(
    tmp_path: Path, repo: Repository
) -> None:
    argv = [str(tmp_path / "does-not-exist-binary")]
    with pytest.raises(run_module.SpawnFailed):
        run_module.run_wrapped(
            argv, repo=repo, sessions_root=tmp_path / "sessions", stdout_stream=io.BytesIO()
        )
    rows = repo.connection.execute("SELECT launch_id, state, session_id FROM process_evidence")
    all_rows = rows.fetchall()
    assert len(all_rows) == 1
    assert all_rows[0]["state"] == "pending"
    assert all_rows[0]["session_id"] is None


# --- canonical correlation from stdout ------------------------------


def test_canonical_correlation_binds_session_id_and_forwards_bytes(
    tmp_path: Path, repo: Repository
) -> None:
    record = json.dumps({"type": "session_meta", "payload": {"id": "sess-canon-1"}})
    argv = _script(
        tmp_path,
        f"""
        import sys
        sys.stdout.write({record!r} + "\\n")
        sys.stdout.write("more output after the meta line\\n")
        sys.stdout.flush()
        """,
    )
    captured = io.BytesIO()
    result = run_module.run_wrapped(
        argv, repo=repo, sessions_root=tmp_path / "sessions", stdout_stream=captured
    )
    assert result.session_id == "sess-canon-1"
    assert result.correlation_method == "stdout_canonical"
    assert record.encode() in captured.getvalue()
    assert b"more output after the meta line" in captured.getvalue()

    db_record = repo.get_process_evidence(result.launch_id)
    assert db_record is not None
    assert db_record.session_id == "sess-canon-1"
    assert db_record.correlation_method == "stdout_canonical"


# --- snapshot correlation --------------------------------------------


def _write_rollout(
    path: Path, session_id: str, cwd: str, ts: str, extra_lines: list[str] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {"type": "session_meta", "timestamp": ts, "payload": {"id": session_id, "cwd": cwd}}
        )
    ]
    lines.extend(extra_lines or [])
    path.write_text("\n".join(lines) + "\n")


def test_snapshot_correlation_matches_newly_created_rollout(
    tmp_path: Path, repo: Repository
) -> None:
    sessions_root = tmp_path / "sessions"
    workspace = str(tmp_path / "myproject")

    # The timestamp must be computed by the *child* at write time, not by
    # the test process before spawn -- otherwise it can predate started_at
    # and fall outside the correlation window by construction.
    body = f"""
    import json
    from datetime import datetime, UTC
    from pathlib import Path
    root = Path({str(sessions_root)!r})
    target = root / "2026" / "08" / "13" / "rollout-new.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    payload = {{"id": "sess-snap-1", "cwd": {workspace!r}}}
    record = {{"type": "session_meta", "timestamp": ts, "payload": payload}}
    target.write_text(json.dumps(record) + "\\n")
    """

    argv = _script(tmp_path, body)
    result = run_module.run_wrapped(
        argv,
        repo=repo,
        sessions_root=sessions_root,
        workspace=workspace,
        stdout_stream=io.BytesIO(),
    )
    assert result.correlation_method == "snapshot_unique"
    assert result.session_id == "sess-snap-1"


def test_snapshot_correlation_matches_existing_rollout_that_grows(
    tmp_path: Path, repo: Repository
) -> None:
    sessions_root = tmp_path / "sessions"
    workspace = str(tmp_path / "myproject")
    existing = sessions_root / "2026" / "08" / "13" / "rollout-existing.jsonl"
    _write_rollout(existing, "sess-resume-1", workspace, "2026-08-13T09:00:00+00:00")

    body = f"""
    import json
    from datetime import datetime, UTC
    from pathlib import Path
    target = Path({str(existing)!r})
    ts = datetime.now(UTC).isoformat()
    with target.open("a") as f:
        f.write(json.dumps({{"type": "turn_started", "timestamp": ts, "payload": {{}}}}) + "\\n")
    """

    argv = _script(tmp_path, body)
    result = run_module.run_wrapped(
        argv,
        repo=repo,
        sessions_root=sessions_root,
        workspace=workspace,
        stdout_stream=io.BytesIO(),
    )
    assert result.correlation_method == "snapshot_unique"
    assert result.session_id == "sess-resume-1"


def test_snapshot_correlation_zero_candidates_records_none(
    tmp_path: Path, repo: Repository
) -> None:
    argv = _script(tmp_path, "pass")  # writes no rollout at all
    result = run_module.run_wrapped(
        argv,
        repo=repo,
        sessions_root=tmp_path / "sessions",
        workspace=str(tmp_path / "myproject"),
        stdout_stream=io.BytesIO(),
    )
    assert result.correlation_method == "none"
    assert result.session_id is None


def test_snapshot_correlation_two_concurrent_candidates_records_none(
    tmp_path: Path, repo: Repository
) -> None:
    sessions_root = tmp_path / "sessions"
    workspace = str(tmp_path / "myproject")

    body = f"""
    import json
    from datetime import datetime, UTC
    from pathlib import Path
    root = Path({str(sessions_root)!r})
    for name in ("a", "b"):
        target = root / "2026" / "08" / "13" / f"rollout-{{name}}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).isoformat()
        payload = {{"id": f"sess-{{name}}", "cwd": {workspace!r}}}
        record = {{"type": "session_meta", "timestamp": ts, "payload": payload}}
        target.write_text(json.dumps(record) + "\\n")
    """

    argv = _script(tmp_path, body)
    result = run_module.run_wrapped(
        argv,
        repo=repo,
        sessions_root=sessions_root,
        workspace=workspace,
        stdout_stream=io.BytesIO(),
    )
    assert result.correlation_method == "none"
    assert result.session_id is None


def test_snapshot_correlation_window_expiry_records_none(tmp_path: Path, repo: Repository) -> None:
    sessions_root = tmp_path / "sessions"
    workspace = str(tmp_path / "myproject")

    old_ts = "2020-01-01T00:00:00+00:00"  # long before the launch window
    body = f"""
    import json
    from pathlib import Path
    root = Path({str(sessions_root)!r})
    target = root / "2026" / "08" / "13" / "rollout-stale.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {{"id": "sess-stale", "cwd": {workspace!r}}}
    record = {{"type": "session_meta", "timestamp": {old_ts!r}, "payload": payload}}
    target.write_text(json.dumps(record) + "\\n")
    """

    argv = _script(tmp_path, body)
    result = run_module.run_wrapped(
        argv,
        repo=repo,
        sessions_root=sessions_root,
        workspace=workspace,
        correlation_window=timedelta(seconds=5),
        stdout_stream=io.BytesIO(),
    )
    assert result.correlation_method == "none"


# --- signal forwarding (unit-level, no real OS signal delivery) ---------


class _FakeProc:
    def __init__(self) -> None:
        self.signals_sent: list[int] = []

    def send_signal(self, signum: int) -> None:
        self.signals_sent.append(signum)


def test_signal_forwarding_calls_send_signal_on_sigint_and_sigterm() -> None:
    import signal

    fake = _FakeProc()
    restore = run_module._install_signal_forwarding(fake)  # type: ignore[arg-type]
    try:
        handler_int = signal.getsignal(signal.SIGINT)
        handler_term = signal.getsignal(signal.SIGTERM)
        assert callable(handler_int)
        assert callable(handler_term)
        frame: FrameType | None = None
        handler_int(signal.SIGINT, frame)  # type: ignore[misc]
        handler_term(signal.SIGTERM, frame)  # type: ignore[misc]
        assert fake.signals_sent == [signal.SIGINT, signal.SIGTERM]
    finally:
        restore()
