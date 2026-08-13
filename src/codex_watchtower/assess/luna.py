"""Luna: routine incremental evidence-backed assessment (spec 5.7).

``codex_watchtower.models.client.assess`` already retries once on a
transport/parse/schema-shape failure. This module adds a second, Luna-
specific validation layer that the generic client cannot perform on its
own: resolving every evidence reference against the actual observation
packet's event/signal/system_refs ids. An assessment whose evidence cites
ids absent from the packet is invalid regardless of schema conformance,
and gets its own one-retry-then-rule-only-fallback path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from codex_watchtower import domain
from codex_watchtower.models.client import assess
from codex_watchtower.models.config import ModelProfile

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "models" / "prompts" / "luna.txt"


def load_luna_prompt() -> str:
    return _PROMPT_PATH.read_text()


@dataclass(frozen=True, slots=True)
class LunaResult:
    assessment: domain.Assessment | None
    fallback_reason: str | None  # None on success


def run_luna(
    profile: ModelProfile,
    observation: domain.Observation,
    *,
    http_client: httpx.Client | None = None,
    prompt: str | None = None,
) -> LunaResult:
    system_prompt = prompt if prompt is not None else load_luna_prompt()

    first = assess(profile, observation, system_prompt, http_client=http_client)
    if first.assessment is None:
        return LunaResult(assessment=None, fallback_reason=first.failure_reason)
    if not domain.validate_evidence_against_packet(first.assessment, observation):
        return LunaResult(assessment=first.assessment, fallback_reason=None)

    # Evidence didn't resolve against this packet: one retry, then fall back.
    second = assess(profile, observation, system_prompt, http_client=http_client)
    if second.assessment is None:
        return LunaResult(assessment=None, fallback_reason=second.failure_reason)
    if domain.validate_evidence_against_packet(second.assessment, observation):
        return LunaResult(assessment=None, fallback_reason="unsupported_evidence_references")
    return LunaResult(assessment=second.assessment, fallback_reason=None)
