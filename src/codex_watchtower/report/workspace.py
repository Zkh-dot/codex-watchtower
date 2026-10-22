"""One active report, with a searchable local catalog and per-source checkpoints."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import sqlite3
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codex_watchtower.codex.discovery import discover_sessions, is_user_session, read_session_meta
from codex_watchtower.privacy.redact import redact_text
from codex_watchtower.report.engine import ReportEngine
from codex_watchtower.storage.db import open_database
from codex_watchtower.storage.repository import Repository


def recent_session(started_at: str) -> bool:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        return now - timedelta(days=7) <= started <= now
    except (ValueError, TypeError):
        return False


def session_title(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace") as stream:
        remaining = 256_000
        for _ in range(100):
            line = stream.readline(min(remaining, 64_000))
            remaining -= len(line)
            if not line or remaining <= 0 or not line.endswith("\n"):
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            p = record.get("payload", {}) if isinstance(record, dict) else {}
            if not isinstance(p, dict) or p.get("role") != "user":
                continue
            content = p.get("content", [])
            if not isinstance(content, list):
                continue
            text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            if text and not text.startswith(("# AGENTS", "<environment_context>", "<skill>")):
                return redact_text(text).text.replace("\n", " ")[:100]
    return "без названия"


class ReportWorkspace:
    def __init__(
        self,
        source: Path,
        sessions_root: Path,
        state: Path,
        output: Path,
        *,
        interval: int,
        model: str,
        config_path: Path | None = None,
    ) -> None:
        self.root = sessions_root.resolve()
        self.initial_source = source.absolute()
        self.state = state
        self.output = output
        self.interval = interval
        self.model = model
        if output.resolve().is_relative_to(state.resolve()) or (
            config_path and output.resolve() == config_path.resolve()
        ):
            raise ValueError("output must be outside the state directory and config")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.engine, self.resources = self._open(self.initial_source)
        self.revision = 0
        self.task: asyncio.Task[None] | None = None
        self.running = False
        self.switch_lock = asyncio.Lock()
        self.catalog: dict[str, dict[str, Any]] = {}
        self.catalog_at = 0.0
        self.catalog_lock = asyncio.Lock()
        self.titles: dict[str, str] = {}

    def _open(self, source: Path) -> tuple[ReportEngine, ExitStack]:
        resources = ExitStack()
        try:
            name = self.key(source)
            lock = resources.enter_context((self.state / f"{name}.lock").open("a"))
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("a report process is already observing this source") from exc
            conn = open_database(self.state / f"{name}.sqlite3")
            resources.callback(conn.close)
            engine = ReportEngine(
                Repository(conn), source, self.output, interval=self.interval, model=self.model
            )
            return engine, resources
        except BaseException:
            resources.close()
            raise

    @staticmethod
    def key(source: Path) -> str:
        return hashlib.sha256(str(source).encode()).hexdigest()[:24]

    def _scan(self) -> dict[str, dict[str, Any]]:
        titles = {}
        # Codex's local thread index has user-visible titles, including renamed ones.
        for index in sorted(self.root.parent.glob("state_*.sqlite"), reverse=True):
            if index.is_symlink():
                continue
            try:
                conn = sqlite3.connect(index.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
                try:
                    titles = dict(conn.execute("SELECT id, substr(title,1,600) FROM threads"))
                finally:
                    conn.close()
                break
            except sqlite3.Error:
                continue
        sessions = discover_sessions(self.root)
        rows = [
            (s.path.absolute(), s.session_id, s.workspace or "", s.started_at or "")
            for s in sessions
            if s.metadata_complete and s.user_session
        ]
        if not any(row[0] == self.initial_source for row in rows):
            meta = read_session_meta(self.initial_source)
            if is_user_session(meta):
                rows.append(
                    (
                        self.initial_source,
                        str(meta.get("id", "")),
                        str(meta.get("cwd", "")),
                        str(meta.get("timestamp", "")),
                    )
                )
        result = {}
        for path, session_id, workspace, started_at in rows:
            if not recent_session(started_at):
                continue
            if path.is_symlink() or (
                path != self.initial_source and not path.resolve().is_relative_to(self.root)
            ):
                continue
            key = self.key(path)
            title = titles.get(session_id)
            if not isinstance(title, str) or not title.strip():
                if self.titles.get(key, "без названия") == "без названия":
                    try:
                        self.titles[key] = session_title(path)
                    except OSError:
                        self.titles[key] = "без названия"
                title = self.titles[key]
            result[key] = {
                "path": path,
                "key": key,
                "session_id": session_id,
                "workspace": redact_text(workspace).text,
                "started_at": started_at,
                "title": redact_text(title).text,
            }
        return result

    async def search(self, query: str) -> list[dict[str, Any]]:
        async with self.catalog_lock:
            if time.monotonic() - self.catalog_at > 15:
                self.catalog = await asyncio.to_thread(self._scan)
                self.catalog_at = time.monotonic()
        words = query.casefold().split()
        matches = [
            row
            for row in self.catalog.values()
            if recent_session(row["started_at"])
            and all(
                word in (row["session_id"] + " " + row["workspace"] + " " + row["title"]).casefold()
                for word in words
            )
        ]
        matches.sort(key=lambda row: row["started_at"], reverse=True)
        return [{k: v for k, v in row.items() if k != "path"} for row in matches[:20]]

    async def select(self, key: str) -> dict[str, Any]:
        async with self.switch_lock:
            if key == self.key(self.engine.source):
                return self.snapshot()
            if key not in self.catalog or not recent_session(self.catalog[key]["started_at"]):
                raise KeyError(key)
            path = self.catalog[key]["path"]
            if path.is_symlink() or (
                path != self.initial_source and not path.resolve().is_relative_to(self.root)
            ):
                raise ValueError("session source is no longer safe")
            engine, resources = self._open(path)
            try:
                old = self.task
                self.task = None
                if old:
                    old.cancel()
                    await asyncio.gather(old, return_exceptions=True)
                self.resources.close()
                self.engine, self.resources = engine, resources
            except BaseException:
                resources.close()
                if self.running:
                    self.task = asyncio.create_task(self.engine.watch())
                raise
            self.revision += 1
            engine.export()
            if self.running:
                self.task = asyncio.create_task(engine.watch())
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        selected = self.catalog.get(self.key(self.engine.source), {})
        return {
            **self.engine.snapshot(),
            "selection_revision": self.revision,
            "session_picker": True,
            "session_title": selected.get("title", ""),
            "session_workspace": selected.get("workspace", ""),
        }

    async def watch(self) -> None:
        self.running = True
        self.task = asyncio.create_task(self.engine.watch())
        try:
            while True:
                await asyncio.sleep(1)
                if self.task and self.task.done():
                    await self.task
                    raise RuntimeError("report watcher stopped")
        finally:
            self.running = False
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    def close(self) -> None:
        self.resources.close()
