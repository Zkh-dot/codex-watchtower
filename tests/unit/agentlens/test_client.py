from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from codex_watchtower.agentlens.client import (
    AgentLensClient,
    AgentLensIncompatible,
    AgentLensUnavailable,
)

FIXTURES = Path(__file__).resolve().parents[3] / "spikes" / "agentlens" / "fixtures"


def _load(name: str) -> dict[str, object]:
    result: dict[str, object] = json.loads((FIXTURES / name).read_text())
    return result


@pytest.fixture
def client() -> AgentLensClient:
    return AgentLensClient(base_url="http://127.0.0.1:4316/mcp")


# --- replay the pinned fixtures ---------------------------------------


@respx.mock
def test_get_recent_sessions_replays_fixture(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(
        return_value=httpx.Response(200, json=_load("get_recent_sessions.json"))
    )
    sessions = client.get_recent_sessions()
    assert len(sessions) == 2
    assert sessions[0].agentlens_session_id == "rollout-2026-08-13T10-00-00-abc123"
    assert sessions[0].updated_at == "2026-08-13T10:05:00Z"


@respx.mock
def test_get_session_detail_replays_fixture(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(
        return_value=httpx.Response(200, json=_load("get_session_detail.json"))
    )
    detail = client.get_session_detail("rollout-2026-08-13T10-00-00-abc123")
    assert detail.agentlens_session_id == "rollout-2026-08-13T10-00-00-abc123"
    assert detail.prompt_tokens == 4200
    assert detail.completion_tokens == 1100
    assert detail.context_tokens == 5300
    assert detail.context_limit == 128000


# --- only fields the fixture proves are exposed ------------------------


def test_detail_type_exposes_only_prompt_and_token_fields() -> None:
    from codex_watchtower.agentlens.types import AgentLensSessionDetail

    fields = set(AgentLensSessionDetail.__dataclass_fields__.keys())
    assert fields == {
        "agentlens_session_id",
        "prompt_tokens",
        "completion_tokens",
        "context_tokens",
        "context_limit",
    }
    # Explicitly not present: loop/error/file/tool fields the pinned Codex
    # parser leaves empty (spec 5.4).
    assert "loop" not in fields
    assert "errors" not in fields
    assert "files" not in fields
    assert "tool_counts" not in fields


# --- unavailable: rpc error, timeout, network error ----------------------


@respx.mock
def test_rpc_error_raises_unavailable(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(
        return_value=httpx.Response(200, json=_load("unavailable.json"))
    )
    with pytest.raises(AgentLensUnavailable):
        client.get_recent_sessions()


@respx.mock
def test_connection_error_raises_unavailable(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    with pytest.raises(AgentLensUnavailable):
        client.get_recent_sessions()


@respx.mock
def test_timeout_raises_unavailable(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(AgentLensUnavailable):
        client.get_recent_sessions()


@respx.mock
def test_non_200_raises_unavailable(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(return_value=httpx.Response(503))
    with pytest.raises(AgentLensUnavailable):
        client.get_recent_sessions()


@respx.mock
def test_invalid_json_body_raises_unavailable(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(
        return_value=httpx.Response(200, content=b"not json at all")
    )
    with pytest.raises(AgentLensUnavailable):
        client.get_recent_sessions()


# --- incompatible: response is well-formed JSON but the wrong shape -----


@respx.mock
def test_missing_result_raises_incompatible(client: AgentLensClient) -> None:
    respx.post("http://127.0.0.1:4316/mcp").mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})
    )
    with pytest.raises(AgentLensIncompatible):
        client.get_recent_sessions()


@respx.mock
def test_missing_sessions_field_raises_incompatible(client: AgentLensClient) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps({"not_sessions": []})}]},
    }
    respx.post("http://127.0.0.1:4316/mcp").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AgentLensIncompatible):
        client.get_recent_sessions()


@respx.mock
def test_content_not_text_block_raises_incompatible(client: AgentLensClient) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "image", "data": "base64..."}]},
    }
    respx.post("http://127.0.0.1:4316/mcp").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AgentLensIncompatible):
        client.get_recent_sessions()
