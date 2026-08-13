"""Shared evidence-validated assessment retry logic for Luna and Terra (spec 5.7).

``codex_watchtower.models.client.assess`` retries once on a transport/parse/
schema-shape failure. Resolving evidence references against the actual
observation packet is a semantic check the generic client cannot perform,
so it gets its own one-retry-then-rule-only-fallback layer here, shared by
both assessors rather than duplicated.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from codex_watchtower import domain
from codex_watchtower.models.client import assess
from codex_watchtower.models.config import ModelProfile


@dataclass(frozen=True, slots=True)
class EvidenceValidatedResult:
    assessment: domain.Assessment | None
    fallback_reason: str | None  # None on success


def assess_with_evidence_validation(
    profile: ModelProfile,
    observation: domain.Observation,
    system_prompt: str,
    *,
    http_client: httpx.Client | None = None,
) -> EvidenceValidatedResult:
    first = assess(profile, observation, system_prompt, http_client=http_client)
    if first.assessment is None:
        return EvidenceValidatedResult(None, first.failure_reason)
    if not domain.validate_evidence_against_packet(first.assessment, observation):
        return EvidenceValidatedResult(first.assessment, None)

    second = assess(profile, observation, system_prompt, http_client=http_client)
    if second.assessment is None:
        return EvidenceValidatedResult(None, second.failure_reason)
    if domain.validate_evidence_against_packet(second.assessment, observation):
        return EvidenceValidatedResult(None, "unsupported_evidence_references")
    return EvidenceValidatedResult(second.assessment, None)
