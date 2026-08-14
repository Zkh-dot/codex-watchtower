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
from typing import TYPE_CHECKING

import httpx

from codex_watchtower.config import (
    BudgetConfig,
    ConfigError,
    ModelEndpointConfig,
    WatchtowerConfig,
    load_config,
)

if TYPE_CHECKING:
    from codex_watchtower.storage.repository import Repository

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
    import os

    import uvicorn

    from codex_watchtower.api.app import APIConfig, create_app
    from codex_watchtower.ingest import IngestionService
    from codex_watchtower.notify.telegram import TelegramNotifier
    from codex_watchtower.storage import db
    from codex_watchtower.storage.repository import Repository

    config.state_dir.mkdir(parents=True, exist_ok=True)
    conn = db.open_database(config.state_dir / "state.db")
    repo = Repository(conn)

    # Dedicated autocommit connection for lease/heartbeat operations.
    # This ensures heartbeat writes commit independently of ingestion
    # transactions, so a long ingestion transaction cannot cause the
    # heartbeat to be invisible to a second connection or lost on
    # rollback (R9#1).
    lease_conn = db.connect(config.state_dir / "state.db")
    lease_repo = Repository(lease_conn)

    # Generate a unique owner identity for this serve process (R7#1).
    # Using a UUID instead of PID avoids the PID-reuse race where a
    # restarted process gets the same PID as a crashed owner.
    import uuid

    owner_id = str(uuid.uuid4())
    owner_pid = os.getpid()

    # Recover abandoned model-call reservations from a previous crashed
    # serve process. This runs only in the serve command, not in
    # open_database(), so inspect/doctor cannot reclaim a live serve
    # process's reservations (R6#1). Only reservations whose owner_id
    # is not registered (or has a stale heartbeat) are reclaimed.
    repo.recover_abandoned_reservations()

    # Register this serve instance so recovery can tell we're alive.
    # Uses the dedicated lease connection (R9#1).
    lease_repo.register_service_instance(owner_id, owner_pid)

    ingestion = IngestionService(config.sessions_root, repo)

    # Build Telegram notifier if configured.
    telegram_notifier: TelegramNotifier | None = None
    if config.telegram.enabled:
        token_env = config.telegram.bot_token_env_var
        token = os.environ.get(token_env) if token_env else None
        if token:
            telegram_notifier = TelegramNotifier(
                bot_token=token,
                chat_id_allowlist=config.telegram.chat_id_allowlist,
            )

    api_config = APIConfig(operator_token=config.operator_token)
    app = create_app(repo, api_config)
    # Expose telegram notifier and model config to the API for /assess.
    app.state.telegram_notifier = telegram_notifier
    app.state.luna_config = config.luna
    app.state.terra_config = config.terra
    app.state.budget = config.budget
    app.state.owner_id = owner_id
    uv_config = uvicorn.Config(app, host=config.bind.host, port=config.bind.port, log_level="info")
    server = uvicorn.Server(uv_config)

    async def ingestion_loop() -> None:
        while True:
            await asyncio.to_thread(ingestion.poll_once)
            # Dispatch notifications for sessions that need attention.
            if telegram_notifier is not None:
                await asyncio.to_thread(
                    _dispatch_notifications,
                    repo,
                    telegram_notifier,
                    config.telegram.chat_id_allowlist,
                )
            # Run periodic Luna assessments if configured.
            if config.luna is not None:
                await asyncio.to_thread(
                    _run_scheduled_luna, repo, config.luna, config.budget, owner_id
                )
            await asyncio.sleep(poll_interval)

    async def heartbeat_loop() -> None:
        """Renew the lease independently of ingestion/model execution (R8#2).

        Uses a dedicated autocommit connection (R9#1) so heartbeat writes
        commit immediately and are visible to other connections even
        while an ingestion transaction is open.
        """
        heartbeat_interval = min(60, poll_interval)
        while True:
            lease_repo.update_heartbeat(owner_id)
            await asyncio.sleep(heartbeat_interval)

    try:
        await asyncio.gather(server.serve(), ingestion_loop(), heartbeat_loop())
    finally:
        lease_repo.deregister_service_instance(owner_id)
        lease_conn.close()


