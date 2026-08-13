"""Typed AgentLens fields, limited to what spec 5.4 says the pinned Codex adapter proves.

AgentLens's general schema supports errors, tool counts, loop signals, and
changed files, but the pinned Codex parser leaves those empty for Codex
sessions specifically. Only prompt/token counters are exposed here; nothing
else is synthesized from AgentLens data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentLensSessionSummary:
    agentlens_session_id: str  # the rollout filename without .jsonl -- not the canonical session id
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class AgentLensSessionDetail:
    agentlens_session_id: str
    prompt_tokens: int | None
    completion_tokens: int | None
    context_tokens: int | None
    context_limit: int | None
