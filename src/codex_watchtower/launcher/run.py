"""`watchtower run -- <cmd>`: spawn Codex as a child and capture exit evidence.

Per spec 5.11: stdio passes through unchanged (stdin/stderr inherited
untouched; stdout is teed -- forwarded byte-for-byte while a copy is parsed
for the canonical session id), the launcher never writes to the child's
stdin, and it never filters, inspects, or alters what Codex does beyond
observing process lifetime. The process-evidence record is created before
spawn (state=pending, pid=null, since no PID can exist yet), updated to
running with the real PID immediately after a successful spawn, and to
exited on any exit path including signal termination.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import BinaryIO

from codex_watchtower.launcher.evidence import DEFAULT_CORRELATION_WINDOW as _DEFAULT_WINDOW
from codex_watchtower.launcher.evidence import (
    CanonicalIdWatcher,
    argv_hash,
    correlate_by_snapshot,
    snapshot_rollouts,
)
from codex_watchtower.storage.repository import Repository

_READ_CHUNK_SIZE = 65536


@dataclass(frozen=True, slots=True)
class LaunchResult:
    launch_id: str
    exit_code: int
    session_id: str | None
    correlation_method: str


class SpawnFailed(RuntimeError):
    """Spawning the child process failed; the evidence record stays `pending`."""


def _install_signal_forwarding(
    proc: subprocess.Popen[bytes],
) -> Callable[[], None]:
    """Forward SIGINT/SIGTERM to ``proc``. Returns a callable that restores prior handlers."""

    def _forward(signum: int, _frame: FrameType | None) -> None:
        proc.send_signal(signum)

    previous_int = signal.signal(signal.SIGINT, _forward)
    previous_term = signal.signal(signal.SIGTERM, _forward)

    def _restore() -> None:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)

    return _restore


def run_wrapped(
    argv: list[str],
    *,
    repo: Repository,
    sessions_root: Path,
    workspace: str | None = None,
    goal: str | None = None,
    expected_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    correlation_window: timedelta = _DEFAULT_WINDOW,
    stdout_stream: BinaryIO | None = None,
) -> LaunchResult:
    launch_id = uuid.uuid4().hex
    ws = workspace or os.getcwd()
    repo.create_process_evidence(
        launch_id,
        argv_hash=argv_hash(argv),
        workspace=ws,
        goal=goal,
        expected_paths=expected_paths,
        forbidden_paths=forbidden_paths,
    )

    before_snapshot = snapshot_rollouts(sessions_root)
    started_at = datetime.now(UTC)

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is operator-supplied by design
            argv, stdin=None, stdout=subprocess.PIPE, stderr=None
        )
    except OSError as exc:
        raise SpawnFailed(f"failed to spawn {argv!r}: {exc}") from exc

    repo.mark_process_running(launch_id, pid=proc.pid, started_at=started_at.isoformat())

    watcher = CanonicalIdWatcher()
    out = stdout_stream if stdout_stream is not None else sys.stdout.buffer
    restore_signals = _install_signal_forwarding(proc)
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            watcher.feed(chunk)
        proc.wait()
    finally:
        restore_signals()

    exit_code = proc.returncode
    exited_at = datetime.now(UTC)
    repo.mark_process_exited(launch_id, exit_code=exit_code, exited_at=exited_at.isoformat())

    session_id = watcher.session_id
    method = "stdout_canonical" if session_id else "none"
    if session_id is None:
        snap = correlate_by_snapshot(
            before_snapshot,
            sessions_root,
            workspace=ws,
            started_at=started_at,
            correlation_window=correlation_window,
        )
        session_id = snap.session_id
        method = snap.method

    if session_id is not None:
        repo.bind_process_session(launch_id, session_id=session_id, correlation_method=method)

    return LaunchResult(
        launch_id=launch_id, exit_code=exit_code, session_id=session_id, correlation_method=method
    )