def _run_scheduled_luna(
    repo: Repository,
    luna_config: ModelEndpointConfig,
    budget: BudgetConfig,
    owner_id: str | None = None,
) -> None:
    """Run scheduled Luna assessments for sessions that need it.

    Uses the AssessmentScheduler to decide which sessions are due for a
    Luna assessment based on lifecycle changes, signal changes, and
    routine cadence. Scheduler state is persisted in scheduler_state
    so evidence-change triggers compare against the values at the last
    completed assessment, not the current values (R4#3).
    """
    import hashlib
    from datetime import UTC, datetime

    from codex_watchtower import domain as _domain
    from codex_watchtower.assess.orchestrator import (
        reconcile_with_assessment,
        run_luna_assessment,
    )
    from codex_watchtower.assess.scheduler import (
        SchedulerState,
        should_schedule_assessment,
    )

    now = datetime.now(UTC)
    for row in repo.list_sessions():
        session_id = row["session_id"]
        # Skip terminal sessions.
        state_str = row["state"]
        try:
            state_enum = _domain.SessionState(state_str)
        except ValueError:
            continue
        if state_enum in (
            _domain.SessionState.terminal_completed,
            _domain.SessionState.terminal_failed,
        ):
            continue

        active_signals = repo.get_active_signals(session_id)
        fingerprint = hashlib.sha256(
            "|".join(sorted(s.id for s in active_signals)).encode()
        ).hexdigest()

        # Load persisted scheduler state from the last completed assessment.
        # This ensures lifecycle/signal changes are detected by comparing
        # against the values at assessment time, not the current values (R4#3).
        persisted = repo.get_scheduler_state(session_id)
        if persisted is not None:
            last_assessed_str = persisted["last_assessed_at"]
            last_lifecycle_str = persisted["last_lifecycle_state"]
            last_fingerprint = persisted["last_signal_fingerprint"]
            last_assessed = None
            if last_assessed_str:
                try:
                    last_assessed = datetime.fromisoformat(
                        last_assessed_str[:-1] + "+00:00"
                        if last_assessed_str.endswith("Z")
                        else last_assessed_str
                    )
                except ValueError:
                    pass
            last_lifecycle = None
            if last_lifecycle_str:
                try:
                    last_lifecycle = _domain.SessionState(last_lifecycle_str)
                except ValueError:
                    pass
        else:
            last_assessed = None
            last_lifecycle = None
            last_fingerprint = None

        sched_state = SchedulerState(
            last_assessed_at=last_assessed,
            last_lifecycle_state=last_lifecycle,
            last_signal_fingerprint=last_fingerprint,
        )

        decision = should_schedule_assessment(
            sched_state,
            now=now,
            current_lifecycle_state=state_enum,
            current_signal_fingerprint=fingerprint,
            material_progress_marker=False,
        )
        if not decision.should_assess:
            continue

        outcome = run_luna_assessment(
            repo, session_id, luna_config, budget, now=now, owner_id=owner_id
        )
        if outcome.assessment is not None or outcome.failure_reason is not None:
            reconcile_with_assessment(repo, session_id, outcome.assessment, now=now)

        # Persist scheduler state snapshot only after a completed assessment
        # (R9#2). On failure (transport error, budget exhaustion, etc.),
        # leave the prior snapshot unchanged so the session remains due
        # for retry on the next poll.
        if outcome.assessment is not None:
            repo.save_scheduler_state(
                session_id,
                last_assessed_at=now.isoformat(),
                last_lifecycle_state=state_enum.value,
                last_signal_fingerprint=fingerprint,
                last_material_progress_cursor=None,
                in_progress=False,
            )


