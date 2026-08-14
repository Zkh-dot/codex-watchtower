from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from codex_watchtower.codex_trace.client import CodexTraceClient, CodexTraceUnavailable

BASE_URL = "http://127.0.0.1:11424"
FIXTURES = Path(__file__).resolve().parents[3] / "spikes" / "codex-trace" / "fixtures"


def _load(name: str) -> dict[str, object]:
    result: dict[str, object] = json.loads((FIXTURES / name).read_text())
    return result


@pytest.fixture
def client() -> CodexTraceClient:
    return CodexTraceClient(base_url=BASE_URL)


# --- replay fixtures, detect unavailable/incompatible cleanly ------------


@respx.mock
def test_list_sessions_replays_fixture(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(
        return_value=httpx.Response(200, json=_load("sessions_response.json"))
    )
    sessions = client.list_sessions()
    assert len(sessions) == 2
    assert sessions[0].session_id == "rollout-2026-08-13T10-00-00-abc123"
    assert sessions[0].path is not None


@respx.mock
def test_unavailable_when_connection_refused(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(side_effect=httpx.ConnectError("refused"))
    assert client.is_available() is False
    with pytest.raises(CodexTraceUnavailable):
        client.list_sessions()


@respx.mock
def test_unavailable_on_non_200(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(return_value=httpx.Response(500))
    assert client.is_available() is False


@respx.mock
def test_incompatible_response_degrades_cleanly(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    assert client.is_available() is False
    with pytest.raises(CodexTraceUnavailable):
        client.list_sessions()


@respx.mock
def test_available_when_sessions_returned(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(
        return_value=httpx.Response(200, json=_load("sessions_response.json"))
    )
    assert client.is_available() is True


# --- correlation by session id / path -------------------------------------


@respx.mock
def test_correlate_by_session_id_unique_match(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(
        return_value=httpx.Response(200, json=_load("sessions_response.json"))
    )
    result = client.correlate_session(session_id="rollout-2026-08-13T10-00-00-abc123")
    assert result is not None
    assert result.session_id == "rollout-2026-08-13T10-00-00-abc123"


@respx.mock
def test_correlate_by_session_id_no_match_returns_none(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(
        return_value=httpx.Response(200, json=_load("sessions_response.json"))
    )
    assert client.correlate_session(session_id="does-not-exist") is None


@respx.mock
def test_correlate_by_path_unique_match(client: CodexTraceClient) -> None:
    respx.post(f"{BASE_URL}/api/sessions").mock(
        return_value=httpx.Response(200, json=_load("sessions_response.json"))
    )
    fixture = _load("sessions_response.json")
    path = fixture["sessions"][0]["path"]  # type: ignore[index]
    result = client.correlate_session(path=path)
    assert result is not None
    assert result.path == path


# --- deep-link generation stays disabled -----------------------------------


def test_drill_down_identifiers_never_include_a_deep_link(client: CodexTraceClient) -> None:
    ids = client.drill_down_identifiers("rollout-2026-08-13T10-00-00-abc123")
    assert ids["api_base"] == BASE_URL
    assert ids["session_id"] == "rollout-2026-08-13T10-00-00-abc123"
    assert ids["deep_link"] is None


# --- never proxied externally, never widens its own bind ------------------


def test_default_base_url_is_loopback_only() -> None:
    assert CodexTraceClient().base_url.startswith("http://127.0.0.1")


def test_client_exposes_no_external_bind_or_proxy_configuration() -> None:
    """Structural guard: the client has no field that could widen its own bind or proxy Trace."""
    fields = set(CodexTraceClient.__dataclass_fields__.keys())
    assert "proxy" not in fields
    assert "external_bind" not in fields
    assert "bind_host" not in fields
