from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from codex_watchtower import domain
from codex_watchtower.assess.luna import load_luna_prompt, run_luna
from codex_watchtower.models.config import ModelProfile

ENDPOINT = "https://models.example.internal/v1/chat/completions"

SESSION = domain.SessionInfo(
    id="sess-1",
    workspace="/w",
    started_at="2026-08-13T10:00:00Z",
    elapsed_seconds=60,
    state=domain.SessionState.active_turn,
)
GOAL = domain.Goal(text="Implement the tailer.", acceptance_criteria=[])
WINDOW = domain.Window(
    from_cursor=None,
    to_cursor=1,
    opened_at="2026-08-13T10:00:00Z",
    closed_at="2026-08-13T10:01:00Z",
)
EVENT = domain.Event(
    id="evt:1", timestamp="2026-08-13T10:00:30Z", kind=domain.EventKind.command, summary="$ pytest"
)
OBSERVATION = domain.Observation(
    session=SESSION,
    goal=GOAL,
    window=WINDOW,
    previous_assessment=None,
    events=[EVENT],
    signals=[],
    system_refs=[],
    redactions=[],
    truncation=domain.Truncation(
        budget_characters=48000, used_characters=100, evicted=domain.Evicted()
    ),
)


def _profile() -> ModelProfile:
    return ModelProfile(name="luna", endpoint=ENDPOINT, model_identifier="test-model-v1")


def _assessment_dict(evidence_ref_id: str = "evt:1") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "progressing",
        "current_action": "Running the test suite.",
        "goal_alignment": "aligned",
        "evidence": [{"ref_type": "event", "ref_id": evidence_ref_id, "claim": "ran a command"}],
        "basis_ids": [evidence_ref_id],
        "concerns": [],
        "needs_attention": False,
        "recommended_human_action": None,
        "confidence_percent": 75,
        "assessed_by": "luna",
        "escalation_reason": None,
        "event_cursor": 1,
        "assessed_at": "2026-08-13T10:01:00Z",
    }


def _envelope(content_obj: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": {"content": json.dumps(content_obj)}}]}


# --- prompt-injection framing ---------------------------------------------


def test_prompt_frames_embedded_content_as_untrusted_evidence() -> None:
    prompt = load_luna_prompt()
    lowered = prompt.lower()
    assert "untrusted" in lowered
    assert "not instructions" in lowered or "never as something to obey" in lowered
    assert "no tools" in lowered or "cannot act" in lowered


def test_prompt_forbids_clearing_deterministic_signals() -> None:
    prompt = load_luna_prompt().lower()
    assert "never clear" in prompt or "may never clear" in prompt


# --- valid parsing and evidence-id validation -----------------------------


@respx.mock
def test_valid_assessment_with_resolving_evidence_succeeds() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_envelope(_assessment_dict())))
    result = run_luna(_profile(), OBSERVATION)
    assert result.assessment is not None
    assert result.fallback_reason is None
    assert result.assessment.evidence[0].ref_id == "evt:1"


# --- unsupported event ids invalidate the response ------------------------


@respx.mock
def test_unsupported_evidence_ref_id_is_invalid_and_retries() -> None:
    route = respx.post(ENDPOINT)
    route.side_effect = [
        httpx.Response(200, json=_envelope(_assessment_dict(evidence_ref_id="evt:does-not-exist"))),
        httpx.Response(200, json=_envelope(_assessment_dict(evidence_ref_id="evt:1"))),
    ]
    result = run_luna(_profile(), OBSERVATION)
    assert result.assessment is not None
    assert result.assessment.evidence[0].ref_id == "evt:1"
    assert route.call_count == 2


# --- one retry then rule-only fallback -------------------------------------


@respx.mock
def test_persistent_unsupported_evidence_falls_back_after_one_retry() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=_envelope(_assessment_dict(evidence_ref_id="evt:does-not-exist"))
        )
    )
    result = run_luna(_profile(), OBSERVATION)
    assert result.assessment is None
    assert result.fallback_reason == "unsupported_evidence_references"


@respx.mock
def test_transport_failure_falls_back_without_luna_specific_retry_beyond_client() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(500))
    profile = ModelProfile(
        name="luna", endpoint=ENDPOINT, model_identifier="test-model-v1", retry_budget=0
    )
    result = run_luna(profile, OBSERVATION)
    assert result.assessment is None
    assert result.fallback_reason is not None
