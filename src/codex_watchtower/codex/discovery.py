"""Discover persisted Codex session files under a date-partitioned root.

Layout: ``<sessions_root>/YYYY/MM/DD/rollout-*.jsonl`` (spec section 5.1).
The exact envelope of the first ("session_meta") record has not been
captured live in this environment (see spikes/codex-lifecycle/README.md);
this module accepts both a ``{"type": "session_meta", "payload": {...}}``
envelope and a flat top-level object, and treats missing/malformed
metadata as an ``unknown`` session rather than a parser failure, per
Task 6 step 3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DiscoveredSession:
    path: Path
    session_id: str
    workspace: str | None
    started_at: str | None
    model: str | None
    source: str | None
    cli_version: str | None
    metadata_complete: bool


def discover_sessions(sessions_root: Path) -> list[DiscoveredSession]:
    """Return every ``rollout-*.jsonl`` under ``sessions_root/YYYY/MM/DD/``.

    A missing root yields an empty list rather than an error: a Watchtower
    instance started before Codex has ever run is a normal, not exceptional,
    state.
    """
    if not sessions_root.is_dir():
        return []
    discovered = []
    for path in sorted(sessions_root.glob("*/*/*/rollout-*.jsonl")):
        if not path.is_file():
            continue
        discovered.append(_inspect(path))
    return discovered


def _read_first_json_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline()
    except OSError:
        return None
    first_line = first_line.strip()
    if not first_line:
        return None
    try:
        record = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _extract_session_meta(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict) and record.get("type") == "session_meta":
        return payload
    return record


def read_session_meta(path: Path) -> dict[str, Any]:
    """Public accessor for a rollout's session_meta, used by launcher correlation (Task 9A)."""
    record = _read_first_json_record(path)
    return _extract_session_meta(record) if record is not None else {}


def _inspect(path: Path) -> DiscoveredSession:
    meta = read_session_meta(path)

    raw_id = meta.get("id")
    if isinstance(raw_id, str) and raw_id:
        session_id = raw_id
        complete = True
    else:
        session_id = path.stem  # fallback: filename stem
        complete = False

    return DiscoveredSession(
        path=path,
        session_id=session_id,
        workspace=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
        started_at=meta.get("timestamp") if isinstance(meta.get("timestamp"), str) else None,
        model=meta.get("model_provider") if isinstance(meta.get("model_provider"), str) else None,
        source=meta.get("source") if isinstance(meta.get("source"), str) else None,
        cli_version=meta.get("cli_version") if isinstance(meta.get("cli_version"), str) else None,
        metadata_complete=complete,
    )
