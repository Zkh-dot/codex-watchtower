from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codex_watchtower import domain
from codex_watchtower.assess import policy
from codex_watchtower.codex import lifecycle

NOW = datetime(2026, 8, 13, 10, 30, 0, tzinfo=UTC)


def _signal(
    kind: domain.SignalKind,
    severity: domain.Severity = domain.Severity.warning,
    signal_id: str = "sig:1",
) -> domain.Signal:
    return domain.Signal(
        id=signal_id,
        kind=kind,
        severity=severity,
        source=domain.SignalSource.local_rule,
        event_ids=[],
        observed_at=NOW.isoformat(),
        freshness=domain.Freshness.current,
        summary="signal",
        payload={},
    )


def _luna(
    status: domain.AssessmentStatus = domain.AssessmentStatus.progressing,
    goal_alignment: domain.GoalAlignment = domain.GoalAlignment.aligned,
) -> domain.Assessment:
    return domain.Assessment(
        status=status,
        current_action="doing work",
        goal_alignment=goal_alignment,
        evidence=[domain.Evidence(ref_type=domain.RefType.system, ref_id="sys:x", claim="x")],
        basis_ids=["sys:x"],
        needs_attention=False,
        confidence_percent=99,
        assessed_by=domain.AssessedBy.luna,
        event_cursor=1,
        assessed_at=NOW.isoformat(),
    )


def _bound_lifecycle_state(session_id: str = "sess-1") -> lifecycle.LifecycleState:
    state = lifecycle.LifecycleState.initial(session_id)
    return lifecycle.bind_execution(state, run_id="run-1", now=NOW, first_binding=True)


def _initial_previous() -> policy.PreviousReconciliationState:
    return policy.PreviousReconciliationState(
        notification_status=None,
        status_epoch=0,
        lifecycle_status_epoch=0,
        attention_epoch=0,
        needs_attention=False,
    )


# --- escalation triggers: every one parametrized, goal_alignment vs status distinct --


GA = domain.GoalAlignment
AS = domain.AssessmentStatus

_CRITICAL_SIGNAL = _signal(domain.SignalKind.forbidden_path, domain.Severity.critical)

_ESCALATION_CASES: list[tuple[dict, str]] = [
    ({"active_signals": [_CRITICAL_SIGNAL], "luna": None}, "deterministic_critical_signal"),
    ({"active_signals": [], "luna": None, "operator_requested": True}, "operator_requested"),
    (
        {"active_signals": [], "luna": None, "deterministic_conflicts_with_luna": True},
        "deterministic_conflict_with_luna",
    ),
    (
        {"active_signals": [], "luna": None, "stagnation_minutes": 26},
        "stagnation_exceeds_25_minutes",
    ),
    (
        {"active_signals": [], "luna": _luna(goal_alignment=GA.possibly_aligned)},
        "luna_goal_alignment",
    ),
    ({"active_signals": [], "luna": _luna(goal_alignment=GA.misaligned)}, "luna_goal_alignment"),
    ({"active_signals": [], "luna": _luna(status=AS.stalled)}, "luna_status"),
    ({"active_signals": [], "luna": _luna(status=AS.looping)}, "luna_status"),
    ({"active_signals": [], "luna": _luna(status=AS.off_scope)}, "luna_status"),
]


@pytest.mark.parametrize(("kwargs", "expected_reason"), _ESCALATION_CASES)
def test_every_documented_escalation_trigger(kwargs: dict, expected_reason: str) -> None:
    decision = policy.should_escalate(**kwargs)
    assert decision.escalate is True
    assert decision.reason == expected_reason


def test_goal_alignment_and_status_triggers_are_distinct_fields() -> None:
    # possibly_aligned + progressing status: still escalates on goal_alignment alone
    luna_alignment_only = _luna(goal_alignment=GA.possibly_aligned, status=AS.progressing)
    d1 = policy.should_escalate(active_signals=[], luna=luna_alignment_only)
    assert d1.reason == "luna_goal_alignment"
    # aligned goal + stalled status: still escalates on status alone
    luna_status_only = _luna(goal_alignment=GA.aligned, status=AS.stalled)
    d2 = policy.should_escalate(active_signals=[], luna=luna_status_only)
    assert d2.reason == "luna_status"


