from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta

from codex_watchtower import domain
from codex_watchtower.codex import lifecycle

T0 = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)


def _turn_event(kind_source: str, ts: str) -> domain.Event:
    return domain.Event(
        id=f"evt:{ts}",
        timestamp=ts,
        kind=domain.EventKind.turn_lifecycle,
        summary="turn boundary",
        source_type=kind_source,
    )


def _process_event(run_id: str, execution_epoch: int, exit_code: int, ts: str) -> domain.Event:
    return domain.Event(
        id=f"evt:exit:{run_id}",
        timestamp=ts,
        kind=domain.EventKind.process_lifecycle,
        summary=f"process exited {exit_code}",
        run_id=run_id,
        execution_epoch=execution_epoch,
        exit_code=exit_code,
    )


def _bound(session_id: str = "sess-1", run_id: str = "run-1") -> lifecycle.LifecycleState:
    state = lifecycle.LifecycleState.initial(session_id)
    return lifecycle.bind_execution(state, run_id=run_id, now=T0, first_binding=True)


# --- multi-turn stays between_turns, never terminal ---------------------


def test_multiple_turn_cycles_stay_between_turns_not_terminal() -> None:
    state = _bound()
    events = [
        _turn_event("turn_started", "2026-08-13T10:01:00Z"),
        _turn_event("turn_complete", "2026-08-13T10:02:00Z"),
        _turn_event("turn_started", "2026-08-13T10:03:00Z"),
        _turn_event("turn_complete", "2026-08-13T10:04:00Z"),
        _turn_event("turn_started", "2026-08-13T10:05:00Z"),
        _turn_event("turn_complete", "2026-08-13T10:06:00Z"),
    ]
    result = lifecycle.on_new_events(state, events, T0)
    assert result.state == domain.SessionState.between_turns
    assert result.state not in (
        domain.SessionState.terminal_completed,
        domain.SessionState.terminal_failed,
    )


# --- terminal completion: zero exit + quiet grace period -----------------


def test_zero_exit_alone_does_not_immediately_terminate() -> None:
    state = _bound()
    result = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=0, now=T0
    )
    assert result.state != domain.SessionState.terminal_completed
    assert result.pending_exit is not None


def test_zero_exit_plus_quiet_grace_period_reaches_terminal_completed() -> None:
    state = _bound()
    after_exit = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=0, now=T0
    )
    later = lifecycle.tick(
        after_exit, now=T0 + timedelta(minutes=11), quiet_grace_period=timedelta(minutes=10)
    )
    assert later.state == domain.SessionState.terminal_completed
    assert later.pending_exit is None


def test_zero_exit_before_grace_period_elapses_stays_pending() -> None:
    state = _bound()
    after_exit = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=0, now=T0
    )
    still_pending = lifecycle.tick(
        after_exit, now=T0 + timedelta(minutes=5), quiet_grace_period=timedelta(minutes=10)
    )
    assert still_pending.state != domain.SessionState.terminal_completed
    assert still_pending.pending_exit is not None


def test_explicit_operator_marker_finalizes_completion_immediately() -> None:
    state = _bound()
    after_exit = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=0, now=T0
    )
    confirmed = lifecycle.confirm_pending_completion(after_exit, now=T0 + timedelta(seconds=1))
    assert confirmed.state == domain.SessionState.terminal_completed


def test_no_state_reaches_terminal_without_process_evidence() -> None:
    state = _bound()
    events = [_turn_event("turn_complete", "2026-08-13T10:01:00Z")]
    result = lifecycle.on_new_events(state, events, T0)
    later = lifecycle.tick(result, now=T0 + timedelta(hours=5))
    assert later.state not in (
        domain.SessionState.terminal_completed,
        domain.SessionState.terminal_failed,
    )
    assert later.state == domain.SessionState.idle  # reversible, not terminal


# --- terminal failure: non-zero exit is immediate ------------------------


def test_nonzero_exit_reaches_terminal_failed_immediately() -> None:
    state = _bound()
    result = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=1, now=T0
    )
    assert result.state == domain.SessionState.terminal_failed


def test_terminal_transition_only_from_current_execution() -> None:
    state = _bound(run_id="run-1")
    # A late exit event for a stale/superseded execution 1, while the
    # session has moved on to execution 0 (current run_id="run-1",
    # execution_epoch=0) -- simulate by targeting a different run id.
    result = lifecycle.on_process_exit(
        state, run_id="stale-run", execution_epoch=0, exit_code=1, now=T0
    )
    assert result.state != domain.SessionState.terminal_failed
    assert result.state == domain.SessionState.active_turn  # unaffected