def _dispatch_notifications(repo: Repository, notifier: object, chat_ids: list[str]) -> None:
    """Dispatch Telegram notifications with durable retry state.

    First, resumes any pending deliveries from previous polls (preserving
    attempt count and backoff). Then checks for new send-worthy sessions
    and initiates delivery, persisting any transient failures for retry
    (review #6).
    """
    from datetime import UTC, datetime

    from codex_watchtower.notify.policy import (
        SEND_WORTHY_NOTIFICATION_STATUSES,
        LastDelivery,
        deduplication_key,
        should_send,
    )
    from codex_watchtower.notify.telegram import (
        PendingDelivery,
        TelegramNotifier,
        process_delivery,
    )

    if not chat_ids:
        return
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    tg = notifier if isinstance(notifier, TelegramNotifier) else None
    if tg is None:
        return

    # 1. Resume pending deliveries from previous polls.
    for pd_row in repo.list_due_pending_deliveries(now_iso):
        pending = PendingDelivery(
            dedup_key=pd_row["dedup_key"],
            chat_id=pd_row["chat_id"],
            text=pd_row["text"],
            attempt=pd_row["attempt"],
            next_retry_at=(
                datetime.fromisoformat(pd_row["next_retry_at"]) if pd_row["next_retry_at"] else None
            ),
        )
        outcome = process_delivery(tg, pending, now=now)
        if outcome.delivered:
            repo.delete_pending_delivery(pd_row["dedup_key"], pd_row["chat_id"])
            repo.record_delivery(
                pd_row["dedup_key"],
                pd_row["session_id"],
                "unknown",
                cursor=None,
                sent_at=now_iso,
            )
        elif outcome.gave_up:
            repo.delete_pending_delivery(pd_row["dedup_key"], pd_row["chat_id"])
        elif outcome.pending is not None:
            repo.upsert_pending_delivery(
                outcome.pending.dedup_key,
                pd_row["session_id"],
                outcome.pending.chat_id,
                outcome.pending.text,
                attempt=outcome.pending.attempt,
                next_retry_at=(
                    outcome.pending.next_retry_at.isoformat()
                    if outcome.pending.next_retry_at
                    else None
                ),
            )

    # 2. Check for new send-worthy sessions.
    for row in repo.list_sessions():
        session_id = row["session_id"]
        reconciled = repo.get_latest_reconciled(session_id)
        if reconciled is None:
            continue
        if reconciled.notification_status not in SEND_WORTHY_NOTIFICATION_STATUSES:
            continue
        key = deduplication_key(reconciled)
        prior = repo.get_delivery(key)
        last_delivery = (
            LastDelivery(last_sent_at=prior.last_sent_at)
            if prior and prior.last_sent_at is not None
            else None
        )
        decision = should_send(reconciled, last_delivery=last_delivery, now=now)
        if not decision.should_send:
            continue
        text = (
            f"*{reconciled.notification_status.value}*: Session {session_id}\n"
            f"State: {reconciled.state.value}\n"
            f"Needs attention: {reconciled.needs_attention}"
        )
        for chat_id in chat_ids:
            # Suppress fresh send if a pending delivery already exists for
            # this (dedup_key, chat_id) — retry is handled by the resume
            # pass above, preserving backoff and attempt state (R3#2).
            if repo.has_pending_delivery(key, chat_id):
                continue
            pending = PendingDelivery(dedup_key=key, chat_id=chat_id, text=text)
            outcome = process_delivery(tg, pending, now=now)
            if outcome.delivered:
                repo.record_delivery(
                    key,
                    session_id,
                    reconciled.notification_status.value,
                    cursor=reconciled.event_cursor,
                    sent_at=now_iso,
                )
            elif outcome.gave_up:
                pass  # terminal failure, don't persist
            elif outcome.pending is not None:
                repo.upsert_pending_delivery(
                    outcome.pending.dedup_key,
                    session_id,
                    outcome.pending.chat_id,
                    outcome.pending.text,
                    attempt=outcome.pending.attempt,
                    next_retry_at=(
                        outcome.pending.next_retry_at.isoformat()
                        if outcome.pending.next_retry_at
                        else None
                    ),
                )


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