def test_healthy_luna_result_does_not_escalate() -> None:
    decision = policy.should_escalate(active_signals=[], luna=_luna())
    assert decision.escalate is False
    assert decision.reason is None


def test_confidence_percent_alone_never_triggers_escalation() -> None:
    low_confidence_luna = _luna()
    low_confidence_luna = low_confidence_luna.model_copy(update={"confidence_percent": 1})
    decision = policy.should_escalate(active_signals=[], luna=low_confidence_luna)
    assert decision.escalate is False


# --- reconciliation: model_assessment null is valid and fully populated ----


def test_reconciled_result_with_null_model_assessment_is_valid() -> None:
    state = _bound_lifecycle_state()
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=3,
        previous=_initial_previous(),
        now=NOW,
    )
    assert result.model_assessment is None
    result.validate_against_schema()


# --- deterministic critical signals force attention despite reassuring model output --


def test_critical_signal_forces_needs_attention_despite_reassuring_model() -> None:
    state = _bound_lifecycle_state()
    reassuring = _luna()  # needs_attention=False, status=progressing
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[_signal(domain.SignalKind.forbidden_path, domain.Severity.critical)],
        model_assessment=reassuring,
        report=None,
        event_cursor=3,
        previous=_initial_previous(),
        now=NOW,
    )
    assert result.needs_attention is True
    result.validate_against_schema()


# --- both epochs advance only on their own deterministic trigger -----------


def test_status_epoch_unchanged_when_notification_status_unchanged() -> None:
    state = _bound_lifecycle_state()
    previous = policy.PreviousReconciliationState(
        notification_status=domain.NotificationStatus.progressing,
        status_epoch=5,
        lifecycle_status_epoch=state.status_epoch,
        attention_epoch=0,
        needs_attention=False,
    )
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=previous,
        now=NOW,
    )
    assert result.notification_status == domain.NotificationStatus.progressing
    assert result.status_epoch == 5


def test_status_epoch_advances_on_signal_narrowing_without_state_change() -> None:
    state = _bound_lifecycle_state()
    previous = policy.PreviousReconciliationState(
        notification_status=domain.NotificationStatus.progressing,
        status_epoch=5,
        lifecycle_status_epoch=state.status_epoch,  # unchanged: no state transition occurred
        attention_epoch=0,
        needs_attention=False,
    )
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[_signal(domain.SignalKind.stagnation)],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=previous,
        now=NOW,
    )
    assert result.notification_status == domain.NotificationStatus.stalled
    assert result.status_epoch == 6


def test_status_epoch_advances_by_lifecycle_delta_on_state_change() -> None:
    state = _bound_lifecycle_state()
    idled = lifecycle.tick(state, now=NOW + timedelta(hours=1))
    assert idled.state == domain.SessionState.idle
    previous = policy.PreviousReconciliationState(
        notification_status=domain.NotificationStatus.progressing,
        status_epoch=5,
        lifecycle_status_epoch=state.status_epoch,  # value before the idle transition
        attention_epoch=0,
        needs_attention=False,
    )
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=idled,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=previous,
        now=NOW + timedelta(hours=1),
    )
    assert result.notification_status == domain.NotificationStatus.idle
    assert result.status_epoch == 5 + (idled.status_epoch - state.status_epoch)


def test_attention_epoch_advances_only_on_false_to_true_transition() -> None:
    state = _bound_lifecycle_state()
    previous = policy.PreviousReconciliationState(
        notification_status=domain.NotificationStatus.progressing,
        status_epoch=0,
        lifecycle_status_epoch=state.status_epoch,
        attention_epoch=2,
        needs_attention=False,
    )
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[_signal(domain.SignalKind.forbidden_path, domain.Severity.critical)],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=previous,
        now=NOW,
    )
    assert result.attention_epoch == 3