def test_late_exit_from_superseded_execution_does_not_terminate_resumed_one() -> None:
    state = _bound(run_id="run-1")  # execution_epoch 0
    resumed = lifecycle.bind_execution(state, run_id="run-2", now=T0, first_binding=False)
    assert resumed.execution_epoch == 1

    # A late exit event that belongs to execution 0 (run-1) arrives after
    # execution 1 (run-2) is already bound.
    late = lifecycle.on_process_exit(
        resumed, run_id="run-1", execution_epoch=0, exit_code=1, now=T0
    )
    assert late.state == domain.SessionState.active_turn
    assert late.run_id == "run-2"
    assert late.execution_epoch == 1


# --- unobserved / live session without process evidence ------------------


def test_unbound_session_with_events_stays_active_between_turns_or_unknown() -> None:
    state = lifecycle.LifecycleState.initial("sess-unobserved")
    events = [_turn_event("turn_started", "2026-08-13T10:00:30Z")]
    result = lifecycle.on_new_events(state, events, T0)
    assert result.state in (
        domain.SessionState.active_turn,
        domain.SessionState.between_turns,
        domain.SessionState.unknown,
    )


def test_quiet_period_expiry_without_process_evidence_yields_idle_not_terminal() -> None:
    state = lifecycle.LifecycleState.initial("sess-unobserved")
    events = [_turn_event("turn_started", "2026-08-13T10:00:30Z")]
    active = lifecycle.on_new_events(state, events, T0)
    later = lifecycle.tick(active, now=T0 + timedelta(minutes=30))
    assert later.state == domain.SessionState.idle


# --- reopen from idle: new event returns to active_turn, epoch untouched -


def test_reopen_from_idle_returns_to_active_turn_keeps_execution_epoch() -> None:
    state = _bound()
    idled = lifecycle.tick(state, now=T0 + timedelta(minutes=30))
    assert idled.state == domain.SessionState.idle
    status_epoch_before = idled.status_epoch

    reopened = lifecycle.on_new_events(
        idled, [_turn_event("turn_started", "2026-08-13T10:35:00Z")], T0 + timedelta(minutes=35)
    )
    assert reopened.state == domain.SessionState.active_turn
    assert reopened.run_id == "run-1"
    assert reopened.execution_epoch == 0  # unchanged: not a new execution
    assert reopened.status_epoch == status_epoch_before + 1


def test_unobserved_session_reopen_keeps_execution_and_run_id_null() -> None:
    state = lifecycle.LifecycleState.initial("sess-unobserved")
    active = lifecycle.on_new_events(
        state, [_turn_event("turn_started", "2026-08-13T10:00:30Z")], T0
    )
    idled = lifecycle.tick(active, now=T0 + timedelta(minutes=30))
    assert idled.state == domain.SessionState.idle

    reopened = lifecycle.on_new_events(
        idled, [_turn_event("turn_started", "2026-08-13T10:35:00Z")], T0 + timedelta(minutes=35)
    )
    assert reopened.state == domain.SessionState.active_turn
    assert reopened.run_id is None
    assert reopened.execution_epoch is None


# --- reopen from execution-terminal state: new execution binding ---------


def test_reopen_from_terminal_completed_opens_new_execution() -> None:
    state = _bound(run_id="run-1")
    exited = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=0, now=T0
    )
    completed = lifecycle.tick(exited, now=T0 + timedelta(minutes=11))
    assert completed.state == domain.SessionState.terminal_completed

    resumed = lifecycle.bind_execution(
        completed, run_id="run-2", now=T0 + timedelta(minutes=15), first_binding=False
    )
    assert resumed.state == domain.SessionState.active_turn
    assert resumed.execution_epoch == 1
    assert resumed.run_id == "run-2"


