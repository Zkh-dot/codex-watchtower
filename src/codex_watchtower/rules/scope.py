"""Detect explicit scope violations: forbidden and unexpected path changes (spec 5.5).

Paths are canonicalized workspace-relative before comparison: an absolute
path is made relative to the workspace, `.`/`..` segments are normalized,
and any symlink component that exists on disk is resolved through
``Path.resolve()`` so a symlink cannot be used to make a forbidden path
look like it lies elsewhere.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from codex_watchtower import domain


def canonicalize_path(path_str: str, workspace: Path) -> str:
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    resolved_workspace = workspace.resolve()
    try:
        return str(resolved.relative_to(resolved_workspace))
    except ValueError:
        return str(resolved)  # outside the workspace entirely


def _matches_prefix(rel_path: str, prefixes: list[str]) -> bool:
    for prefix in prefixes:
        normalized = prefix.strip("/")
        if rel_path == normalized or rel_path.startswith(normalized + "/"):
            return True
    return False


def detect_scope_violations(
    events: list[domain.Event],
    *,
    workspace: Path,
    expected_paths: list[str],
    forbidden_paths: list[str],
) -> list[domain.Signal]:
    signals = []
    for event in events:
        if event.kind != domain.EventKind.file_changed or event.path is None:
            continue
        rel = canonicalize_path(event.path, workspace)
        key_hash = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]

        if _matches_prefix(rel, forbidden_paths):
            signals.append(
                domain.Signal(
                    id=f"sig:forbidden_path:{key_hash}",
                    kind=domain.SignalKind.forbidden_path,
                    severity=domain.Severity.critical,
                    source=domain.SignalSource.local_rule,
                    event_ids=[event.id],
                    observed_at=event.timestamp,
                    freshness=domain.Freshness.current,
                    summary=f"Change under forbidden path: {rel}",
                    payload={"path": rel[:500]},
                )
            )
        elif expected_paths and not _matches_prefix(rel, expected_paths):
            signals.append(
                domain.Signal(
                    id=f"sig:scope_expansion:{key_hash}",
                    kind=domain.SignalKind.scope_expansion,
                    severity=domain.Severity.warning,
                    source=domain.SignalSource.local_rule,
                    event_ids=[event.id],
                    observed_at=event.timestamp,
                    freshness=domain.Freshness.current,
                    summary=f"Change outside expected paths: {rel}",
                    payload={"path": rel[:500]},
                )
            )
    return signals
