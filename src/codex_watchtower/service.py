"""Service lifecycle: serve/run/inspect/assess/doctor command bodies (Task 29).

Kept out of cli.py so ``--version`` and ``--help`` never import storage,
ingest, the API, or the launcher.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx

from codex_watchtower.config import ConfigError, WatchtowerConfig, load_config

DEFAULT_CONFIG_PATHS = (
    Path("config.toml"),
    Path.home() / ".config" / "codex-watchtower" / "config.toml",
)


def resolve_config_path(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    return None


def _load_config_or_none(config_path: Path | None) -> tuple[WatchtowerConfig | None, str | None]:
    if config_path is None:
        return None, (
            "no config.toml found (checked ./config.toml and "
            "~/.config/codex-watchtower/config.toml); copy config.example.toml to get started"
        )
    try:
        return load_config(config_path), None
    except ConfigError as exc:
        return None, f"invalid configuration in {config_path}: {exc}"
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        return None, f"cannot read {config_path}: {exc}"


# --- doctor ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    healthy: bool
    detail: str


def run_doctor_checks(config_path: Path | None) -> list[DoctorCheck]:
    config, error = _load_config_or_none(config_path)
    if config is None:
        return [DoctorCheck("config", False, error or "unknown configuration error")]

    checks = [DoctorCheck("config", True, f"loaded {config_path}")]

    if config.sessions_root.is_dir():
        checks.append(DoctorCheck("sessions_root", True, str(config.sessions_root)))
    else:
        checks.append(
            DoctorCheck(
                "sessions_root",
                False,
                f"{config.sessions_root} does not exist (Codex may not have run yet)",
            )
        )

    if config.agentlens.enabled:
        from codex_watchtower.agentlens.client import (
            AgentLensClient,
            AgentLensIncompatible,
            AgentLensUnavailable,
        )

        client = AgentLensClient(base_url=config.agentlens.base_url)
        try:
            client.get_recent_sessions()
        except (AgentLensUnavailable, AgentLensIncompatible) as exc:
            checks.append(DoctorCheck("agentlens", False, f"unavailable: {exc}"))
        else:
            checks.append(DoctorCheck("agentlens", True, "reachable"))
    else:
        checks.append(DoctorCheck("agentlens", True, "disabled (optional)"))

    from codex_watchtower.storage import db

    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        conn = db.open_database(config.state_dir / "state.db")
        conn.close()
    except (OSError, db.DatabaseCorruptionError) as exc:
        checks.append(DoctorCheck("state_dir", False, str(exc)))
    else:
        checks.append(DoctorCheck("state_dir", True, str(config.state_dir)))

    return checks


def doctor_main(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(getattr(args, "config", None))
    checks = run_doctor_checks(config_path)
    healthy = True
    for check in checks:
        status = "OK" if check.healthy else "FAIL"
        print(f"[{status}] {check.name}: {check.detail}")
        healthy = healthy and check.healthy
    return 0 if healthy else 1


# --- inspect -----------------------------------------------------------


def inspect_main(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(getattr(args, "config", None))
    config, error = _load_config_or_none(config_path)
    if config is None:
        print(error)
        return 1

    from codex_watchtower.storage import db
    from codex_watchtower.storage.repository import Repository

    config.state_dir.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(config.state_dir / "state.db")
    repo = Repository(conn)
    reconciled = repo.get_latest_reconciled(args.session_id)
    if reconciled is None:
        print(f"no reconciled state for session {args.session_id!r}")
        return 1
    print(json.dumps(reconciled.model_dump(mode="json"), indent=2))
    return 0


# --- assess --------------------------------------------------------------


def assess_main(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(getattr(args, "config", None))
    config, error = _load_config_or_none(config_path)
    if config is None:
        print(error)
        return 1
    if config.operator_token is None:
        print("operator_token is not configured; the assess endpoint is disabled")
        return 1

    url = f"http://{config.bind.host}:{config.bind.port}/api/v1/sessions/{args.session_id}/assess"
    try:
        response = httpx.post(
            url, headers={"Authorization": f"Bearer {config.operator_token}"}, timeout=10.0
        )
    except httpx.HTTPError as exc:
        print(f"request failed: {exc} (is 'watchtower serve' running?)")
        return 1
    print(f"{response.status_code} {response.text}")
    return 0 if response.status_code == 200 else 1


# --- run -----------------------------------------------------------------


def run_main(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(getattr(args, "config", None))
    config, error = _load_config_or_none(config_path)
    if config is None:
        print(error)
        return 1

    argv = list(args.wrapped_argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("usage: watchtower run -- <command> [args...]")
        return 2

    from codex_watchtower.launcher.run import SpawnFailed, run_wrapped
    from codex_watchtower.storage import db
    from codex_watchtower.storage.repository import Repository

    config.state_dir.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(config.state_dir / "state.db")
    repo = Repository(conn)
    try:
        result = run_wrapped(argv, repo=repo, sessions_root=config.sessions_root)
    except SpawnFailed as exc:
        print(f"failed to spawn {argv!r}: {exc}")
        return 1
    return result.exit_code


# --- serve -----------------------------------------------------------------


async def _run_server_and_ingestion(config: WatchtowerConfig, *, poll_interval: float) -> None:
    import asyncio

    import uvicorn

    from codex_watchtower.api.app import APIConfig, create_app
    from codex_watchtower.ingest import IngestionService
    from codex_watchtower.storage import db
    from codex_watchtower.storage.repository import Repository

    config.state_dir.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(config.state_dir / "state.db")
    repo = Repository(conn)
    ingestion = IngestionService(config.sessions_root, repo)

    api_config = APIConfig(operator_token=config.operator_token)
    app = create_app(repo, api_config)
    uv_config = uvicorn.Config(app, host=config.bind.host, port=config.bind.port, log_level="info")
    server = uvicorn.Server(uv_config)

    async def ingestion_loop() -> None:
        while True:
            await asyncio.to_thread(ingestion.poll_once)
            await asyncio.sleep(poll_interval)

    await asyncio.gather(server.serve(), ingestion_loop())


def serve_main(args: argparse.Namespace) -> int:
    import asyncio

    config_path = resolve_config_path(getattr(args, "config", None))
    config, error = _load_config_or_none(config_path)
    if config is None:
        print(error)
        return 1

    try:
        asyncio.run(_run_server_and_ingestion(config, poll_interval=5.0))
    except KeyboardInterrupt:
        pass
    return 0