def test_repeated_reopen_cycles_each_produce_new_report_version() -> None:
    state = _bound(run_id="run-1")
    exited = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=0, now=T0
    )
    completed = lifecycle.tick(exited, now=T0 + timedelta(minutes=11))
    first_report = lifecycle.report_for_terminal_or_idle(completed)
    assert first_report.report is not None
    assert first_report.report.report_version == 1
    assert first_report.report.supersedes is None

    resumed = lifecycle.bind_execution(
        first_report.state, run_id="run-2", now=T0 + timedelta(minutes=15), first_binding=False
    )
    exited_2 = lifecycle.on_process_exit(
        resumed, run_id="run-2", execution_epoch=1, exit_code=0, now=T0 + timedelta(minutes=16)
    )
    completed_2 = lifecycle.tick(exited_2, now=T0 + timedelta(minutes=27))
    second_report = lifecycle.report_for_terminal_or_idle(completed_2)
    assert second_report.report is not None
    assert second_report.report.report_version == 2
    assert second_report.report.supersedes == 1
    assert second_report.superseded_report_version == 1


# --- counter exhaustion: status_epoch, execution_epoch, report_version ---


def test_status_epoch_exhaustion_reaches_fatal_and_freezes_epoch() -> None:
    state = replace_status_epoch(_bound(), lifecycle.MAX_STATUS_EPOCH)
    idled = lifecycle.tick(state, now=T0 + timedelta(minutes=30))
    assert idled.state == domain.SessionState.identity_broken
    assert idled.fatal is not None
    assert idled.fatal.reason == domain.FatalReason.status_epoch_exhausted
    assert idled.status_epoch == lifecycle.MAX_STATUS_EPOCH  # frozen, not incremented


def test_execution_epoch_exhaustion_reaches_fatal_and_freezes_epoch() -> None:
    state = replace_execution_epoch(_bound(), lifecycle.MAX_EXECUTION_EPOCH)
    resumed = lifecycle.bind_execution(state, run_id="run-x", now=T0, first_binding=False)
    assert resumed.state == domain.SessionState.identity_broken
    assert resumed.fatal is not None
    assert resumed.fatal.reason == domain.FatalReason.execution_epoch_exhausted
    assert resumed.execution_epoch == lifecycle.MAX_EXECUTION_EPOCH


def test_report_version_exhaustion_reaches_fatal_and_freezes_version() -> None:
    state = replace_report_version(_bound(), lifecycle.MAX_REPORT_VERSION)
    exited = lifecycle.on_process_exit(
        state, run_id="run-1", execution_epoch=0, exit_code=0, now=T0
    )
    completed = lifecycle.tick(exited, now=T0 + timedelta(minutes=11))
    transition = lifecycle.report_for_terminal_or_idle(completed)
    assert transition.state.state == domain.SessionState.identity_broken
    assert transition.state.fatal is not None
    assert transition.state.fatal.reason == domain.FatalReason.report_version_exhausted
    assert transition.state.report_version == lifecycle.MAX_REPORT_VERSION
    assert transition.report is None


def replace_status_epoch(state: lifecycle.LifecycleState, value: int) -> lifecycle.LifecycleState:
    return dc_replace(state, status_epoch=value)


def replace_execution_epoch(
    state: lifecycle.LifecycleState, value: int
) -> lifecycle.LifecycleState:
    return dc_replace(state, execution_epoch=value)


def replace_report_version(state: lifecycle.LifecycleState, value: int) -> lifecycle.LifecycleState:
    return dc_replace(state, report_version=value)


# --- prose never marks completion ----------------------------------------


def test_message_text_saying_done_does_not_mark_completion() -> None:
    state = _bound()
    events = [
        domain.Event(
            id="evt:1",
            timestamp="2026-08-13T10:01:00Z",
            kind=domain.EventKind.message,
            summary="Done! All tests pass and the task is complete.",
        )
    ]
    result = lifecycle.on_new_events(state, events, T0)
    assert result.state not in (
        domain.SessionState.terminal_completed,
        domain.SessionState.terminal_failed,
    )
    assert result.state == domain.SessionState.active_turn


def test_prefix_mismatch_marks_identity_broken() -> None:
    state = _bound()
    result = lifecycle.mark_identity_broken(
        state, detail="rollout-x.jsonl: prefix mismatch", now=T0
    )
    assert result.state == domain.SessionState.identity_broken
    assert result.fatal is not None
    assert result.fatal.reason == domain.FatalReason.prefix_mismatch
    assert result.fatal.detail == "rollout-x.jsonl: prefix mismatch"


def test_fatal_state_ignores_further_transitions() -> None:
    state = _bound()
    broken = lifecycle.mark_identity_broken(state, detail="x", now=T0)
    still_broken = lifecycle.on_new_events(
        broken, [_turn_event("turn_started", "2026-08-13T10:05:00Z")], T0
    )
    assert still_broken.state == domain.SessionState.identity_broken
    assert still_broken == broken
