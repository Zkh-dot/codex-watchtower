from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_watchtower import domain
from codex_watchtower.assess import policy as reconcile_policy
from codex_watchtower.codex import lifecycle
from codex_watchtower.notify.policy import (
    LastDelivery,
    deduplication_key,
    should_send,
)

NOW = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)


def _signal(
    kind: domain.SignalKind, severity: domain.Severity, signal_id: str = "sig:1"
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


def _initial_previous() -> reconcile_policy.PreviousReconciliationState:
    return reconcile_policy.PreviousReconciliationState(
        notification_status=None,
        status_epoch=0,
        lifecycle_status_epoch=0,
        attention_epoch=0,
        needs_attention=False,
    )


def _previous_from(
    reconciled: domain.ReconciledAssessment, lc_status_epoch: int
) -> reconcile_policy.PreviousReconciliationState:
    return reconcile_policy.PreviousReconciliationState(
        notification_status=reconciled.notification_status,
        status_epoch=reconciled.status_epoch,
        lifecycle_status_epoch=lc_status_epoch,
        attention_epoch=reconciled.attention_epoch,
        needs_attention=reconciled.needs_attention,
    )


# --- dedup key composition -------------------------------------------------


def test_dedup_key_excludes_event_cursor() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    r1 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    r2 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=999,
        previous=_previous_from(r1, lc.status_epoch),
        now=NOW,
    )
    assert deduplication_key(r1) == deduplication_key(r2)


def test_dedup_key_uses_signal_fingerprint_from_rule_signals_not_concerns() -> None:
    """signal_fingerprint is computed from active_signals (rule engine), never from
    assessment concerns, which carry no ref_type and cannot yield stable identity."""
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    reassuring_model_with_concerns = domain.Assessment(
        status=domain.AssessmentStatus.progressing,
        current_action="doing work",
        goal_alignment=domain.GoalAlignment.aligned,
        evidence=[domain.Evidence(ref_type=domain.RefType.system, ref_id="sys:x", claim="x")],
        basis_ids=["sys:x"],
        concerns=[
            domain.Concern(
                severity=domain.Severity.warning,
                kind=domain.ConcernKind.other,
                explanation="a concern with no ref_type",
                evidence_refs=["sys:x"],
            )
        ],
        needs_attention=False,
        confidence_percent=90,
        assessed_by=domain.AssessedBy.luna,
        event_cursor=1,
        assessed_at=NOW.isoformat(),
    )
    r_no_signal = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    r_with_model_concern = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[],
        model_assessment=reassuring_model_with_concerns,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    # Concerns differ, but no rule signal exists in either case, so the
    # fingerprint (and therefore the dedup key) must be identical.
    assert deduplication_key(r_no_signal) == deduplication_key(r_with_model_concern)


# --- advisory guarantee: model narrowing with no matching signal sends nothing --


def test_luna_looping_with_no_matching_signal_changes_status_but_sends_nothing() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    luna_says_looping = domain.Assessment(
        status=domain.AssessmentStatus.looping,
        current_action="stuck in a loop, apparently",
        goal_alignment=domain.GoalAlignment.possibly_aligned,
        evidence=[domain.Evidence(ref_type=domain.RefType.system, ref_id="sys:x", claim="x")],
        basis_ids=["sys:x"],
        needs_attention=True,
        confidence_percent=80,
        assessed_by=domain.AssessedBy.luna,
        event_cursor=1,
        assessed_at=NOW.isoformat(),
    )
    reconciled = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[],  # no repeated_command/recurring_error signal
        model_assessment=luna_says_looping,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    # Displayed status reflects nothing here since STATUS_PROJECTION only
    # allows the model to narrow status, but notification_status stays
    # deterministic and is what the notifier reads.
    assert reconciled.notification_status == domain.NotificationStatus.progressing
    assert (
        reconciled.needs_attention is False
    )  # deterministic-only; model's needs_attention ignored

    decision = should_send(reconciled, last_delivery=None, now=NOW)
    assert decision.should_send is False


# --- status_epoch: waiting -> active_turn -> waiting sends twice ----------


