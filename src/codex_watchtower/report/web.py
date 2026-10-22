"""Self-contained HTML snapshot, safe JSON embedding and reconnectable SSE."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

if TYPE_CHECKING:
    from codex_watchtower.report.engine import ReportEngine
    from codex_watchtower.report.workspace import ReportWorkspace


class SessionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-f0-9]{24}$")


def render_html(snapshot: dict[str, Any]) -> str:
    template = Path(__file__).with_name("page.html").read_text(encoding="utf-8")
    data = json.dumps(snapshot, ensure_ascii=False).replace("<", "\\u003c")
    data = data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return template.replace("__INITIAL_SNAPSHOT__", data)


def export_html(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".watchtower-", suffix=".html", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(render_html(snapshot))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def report_events(
    engine: ReportEngine | ReportWorkspace, request: Request
) -> AsyncIterator[str]:
    last = ""
    while not await request.is_disconnected():
        data = json.dumps(engine.snapshot(), ensure_ascii=False, separators=(",", ":"))
        revision = hashlib.sha256(data.encode()).hexdigest()
        if revision != last:
            # Every new connection gets the current snapshot, even with Last-Event-ID.
            yield f"id: {revision}\ndata: {data}\n\n"
            last = revision
        else:
            yield ": heartbeat\n\n"
        await asyncio.sleep(1)


def create_report_app(engine: ReportEngine, *, workspace: ReportWorkspace | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    provider = workspace if workspace else engine

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return render_html(provider.snapshot())

    @app.get("/report.json")
    async def snapshot() -> JSONResponse:
        return JSONResponse(provider.snapshot())

    @app.get("/sessions")
    async def sessions(q: str = Query(default="", max_length=120)) -> JSONResponse:
        if workspace is None:
            raise HTTPException(404, "Session switching is unavailable")
        return JSONResponse(await workspace.search(q))

    @app.post("/session")
    async def select_session(selection: SessionSelection, request: Request) -> JSONResponse:
        if workspace is None:
            raise HTTPException(404, "Session switching is unavailable")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin selection is forbidden")
        try:
            return JSONResponse(await asyncio.shield(workspace.select(selection.key)))
        except KeyError:
            raise HTTPException(404, "Session not found; refresh the search") from None
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(409, "Cannot open session; current report is preserved") from None

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            report_events(provider, request),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    return app
