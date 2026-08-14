"""Luna: routine incremental evidence-backed assessment (spec 5.7)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from codex_watchtower import domain
from codex_watchtower.assess._shared import assess_with_evidence_validation
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
    max_request_bytes: int | None = None,
) -> LunaResult:
    system_prompt = prompt if prompt is not None else load_luna_prompt()
    result = assess_with_evidence_validation(
        profile,
        observation,
        system_prompt,
        http_client=http_client,
        max_request_bytes=max_request_bytes,
    )
    return LunaResult(assessment=result.assessment, fallback_reason=result.fallback_reason)
