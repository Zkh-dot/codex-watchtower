from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codex_watchtower.api.app import DEFAULT_BIND_HOST, APIConfig, create_app
from codex_watchtower.ingest import IngestionService
from codex_watchtower.storage import db
from codex_watchtower.storage.repository import Repository

NOW = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)


def _seed_session(tmp_path: Path) -> Repository:
    import json as jsonlib

    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "13" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-08-13T10:00:00Z",
            "payload": {"id": "sess-1", "cwd": "/w"},
        },
        {"type": "turn_started", "timestamp": "2026-08-13T10:00:05Z", "payload": {}},
        {
            "type": "agent_message",
            "timestamp": "2026-08-13T10:00:10Z",
            "payload": {"text": "hello"},
        },
    ]
    with rollout.open("w") as f:
        for record in records:
            f.write(jsonlib.dumps(record) + "\n")

    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    IngestionService(sessions_root, repo).poll_once(now=NOW)
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return _seed_session(tmp_path)


@pytest.fixture
def client(repo: Repository) -> TestClient:
    app = create_app(repo)
    return TestClient(app)


# --- default bind ----------------------------------------------------


def test_default_bind_host_is_loopback() -> None:
    assert DEFAULT_BIND_HOST == "127.0.0.1"


# --- basic endpoints ---------------------------------------------------


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_sessions(client: TestClient) -> None:
    response = client.get("/api/v1/sessions")
    assert response.status_code == 200
    body = response.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == "sess-1"


def test_get_session_returns_reconciled_assessment_schema_shape(client: TestClient) -> None:
    from codex_watchtower import schemas

    response = client.get("/api/v1/sessions/sess-1")
    assert response.status_code == 200
    body = response.json()
    schemas.validate("reconciled_assessment", body)


def test_get_session_404_for_unknown_session(client: TestClient) -> None:
    response = client.get("/api/v1/sessions/does-not-exist")
    assert response.status_code == 404


def test_get_assessment_returns_reconciled_assessment_schema_shape(client: TestClient) -> None:
    from codex_watchtower import schemas

    response = client.get("/api/v1/sessions/sess-1/assessment")
    assert response.status_code == 200
    schemas.validate("reconciled_assessment", response.json())


# --- events pagination, no tailer cursor exposed --------------------------


def test_events_pagination(client: TestClient) -> None:
    response = client.get("/api/v1/sessions/sess-1/events")
    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 2  # turn_started + agent_message
    assert body["events"][0]["event_sequence"] == 0


def test_events_after_cursor_returns_only_newer(client: TestClient) -> None:
    response = client.get("/api/v1/sessions/sess-1/events", params={"after": 0})
    body = response.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["event_sequence"] == 1


def test_events_limit_produces_next_cursor(client: TestClient) -> None:
    response = client.get("/api/v1/sessions/sess-1/events", params={"limit": 1})
    body = response.json()
    assert len(body["events"]) == 1
    assert body["next_cursor"] == 0


def test_no_route_exposes_tailer_file_cursor(client: TestClient) -> None:
    response = client.get("/api/v1/sessions/sess-1/events")
    body = response.json()
    for event in body["events"]:
        assert "device" not in event
        assert "inode" not in event
        assert "byte_offset" not in event
        assert "checkpoint_hash" not in event

    session_response = client.get("/api/v1/sessions/sess-1")
    session_body = session_response.json()
    assert "device" not in str(session_body.keys())
    for key in session_body:
        assert "cursor" not in key or key == "event_cursor"  # event_cursor is the event *sequence*


# --- guarded POST assess: disabled by default, 404 not 401 ---------------


def test_assess_disabled_without_operator_token_returns_404(client: TestClient) -> None:
    response = client.post("/api/v1/sessions/sess-1/assess")
    assert response.status_code == 404


def _assess_config(**overrides: object) -> APIConfig:
    # starlette's TestClient sends Host: testserver by default; tests that
    # expect to get past the host allowlist need it included explicitly,
    # same as a real deployment would list its own bind host/domain.
    kwargs: dict[str, object] = {"operator_token": "secret-token", "allowed_hosts": ["testserver"]}
    kwargs.update(overrides)
    return APIConfig(**kwargs)  # type: ignore[arg-type]


def test_assess_requires_bearer_auth(repo: Repository) -> None:
    app = create_app(repo, _assess_config())
    client = TestClient(app)
    response = client.post("/api/v1/sessions/sess-1/assess")
    assert response.status_code == 401


def test_assess_succeeds_with_correct_bearer_token(repo: Repository) -> None:
    app = create_app(repo, _assess_config())
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/sess-1/assess", headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_assess_rejects_wrong_bearer_token(repo: Repository) -> None:
    app = create_app(repo, _assess_config())
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/sess-1/assess", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_assess_rejects_disallowed_origin(repo: Repository) -> None:
    app = create_app(repo, _assess_config(allowed_origins=["https://ok.example"]))
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/sess-1/assess",
        headers={"Authorization": "Bearer secret-token", "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_assess_allows_allowlisted_origin(repo: Repository) -> None:
    app = create_app(repo, _assess_config(allowed_origins=["https://ok.example"]))
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/sess-1/assess",
        headers={"Authorization": "Bearer secret-token", "Origin": "https://ok.example"},
    )
    assert response.status_code == 200


