from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_watchtower import domain
from codex_watchtower.assess.scheduler import (
    SchedulerState,
    abandon_assessment,
    begin_assessment,
    complete_assessment,
    should_schedule_assessment,
)

T0 = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC)


def _base_kwargs(**overrides: object) -> dict:
    kwargs = {
        "now": T0,
        "current_lifecycle_state": domain.SessionState.active_turn,
        "current_signal_fingerprint": "fp-a",
        "material_progress_marker": False,
    }
    kwargs.update(overrides)
    return kwargs


# --- routine ten-minute cadence, using an explicit (fake) clock -----------


def test_first_assessment_always_scheduled() -> None:
    state = SchedulerState()
    decision = should_schedule_assessment(state, **_base_kwargs())
    assert decision.should_assess is True
    assert decision.reason == "routine_cadence"


def test_within_ten_minutes_of_last_assessment_does_not_trigger() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(state, **_base_kwargs(now=T0 + timedelta(minutes=5)))
    assert decision.should_assess is False


def test_exactly_ten_minutes_triggers_routine_cadence() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(state, **_base_kwargs(now=T0 + timedelta(minutes=10)))
    assert decision.should_assess is True
    assert decision.reason == "routine_cadence"


def test_just_under_ten_minutes_does_not_trigger() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(
        state, **_base_kwargs(now=T0 + timedelta(minutes=9, seconds=59))
    )
    assert decision.should_assess is False


# --- material progress and lifecycle changes trigger immediately ----------


def test_material_progress_marker_triggers_immediately() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(
        state, **_base_kwargs(now=T0 + timedelta(seconds=30), material_progress_marker=True)
    )
    assert decision.should_assess is True
    assert decision.reason == "material_progress"


def test_lifecycle_state_change_triggers_immediately() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(
        state,
        **_base_kwargs(
            now=T0 + timedelta(seconds=30),
            current_lifecycle_state=domain.SessionState.between_turns,
        ),
    )
    assert decision.should_assess is True
    assert decision.reason == "lifecycle_change"


def test_new_signal_fingerprint_triggers_immediately() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(
        state,
        **_base_kwargs(now=T0 + timedelta(seconds=30), current_signal_fingerprint="fp-b"),
    )
    assert decision.should_assess is True
    assert decision.reason == "signal_change"


# --- wording-only / duplicate signal changes do not trigger ---------------


def test_duplicate_signal_fingerprint_does_not_trigger() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(
        state,
        **_base_kwargs(now=T0 + timedelta(seconds=30), current_signal_fingerprint="fp-a"),
    )
    assert decision.should_assess is False


def test_unchanged_lifecycle_state_does_not_trigger() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(
        state,
        **_base_kwargs(
            now=T0 + timedelta(seconds=30),
            current_lifecycle_state=domain.SessionState.active_turn,
        ),
    )
    assert decision.should_assess is False


def test_wording_only_change_is_excluded_upstream_not_here() -> None:
    """material_progress_marker=False models a wording-only prose change.

    rules/progress.py already decides materiality via its similarity
    threshold before this scheduler ever sees a marker flag; the scheduler
    itself only has to not invent a trigger when told there wasn't one.
    """
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
    )
    decision = should_schedule_assessment(
        state, **_base_kwargs(now=T0 + timedelta(seconds=30), material_progress_marker=False)
    )
    assert decision.should_assess is False


# --- per-session concurrency lock and coalescing ---------------------------


def test_in_progress_assessment_coalesces_further_triggers() -> None:
    state = SchedulerState(
        last_assessed_at=T0,
        last_signal_fingerprint="fp-a",
        last_lifecycle_state=domain.SessionState.active_turn,
        in_progress=True,
    )
    decision = should_schedule_assessment(
        state,
        **_base_kwargs(
            now=T0 + timedelta(minutes=20),  # would otherwise trigger on cadence
            material_progress_marker=True,  # and on material progress
        ),
    )
    assert decision.should_assess is False


def test_begin_and_complete_assessment_round_trip() -> None:
    state = SchedulerState()
    started = begin_assessment(state)
    assert started.in_progress is True

    completed = complete_assessment(
        started,
        now=T0,
        signal_fingerprint="fp-a",
        lifecycle_state=domain.SessionState.active_turn,
    )
    assert completed.in_progress is False
    assert completed.last_assessed_at == T0
    assert completed.last_signal_fingerprint == "fp-a"
    assert completed.last_lifecycle_state == domain.SessionState.active_turn


def test_lock_releases_after_completion_allowing_next_schedule() -> None:
    state = SchedulerState()
    started = begin_assessment(state)
    completed = complete_assessment(
        started, now=T0, signal_fingerprint="fp-a", lifecycle_state=domain.SessionState.active_turn
    )
    decision = should_schedule_assessment(completed, **_base_kwargs(now=T0 + timedelta(minutes=11)))
    assert decision.should_assess is True


def test_abandon_assessment_releases_lock_without_recording_completion() -> None:
    state = SchedulerState(last_assessed_at=T0)
    started = begin_assessment(state)
    abandoned = abandon_assessment(started)
    assert abandoned.in_progress is False
    assert abandoned.last_assessed_at == T0  # unchanged: no completion was recorded
