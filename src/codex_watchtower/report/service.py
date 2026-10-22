"""CLI session selection and single-process loopback server lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import socket
import sys
from pathlib import Path

import uvicorn

from codex_watchtower.codex.discovery import discover_sessions
from codex_watchtower.config import WatchtowerConfig
from codex_watchtower.privacy.redact import redact_text
from codex_watchtower.report.engine import ReportEngine
from codex_watchtower.report.web import create_report_app
from codex_watchtower.service import _load_config_or_none, resolve_config_path
from codex_watchtower.storage.db import open_database
from codex_watchtower.storage.repository import Repository


def session_title(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace") as stream:
        for _, line in zip(range(100), stream, strict=False):
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


def select_source(args: argparse.Namespace, root: Path) -> Path:
    if args.log:
        return Path(args.log).expanduser().absolute()
    sessions = discover_sessions(root)
    if args.session:
        matches = [s.path for s in sessions if s.session_id == args.session]
        if len(matches) != 1:
            raise ValueError("session ID must match exactly one rollout; use --log for duplicates")
        return matches[0].absolute()
    if not sys.stdin.isatty():
        raise ValueError("specify --session or --log when stdin is not a terminal")
    if not sessions:
        raise ValueError("no Codex sessions found")
    sessions.sort(key=lambda s: s.started_at or "", reverse=True)
    for i, session in enumerate(sessions, 1):
        print(
            f"{i}. {session.session_id}  {session.started_at or '?'}  {session_title(session.path)}"
        )
    try:
        index = int(input("Номер сессии: ")) - 1
    except (ValueError, EOFError) as exc:
        raise ValueError("invalid session selection") from exc
    if not 0 <= index < len(sessions):
        raise ValueError("session selection out of range")
    return sessions[index].path.absolute()


async def serve_report(engine: ReportEngine) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        server = uvicorn.Server(
            uvicorn.Config(create_report_app(engine), log_level="warning", access_log=False)
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not serving.done():
            await asyncio.sleep(0.01)
        if serving.done():
            await serving
            raise RuntimeError("report HTTP server did not start")
        print(f"Report: http://127.0.0.1:{sock.getsockname()[1]}/", flush=True)
        print(f"HTML: {engine.output}", flush=True)
        watching = asyncio.create_task(engine.watch())
        try:
            done, _ = await asyncio.wait({serving, watching}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            watching.cancel()
            server.should_exit = True
            await asyncio.gather(watching, serving, return_exceptions=True)


def report_main(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    if config_path is None and args.config is None:
        config: WatchtowerConfig | None = WatchtowerConfig(
            sessions_root=Path.home() / ".codex/sessions",
            state_dir=Path.home() / ".local/state/codex-watchtower",
        )
    else:
        config, error = _load_config_or_none(config_path)
        if config is None:
            print(error, file=sys.stderr)
            return 2
    assert config is not None
    try:
        if not 300 <= args.interval <= 600:
            raise ValueError("--interval must be between 300 and 600 seconds")
        source = select_source(args, config.sessions_root)
        # A separate database avoids consuming the existing serve mode's ingestion cursor.
        state = config.state_dir / "progress"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        import hashlib

        name = hashlib.sha256(str(source).encode()).hexdigest()[:24]
        with (state / f"{name}.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("a report process is already observing this source") from exc
            conn = open_database(state / f"{name}.sqlite3")
            try:
                engine = ReportEngine(
                    Repository(conn),
                    source,
                    Path(args.output).expanduser(),
                    interval=args.interval,
                    model=args.model,
                )
                # Refuse to overwrite the database, lock, source, or config via --output.
                output = engine.output.resolve()
                if output.is_relative_to(state.resolve()) or (
                    args.config and output == Path(args.config).expanduser().resolve()
                ):
                    raise ValueError("output must be outside the state directory and config")
                engine.export()
                asyncio.run(serve_report(engine))
            finally:
                conn.close()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"report: {exc}", file=sys.stderr)
        return 2
    return 0
