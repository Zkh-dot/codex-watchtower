"""Schedule assessments by evidence change, not by filesystem event or fixed interval (spec 5.7).

Luna is invoked when ten minutes have passed since the last assessment, a
material progress marker occurred, or lifecycle/state changed -- not on
every ingested event. "Material" is decided upstream: a progress marker
(``rules/progress.py``) already excludes near-verbatim wording changes via
its similarity threshold, and a signal-fingerprint comparison here already
excludes a duplicate/unchanged signal set, so neither wording-only prose
nor a repeated identical signal reaches this scheduler as a trigger.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from codex_watchtower import domain

DEFAULT_ROUTINE_INTERVAL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class SchedulerState:
    last_assessed_at: datetime | None = None
    last_signal_fingerprint: str | None = None
    last_lifecycle_state: domain.SessionState | None = None
    in_progress: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    should_assess: bool
    # "lifecycle_change" | "material_progress" | "signal_change" | "routine_cadence" | None
    reason: str | None


def should_schedule_assessment(
    state: SchedulerState,
    *,
    now: datetime,
    current_lifecycle_state: domain.SessionState,
    current_signal_fingerprint: str,
    material_progress_marker: bool,
    routine_interval: timedelta = DEFAULT_ROUTINE_INTERVAL,
) -> ScheduleDecision:
    if state.in_progress:
        return ScheduleDecision(False, None)  # coalesce: one assessment in flight per session

    if (
        state.last_lifecycle_state is not None
        and current_lifecycle_state != state.last_lifecycle_state
    ):
        return ScheduleDecision(True, "lifecycle_change")

    if material_progress_marker:
        return ScheduleDecision(True, "material_progress")

    if (
        state.last_signal_fingerprint is not None
        and current_signal_fingerprint != state.last_signal_fingerprint
    ):
        return ScheduleDecision(True, "signal_change")

    if state.last_assessed_at is None:
        return ScheduleDecision(True, "routine_cadence")  # never assessed yet

    if now - state.last_assessed_at >= routine_interval:
        return ScheduleDecision(True, "routine_cadence")

    return ScheduleDecision(False, None)


def begin_assessment(state: SchedulerState) -> SchedulerState:
    return replace(state, in_progress=True)


def complete_assessment(
    state: SchedulerState,
    *,
    now: datetime,
    signal_fingerprint: str,
    lifecycle_state: domain.SessionState,
) -> SchedulerState:
    return replace(
        state,
        in_progress=False,
        last_assessed_at=now,
        last_signal_fingerprint=signal_fingerprint,
        last_lifecycle_state=lifecycle_state,
    )


def abandon_assessment(state: SchedulerState) -> SchedulerState:
    """Release the in-progress lock without recording a completed assessment (e.g. on failure)."""
    return replace(state, in_progress=False)
