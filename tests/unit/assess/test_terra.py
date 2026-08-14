from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from codex_watchtower import domain
from codex_watchtower.assess.terra import load_terra_prompt, run_terra
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
    return ModelProfile(name="terra", endpoint=ENDPOINT, model_identifier="test-model-v1")


def _luna_assessment() -> domain.Assessment:
    return domain.Assessment(
        status=domain.AssessmentStatus.stalled,
        current_action="Repeating the same command.",
        goal_alignment=domain.GoalAlignment.possibly_aligned,
        evidence=[
            domain.Evidence(ref_type=domain.RefType.event, ref_id="evt:1", claim="ran pytest")
        ],
        basis_ids=["evt:1"],
        needs_attention=True,
        confidence_percent=55,
        assessed_by=domain.AssessedBy.luna,
        event_cursor=1,
        assessed_at="2026-08-13T10:01:00Z",
    )


def _terra_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1.0",
        "status": "investigating",
        "current_action": "Reviewing whether the repetition is a genuine loop.",
        "goal_alignment": "possibly_aligned",
        "evidence": [{"ref_type": "event", "ref_id": "evt:1", "claim": "ran pytest"}],
        "basis_ids": ["evt:1"],
        "concerns": [],
        "needs_attention": True,
        "recommended_human_action": "Check whether the test is flaky.",
        "confidence_percent": 60,
        "assessed_by": "terra",
        "escalation_reason": None,
        "event_cursor": 1,
        "assessed_at": "2026-08-13T10:01:30Z",
    }
    base.update(overrides)
    return base


def _envelope(content_obj: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": {"content": json.dumps(content_obj)}}]}


def test_terra_prompt_frames_content_as_untrusted_and_requires_adjudication() -> None:
    prompt = load_terra_prompt().lower()
    assert "untrusted" in prompt
    assert "adjudicate" in prompt
    assert "deterministic evidence is authoritative" in prompt


@respx.mock
def test_terra_receives_luna_assessment_as_previous_assessment() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_envelope(_terra_dict()))

    respx.post(ENDPOINT).mock(side_effect=_handler)
    luna = _luna_assessment()
    result = run_terra(_profile(), OBSERVATION, luna, escalation_reason="luna_status")
    assert result.assessment is not None

    user_content = json.loads(captured["body"]["messages"][1]["content"])
    assert user_content["previous_assessment"]["assessed_by"] == "luna"
    assert user_content["previous_assessment"]["status"] == "stalled"


@respx.mock
def test_terra_result_carries_escalation_reason_when_model_omits_it() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_envelope(_terra_dict(escalation_reason=None)))
    )
    result = run_terra(
        _profile(),
        OBSERVATION,
        _luna_assessment(),
        escalation_reason="deterministic_critical_signal",
    )
    assert result.assessment is not None
    assert result.assessment.escalation_reason == "deterministic_critical_signal"


@respx.mock
def test_terra_falls_back_after_persistent_unsupported_evidence() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_envelope(
                _terra_dict(
                    evidence=[{"ref_type": "event", "ref_id": "evt:does-not-exist", "claim": "x"}],
                    basis_ids=["evt:does-not-exist"],
                )
            ),
        )
    )
    result = run_terra(_profile(), OBSERVATION, _luna_assessment(), escalation_reason="luna_status")
    assert result.assessment is None
    assert result.fallback_reason == "unsupported_evidence_references"
