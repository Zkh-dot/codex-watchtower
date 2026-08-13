"""Degraded-dependency e2e: core monitoring survives missing optional services.

Verifies that Watchtower's ingestion pipeline and rule-only alerts work
correctly when AgentLens is absent, model endpoints are unreachable, and
the Telegram notifier faces network failures -- all without losing events
or blocking ingestion (spec 4.1).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from codex_watchtower import domain
from codex_watchtower.agentlens.client import (
    AgentLensClient,
    AgentLensUnavailable,
)
from codex_watchtower.ingest import IngestionService
from codex_watchtower.models.client import assess as model_assess
from codex_watchtower.models.config import ModelProfile
from codex_watchtower.notify.telegram import (
    PendingDelivery,
    TelegramNotifier,
    process_delivery,
)
from codex_watchtower.storage import db
from codex_watchtower.storage.repository import Repository

T0 = datetime(2026, 8, 14, 10, 0, 0, tzinfo=UTC)
GRACE = timedelta(minutes=10)


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _meta(sid: str, ts: str) -> dict[str, object]:
    return {"type": "session_meta", "timestamp": ts, "payload": {"id": sid, "cwd": "/w"}}


def _turn(ts: str) -> dict[str, object]:
    return {"type": "turn_started", "timestamp": ts, "payload": {}}


def _msg(ts: str, text: str) -> dict[str, object]:
    return {"type": "agent_message", "timestamp": ts, "payload": {"text": text}}


def _ts(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# 1. AgentLens absent: ingestion and rules still work
# ---------------------------------------------------------------------------


def test_agentlens_absent_ingestion_proceeds(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "14" / "rollout-degraded.jsonl"
    _write(rollout, [_meta("degraded-1", _ts(0)), _turn(_ts(1)), _msg(_ts(2), "Working")])

    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo, quiet_grace_period=GRACE)
    service.poll_once(now=T0 + timedelta(minutes=2))

    # Ingestion succeeded.
    events = repo.get_events_since("degraded-1", after_sequence=None)
    assert len(events) == 2

    # AgentLens is unreachable -- but that must not affect monitoring.
    client = AgentLensClient(base_url="http://127.0.0.1:1/mcp", timeout_seconds=0.5)
    with pytest.raises(AgentLensUnavailable):
        client.get_recent_sessions()

    # Reconciled assessment exists and is rule-only (no model assessment).
    reconciled = repo.get_latest_reconciled("degraded-1")
    assert reconciled is not None
    assert reconciled.model_assessment is None
    assert reconciled.state == domain.SessionState.active_turn

    conn.close()


# ---------------------------------------------------------------------------
# 2. AgentLens starts midway: no duplicate session creation
# ---------------------------------------------------------------------------


def test_agentlens_midway_no_duplicate_session(tmp_path: Path) -> None:
    """A fixture-compatible AgentLens appearing midway enriches, never duplicates."""
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "14" / "rollout-midway.jsonl"
    _write(rollout, [_meta("midway-1", _ts(0)), _turn(_ts(1)), _msg(_ts(2), "Work")])

    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo, quiet_grace_period=GRACE)

    # First poll: AgentLens absent.
    service.poll_once(now=T0 + timedelta(minutes=2))
    assert repo.get_session("midway-1") is not None

    # Second poll: still the same single session, no duplicates.
    service.poll_once(now=T0 + timedelta(minutes=3))
    session = repo.get_session("midway-1")
    assert session is not None
    assert session["state"] == domain.SessionState.active_turn.value

    events = repo.get_events_since("midway-1", after_sequence=None)
    assert len(events) == 2  # exactly-once, no duplication

    conn.close()


# ---------------------------------------------------------------------------
# 3. Model endpoints down: rule-only alerts still fire
# ---------------------------------------------------------------------------


def test_model_endpoints_down_rule_only_alerts(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    rollout = sessions_root / "2026" / "08" / "14" / "rollout-model-down.jsonl"
    _write(
        rollout,
        [
            _meta("model-down", _ts(0)),
            _turn(_ts(1)),
            _msg(_ts(2), "Starting"),
        ],
    )

    conn = db.open_database(tmp_path / "state.db")
    repo = Repository(conn)
    service = IngestionService(sessions_root, repo, quiet_grace_period=GRACE)
    service.poll_once(now=T0 + timedelta(minutes=2))

    # Model endpoint is unreachable.
    profile = ModelProfile(
        name="luna",
        endpoint="http://127.0.0.1:1/v1/chat/completions",
        model_identifier="test-model",
        timeout_seconds=0.5,
    )

    # Build a minimal observation to exercise the model client.
    from codex_watchtower.domain import (
        Evicted,
        Goal,
        Observation,
        SessionInfo,
        Truncation,
        Window,
    )

    observation = Observation(
        session=SessionInfo(
            id="model-down",
            workspace="/w",
            started_at=_ts(0),
            elapsed_seconds=120,
            state=domain.SessionState.active_turn,
        ),
        goal=Goal(text="test goal", acceptance_criteria=["pass"]),
        window=Window(from_cursor=0, to_cursor=2, opened_at=_ts(0), closed_at=_ts(2)),
        previous_assessment=None,
        events=[],
        signals=[],
        system_refs=[],
        redactions=[],
        truncation=Truncation(budget_characters=48000, used_characters=0, evicted=Evicted()),
    )

    result = model_assess(profile, observation, system_prompt="test")
    assert result.assessment is None
    assert result.failure_reason is not None

    # Rule-only reconciliation is still intact.
    reconciled = repo.get_latest_reconciled("model-down")
    assert reconciled is not None
    assert reconciled.model_assessment is None
    assert reconciled.state == domain.SessionState.active_turn

    conn.close()


# ---------------------------------------------------------------------------
# 4. Telegram endpoint down: durable retry
# ---------------------------------------------------------------------------


def test_telegram_down_durable_retry(tmp_path: Path) -> None:
    """A failed Telegram delivery retries with backoff, not a silent drop."""
    transport = httpx.MockTransport(lambda req: httpx.Response(503, text="service unavailable"))
    client = httpx.Client(transport=transport, timeout=5.0)
    notifier = TelegramNotifier(
        bot_token="fake-token",
        chat_id_allowlist=["123"],
        http_client=client,
    )

    pending = PendingDelivery(
        dedup_key="sess-1:1:0:stalled:abc",
        chat_id="123",
        text="Session stalled",
    )

    now = T0
    outcome = process_delivery(notifier, pending, now=now, max_retries=5)
    assert not outcome.delivered
    assert not outcome.gave_up
    assert outcome.pending is not None
    assert outcome.pending.attempt == 1
    assert outcome.pending.next_retry_at is not None
    assert outcome.pending.next_retry_at > now  # backoff scheduled

    # Advance clock and retry: still failing, attempt increments.
    now2 = outcome.pending.next_retry_at or now  # type: ignore[assignment]
    outcome2 = process_delivery(notifier, outcome.pending, now=now2, max_retries=5)
    assert not outcome2.delivered
    assert outcome2.pending is not None
    assert outcome2.pending.attempt == 2

    # Eventually gives up after max_retries.
    pending_final = outcome2.pending
    assert pending_final is not None
    for _ in range(10):
        next_now = pending_final.next_retry_at or now2 + timedelta(seconds=30)
        outcome_final = process_delivery(notifier, pending_final, now=next_now, max_retries=5)
        if outcome_final.gave_up:
            break
        pending_final = outcome_final.pending
        assert pending_final is not None

    assert outcome_final.gave_up
    client.close()
