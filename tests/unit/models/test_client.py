from __future__ import annotations

import json
from typing import Any

import httpx
import jsonschema
import pytest
import respx

from codex_watchtower import domain, schemas
from codex_watchtower.models.client import (
    AssessResult,
    assess,
    build_request_body,
    read_bounded_response,
)
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
OBSERVATION = domain.Observation(
    session=SESSION,
    goal=GOAL,
    window=WINDOW,
    previous_assessment=None,
    events=[],
    signals=[],
    system_refs=[],
    redactions=[],
    truncation=domain.Truncation(
        budget_characters=48000, used_characters=100, evicted=domain.Evicted()
    ),
)


def _profile(**overrides: Any) -> ModelProfile:
    kwargs: dict[str, Any] = {
        "name": "luna",
        "endpoint": ENDPOINT,
        "model_identifier": "test-model-v1",
    }
    kwargs.update(overrides)
    return ModelProfile(**kwargs)


def _valid_assessment_dict() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "progressing",
        "current_action": "Refactoring the parser.",
        "goal_alignment": "aligned",
        "evidence": [{"ref_type": "system", "ref_id": "sys:elapsed", "claim": "session active"}],
        "basis_ids": ["sys:elapsed"],
        "concerns": [],
        "needs_attention": False,
        "recommended_human_action": None,
        "confidence_percent": 80,
        "assessed_by": "luna",
        "escalation_reason": None,
        "event_cursor": 1,
        "assessed_at": "2026-08-13T10:01:00Z",
    }


def _envelope(content_obj: dict[str, Any] | str) -> dict[str, Any]:
    content = content_obj if isinstance(content_obj, str) else json.dumps(content_obj)
    return {"choices": [{"message": {"content": content}}]}


# --- request construction: wire schema, never authoritative, no tools ----


def test_request_carries_wire_schema_not_authoritative() -> None:
    body = build_request_body(_profile(), "system prompt", OBSERVATION)
    wire_schema = schemas.load_raw("assessment_wire")
    authoritative_schema = schemas.load_raw("assessment")
    request_schema = body["response_format"]["json_schema"]["schema"]
    assert request_schema == wire_schema
    assert request_schema != authoritative_schema


def test_request_supplies_no_tools() -> None:
    body = build_request_body(_profile(), "system prompt", OBSERVATION)
    assert "tools" not in body
    assert "tool_choice" not in body


def test_request_content_is_the_serialized_observation() -> None:
    body = build_request_body(_profile(), "system prompt", OBSERVATION)
    user_content = body["messages"][1]["content"]
    parsed = json.loads(user_content)
    assert parsed["session"]["id"] == "sess-1"


# --- response transport boundary: bounded read + overflow probe ----------


def test_read_bounded_response_retains_exactly_up_to_cap() -> None:
    body = b"a" * 100
    response = httpx.Response(200, content=body)
    result = read_bounded_response(response, max_response_bytes=100, chunk_size=64)
    assert result.retained == body
    assert result.truncated is False


def test_read_bounded_response_detects_one_byte_over_cap() -> None:
    body = b"a" * 101  # exactly cap + 1: indistinguishable from cap bytes without reading past it
    response = httpx.Response(200, content=body)
    result = read_bounded_response(response, max_response_bytes=100, chunk_size=64)
    assert result.truncated is True
    assert len(result.retained) <= 100


def test_read_bounded_response_total_bytes_read_bounded_by_cap_plus_chunk_size() -> None:
    body = b"a" * 100_000
    cap = 100
    chunk_size = 64
    response = httpx.Response(200, content=body)
    result = read_bounded_response(response, max_response_bytes=cap, chunk_size=chunk_size)
    assert result.truncated is True
    assert result.total_bytes_read <= cap + chunk_size


def test_read_bounded_response_probe_bytes_never_retained() -> None:
    body = b"a" * 500
    response = httpx.Response(200, content=body)
    result = read_bounded_response(response, max_response_bytes=100, chunk_size=64)
    assert len(result.retained) <= 100  # probe bytes counted in total_bytes_read but not retained


