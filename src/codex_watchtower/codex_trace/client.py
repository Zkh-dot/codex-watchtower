"""Optional Codex Trace adapter: detection and manual drill-down identifiers (spec 5.10, 12).

No live Codex Trace instance was available in this environment; the
fixtures in spikes/codex-trace/fixtures/ are synthetic, matching the
documented route shapes recorded in docs/references/references.md. See
that spike's README for what is and is not verified.

Codex rollout JSONL remains the source of truth; Codex Trace is an
optional, manually correlated drill-down. It is treated as an
unauthenticated loopback service with permissive CORS -- this adapter
never proxies it externally and never widens its own bind to accommodate
it. Deep-link generation stays disabled since no stable clickable-URL
contract is verified; only the API base and session id are exposed for an
operator to open manually.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:11424"
DEFAULT_TIMEOUT_SECONDS = 2.0


class CodexTraceUnavailable(RuntimeError):
    """Codex Trace did not respond usably: absent, network error, or incompatible response."""


@dataclass(frozen=True, slots=True)
class CodexTraceSession:
    session_id: str
    path: str | None


@dataclass
class CodexTraceClient:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    http_client: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        return self.http_client or httpx.Client(timeout=self.timeout_seconds)

    def list_sessions(self) -> list[CodexTraceSession]:
        client = self._client()
        try:
            response = client.post(f"{self.base_url}/api/sessions", json={})
        except httpx.HTTPError as exc:
            raise CodexTraceUnavailable(f"request failed: {exc}") from exc
        if response.status_code != 200:
            raise CodexTraceUnavailable(f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise CodexTraceUnavailable(f"invalid JSON response: {exc}") from exc
        sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, list):
            raise CodexTraceUnavailable("incompatible response: missing 'sessions' list")

        result = []
        for entry in sessions:
            if not isinstance(entry, dict):
                continue
            session_id = entry.get("id")
            if isinstance(session_id, str) and session_id:
                path = entry.get("path")
                result.append(
                    CodexTraceSession(
                        session_id=session_id, path=path if isinstance(path, str) else None
                    )
                )
        return result

    def is_available(self) -> bool:
        try:
            self.list_sessions()
        except CodexTraceUnavailable:
            return False
        return True

    def correlate_session(
        self, session_id: str | None = None, *, path: str | None = None
    ) -> CodexTraceSession | None:
        """Match by canonical session id, or by rollout path as a fallback.

        Ambiguity is never resolved by guessing: only an exact, unique
        match on either field correlates.
        """
        sessions = self.list_sessions()
        if session_id is not None:
            matches = [s for s in sessions if s.session_id == session_id]
            if len(matches) == 1:
                return matches[0]
            return None
        if path is not None:
            matches = [s for s in sessions if s.path == path]
            if len(matches) == 1:
                return matches[0]
        return None

    def drill_down_identifiers(self, session_id: str) -> dict[str, str | None]:
        """API base and session id only. Never a clickable deep link (spec 5.10/12)."""
        return {"api_base": self.base_url, "session_id": session_id, "deep_link": None}