def test_assess_rejects_disallowed_host(repo: Repository) -> None:
    app = create_app(repo, _assess_config(allowed_hosts=["watchtower.internal"]))
    client = TestClient(app, base_url="http://attacker.example")
    response = client.post(
        "/api/v1/sessions/sess-1/assess", headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 403


def test_no_wildcard_cors_header_present(repo: Repository) -> None:
    app = create_app(repo, _assess_config())
    client = TestClient(app)
    response = client.get("/api/v1/sessions", headers={"Origin": "https://anything.example"})
    assert response.headers.get("access-control-allow-origin") != "*"


def test_assess_idempotency_key_returns_cached_result(repo: Repository) -> None:
    app = create_app(repo, _assess_config())
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret-token", "Idempotency-Key": "req-1"}
    first = client.post("/api/v1/sessions/sess-1/assess", headers=headers)
    second = client.post("/api/v1/sessions/sess-1/assess", headers=headers)
    assert first.json() == second.json()  # same request_id: served from cache, not re-executed


def test_assess_concurrency_limit_per_session(repo: Repository) -> None:
    app = create_app(repo, _assess_config())
    app.state.assess_in_progress.add("sess-1")  # simulate an in-flight request
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/sess-1/assess", headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 429


def test_assess_404_for_unknown_session(repo: Repository) -> None:
    app = create_app(repo, _assess_config())
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/does-not-exist/assess", headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 404


# --- no endpoint can mutate Codex or the workspace ------------------------


def test_no_endpoint_accepts_a_command_or_file_write_payload(client: TestClient) -> None:
    """Structural check: only GET/POST /assess exist; no PUT/PATCH/DELETE routes at all."""
    from codex_watchtower.api.routes import router

    all_methods: set[str] = set()
    for route in router.routes:
        route_methods = getattr(route, "methods", None)
        if route_methods:
            all_methods |= route_methods
    assert "PUT" not in all_methods
    assert "PATCH" not in all_methods
    assert "DELETE" not in all_methods


def test_assess_response_body_never_reflects_codex_mutation() -> None:
    """The assess endpoint's own docstring documents its mutation scope: Watchtower state only."""
    from codex_watchtower.api import routes

    assert routes.post_assess.__doc__ is not None
    doc = " ".join(routes.post_assess.__doc__.split())  # normalize wrapped whitespace
    assert "mutates Watchtower's own state" in doc
    assert "never mutates Codex or the workspace" in doc


# --- SSE reconnect cursor support ------------------------------------------
#
# The generator is tested directly (async, bounded iterations, zero poll
# delay) rather than through a live streaming HTTP connection: consuming a
# connection that only terminates after minutes through a synchronous test
# client is exactly the kind of thing that hangs a suite without proving
# anything more than the same logic already covers directly.


async def test_sse_generator_emits_existing_state_with_ids(repo: Repository) -> None:
    from codex_watchtower.api.routes import sse_event_generator

    chunks = [
        chunk async for chunk in sse_event_generator(repo, None, poll_interval=0, max_iterations=1)
    ]
    assert chunks  # the seeded session's reconciled state is emitted immediately
    assert all(c.startswith("id: ") for c in chunks)
    assert all("data: " in c for c in chunks)


async def test_sse_generator_reconnect_cursor_skips_already_seen(repo: Repository) -> None:
    from codex_watchtower.api.routes import sse_event_generator

    latest_seq = repo.get_reconciled_since(None)[-1]["updated_seq"]
    chunks = [
        chunk
        async for chunk in sse_event_generator(repo, latest_seq, poll_interval=0, max_iterations=1)
    ]
    assert chunks == []  # nothing new since the cursor: no replay of already-seen state


async def test_sse_generator_advances_cursor_across_calls(repo: Repository) -> None:
    from codex_watchtower.api.routes import sse_event_generator

    first_pass = [
        chunk async for chunk in sse_event_generator(repo, None, poll_interval=0, max_iterations=1)
    ]
    assert first_pass
    first_id_line = next(line for line in first_pass[0].split("\n") if line.startswith("id: "))
    first_cursor = int(first_id_line.removeprefix("id: "))

    second_pass = [
        chunk
        async for chunk in sse_event_generator(
            repo, first_cursor, poll_interval=0, max_iterations=1
        )
    ]
    assert second_pass == []  # already caught up; reconnecting with that cursor replays nothing


def test_sse_route_returns_event_stream_content_type(repo: Repository) -> None:
    app = create_app(repo)
    app.state.sse_max_iterations = 1
    app.state.sse_poll_interval = 0
    client = TestClient(app)
    response = client.get("/api/v1/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: " in response.text