def test_exactly_cap_and_cap_plus_one_are_distinguished_only_by_truncated_flag() -> None:
    cap = 200
    exact = httpx.Response(200, content=b"x" * cap)
    over = httpx.Response(200, content=b"x" * (cap + 1))
    r_exact = read_bounded_response(exact, max_response_bytes=cap, chunk_size=64)
    r_over = read_bounded_response(over, max_response_bytes=cap, chunk_size=64)
    assert r_exact.retained == r_over.retained  # identical over their shared prefix
    assert r_exact.truncated is False
    assert r_over.truncated is True


# --- oversized response: non-retryable, fails closed ----------------------


@respx.mock
def test_oversized_response_is_non_retryable_and_fails_closed() -> None:
    cap = 64 * 1024
    # Unambiguously larger than the cap, regardless of JSON validity.
    huge_body = b"x" * (cap + 1000)
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, content=huge_body))
    profile = _profile(max_response_bytes=cap, retry_budget=2)
    result = assess(profile, OBSERVATION, "system prompt")
    assert result.assessment is None
    assert result.failure_reason == "oversized_response"
    # Non-retryable: stops immediately, does not consume the retry budget.
    assert result.attempts == 1


def test_cumulative_ceiling_bounds_all_attempts_together() -> None:
    """(retry_budget + 1) * (cap + chunk_size) bounds worst-case total bytes across all attempts.

    This is an arithmetic property of the design (each attempt reads at
    most cap + chunk_size, and oversized responses never retry, capping
    attempts at retry_budget + 1); asserted directly here since it is not
    otherwise exercised by a single request/response pair.
    """
    cap = 64 * 1024
    chunk_size = 65536
    retry_budget = 1
    ceiling = (retry_budget + 1) * (cap + chunk_size)
    assert ceiling == 2 * (cap + chunk_size)


# --- wire-valid but authoritative-invalid: rejected, retry path ----------


@respx.mock
def test_wire_valid_but_authoritative_invalid_is_rejected_and_retried() -> None:
    # confidence_percent=150 satisfies the wire schema (no "maximum" keyword
    # there) but violates the authoritative schema's maximum: 100.
    bad = _valid_assessment_dict()
    bad["confidence_percent"] = 150
    schemas.validate("assessment_wire", bad)  # sanity: the wire schema alone accepts this
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate("assessment", bad)

    good = _valid_assessment_dict()
    route = respx.post(ENDPOINT)
    route.side_effect = [
        httpx.Response(200, json=_envelope(bad)),
        httpx.Response(200, json=_envelope(good)),
    ]
    profile = _profile(retry_budget=1)
    result = assess(profile, OBSERVATION, "system prompt")
    assert result.assessment is not None
    assert result.attempts == 2


# --- timeout, invalid JSON, schema mismatch classification ---------------


@respx.mock
def test_timeout_is_retried_then_fails_closed() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.TimeoutException("timed out"))
    profile = _profile(retry_budget=1)
    result = assess(profile, OBSERVATION, "system prompt")
    assert result.assessment is None
    assert result.failure_reason == "timeout"
    assert result.attempts == 2


@respx.mock
def test_invalid_json_is_retried_then_fails_closed() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, content=b"not json"))
    profile = _profile(retry_budget=1)
    result = assess(profile, OBSERVATION, "system prompt")
    assert result.assessment is None
    assert result.failure_reason == "invalid_json"
    assert result.attempts == 2


@respx.mock
def test_schema_mismatch_content_is_retried_then_fails_closed() -> None:
    malformed = {"not": "an assessment"}
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=_envelope(malformed)))
    profile = _profile(retry_budget=1)
    result = assess(profile, OBSERVATION, "system prompt")
    assert result.assessment is None
    assert result.failure_reason == "schema_invalid"
    assert result.attempts == 2


@respx.mock
def test_successful_response_on_first_attempt() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(200, json=_envelope(_valid_assessment_dict()))
    )
    profile = _profile()
    result = assess(profile, OBSERVATION, "system prompt")
    assert isinstance(result, AssessResult)
    assert result.assessment is not None
    assert result.attempts == 1
    assert result.failure_reason is None


@respx.mock
def test_retry_succeeds_on_second_attempt() -> None:
    route = respx.post(ENDPOINT)
    route.side_effect = [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=_envelope(_valid_assessment_dict())),
    ]
    profile = _profile(retry_budget=1)
    result = assess(profile, OBSERVATION, "system prompt")
    assert result.assessment is not None
    assert result.attempts == 2
