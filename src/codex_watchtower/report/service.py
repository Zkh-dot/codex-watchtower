"""CLI session selection and single-process loopback server lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from pathlib import Path

import uvicorn

from codex_watchtower.codex.discovery import discover_sessions, session_title
from codex_watchtower.config import WatchtowerConfig
from codex_watchtower.report.engine import ReportEngine
from codex_watchtower.report.web import create_report_app
from codex_watchtower.report.workspace import ReportWorkspace
from codex_watchtower.service import _load_config_or_none, resolve_config_path


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


async def serve_report(
    engine: ReportEngine, workspace: ReportWorkspace | None = None, *, port: int = 0
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        server = uvicorn.Server(
            uvicorn.Config(
                create_report_app(engine, workspace=workspace),
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=3,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not serving.done():
            await asyncio.sleep(0.01)
        if serving.done():
            await serving
            raise RuntimeError("report HTTP server did not start")
        print(f"Report: http://127.0.0.1:{sock.getsockname()[1]}/", flush=True)
        print(f"HTML: {engine.output}", flush=True)
        watching = asyncio.create_task(workspace.watch() if workspace else engine.watch())
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
        if not 0 <= args.port <= 65535:
            raise ValueError("--port must be between 0 and 65535")
        source = select_source(args, config.sessions_root)
        workspace = ReportWorkspace(
            source,
            config.sessions_root,
            config.state_dir / "progress",
            Path(args.output).expanduser(),
            interval=args.interval,
            model=args.model,
            config_path=Path(args.config).expanduser() if args.config else None,
        )
        try:
            workspace.engine.export()
            asyncio.run(serve_report(workspace.engine, workspace, port=args.port))
        finally:
            workspace.close()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"report: {exc}", file=sys.stderr)
        return 2
    return 0
