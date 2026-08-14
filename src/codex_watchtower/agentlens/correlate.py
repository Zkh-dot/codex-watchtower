"""Correlate AgentLens sessions to Watchtower sessions with canonical evidence only (spec 5.4).

AgentLens identifies a Codex session by the rollout filename without
``.jsonl``, not by the canonical ``id`` inside ``session_meta``. The two
happen to be derivable from the same file today, but neither
``get_recent_sessions`` nor ``get_session_detail`` exposes canonical
workspace or precise start-time fields, so this is a filename heuristic,
not a proof of identity backed by AgentLens itself. Absent evidence or
ambiguity returns no match; this module never creates a second logical
session from AgentLens data, and never guesses from minute-level dates or
model names.
"""

from __future__ import annotations

from pathlib import Path

from codex_watchtower.agentlens.types import AgentLensSessionSummary


def agentlens_id_for_rollout(path: Path) -> str:
    """The identifier AgentLens would use for this rollout: filename minus .jsonl."""
    return path.stem


def correlate_agentlens_session(
    agentlens_sessions: list[AgentLensSessionSummary], rollout_path: Path
) -> AgentLensSessionSummary | None:
    """Return the unique AgentLens session matching this rollout's filename, or None.

    Zero or more-than-one match both return None: ambiguity is never
    resolved by guessing (e.g. by nearest updated_at or model name).
    """
    expected_id = agentlens_id_for_rollout(rollout_path)
    matches = [s for s in agentlens_sessions if s.agentlens_session_id == expected_id]
    if len(matches) == 1:
        return matches[0]
    return None
