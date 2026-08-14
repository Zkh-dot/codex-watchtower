"""Production assessment orchestrator: budget gate, Luna, Terra, persistence.

This module wires the full assessment cascade into the shipping `serve`
loop and the `/assess` API endpoint. It is the single production caller
of `run_luna`, `run_terra`, `insert_model_call`, and the budget ceiling
methods on the repository (review #1, #2, #3).

The orchestrator enforces:
- **Per-session assessment ceiling** (`budget.per_session_assessment_ceiling`):
  checked atomically before any model call. If the ceiling is exhausted,
  the assessment is skipped with a `budget_exhausted` reason.
- **Daily cost ceiling** (`budget.daily_cost_ceiling_cents`): checked
  before any model call. If the daily cost is at or above the ceiling,
  the assessment is skipped.
- **Request size guard** (`max_request_bytes`): the serialized provider
  request body is measured and rejected if it exceeds the budget.
- **Model call persistence**: every Luna/Terra invocation is recorded
  in `model_calls` with latency, input size, success/failure, and
  estimated cost.
- **Reconciliation**: after the cascade, the reconciled assessment is
  persisted with the model assessment attached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from codex_watchtower import domain
from codex_watchtower.assess import policy
from codex_watchtower.assess.luna import run_luna
from codex_watchtower.assess.terra import run_terra
from codex_watchtower.codex import lifecycle
from codex_watchtower.config import BudgetConfig, ModelEndpointConfig
from codex_watchtower.observations import build_observation
from codex_watchtower.storage.repository import Repository


@dataclass(frozen=True, slots=True)
class AssessmentOutcome:
    assessment: domain.Assessment | None
    model_call_id: int | None
    budget_exhausted: bool
    failure_reason: str | None


def _build_session_observation(
    repo: Repository,
    session_id: str,
    *,
    budget_characters: int,
    now: datetime,
) -> domain.Observation | None:
    """Build a bounded observation packet from repository state."""
    session_row = repo.get_session(session_id)
    if session_row is None:
        return None

    state = _lifecycle_state_from_row(session_id, session_row)
    events = repo.get_recent_domain_events(session_id)
    signals = repo.get_active_signals(session_id)

    started_at_str = session_row["started_at"] or now.isoformat()
    try:
        started_at = datetime.fromisoformat(
            started_at_str[:-1] + "+00:00" if started_at_str.endswith("Z") else started_at_str
        )
    except ValueError:
        started_at = now
    elapsed = max(0, int((now - started_at).total_seconds()))

    session_info = domain.SessionInfo(
        id=session_id,
        workspace=session_row["workspace"] or "",
        started_at=started_at_str,
        elapsed_seconds=elapsed,
        state=state.state,
        model=session_row["model"],
    )

    goal_text = session_row["goal_text"] or "No goal recorded."
    expected_paths = (
        json.loads(session_row["expected_paths"]) if session_row["expected_paths"] else []
    )
    forbidden_paths = (
        json.loads(session_row["forbidden_paths"]) if session_row["forbidden_paths"] else []
    )
    goal = domain.Goal(
        text=goal_text,
        expected_paths=expected_paths,
        forbidden_paths=forbidden_paths,
        acceptance_criteria=[],
    )

    cursor = repo.max_event_sequence(session_id)
    window = domain.Window(
        from_cursor=None,
        to_cursor=cursor or 0,
        opened_at=started_at_str,
        closed_at=now.isoformat(),
    )

    reconciled = repo.get_latest_reconciled(session_id)
    previous_assessment = reconciled.model_assessment if reconciled else None

    result = build_observation(
        session=session_info,
        goal=goal,
        window=window,
        previous_assessment=previous_assessment,
        events=events,
        signals=signals,
        system_refs=[],
        budget_characters=budget_characters,
    )
    return result.observation


def _lifecycle_state_from_row(session_id: str, row: Any) -> lifecycle.LifecycleState:
    """Reconstruct LifecycleState from a sessions row (mirrors ingest.py)."""
    get = row.__getitem__
    fatal = None
    if get("fatal_reason") is not None:
        fatal = lifecycle.FatalInfo(
            reason=domain.FatalReason(get("fatal_reason")),
            detected_at=get("fatal_detected_at") or "",
            detail=get("fatal_detail"),
        )
    pending_exit = None
    if get("pending_exit_run_id") is not None:
        pending_exit = lifecycle.PendingExit(
            run_id=get("pending_exit_run_id"),
            execution_epoch=get("pending_exit_execution_epoch"),
            exited_at=get("pending_exit_exited_at"),
        )
    return lifecycle.LifecycleState(
        session_id=session_id,
        state=domain.SessionState(get("state")),
        run_id=get("current_run_id"),
        execution_epoch=get("current_execution_epoch"),
        status_epoch=get("status_epoch"),
        last_event_at=get("last_event_at"),
        report_version=get("report_version"),
        pending_exit=pending_exit,
        fatal=fatal,
    )


def _estimate_cost_cents(input_characters: int, assessed_by: str) -> int:
    """Conservative cost estimate: 1 cent per 10K input characters, min 1."""
    return max(1, input_characters // 10_000)


def _reserve_and_check_budget(
    repo: Repository,
    session_id: str,
    assessed_by: str,
    budget: BudgetConfig,
    *,
    started_at: str,
    input_characters: int,
    owner_id: str | None = None,
) -> tuple[int | None, str | None]:
    """Atomically reserve a model-call slot and check ceilings.

    Uses ``BEGIN IMMEDIATE`` so concurrent callers block on the
    transaction and cannot both pass the last slot (R4#2).

    Returns (row_id, failure_reason). If failure_reason is not None,
    the reservation was cancelled and the caller must not proceed.
    If row_id is not None, the caller must finalize or cancel it.
    """
    estimated = _estimate_cost_cents(input_characters, assessed_by)
    repo.begin_transaction()
    try:
        row_id = repo.reserve_model_call(
            session_id,
            assessed_by,
            started_at=started_at,
            input_characters=input_characters,
            estimated_cost_cents=estimated,
            owner_id=owner_id,
        )
        if budget.per_session_assessment_ceiling is not None:
            count = repo.count_model_calls_for_session(session_id)
            if count > budget.per_session_assessment_ceiling:
                repo.cancel_model_call(row_id)
                repo.commit_transaction()
                return None, "per_session_ceiling_exhausted"
        if budget.daily_cost_ceiling_cents is not None:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            spent = repo.sum_daily_cost_cents(today)
            if spent > budget.daily_cost_ceiling_cents:
                repo.cancel_model_call(row_id)
                repo.commit_transaction()
                return None, "daily_cost_ceiling_exhausted"
        repo.commit_transaction()
    except Exception:
        repo.rollback_transaction()
        raise
    return row_id, None


def _finalize_model_call(
    repo: Repository,
    row_id: int,
    *,
    started_at: str,
    success: bool,
    input_characters: int | None,
    assessed_by: str,
    error_class: str | None = None,
) -> None:
    finished_at = datetime.now(UTC).isoformat()
    started_dt = datetime.fromisoformat(
        started_at[:-1] + "+00:00" if started_at.endswith("Z") else started_at
    )
    latency_ms = max(0, int((datetime.now(UTC) - started_dt).total_seconds() * 1000))
    estimated_cost = _estimate_cost_cents(input_characters or 0, assessed_by)
    repo.finalize_model_call(
        row_id,
        success=success,
        latency_ms=latency_ms,
        finished_at=finished_at,
        error_class=error_class,
        estimated_cost_cents=estimated_cost,
    )


def run_luna_assessment(
    repo: Repository,
    session_id: str,
    luna_config: ModelEndpointConfig,
    budget: BudgetConfig,
    *,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
    owner_id: str | None = None,
) -> AssessmentOutcome:
    """Run a Luna assessment with atomic budget enforcement and persistence."""
    now = now or datetime.now(UTC)

    observation = _build_session_observation(
        repo, session_id, budget_characters=budget.packet_character_budget, now=now
    )
    if observation is None:
        return AssessmentOutcome(
            assessment=None,
            model_call_id=None,
            budget_exhausted=False,
            failure_reason="session_not_found",
        )

    profile = luna_config.to_model_profile()
    started_at = now.isoformat()
    input_chars = len(observation.model_dump_json())

    row_id, budget_reason = _reserve_and_check_budget(
        repo,
        session_id,
        "luna",
        budget,
        started_at=started_at,
        input_characters=input_chars,
        owner_id=owner_id,
    )
    if budget_reason is not None:
        return AssessmentOutcome(
            assessment=None,
            model_call_id=None,
            budget_exhausted=True,
            failure_reason=budget_reason,
        )

    assert row_id is not None
    try:
        result = run_luna(
            profile,
            observation,
            http_client=http_client,
            max_request_bytes=budget.packet_character_budget * 4,
        )
    except Exception:
        # Release the reservation so the slot is not permanently consumed (R4#4).
        repo.cancel_model_call(row_id)
        raise

    success = result.assessment is not None
    _finalize_model_call(
        repo,
        row_id,
        started_at=started_at,
        success=success,
        input_characters=input_chars,
        assessed_by="luna",
        error_class=result.fallback_reason,
    )

    return AssessmentOutcome(
        assessment=result.assessment,
        model_call_id=row_id,
        budget_exhausted=False,
        failure_reason=result.fallback_reason,
    )


def run_terra_assessment(
    repo: Repository,
    session_id: str,
    terra_config: ModelEndpointConfig,
    budget: BudgetConfig,
    luna_assessment: domain.Assessment | None,
    *,
    escalation_reason: str = "operator_requested",
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
    owner_id: str | None = None,
) -> AssessmentOutcome:
    """Run a Terra escalation with atomic budget enforcement and persistence."""
    now = now or datetime.now(UTC)

    observation = _build_session_observation(
        repo, session_id, budget_characters=budget.packet_character_budget, now=now
    )
    if observation is None:
        return AssessmentOutcome(
            assessment=None,
            model_call_id=None,
            budget_exhausted=False,
            failure_reason="session_not_found",
        )

    profile = terra_config.to_model_profile()
    started_at = now.isoformat()
    input_chars = len(observation.model_dump_json())

    row_id, budget_reason = _reserve_and_check_budget(
        repo,
        session_id,
        "terra",
        budget,
        started_at=started_at,
        input_characters=input_chars,
        owner_id=owner_id,
    )
    if budget_reason is not None:
        return AssessmentOutcome(
            assessment=None,
            model_call_id=None,
            budget_exhausted=True,
            failure_reason=budget_reason,
        )

    assert row_id is not None
    try:
        result = run_terra(
            profile,
            observation,
            luna_assessment,
            escalation_reason=escalation_reason,
            http_client=http_client,
            max_request_bytes=budget.packet_character_budget * 4,
        )
    except Exception:
        repo.cancel_model_call(row_id)
        raise

    success = result.assessment is not None
    _finalize_model_call(
        repo,
        row_id,
        started_at=started_at,
        success=success,
        input_characters=input_chars,
        assessed_by="terra",
        error_class=result.fallback_reason,
    )

    return AssessmentOutcome(
        assessment=result.assessment,
        model_call_id=row_id,
        budget_exhausted=False,
        failure_reason=result.fallback_reason,
    )


def reconcile_with_assessment(
    repo: Repository,
    session_id: str,
    model_assessment: domain.Assessment | None,
    *,
    now: datetime | None = None,
) -> domain.ReconciledAssessment | None:
    """Run rules + reconciliation with a model assessment attached."""
    now = now or datetime.now(UTC)
    session_row = repo.get_session(session_id)
    if session_row is None:
        return None

    state = _lifecycle_state_from_row(session_id, session_row)

    # Re-run rules to get fresh signals.
    from codex_watchtower.rules.progress import detect_stagnation
    from codex_watchtower.rules.repetition import detect_recurring_errors, detect_repeated_commands
    from codex_watchtower.rules.scope import detect_scope_violations
    from codex_watchtower.rules.tests import detect_test_regression

    recent_events = repo.get_recent_domain_events(session_id)
    local_signals: list[domain.Signal] = []
    local_signals.extend(detect_repeated_commands(recent_events, now=now))
    local_signals.extend(detect_recurring_errors(recent_events, now=now))
    stagnation = detect_stagnation(recent_events, now=now, session_reference_time=now)
    if stagnation is not None:
        local_signals.append(stagnation)
    workspace = session_row["workspace"] or ""
    if workspace:
        expected_paths = (
            json.loads(session_row["expected_paths"]) if session_row["expected_paths"] else []
        )
        forbidden_paths = (
            json.loads(session_row["forbidden_paths"]) if session_row["forbidden_paths"] else []
        )
        local_signals.extend(
            detect_scope_violations(
                recent_events,
                workspace=Path(workspace),
                expected_paths=expected_paths,
                forbidden_paths=forbidden_paths,
            )
        )
    test_regression = detect_test_regression(recent_events)
    if test_regression is not None:
        local_signals.append(test_regression)

    repo.deactivate_all_signals(session_id)
    for signal in local_signals:
        repo.upsert_signal(session_id, signal, active=True)
    active_signals = repo.get_active_signals(session_id)

    previous_row = repo.get_reconciled_row(session_id)
    if previous_row is not None:
        previous = policy.PreviousReconciliationState(
            notification_status=domain.NotificationStatus(previous_row["notification_status"]),
            status_epoch=previous_row["status_epoch"],
            lifecycle_status_epoch=previous_row["lifecycle_status_epoch"],
            attention_epoch=previous_row["attention_epoch"],
            needs_attention=bool(previous_row["needs_attention"]),
        )
    else:
        previous = policy.PreviousReconciliationState(
            notification_status=None,
            status_epoch=0,
            lifecycle_status_epoch=0,
            attention_epoch=0,
            needs_attention=False,
        )

    reconciled = policy.reconcile(
        session_id=session_id,
        lifecycle_state=state,
        active_signals=active_signals,
        model_assessment=model_assessment,
        report=None,
        event_cursor=repo.max_event_sequence(session_id),
        previous=previous,
        now=now,
    )
    repo.save_reconciled(reconciled, lifecycle_status_epoch=state.status_epoch)
    return reconciled
