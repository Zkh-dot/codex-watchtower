"""Switching preserves per-session state and never runs two analysis loops."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from codex_watchtower.report.engine import ReportEngine
from codex_watchtower.report.web import create_report_app
from codex_watchtower.report.workspace import ReportWorkspace
from tests.unit.test_progress_report import report_for


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "codex" / "sessions"
    folder = root / "2026/10/22"
    folder.mkdir(parents=True)
    for name, cwd, title in [
        ("a", "/home/user/mtg", "Расчёт колоды"),
        ("b", "/home/user/demo", "Починить API"),
    ]:
        records = [
            {
                "type": "session_meta",
                "payload": {"id": name, "cwd": cwd, "timestamp": datetime.now(UTC).isoformat()},
            },
            {
                "type": "response_item",
                "timestamp": "2026-10-22T08:01:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"text": title}]},
            },
        ]
        (folder / f"rollout-{name}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )
    observer = ReportWorkspace(
        folder / "rollout-a.jsonl",
        root,
        tmp_path / "state",
        tmp_path / "report.html",
        interval=300,
        model="offline",
    )
    yield observer
    observer.close()


@pytest.mark.asyncio
async def test_search_excludes_old_sessions_even_when_catalog_is_cached(workspace):
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path = workspace.initial_source
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[0]["payload"]["timestamp"] = old
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert [row["session_id"] for row in await workspace.search("")] == ["b"]
    assert workspace.snapshot()["session_id"] == "a"
    key = next(iter(workspace.catalog))
    for timestamp in (old, "invalid", "2026-01-01", "2999-01-01T00:00:00Z"):
        workspace.catalog[key]["started_at"] = timestamp
        assert await workspace.search("") == []
        with pytest.raises(KeyError):
            await workspace.select(key)


@pytest.mark.asyncio
async def test_search_by_folder_id_and_title_and_saved_report_round_trip(workspace):
    assert [r["session_id"] for r in await workspace.search("mtg")] == ["a"]
    assert [r["session_id"] for r in await workspace.search("колОДы")] == ["a"]
    assert [r["session_id"] for r in await workspace.search("demo API")] == ["b"]
    rows = await workspace.search("")
    assert all("path" not in row for row in rows)
    keys = {r["session_id"]: r["key"] for r in rows}
    workspace.engine.ingest()
    ref = workspace.engine.repo.get_events_since("a", None)[0]["logical_event_id"]
    workspace.engine.assessor = AsyncMock(return_value=report_for(ref))
    await workspace.engine.assess()
    assert workspace.snapshot()["version"] == 1
    selected = await workspace.select(keys["b"])
    assert selected["session_id"] == "b" and selected["version"] == 0
    assert selected["selection_revision"] == 1
    assert (await workspace.select(keys["b"]))["selection_revision"] == 1
    restored = await workspace.select(keys["a"])
    assert restored["version"] == 1 and restored["selection_revision"] == 2
    assert restored["report"]["tasks"][0]["id"] == "report"


@pytest.mark.asyncio
async def test_renamed_codex_title_and_fallback_without_index(workspace):
    index = workspace.root.parent / "state_5.sqlite"
    conn = sqlite3.connect(index)
    conn.execute("CREATE TABLE threads(id TEXT, title TEXT)")
    conn.execute("INSERT INTO threads VALUES ('a', 'Новый заголовок password=abcdefghij')")
    conn.commit()
    conn.close()
    rows = await workspace.search("Новый заголовок")
    assert len(rows) == 1 and "REDACTED" in rows[0]["title"]
    assert "abcdefghij" not in rows[0]["title"]
    index.unlink()
    workspace.catalog_at = 0
    assert len(await workspace.search("Расчёт колоды")) == 1


@pytest.mark.asyncio
async def test_locked_or_replaced_source_preserves_selected_session(workspace, tmp_path):
    rows = await workspace.search("API")
    key = rows[0]["key"]
    path = workspace.catalog[key]["path"]
    other = ReportWorkspace(
        path,
        workspace.root,
        workspace.state,
        tmp_path / "other.html",
        interval=300,
        model="offline",
    )
    try:
        with pytest.raises(ValueError, match="already observing"):
            await workspace.select(key)
        assert workspace.engine.session_id == "a"
    finally:
        other.close()
    path.unlink()
    path.symlink_to(workspace.initial_source)
    with pytest.raises(ValueError, match="safe"):
        await workspace.select(key)
    assert workspace.engine.session_id == "a"


@pytest.mark.asyncio
async def test_switch_cancels_previous_loop_before_starting_next(workspace, monkeypatch):
    active = set()
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def watch(engine):
        assert not active
        active.add(engine.session_id)
        started[engine.session_id].set()
        try:
            await asyncio.Future()
        finally:
            active.remove(engine.session_id)

    monkeypatch.setattr(ReportEngine, "watch", watch)
    rows = await workspace.search("API")
    supervisor = asyncio.create_task(workspace.watch())
    try:
        await asyncio.wait_for(started["a"].wait(), 2)
        await workspace.select(rows[0]["key"])
        await asyncio.wait_for(started["b"].wait(), 2)
        assert active == {"b"} and not supervisor.done()
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
    assert not active


@pytest.mark.asyncio
async def test_search_and_selection_api_reject_paths_and_foreign_origins(workspace):
    app = create_report_app(workspace.engine, workspace=workspace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        rows = (await client.get("/sessions", params={"q": "API"})).json()
        assert len(rows) == 1
        body = {"key": rows[0]["key"]}
        assert (
            await client.post("/session", json=body, headers={"Origin": "https://external.invalid"})
        ).status_code == 403
        assert (await client.post("/session", json={"key": "../../private"})).status_code == 422
        assert (await client.post("/session", json={"key": "f" * 24})).status_code == 404
        assert (await client.get("/sessions", params={"q": "x" * 121})).status_code == 422
        response = await client.post(
            "/session", json=body, headers={"Origin": "http://127.0.0.1:8765"}
        )
        assert response.status_code == 200
        assert response.json()["session_id"] == "b"
        assert (await client.get("/report.json")).json()["selection_revision"] == 1
        assert "session-picker" in (await client.get("/")).text
