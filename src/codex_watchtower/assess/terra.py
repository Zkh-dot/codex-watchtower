"""Terra: escalation judge for anomalous, conflicting, or ambiguous cases (spec 5.7).

Terra receives the same observation packet as Luna, with Luna's assessment
attached as ``previous_assessment`` -- an explicit request to adjudicate,
not the full session transcript. Escalation triggers themselves
(``should_escalate``) live in ``assess/policy.py`` alongside the rest of
the reconciliation policy, since they are as much a reconciler concern as
an assessor one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from codex_watchtower import domain
from codex_watchtower.assess._shared import assess_with_evidence_validation
from codex_watchtower.models.config import ModelProfile

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "models" / "prompts" / "terra.txt"


def load_terra_prompt() -> str:
    return _PROMPT_PATH.read_text()


@dataclass(frozen=True, slots=True)
class TerraResult:
    assessment: domain.Assessment | None
    fallback_reason: str | None  # None on success


def run_terra(
    profile: ModelProfile,
    observation: domain.Observation,
    luna_assessment: domain.Assessment | None,
    *,
    escalation_reason: str,
    http_client: httpx.Client | None = None,
    prompt: str | None = None,
) -> TerraResult:
    system_prompt = prompt if prompt is not None else load_terra_prompt()
    packet = observation.model_copy(update={"previous_assessment": luna_assessment})
    result = assess_with_evidence_validation(
        profile, packet, system_prompt, http_client=http_client
    )
    assessment = result.assessment
    if assessment is not None and assessment.escalation_reason is None:
        assessment = assessment.model_copy(update={"escalation_reason": escalation_reason})
    return TerraResult(assessment=assessment, fallback_reason=result.fallback_reason)