def test_waiting_active_turn_waiting_cycle_sends_twice() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)

    lc_waiting_1 = lifecycle.enter_waiting(lc, now=NOW)
    r1 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting_1,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    d1 = should_send(r1, last_delivery=None, now=NOW)
    assert d1.should_send is True

    prev_after_1 = _previous_from(r1, lc_waiting_1.status_epoch)
    t2 = NOW + timedelta(minutes=1)
    lc_active_again = lifecycle.on_new_events(
        lc_waiting_1,
        [
            domain.Event(
                id="evt:resume",
                timestamp=t2.isoformat(),
                kind=domain.EventKind.message,
                summary="back to work",
            )
        ],
        t2,
    )
    r2 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_active_again,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=2,
        previous=prev_after_1,
        now=t2,
    )
    d2 = should_send(r2, last_delivery=None, now=t2)
    assert d2.should_send is False  # progressing is not send-worthy

    prev_after_2 = _previous_from(r2, lc_active_again.status_epoch)
    t3 = NOW + timedelta(minutes=2)
    lc_waiting_2 = lifecycle.enter_waiting(lc_active_again, now=t3)
    r3 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting_2,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=3,
        previous=prev_after_2,
        now=t3,
    )
    assert deduplication_key(r3) != deduplication_key(r1)  # a distinct episode
    d3 = should_send(r3, last_delivery=None, now=t3)
    assert d3.should_send is True  # second "waiting" episode sends again


def test_repeats_inside_one_episode_send_once() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    lc_waiting = lifecycle.enter_waiting(lc, now=NOW)
    r1 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    d1 = should_send(r1, last_delivery=None, now=NOW)
    assert d1.should_send is True

    # A repeat reconciliation of the *same* episode (nothing changed).
    prev_after_1 = _previous_from(r1, lc_waiting.status_epoch)
    r2 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=prev_after_1,
        now=NOW + timedelta(seconds=5),
    )
    assert deduplication_key(r2) == deduplication_key(r1)
    d2 = should_send(
        r2, last_delivery=LastDelivery(last_sent_at=NOW.isoformat()), now=NOW + timedelta(seconds=5)
    )
    assert d2.should_send is False  # same episode, no cooldown elapsed, no digest due


# --- attention_epoch: activate, clear, reactivate sends twice --------------


def test_attention_activate_clear_reactivate_sends_twice_while_status_stays_progressing() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)

    critical = _signal(domain.SignalKind.test_regression, domain.Severity.critical, "sig:crit")
    r1 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[critical],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    assert r1.notification_status == domain.NotificationStatus.progressing
    assert r1.needs_attention is True
    d1 = should_send(r1, last_delivery=None, now=NOW)
    assert d1.should_send is True  # attention_transition, despite notification_status=progressing

    prev_after_1 = _previous_from(r1, lc.status_epoch)
    t2 = NOW + timedelta(minutes=1)
    r2 = reconcile_policy.reconcile(  # signal cleared
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=2,
        previous=prev_after_1,
        now=t2,
    )
    assert r2.needs_attention is False
    d2 = should_send(r2, last_delivery=None, now=t2)
    assert d2.should_send is False  # clearing is not itself a send trigger

    prev_after_2 = _previous_from(r2, lc.status_epoch)
    t3 = NOW + timedelta(minutes=2)
    r3 = reconcile_policy.reconcile(  # signal reactivates
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[critical],
        model_assessment=None,
        report=None,
        event_cursor=3,
        previous=prev_after_2,
        now=t3,
    )
    assert r3.attention_epoch == r1.attention_epoch + 1
    assert deduplication_key(r3) != deduplication_key(r1)
    d3 = should_send(r3, last_delivery=None, now=t3)
    assert d3.should_send is True  # second false->true transition sends again


# --- entering identity_broken always sends ---------------------------------


def test_entering_identity_broken_always_sends_even_if_attention_already_true() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    unrelated_critical = _signal(domain.SignalKind.test_regression, domain.Severity.critical)
    r1 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[unrelated_critical],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    assert r1.needs_attention is True

    prev_after_1 = _previous_from(r1, lc.status_epoch)
    broken = lifecycle.mark_identity_broken(lc, detail="rollout-x.jsonl: prefix mismatch", now=NOW)
    r2 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=broken,
        active_signals=[unrelated_critical],
        model_assessment=None,
        report=None,
        event_cursor=2,
        previous=prev_after_1,
        now=NOW,
    )
    assert r2.state == domain.SessionState.identity_broken
    # needs_attention was already true (no false->true transition this round)
    assert deduplication_key(r2) != deduplication_key(r1)  # notification_status differs
    decision = should_send(r2, last_delivery=None, now=NOW)
    assert decision.should_send is True
    assert decision.reason == "notification_status_entered"


