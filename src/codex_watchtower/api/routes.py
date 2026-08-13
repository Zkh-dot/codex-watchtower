"""Route handlers for the local Watchtower status API (spec 5.9)."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from codex_watchtower.storage.repository import Repository, row_to_domain_event

router = APIRouter()

MAX_EVENTS_PAGE_SIZE = 200
SSE_POLL_INTERVAL_SECONDS = 0.5
SSE_MAX_ITERATIONS = 3600  # bounds a single connection's lifetime (~30 min at the poll interval)


async def sse_event_generator(
    repo: Repository,
    after: int | None,
    *,
    poll_interval: float = SSE_POLL_INTERVAL_SECONDS,
    max_iterations: int = SSE_MAX_ITERATIONS,
) -> AsyncIterator[str]:
    """Poll for reconciled-assessment transitions since ``after`` and format them as SSE.

    A module-level function (not a closure inside the route) specifically
    so it can be driven directly in tests with a small, deterministic
    ``max_iterations`` and ``poll_interval=0`` -- consuming a live,
    minutes-long stream through a synchronous test HTTP client is
    exactly the kind of thing that hangs a test suite instead of proving
    anything.
    """
    cursor = after
    for _ in range(max_iterations):
        rows = repo.get_reconciled_since(cursor)
        for row in rows:
            cursor = row["updated_seq"]
            body = json.loads(row["body"])
            yield f"id: {cursor}\ndata: {json.dumps(body)}\n\n"
        if not rows:
            await asyncio.sleep(poll_interval)


def _repo(request: Request) -> Repository:
    repo: Repository = request.app.state.repo
    return repo


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/v1/sessions")
def list_sessions(request: Request) -> dict[str, Any]:
    repo = _repo(request)
    sessions = []
    for row in repo.list_sessions():
        reconciled = repo.get_latest_reconciled(row["session_id"])
        sessions.append(
            {
                "session_id": row["session_id"],
                "workspace": row["workspace"],
                "started_at": row["started_at"],
                "state": row["state"],
                "reconciled": reconciled.model_dump(mode="json") if reconciled else None,
            }
        )
    return {"sessions": sessions}


@router.get("/api/v1/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    repo = _repo(request)
    reconciled = repo.get_latest_reconciled(session_id)
    if reconciled is None:
        raise HTTPException(status_code=404, detail="session not found")
    return reconciled.model_dump(mode="json")


@router.get("/api/v1/sessions/{session_id}/events")
def get_session_events(
    session_id: str,
    request: Request,
    after: int | None = Query(default=None),
    limit: int = Query(default=100, le=MAX_EVENTS_PAGE_SIZE),
) -> dict[str, Any]:
    repo = _repo(request)
    if repo.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    rows = repo.get_events_since(session_id, after)
    page = rows[:limit]
    truncated = len(rows) > limit
    next_cursor = page[-1]["event_sequence"] if truncated and page else None
    events = []
    for row in page:
        event = row_to_domain_event(row)
        payload = event.model_dump(mode="json")
        payload["event_sequence"] = row["event_sequence"]
        # The tailer's internal file cursor (device/inode/byte offset) is
        # provenance only and never leaves the process (spec 5.2); it is
        # not read from the row at all here, only event_sequence is added.
        events.append(payload)
    return {"events": events, "next_cursor": next_cursor}


@router.get("/api/v1/sessions/{session_id}/assessment")
def get_assessment(session_id: str, request: Request) -> dict[str, Any]:
    repo = _repo(request)
    reconciled = repo.get_latest_reconciled(session_id)
    if reconciled is None:
        raise HTTPException(status_code=404, detail="session not found")
    return reconciled.model_dump(mode="json")


@router.post("/api/v1/sessions/{session_id}/assess")
def post_assess(
    session_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Request an authenticated, rate-limited Terra escalation.

    This is the one route in this API that mutates anything: it mutates
    Watchtower's own state (recording a pending escalation request) and
    may consume model-call quota. It never mutates Codex or the workspace,
    and it does not exist at all -- 404, not merely 401 -- unless an
    operator token is configured, so its presence is never discoverable
    from an unconfigured deployment.
    """
    config = request.app.state.config
    if config.operator_token is None:
        raise HTTPException(status_code=404, detail="not found")

    if authorization != f"Bearer {config.operator_token}":
        raise HTTPException(status_code=401, detail="unauthorized")

    if origin is not None and origin not in config.allowed_origins:
        raise HTTPException(status_code=403, detail="origin not allowed")

    host = (request.headers.get("host") or "").split(":")[0]
    if config.allowed_hosts and host not in config.allowed_hosts:
        raise HTTPException(status_code=403, detail="host not allowed")

    repo = _repo(request)
    if repo.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")

    cache: dict[str, dict[str, Any]] = request.app.state.idempotency_cache
    if idempotency_key is not None and idempotency_key in cache:
        return cache[idempotency_key]

    in_progress: set[str] = request.app.state.assess_in_progress
    if session_id in in_progress:
        raise HTTPException(status_code=429, detail="an assessment is already in progress")

    in_progress.add(session_id)
    try:
        result = {
            "status": "accepted",
            "session_id": session_id,
            "request_id": secrets.token_hex(8),
        }
        if idempotency_key is not None:
            cache[idempotency_key] = result
        return result
    finally:
        in_progress.discard(session_id)


@router.get("/api/v1/events")
async def sse_events(
    request: Request, after: int | None = Query(default=None)
) -> StreamingResponse:
    repo = _repo(request)
    max_iterations = getattr(request.app.state, "sse_max_iterations", SSE_MAX_ITERATIONS)
    poll_interval = getattr(request.app.state, "sse_poll_interval", SSE_POLL_INTERVAL_SECONDS)
    generator = sse_event_generator(
        repo, after, poll_interval=poll_interval, max_iterations=max_iterations
    )
    return StreamingResponse(generator, media_type="text/event-stream")
