from __future__ import annotations

from pathlib import Path

from codex_watchtower.agentlens.correlate import (
    agentlens_id_for_rollout,
    correlate_agentlens_session,
)
from codex_watchtower.agentlens.types import AgentLensSessionSummary


def test_agentlens_id_is_filename_without_jsonl_extension() -> None:
    path = Path("/home/user/.codex/sessions/2026/08/13/rollout-2026-08-13T10-00-00-abc123.jsonl")
    assert agentlens_id_for_rollout(path) == "rollout-2026-08-13T10-00-00-abc123"


def test_filename_vs_session_meta_id_mismatch_is_explicit() -> None:
    """AgentLens's identifier (filename) is not the canonical session_meta id.

    This test documents that distinction directly: the filename-derived id
    is what correlation uses, and it is not expected to equal a Watchtower
    session's canonical `session_meta.id` in general.
    """
    path = Path("/sessions/2026/08/13/rollout-2026-08-13T10-00-00-abc123.jsonl")
    canonical_session_id = "sess-completely-different-uuid"
    assert agentlens_id_for_rollout(path) != canonical_session_id


def test_unique_match_correlates() -> None:
    path = Path("/sessions/2026/08/13/rollout-abc123.jsonl")
    sessions = [
        AgentLensSessionSummary(agentlens_session_id="rollout-abc123", updated_at="t1"),
        AgentLensSessionSummary(agentlens_session_id="rollout-other", updated_at="t2"),
    ]
    result = correlate_agentlens_session(sessions, path)
    assert result is not None
    assert result.agentlens_session_id == "rollout-abc123"


def test_no_candidates_returns_none() -> None:
    path = Path("/sessions/2026/08/13/rollout-abc123.jsonl")
    result = correlate_agentlens_session([], path)
    assert result is None


def test_absent_canonical_evidence_returns_none_instead_of_guessing() -> None:
    path = Path("/sessions/2026/08/13/rollout-abc123.jsonl")
    sessions = [
        AgentLensSessionSummary(agentlens_session_id="rollout-unrelated-1", updated_at="t1"),
        AgentLensSessionSummary(agentlens_session_id="rollout-unrelated-2", updated_at="t2"),
    ]
    result = correlate_agentlens_session(sessions, path)
    assert result is None


def test_ambiguous_duplicate_ids_return_none() -> None:
    """Two AgentLens entries claiming the same filename-derived id is ambiguous, not a match."""
    path = Path("/sessions/2026/08/13/rollout-abc123.jsonl")
    sessions = [
        AgentLensSessionSummary(agentlens_session_id="rollout-abc123", updated_at="t1"),
        AgentLensSessionSummary(agentlens_session_id="rollout-abc123", updated_at="t2"),
    ]
    result = correlate_agentlens_session(sessions, path)
    assert result is None