# --- wording-only / identical-episode repeat sends nothing -----------------


def test_changed_prose_with_identical_evidence_is_suppressed() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    lc_waiting = lifecycle.enter_waiting(lc, now=NOW)

    def _assessment(action: str) -> domain.Assessment:
        return domain.Assessment(
            status=domain.AssessmentStatus.waiting,
            current_action=action,
            goal_alignment=domain.GoalAlignment.aligned,
            evidence=[domain.Evidence(ref_type=domain.RefType.system, ref_id="sys:x", claim="x")],
            basis_ids=["sys:x"],
            needs_attention=False,
            confidence_percent=80,
            assessed_by=domain.AssessedBy.luna,
            event_cursor=1,
            assessed_at=NOW.isoformat(),
        )

    r1 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting,
        active_signals=[],
        model_assessment=_assessment("waiting for approval"),
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    prev_after_1 = _previous_from(r1, lc_waiting.status_epoch)
    r2 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting,
        active_signals=[],
        model_assessment=_assessment(
            "still waiting for the operator to approve"
        ),  # wording changed
        report=None,
        event_cursor=1,
        previous=prev_after_1,
        now=NOW,
    )
    assert deduplication_key(r1) == deduplication_key(r2)  # prose is not part of the key
    decision = should_send(
        r2, last_delivery=LastDelivery(last_sent_at=NOW.isoformat()), now=NOW + timedelta(seconds=1)
    )
    assert decision.should_send is False


# --- restart replaying the same window sends nothing ------------------------


def test_restart_replaying_same_window_sends_nothing() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    lc_waiting = lifecycle.enter_waiting(lc, now=NOW)
    r1 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=5,
        previous=_initial_previous(),
        now=NOW,
    )
    d1 = should_send(r1, last_delivery=None, now=NOW)
    assert d1.should_send is True

    # "Restart": same lifecycle state, but the cursor was recomputed from
    # scratch during replay and differs.
    prev_after_1 = _previous_from(r1, lc_waiting.status_epoch)
    r2 = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc_waiting,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=12,
        previous=prev_after_1,
        now=NOW + timedelta(minutes=1),
    )
    assert deduplication_key(r1) == deduplication_key(r2)  # cursor excluded from the key
    decision = should_send(
        r2, last_delivery=LastDelivery(last_sent_at=NOW.isoformat()), now=NOW + timedelta(minutes=1)
    )
    assert decision.should_send is False


# --- critical cooldown resend + digest --------------------------------------


def test_critical_cooldown_resend_after_elapsed_cooldown() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    critical = _signal(domain.SignalKind.forbidden_path, domain.Severity.critical)
    reconciled = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[critical],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    sent_at = NOW
    still_within_cooldown = should_send(
        reconciled,
        last_delivery=LastDelivery(last_sent_at=sent_at.isoformat()),
        now=NOW + timedelta(minutes=10),
        critical_cooldown=timedelta(minutes=30),
    )
    assert still_within_cooldown.should_send is False

    after_cooldown = should_send(
        reconciled,
        last_delivery=LastDelivery(last_sent_at=sent_at.isoformat()),
        now=NOW + timedelta(minutes=31),
        critical_cooldown=timedelta(minutes=30),
    )
    assert after_cooldown.should_send is True
    assert after_cooldown.reason == "critical_cooldown_resend"


def test_digest_due_sends_even_without_other_triggers() -> None:
    lc = lifecycle.LifecycleState.initial("sess-1")
    lc = lifecycle.bind_execution(lc, run_id="run-1", now=NOW, first_binding=True)
    reconciled = reconcile_policy.reconcile(
        session_id="sess-1",
        lifecycle_state=lc,
        active_signals=[],
        model_assessment=None,
        report=None,
        event_cursor=1,
        previous=_initial_previous(),
        now=NOW,
    )
    decision = should_send(
        reconciled,
        last_delivery=LastDelivery(last_sent_at=NOW.isoformat()),
        now=NOW + timedelta(hours=1),
        digest_due=True,
    )
    assert decision.should_send is True
    assert decision.reason == "digest"
