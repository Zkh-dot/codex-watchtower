"""Provider-neutral structured model client (spec 5.7, 7.3).

Requests always carry ``schemas/assessment.wire.schema.json`` -- the
conservative, strict-mode-safe projection -- never the authoritative
``assessment.schema.json``, since which keywords a given deployment's
strict structured-output mode accepts is unmeasured (spikes/models/README.md).
Every response is nonetheless re-validated against the authoritative schema
after parsing: a response that satisfies the wire schema but violates a
bound the wire schema omitted is invalid and takes the retry path.

The response-size boundary implements spec 7.3 precisely because two
requirements are not simultaneously satisfiable by reading only the cap:
"read no more than the cap" and "detect exceeding the cap" both need to
hold, and without a trusted Content-Length a response of exactly ``cap``
bytes is byte-identical to one of ``cap + 1`` bytes over their shared
prefix. The client therefore reads at most ``cap`` bytes into a retained
buffer, plus one bounded overflow probe beyond it whose bytes are counted
but never retained or parsed -- so total bytes read in one attempt are at
most ``cap + chunk_size``, and an oversized response is rejected before
``json.loads`` ever sees it, so an oversized numeric literal is never
materialized.

The model receives no tools: it cannot write files or contact Codex.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import jsonschema
import pydantic

from codex_watchtower import domain, schemas
from codex_watchtower.models.config import ModelProfile


@dataclass(frozen=True, slots=True)
class BoundedRead:
    retained: bytes
    total_bytes_read: int
    truncated: bool  # True iff more data existed beyond max_response_bytes


def read_bounded_response(
    response: httpx.Response, *, max_response_bytes: int, chunk_size: int
) -> BoundedRead:
    retained = bytearray()
    total_read = 0
    for chunk in response.iter_bytes(chunk_size=chunk_size):
        capacity = max_response_bytes - len(retained)
        if capacity <= 0:
            total_read += len(chunk)
            return BoundedRead(bytes(retained), total_read, truncated=True)
        if len(chunk) > capacity:
            retained.extend(chunk[:capacity])
            total_read += len(chunk)
            return BoundedRead(bytes(retained), total_read, truncated=True)
        retained.extend(chunk)
        total_read += len(chunk)
    return BoundedRead(bytes(retained), total_read, truncated=False)


def build_request_body(
    profile: ModelProfile, system_prompt: str, observation: domain.Observation
) -> dict[str, Any]:
    wire_schema = schemas.load_raw("assessment_wire")
    body: dict[str, Any] = {
        "model": profile.model_identifier,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    observation.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "watchtower_assessment",
                "schema": wire_schema,
                "strict": True,
            },
        },
    }
    return body


def _auth_headers(profile: ModelProfile) -> dict[str, str]:
    if profile.api_key_env_var is None:
        return {}
    import os

    key = os.environ.get(profile.api_key_env_var)
    return {"Authorization": f"Bearer {key}"} if key else {}


@dataclass(frozen=True, slots=True)
class AssessResult:
    assessment: domain.Assessment | None
    attempts: int
    failure_reason: str | None  # None on success


def assess(
    profile: ModelProfile,
    observation: domain.Observation,
    system_prompt: str,
    *,
    http_client: httpx.Client | None = None,
) -> AssessResult:
    client = http_client or httpx.Client(
        timeout=profile.timeout_seconds,
        trust_env=profile.trust_env,
        follow_redirects=profile.follow_redirects,
    )
    body = build_request_body(profile, system_prompt, observation)
    max_attempts = profile.retry_budget + 1

    attempts = 0
    last_reason: str | None = "retries_exhausted"
    while attempts < max_attempts:
        attempts += 1
        try:
            # Use streaming so the response body is never fully materialized
            # before the bounded read cap applies. Without stream=True,
            # httpx reads the entire body into memory before we can check
            # the size, defeating the oversized-response protection.
            with client.stream(
                "POST", profile.endpoint, json=body, headers=_auth_headers(profile)
            ) as response:
                if response.status_code != 200:
                    last_reason = f"http_{response.status_code}"
                    continue

                read_result = read_bounded_response(
                    response,
                    max_response_bytes=profile.max_response_bytes,
                    chunk_size=profile.chunk_size,
                )
        except httpx.TimeoutException:
            last_reason = "timeout"
            continue
        except httpx.HTTPError:
            last_reason = "network_error"
            continue

        if read_result.truncated:
            # Non-retryable: the same endpoint returning the same oversized
            # body cannot be made acceptable by asking again.
            return AssessResult(
                assessment=None, attempts=attempts, failure_reason="oversized_response"
            )

        try:
            envelope = json.loads(read_result.retained)
            content_str = envelope["choices"][0]["message"]["content"]
            parsed = json.loads(content_str)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            last_reason = "invalid_json"
            continue

        try:
            schemas.validate("assessment_wire", parsed)
            assessment = domain.Assessment.model_validate(parsed)
        except (jsonschema.ValidationError, pydantic.ValidationError):
            last_reason = "schema_invalid"
            continue

        return AssessResult(assessment=assessment, attempts=attempts, failure_reason=None)

    return AssessResult(assessment=None, attempts=attempts, failure_reason=last_reason)
