"""Derive session lifecycle from explicit evidence only (spec 5.8, 5.11).

Ownership split with the policy reconciler (Phase 6, Task 22): this module
owns deterministic session ``state``, the bound execution (``run_id``/
``execution_epoch``), ``status_epoch``, and report versioning -- everything
derivable from the transcript and process evidence alone. It does **not**
own ``attention_epoch`` or ``notification_status``: those depend on rule
signals and, in v0.2.0, model output, which belong to the reconciler.
``status_epoch`` increments here on every session-*state* change, which is
a subset of every ``notification_status`` change (state and
notification_status move together except when a model/rule narrows within
``active_turn`` without a state change, e.g. ``progressing`` ->
``stalled``); the reconciler layers additional increments for that
narrowing case on top of what this module already does. Fatal detection
here is limited to the three counters this module owns
(``status_epoch_exhausted``, ``execution_epoch_exhausted``,
``report_version_exhausted``) plus ``prefix_mismatch``, which the tailer
reports directly. ``attention_epoch_exhausted`` is detected in Task 22.

No path reaches a terminal state without process evidence bound to the
session's *current* execution -- a late exit event from a superseded
execution must not terminate a resumed one. Message/reasoning text is never
inspected for completion language: only ``process_lifecycle`` events (exit
evidence) and the configured quiet grace period drive terminal/idle
transitions.

Per spec 5.8 rule 6, a zero exit alone does not finalize
``terminal_completed``: it stages a pending completion that finalizes only
after the quiet grace period elapses with no new turn (or an explicit
operator marker arrives first, ``confirm_pending_completion``). A resumed
execution binding before the grace period elapses cancels the pending
completion instead of finalizing it. Non-zero exit is unambiguous and
finalizes ``terminal_failed`` immediately -- the plan does not condition
failure reporting on the grace period the way it does completion.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from codex_watchtower import domain

MAX_STATUS_EPOCH = 1_000_000
MAX_EXECUTION_EPOCH = 100_000
MAX_REPORT_VERSION = 100_000

DEFAULT_QUIET_GRACE_PERIOD = timedelta(minutes=10)

_TERMINAL_STATES = frozenset(
    {domain.SessionState.terminal_completed, domain.SessionState.terminal_failed}
)
_REOPENABLE_STATES = _TERMINAL_STATES | {domain.SessionState.idle}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)


@dataclass(frozen=True, slots=True)
class FatalInfo:
    reason: domain.FatalReason
    detected_at: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReportInfo:
    report_version: int
    provisional: bool
    supersedes: int | None


@dataclass(frozen=True, slots=True)
class PendingExit:
    run_id: str
    execution_epoch: int
    exited_at: str


@dataclass(frozen=True, slots=True)
class LifecycleState:
    session_id: str
    state: domain.SessionState = domain.SessionState.unknown
    run_id: str | None = None
    execution_epoch: int | None = None
    status_epoch: int = 0
    last_event_at: str | None = None
    report_version: int = 0
    pending_exit: PendingExit | None = None
    fatal: FatalInfo | None = None

    @staticmethod
    def initial(session_id: str) -> LifecycleState:
        return LifecycleState(session_id=session_id)


@dataclass(frozen=True, slots=True)
class Transition:
    state: LifecycleState
    report: ReportInfo | None = None
    superseded_report_version: int | None = None


def _enter_fatal(state: LifecycleState, fatal: FatalInfo) -> LifecycleState:
    return replace(state, state=domain.SessionState.identity_broken, fatal=fatal)


def _move_to(
    state: LifecycleState,
    target: domain.SessionState,
    now: datetime,
    *,
    event_timestamp: str | None = None,
) -> LifecycleState:
    """Move to ``target``, bumping status_epoch iff the state actually changes.

    Saturation on the transition that would need the increment is exactly
    the case spec section 5.8 calls out as otherwise unreachable: the fatal
    path itself must not depend on an increment that cannot happen.
    Saturating here routes to identity_broken instead of silently reusing
    the epoch.
    """
    last_event_at = event_timestamp if event_timestamp is not None else state.last_event_at
    if target == state.state:
        return replace(state, last_event_at=last_event_at)
    if state.status_epoch >= MAX_STATUS_EPOCH:
        return _enter_fatal(
            state,
            FatalInfo(
                reason=domain.FatalReason.status_epoch_exhausted,
                detected_at=now.isoformat(),
                detail=f"status_epoch saturated at {MAX_STATUS_EPOCH}",
            ),
        )
    return replace(
        state, state=target, status_epoch=state.status_epoch + 1, last_event_at=last_event_at
    )


def _next_report(state: LifecycleState, *, provisional: bool) -> Transition:
    if state.report_version >= MAX_REPORT_VERSION:
        fatal_state = _enter_fatal(
            state,
            FatalInfo(
                reason=domain.FatalReason.report_version_exhausted,
                detected_at=state.last_event_at or "",
                detail=f"report_version saturated at {MAX_REPORT_VERSION}",
            ),
        )
        return Transition(state=fatal_state)
    new_version = state.report_version + 1
    supersedes = state.report_version if state.report_version > 0 else None
    new_state = replace(state, report_version=new_version)
    report = ReportInfo(report_version=new_version, provisional=provisional, supersedes=supersedes)
    return Transition(state=new_state, report=report, superseded_report_version=supersedes)


# --- public transitions --------------------------------------------------


def mark_identity_broken(state: LifecycleState, *, detail: str, now: datetime) -> LifecycleState:
    """The tailer reported a prefix-hash mismatch: fail closed permanently."""
    if state.fatal is not None:
        return state
    return _enter_fatal(
        state,
        FatalInfo(
            reason=domain.FatalReason.prefix_mismatch, detected_at=now.isoformat(), detail=detail
        ),
    )


def bind_execution(
    state: LifecycleState, *, run_id: str, now: datetime, first_binding: bool
) -> LifecycleState:
    """A launcher (fresh run or `codex exec resume`) bound an execution to this session.

    ``first_binding=True`` is the session's very first bound execution
    (execution_epoch 0); otherwise this is a resume and execution_epoch
    advances -- never for a quiet-period reopen of the *same* process,
    which goes through ``on_new_events`` instead and leaves execution_epoch
    untouched. A resume arriving before a pending zero-exit completion
    finalized cancels that pending completion: the execution it belonged to
    has been superseded before it was ever reported.
    """
    if state.fatal is not None:
        return state

    if first_binding or state.execution_epoch is None:
        new_epoch = 0
    else:
        if state.execution_epoch >= MAX_EXECUTION_EPOCH:
            return _enter_fatal(
                state,
                FatalInfo(
                    reason=domain.FatalReason.execution_epoch_exhausted,
                    detected_at=now.isoformat(),
                    detail=f"execution_epoch saturated at {MAX_EXECUTION_EPOCH}",
                ),
            )
        new_epoch = state.execution_epoch + 1

    bound = replace(state, run_id=run_id, execution_epoch=new_epoch, pending_exit=None)
    return _move_to(bound, domain.SessionState.active_turn, now, event_timestamp=now.isoformat())


def on_new_events(
    state: LifecycleState, events: list[domain.Event], now: datetime
) -> LifecycleState:
    """Advance state from transcript evidence. Message/reasoning text is never inspected."""
    result = state
    for event in events:
        if result.fatal is not None:
            return result
        if event.kind == domain.EventKind.process_lifecycle:
            assert event.run_id is not None
            assert event.execution_epoch is not None
            assert event.exit_code is not None
            result = on_process_exit(
                result,
                run_id=event.run_id,
                execution_epoch=event.execution_epoch,
                exit_code=event.exit_code,
                now=now,
            )
            continue

        if event.kind == domain.EventKind.turn_lifecycle:
            target = (
                domain.SessionState.between_turns
                if event.source_type == "turn_complete"
                else domain.SessionState.active_turn
            )
        else:
            target = domain.SessionState.active_turn

        result = _move_to(result, target, now, event_timestamp=event.timestamp)

    return result


def on_process_exit(
    state: LifecycleState, *, run_id: str, execution_epoch: int, exit_code: int, now: datetime
) -> LifecycleState:
    """Process evidence for an execution. Only the session's *current* execution can terminate it.

    A late exit event from a superseded execution (run_id/execution_epoch
    that no longer matches the session's bound execution) is evidence about
    history, not about the present, and is ignored for state purposes.
    """
    if state.fatal is not None:
        return state
    if run_id != state.run_id or execution_epoch != state.execution_epoch:
        return state  # stale evidence from a superseded execution; ignored

    if exit_code != 0:
        return _move_to(
            state, domain.SessionState.terminal_failed, now, event_timestamp=now.isoformat()
        )

    # Zero exit stages a pending completion; it finalizes only after the
    # quiet grace period (see tick()) or an explicit operator marker.
    pending = PendingExit(run_id=run_id, execution_epoch=execution_epoch, exited_at=now.isoformat())
    return replace(state, pending_exit=pending)


def confirm_pending_completion(state: LifecycleState, *, now: datetime) -> LifecycleState:
    """An explicit operator-provided terminal marker finalizes a pending completion immediately."""
    if state.fatal is not None or state.pending_exit is None:
        return state
    finalized = _move_to(state, domain.SessionState.terminal_completed, now)
    return replace(finalized, pending_exit=None)


def tick(
    state: LifecycleState,
    *,
    now: datetime,
    quiet_grace_period: timedelta = DEFAULT_QUIET_GRACE_PERIOD,
) -> LifecycleState:
    """Periodic time-driven check: finalize a pending completion, or idle-timeout a quiet session.

    Idle-timeout only applies when the session is not already terminal/idle
    and has no pending completion in flight. The caller must not invoke
    this while a long-running command is known to still be active (spec
    5.5: time alone is not evidence of stagnation) -- that exclusion lives
    with the rule engine / orchestrator, not here.
    """
    if state.fatal is not None:
        return state

    if state.pending_exit is not None:
        exited_at = _parse(state.pending_exit.exited_at)
        if now - exited_at >= quiet_grace_period:
            finalized = _move_to(state, domain.SessionState.terminal_completed, now)
            return replace(finalized, pending_exit=None)
        return state

    if state.state in _REOPENABLE_STATES:
        return state
    if state.last_event_at is None:
        return state
    elapsed = now - _parse(state.last_event_at)
    if elapsed < quiet_grace_period:
        return state
    return _move_to(state, domain.SessionState.idle, now)


def enter_waiting(state: LifecycleState, *, now: datetime) -> LifecycleState:
    """A deterministic rule detected the session is waiting for input/approval."""
    if state.fatal is not None:
        return state
    return _move_to(state, domain.SessionState.waiting, now, event_timestamp=now.isoformat())


def report_for_terminal_or_idle(
    state: LifecycleState, *, previous_state: domain.SessionState | None = None
) -> Transition:
    """Emit (or supersede) a report only when state has *just entered* idle or terminal.

    Idle reports are provisional (no process evidence exists at all);
    execution-terminal reports are final for that execution and still
    supersedable by a later resume, per spec 5.8's "Executions and session
    lifetime". A report is emitted only on the transition into a reportable
    state — not on every poll while already in that state — to avoid
    duplicate reports and false supersedes.
    """
    if previous_state == state.state:
        return Transition(state=state)
    if state.state == domain.SessionState.idle and previous_state != domain.SessionState.idle:
        return _next_report(state, provisional=True)
    if state.state in _TERMINAL_STATES and previous_state not in _TERMINAL_STATES:
        return _next_report(state, provisional=False)
    if state.state in _REOPENABLE_STATES and previous_state not in _REOPENABLE_STATES:
        # Transitioned from active/between_turns/waiting into a reportable state.
        if state.state == domain.SessionState.idle:
            return _next_report(state, provisional=True)
        return _next_report(state, provisional=False)
    return Transition(state=state)