def test_attention_epoch_unchanged_when_already_true() -> None:
    state = _bound_lifecycle_state()
    previous = policy.PreviousReconciliationState(
        notification_status=domain.NotificationStatus.progressing,
        status_epoch=0,
        lifecycle_status_epoch=state.status_epoch,
        attention_epoch=2,
        needs_attention=True,  # already true: no false->true transition this round
    )
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[_signal(domain.SignalKind.forbidden_path, domain.Severity.critical)],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=previous,
        now=NOW,
    )
    assert result.attention_epoch == 2


def test_neither_epoch_advanced_by_model_output_alone() -> None:
    state = _bound_lifecycle_state()
    previous = policy.PreviousReconciliationState(
        notification_status=domain.NotificationStatus.progressing,
        status_epoch=5,
        lifecycle_status_epoch=state.status_epoch,
        attention_epoch=1,
        needs_attention=False,
    )
    alarming_model_output = _luna(status=domain.AssessmentStatus.stalled)  # no matching signal!
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[],
        model_assessment=alarming_model_output,
        report=None,
        event_cursor=1,
        previous=previous,
        now=NOW,
    )
    # notification_status is deterministic-only: no matching signal, so it
    # stays progressing regardless of what the model said.
    assert result.notification_status == domain.NotificationStatus.progressing
    assert result.status_epoch == 5
    assert result.attention_epoch == 1
    assert result.needs_attention is False


# --- Terra prose supersedes Luna only when valid --------------------------


def test_model_narrowing_of_status_is_reflected_when_backed_by_signal() -> None:
    state = _bound_lifecycle_state()
    terra = _luna(status=domain.AssessmentStatus.stalled)
    terra = terra.model_copy(update={"assessed_by": domain.AssessedBy.terra})
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[_signal(domain.SignalKind.stagnation)],
        model_assessment=terra,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    assert result.status == domain.AssessmentStatus.stalled
    assert result.notification_status == domain.NotificationStatus.stalled


def test_model_status_outside_permitted_row_is_ignored() -> None:
    state = _bound_lifecycle_state()  # active_turn
    invalid_narrowing = _luna(status=domain.AssessmentStatus.terminal_completed)
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=state,
        active_signals=[],
        model_assessment=invalid_narrowing,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    assert result.status != domain.AssessmentStatus.terminal_completed
    assert result.status in domain.STATUS_PROJECTION[domain.SessionState.active_turn]


# --- fatal path: saturated status_epoch, epochs freeze, dedup key ----------


def test_fatal_path_from_saturated_status_epoch_freezes_epochs() -> None:
    from dataclasses import replace

    state = replace(_bound_lifecycle_state(), status_epoch=lifecycle.MAX_STATUS_EPOCH)
    idled = lifecycle.tick(state, now=NOW + timedelta(hours=1))
    assert idled.state == domain.SessionState.identity_broken
    assert idled.fatal is not None

    previous = policy.PreviousReconciliationState(
        notification_status=domain.NotificationStatus.progressing,
        status_epoch=42,
        lifecycle_status_epoch=state.status_epoch,
        attention_epoch=3,
        needs_attention=False,
    )
    result = policy.reconcile(
        session_id="sess-1",
        lifecycle_state=idled,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=previous,
        now=NOW + timedelta(hours=1),
    )
    assert result.fatal is not None
    assert result.fatal.reason == domain.FatalReason.status_epoch_exhausted
    assert result.status_epoch == lifecycle.MAX_STATUS_EPOCH
    assert result.attention_epoch == 3  # frozen, unrelated counter untouched
    assert result.needs_attention is True
    result.validate_against_schema()


def test_fatal_dedup_key_has_no_epoch_and_uses_session_and_reason() -> None:
    key = policy.fatal_deduplication_key("sess-1", domain.FatalReason.status_epoch_exhausted)
    assert key == "sess-1:fatal:status_epoch_exhausted"
    assert "epoch" not in key.split(":")[1:2]  # no numeric epoch segment embedded


# --- Terra escalation gate: healthy Luna never calls Terra (integration-ish) --


def test_healthy_result_never_reaches_escalation() -> None:
    decision = policy.should_escalate(active_signals=[], luna=_luna())
    assert decision.escalate is False
