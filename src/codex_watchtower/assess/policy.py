"""Policy reconciler: the authoritative ReconciledAssessment (spec 5.8).

Ownership recap, since three different components each own a slice of the
reconciled result:

- ``codex.lifecycle`` owns deterministic ``state``, the bound execution
  (``run_id``/``execution_epoch``), report versioning, and detects fatal
  conditions for the three counters it owns directly (status_epoch,
  execution_epoch, report_version exhaustion) plus prefix_mismatch.
- This module owns ``notification_status`` (deterministic: session state
  plus which rule-signal kinds are active -- never model output),
  ``needs_attention``/``attention_epoch`` (deterministic: driven by
  critical-signal presence), attention_epoch_exhaustion (the fourth fatal
  reason lifecycle.py does not detect, since attention_epoch is not its
  field), the final exposed ``status_epoch`` (see below), and the display
  ``status`` (deterministic floor, optionally narrowed by a *validated*
  model assessment within the row its state permits).
- The model assessors (Luna/Terra) own ``model_assessment`` -- advisory in
  v0.1.0, embedded unchanged, never consulted for delivery.

**status_epoch** is exposed as ``lifecycle_state.status_epoch`` plus
however many additional rule-signal-narrowing transitions this module has
observed on top of it. Every session *state* change is necessarily also a
notification_status change (each state's base notification value is
distinct), so a delta in ``lifecycle_state.status_epoch`` since the last
reconciliation already accounts for exactly those transitions -- adding a
second increment on top would double-count. The one transition
lifecycle.py cannot see at all is a rule-signal narrowing within
``active_turn`` (progressing <-> stalled/looping/off_scope) with no state
change, which is why this module needs its own delta-aware computation
rather than simply repeating lifecycle_state.status_epoch verbatim. The
fatal path is the one documented exception: the exhausted counter must
read exactly its committed maximum, matching the schema's own conditional,
regardless of this delta arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from codex_watchtower import domain
from codex_watchtower.codex import lifecycle

MAX_ATTENTION_EPOCH = 1_000_000

_NOTIFICATION_BASE: dict[domain.SessionState, domain.NotificationStatus] = {
    domain.SessionState.active_turn: domain.NotificationStatus.progressing,
    domain.SessionState.between_turns: domain.NotificationStatus.between_turns,
    domain.SessionState.waiting: domain.NotificationStatus.waiting,
    domain.SessionState.terminal_completed: domain.NotificationStatus.terminal_completed,
    domain.SessionState.terminal_failed: domain.NotificationStatus.terminal_failed,
    domain.SessionState.idle: domain.NotificationStatus.idle,
    domain.SessionState.identity_broken: domain.NotificationStatus.identity_broken,
    domain.SessionState.unknown: domain.NotificationStatus.unknown,
}

_STATUS_BASE: dict[domain.SessionState, domain.AssessmentStatus] = {
    domain.SessionState.active_turn: domain.AssessmentStatus.progressing,
    domain.SessionState.between_turns: domain.AssessmentStatus.between_turns,
    domain.SessionState.waiting: domain.AssessmentStatus.waiting,
    domain.SessionState.terminal_completed: domain.AssessmentStatus.terminal_completed,
    domain.SessionState.terminal_failed: domain.AssessmentStatus.terminal_failed,
    domain.SessionState.idle: domain.AssessmentStatus.idle,
    domain.SessionState.identity_broken: domain.AssessmentStatus.unknown,
    domain.SessionState.unknown: domain.AssessmentStatus.unknown,
}

_STAGNATION_KINDS = {domain.SignalKind.stagnation}
_LOOPING_KINDS = {domain.SignalKind.repeated_command, domain.SignalKind.recurring_error}
_OFF_SCOPE_KINDS = {domain.SignalKind.scope_expansion, domain.SignalKind.forbidden_path}


# --- Terra escalation triggers (spec 5.7) --------------------------------


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    escalate: bool
    reason: str | None


def should_escalate(
    *,
    active_signals: list[domain.Signal],
    luna: domain.Assessment | None,
    deterministic_conflicts_with_luna: bool = False,
    stagnation_minutes: int | None = None,
    operator_requested: bool = False,
) -> EscalationDecision:
    """confidence_percent is deliberately never consulted: see spec 5.7."""
    if any(s.severity == domain.Severity.critical for s in active_signals):
        return EscalationDecision(True, "deterministic_critical_signal")
    if operator_requested:
        return EscalationDecision(True, "operator_requested")
    if deterministic_conflicts_with_luna:
        return EscalationDecision(True, "deterministic_conflict_with_luna")
    if stagnation_minutes is not None and stagnation_minutes > 25:
        return EscalationDecision(True, "stagnation_exceeds_25_minutes")
    if luna is not None:
        if luna.goal_alignment in (
            domain.GoalAlignment.possibly_aligned,
            domain.GoalAlignment.misaligned,
        ):
            return EscalationDecision(True, "luna_goal_alignment")
        if luna.status in (
            domain.AssessmentStatus.stalled,
            domain.AssessmentStatus.looping,
            domain.AssessmentStatus.off_scope,
        ):
            return EscalationDecision(True, "luna_status")
    return EscalationDecision(False, None)


# --- deterministic status/notification computation ------------------------


def _narrowing_kinds(active_signals: list[domain.Signal]) -> set[domain.SignalKind]:
    return {s.kind for s in active_signals}


def compute_notification_status(
    state: domain.SessionState, active_signals: list[domain.Signal]
) -> domain.NotificationStatus:
    if state != domain.SessionState.active_turn:
        return _NOTIFICATION_BASE[state]
    kinds = _narrowing_kinds(active_signals)
    if kinds & _STAGNATION_KINDS:
        return domain.NotificationStatus.stalled
    if kinds & _LOOPING_KINDS:
        return domain.NotificationStatus.looping
    if kinds & _OFF_SCOPE_KINDS:
        return domain.NotificationStatus.off_scope
    return domain.NotificationStatus.progressing


def compute_display_status(
    state: domain.SessionState,
    active_signals: list[domain.Signal],
    model_assessment: domain.Assessment | None,
) -> domain.AssessmentStatus:
    if state != domain.SessionState.active_turn:
        return _STATUS_BASE[state]
    allowed = domain.STATUS_PROJECTION[state]
    if model_assessment is not None and model_assessment.status in allowed:
        return model_assessment.status
    kinds = _narrowing_kinds(active_signals)
    if kinds & _STAGNATION_KINDS:
        return domain.AssessmentStatus.stalled
    if kinds & _LOOPING_KINDS:
        return domain.AssessmentStatus.looping
    if kinds & _OFF_SCOPE_KINDS:
        return domain.AssessmentStatus.off_scope
    return domain.AssessmentStatus.progressing


def compute_needs_attention(active_signals: list[domain.Signal]) -> bool:
    return any(s.severity == domain.Severity.critical for s in active_signals)


# --- rule-only fallback assessment -----------------------------------------


def build_rule_only_assessment(
    *,
    active_signals: list[domain.Signal],
    status: domain.AssessmentStatus,
    goal_alignment: domain.GoalAlignment,
    event_cursor: int | None,
    now: datetime,
    system_ref_id: str = "sys:session_state",
) -> domain.Assessment:
    """Synthesize an assessment when both Luna and Terra failed to produce a valid one."""
    concerns = [
        domain.Concern(
            severity=s.severity,
            kind=domain.ConcernKind.other,
            explanation=s.summary,
            evidence_refs=[s.id],
        )
        for s in active_signals[:8]
    ]
    return domain.Assessment(
        status=status,
        current_action="No model assessment available; reporting from deterministic evidence only.",
        goal_alignment=goal_alignment,
        evidence=[
            domain.Evidence(
                ref_type=domain.RefType.system,
                ref_id=system_ref_id,
                claim="Deterministic session state used because no model assessment was available.",
            )
        ],
        basis_ids=[system_ref_id],
        concerns=concerns,
        needs_attention=compute_needs_attention(active_signals),
        recommended_human_action=None,
        confidence_percent=0,
        assessed_by=domain.AssessedBy.rules,
        escalation_reason=None,
        event_cursor=event_cursor,
        assessed_at=now.isoformat(),
    )


# --- reconciliation ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviousReconciliationState:
    notification_status: domain.NotificationStatus | None
    status_epoch: int  # previous *exposed* status_epoch
    # previous lifecycle_state.status_epoch, to detect state-driven bumps
    lifecycle_status_epoch: int
    attention_epoch: int
    needs_attention: bool


def reconcile(
    *,
    session_id: str,
    lifecycle_state: lifecycle.LifecycleState,
    active_signals: list[domain.Signal],
    model_assessment: domain.Assessment | None,
    report: lifecycle.ReportInfo | None,
    event_cursor: int | None,
    previous: PreviousReconciliationState,
    now: datetime,
) -> domain.ReconciledAssessment:
    state = lifecycle_state.state
    needs_attention = compute_needs_attention(active_signals)

    # attention_epoch: bump on a deterministic false->true transition. This
    # is a superset of what lifecycle.py can see (it has no visibility into
    # signals at all), so it is computed here in full, not layered.
    attention_epoch = previous.attention_epoch
    attention_exhausted = False
    if needs_attention and not previous.needs_attention:
        if attention_epoch >= MAX_ATTENTION_EPOCH:
            attention_exhausted = True
        else:
            attention_epoch += 1

    notification_status = compute_notification_status(state, active_signals)

    # status_epoch: every state change is also a notification_status change
    # (each state's base notification value is distinct), so a delta in
    # lifecycle_state.status_epoch already accounts for exactly those
    # transitions. The only case lifecycle.py cannot see is a rule-signal
    # narrowing (e.g. progressing <-> stalled) with no state change, which
    # gets exactly one additional increment here.
    lifecycle_delta = lifecycle_state.status_epoch - previous.lifecycle_status_epoch
    if lifecycle_delta > 0:
        status_epoch = previous.status_epoch + lifecycle_delta
    elif notification_status != previous.notification_status:
        status_epoch = previous.status_epoch + 1
    else:
        status_epoch = previous.status_epoch

    fatal = None
    if lifecycle_state.fatal is not None:
        fatal = domain.Fatal(
            reason=lifecycle_state.fatal.reason,
            detected_at=lifecycle_state.fatal.detected_at,
            detail=lifecycle_state.fatal.detail,
        )
    elif attention_exhausted:
        fatal = domain.Fatal(
            reason=domain.FatalReason.attention_epoch_exhausted,
            detected_at=now.isoformat(),
            detail=f"attention_epoch saturated at {MAX_ATTENTION_EPOCH}",
        )

    if fatal is not None:
        state = domain.SessionState.identity_broken
        notification_status = domain.NotificationStatus.identity_broken
        needs_attention = True
        if not any(s.severity == domain.Severity.critical for s in active_signals):
            active_signals = [
                *active_signals,
                domain.Signal(
                    id=f"sig:fatal:{fatal.reason.value}",
                    kind=domain.SignalKind.dependency_unavailable,
                    severity=domain.Severity.critical,
                    source=domain.SignalSource.system,
                    event_ids=[],
                    observed_at=fatal.detected_at,
                    freshness=domain.Freshness.current,
                    summary=fatal.detail or f"Fatal condition: {fatal.reason.value}",
                    payload={},
                ),
            ]
        # Both epochs freeze on fatal (spec 5.8's single documented
        # exception to "increment on change") -- except the exhausted
        # counter itself, which the schema requires to read exactly its
        # committed maximum, not whatever it happened to be at last time.
        status_epoch = (
            lifecycle.MAX_STATUS_EPOCH
            if fatal.reason == domain.FatalReason.status_epoch_exhausted
            else previous.status_epoch
        )
        attention_epoch = (
            MAX_ATTENTION_EPOCH
            if fatal.reason == domain.FatalReason.attention_epoch_exhausted
            else previous.attention_epoch
        )

    display_status = compute_display_status(state, active_signals, model_assessment)

    active_signal_refs = [
        domain.ActiveSignal(id=s.id, kind=s.kind, severity=s.severity) for s in active_signals
    ]
    fingerprint = domain.canonical_signal_fingerprint(active_signal_refs)

    report_field = None
    if report is not None:
        report_field = domain.Report(
            report_version=report.report_version,
            provisional=report.provisional,
            supersedes=report.supersedes,
        )

    return domain.ReconciledAssessment(
        session_id=session_id,
        run_id=lifecycle_state.run_id,
        execution_epoch=lifecycle_state.execution_epoch,
        state=state,
        status=display_status,
        notification_status=notification_status,
        status_epoch=status_epoch,
        attention_epoch=attention_epoch,
        fatal=fatal,
        needs_attention=needs_attention,
        active_signals=active_signal_refs,
        signal_fingerprint=fingerprint,
        model_assessment=model_assessment,
        report=report_field,
        event_cursor=event_cursor,
        reconciled_at=now.isoformat(),
    )


def deduplication_key(reconciled: domain.ReconciledAssessment) -> str:
    return (
        f"{reconciled.session_id}:{reconciled.status_epoch}:{reconciled.attention_epoch}:"
        f"{reconciled.notification_status.value}:{reconciled.signal_fingerprint}"
    )


def fatal_deduplication_key(session_id: str, reason: domain.FatalReason) -> str:
    return f"{session_id}:fatal:{reason.value}"
