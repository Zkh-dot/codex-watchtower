"""FastAPI application factory for the local Watchtower status API (spec 5.9).

Local bind only by default: ``127.0.0.1`` (``DEFAULT_BIND_HOST``, applied
by the service CLI's ``uvicorn.run`` call, Task 29). No route mutates
Codex or the workspace. The one route that mutates anything --
``POST /api/v1/sessions/{id}/assess`` -- mutates only Watchtower's own
state (it records a requested Terra escalation) and may consume model-call
quota; that is documented on the route itself, and the route does not
exist at all (404, not merely 401) unless an operator token is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import FastAPI

from codex_watchtower.storage.repository import Repository

DEFAULT_BIND_HOST = "127.0.0.1"


@dataclass
class APIConfig:
    operator_token: str | None = None
    # Exact-match allowlist; deliberately never "*" (spec 5.9: disabled
    # wildcard CORS).
    allowed_origins: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "localhost"])
    idempotency_ttl_seconds: int = 300


def create_app(repo: Repository, config: APIConfig | None = None) -> FastAPI:
    app = FastAPI(title="Codex Watchtower", version="0.1.0")
    idempotency_cache: dict[str, dict[str, object]] = {}
    assess_in_progress: set[str] = set()
    app.state.repo = repo
    app.state.config = config or APIConfig()
    app.state.idempotency_cache = idempotency_cache
    app.state.assess_in_progress = assess_in_progress

    from codex_watchtower.api.routes import router

    app.include_router(router)
    return app
