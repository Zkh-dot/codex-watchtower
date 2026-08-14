"""Process-evidence correlation protocols (spec 5.11).

Two independent, ranked-by-confidence protocols bind a launched process to
the rollout it produced, since no PID appears in Codex's ``session_meta``
and the filesystem watcher cannot attribute a file to a process by itself:

1. **Canonical, from the child's own output.** ``CanonicalIdWatcher`` tees
   ``codex exec --json`` stdout and looks for the session identifier the
   child itself reports. This is a direct binding, not an inference.
2. **Snapshot difference, fail-closed.** ``correlate_by_snapshot`` compares
   a pre-spawn snapshot of rollout files to the post-exit state. A resume
   appends to an existing rollout instead of creating one, so the candidate
   set is both newly created files and previously known files that grew.
   It correlates only when exactly one candidate has a matching workspace
   and its first new record falls inside the correlation window; zero or
   multiple candidates both record ``correlation_method=none`` rather than
   guessing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from codex_watchtower.codex.discovery import read_session_meta

DEFAULT_CORRELATION_WINDOW = timedelta(seconds=30)


def argv_hash(argv: list[str]) -> str:
    canonical = json.dumps(argv, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_rfc3339(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


class CanonicalIdWatcher:
    """Feed raw stdout chunks; detects a session identifier without altering them.

    The exact ``codex exec --json`` stdout event shape has not been
    captured live in this environment (spikes/models/README.md and
    spikes/codex-lifecycle/README.md record what has and has not been
    verified). This watcher accepts either a top-level ``session_id``
    string field or a ``{"type": "session_meta", "payload": {"id": ...}}``
    envelope matching the rollout format assumed elsewhere in this
    codebase, and ignores everything else -- consistent with treating
    unrecognized shapes as non-fatal, never as a crash.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self.session_id: str | None = None

    def feed(self, chunk: bytes) -> None:
        if self.session_id is not None or not chunk:
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._try_line(line)
            if self.session_id is not None:
                return

    def _try_line(self, line: bytes) -> None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(record, dict):
            return
        candidate = record.get("session_id")
        if isinstance(candidate, str) and candidate:
            self.session_id = candidate
            return
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            sid = record["payload"].get("id")
            if isinstance(sid, str) and sid:
                self.session_id = sid


@dataclass(frozen=True, slots=True)
class RolloutSnapshot:
    device: int
    inode: int
    size: int


def snapshot_rollouts(sessions_root: Path) -> dict[Path, RolloutSnapshot]:
    if not sessions_root.is_dir():
        return {}
    result: dict[Path, RolloutSnapshot] = {}
    for path in sessions_root.glob("*/*/*/rollout-*.jsonl"):
        if not path.is_file():
            continue
        st = path.stat()
        result[path] = RolloutSnapshot(device=st.st_dev, inode=st.st_ino, size=st.st_size)
    return result


@dataclass(frozen=True, slots=True)
class SnapshotCorrelationResult:
    session_id: str | None
    method: str  # "snapshot_unique" | "none"


def _first_new_record(path: Path, previous_size: int) -> dict[str, Any] | None:
    with path.open("rb") as f:
        f.seek(previous_size)
        chunk = f.read()
    first_line = chunk.split(b"\n", 1)[0]
    if not first_line:
        return None
    try:
        record = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def correlate_by_snapshot(
    before: dict[Path, RolloutSnapshot],
    sessions_root: Path,
    *,
    workspace: str,
    started_at: datetime,
    correlation_window: timedelta = DEFAULT_CORRELATION_WINDOW,
) -> SnapshotCorrelationResult:
    after = snapshot_rollouts(sessions_root)
    candidates: list[Path] = []
    for path, snap_after in after.items():
        snap_before = before.get(path)
        if snap_before is None:
            candidates.append(path)  # newly created rollout
        elif snap_after.size > snap_before.size:
            candidates.append(path)  # existing rollout that grew: a resume

    matches: list[str] = []
    for path in candidates:
        meta = read_session_meta(path)
        if meta.get("cwd") != workspace:
            continue
        previous_size = before[path].size if path in before else 0
        record = _first_new_record(path, previous_size)
        if record is None:
            continue
        ts_raw = record.get("timestamp")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = _parse_rfc3339(ts_raw)
        except ValueError:
            continue
        if not (started_at <= ts <= started_at + correlation_window):
            continue
        session_id = meta.get("id")
        if isinstance(session_id, str) and session_id:
            matches.append(session_id)

    unique = set(matches)
    if len(unique) == 1:
        return SnapshotCorrelationResult(session_id=next(iter(unique)), method="snapshot_unique")
    return SnapshotCorrelationResult(session_id=None, method="none")
