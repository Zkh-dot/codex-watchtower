"""Version-gated AgentLens MCP compatibility adapter (spec 5.4).

No live AgentLens instance was available in this environment; the request/
response shapes below are replayed from the synthetic fixtures in
spikes/agentlens/fixtures/, built to match the documented Streamable HTTP
MCP contract (JSON-RPC 2.0 over HTTP POST to ``/mcp``, tool results as a
``content`` list of ``{"type": "text", "text": "<json>"}`` blocks) recorded
in docs/references/references.md. See spikes/agentlens/README.md for what
is and is not verified.

AgentLens outages, timeouts, and unrecognized response shapes all degrade
to unavailable/incompatible rather than raising into ingestion: a missing
or misbehaving AgentLens must not halt Codex monitoring (spec 4.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from codex_watchtower.agentlens.types import AgentLensSessionDetail, AgentLensSessionSummary

DEFAULT_BASE_URL = "http://127.0.0.1:4316/mcp"
DEFAULT_TIMEOUT_SECONDS = 5.0


class AgentLensUnavailable(RuntimeError):
    """The server did not respond usably: network error, timeout, or RPC error."""


class AgentLensIncompatible(RuntimeError):
    """The server responded, but not in a shape this version-pinned adapter understands."""


@dataclass
class AgentLensClient:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    http_client: httpx.Client | None = None

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        client = self.http_client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.post(self.base_url, json=body, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AgentLensUnavailable(f"request failed: {exc}") from exc
        if response.status_code != 200:
            raise AgentLensUnavailable(f"HTTP {response.status_code}")
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise AgentLensUnavailable(f"invalid JSON response: {exc}") from exc
        return data

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        data = self._post(body)
        if "error" in data:
            raise AgentLensUnavailable(f"RPC error: {data['error']}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise AgentLensIncompatible("response missing 'result' object")
        content = result.get("content")
        if not (isinstance(content, list) and content):
            raise AgentLensIncompatible("response 'result.content' is not a non-empty list")
        first = content[0]
        if not (isinstance(first, dict) and first.get("type") == "text"):
            raise AgentLensIncompatible("response content[0] is not a text block")
        try:
            payload: dict[str, Any] = json.loads(first["text"])
        except (json.JSONDecodeError, KeyError) as exc:
            raise AgentLensIncompatible(f"tool content is not parseable JSON: {exc}") from exc
        return payload

    def get_recent_sessions(self) -> list[AgentLensSessionSummary]:
        payload = self._call_tool("get_recent_sessions", {})
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            raise AgentLensIncompatible("get_recent_sessions: missing 'sessions' list")
        result = []
        for entry in sessions:
            if not isinstance(entry, dict):
                raise AgentLensIncompatible("get_recent_sessions: session entry is not an object")
            session_id = entry.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise AgentLensIncompatible("get_recent_sessions: missing 'session_id'")
            updated_at = entry.get("updated_at")
            result.append(
                AgentLensSessionSummary(
                    agentlens_session_id=session_id,
                    updated_at=updated_at if isinstance(updated_at, str) else None,
                )
            )
        return result

    def get_session_detail(self, agentlens_session_id: str) -> AgentLensSessionDetail:
        payload = self._call_tool("get_session_detail", {"session_id": agentlens_session_id})
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise AgentLensIncompatible("get_session_detail: missing 'session_id'")

        def _optional_int(key: str) -> int | None:
            value = payload.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return AgentLensSessionDetail(
            agentlens_session_id=session_id,
            prompt_tokens=_optional_int("prompt_tokens"),
            completion_tokens=_optional_int("completion_tokens"),
            context_tokens=_optional_int("context_tokens"),
            context_limit=_optional_int("context_limit"),
        )
