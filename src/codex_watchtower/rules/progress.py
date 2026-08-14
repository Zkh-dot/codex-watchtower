"""Distinguish inactivity from legitimate long-running work (spec 5.5).

A progress marker is one of: a materially changed reasoning/hypothesis
summary, a newly changed file, an improving test result, or a previously
failing command succeeding. Message prose is deliberately not treated as a
marker on its own -- the same "prose is not evidence" principle spec 5.8
applies to session completion applies here to avoid a chatty but stuck
agent resetting its own stagnation timer.

Time alone is never evidence of stagnation while a long-running command is
still active: ``active_command_started_at`` suppresses the timer until the
command exits or exceeds its own configured maximum duration.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime, timedelta

from codex_watchtower import domain
from codex_watchtower.rules.repetition import normalize_command

DEFAULT_STAGNATION_THRESHOLD = timedelta(minutes=25)
DEFAULT_ACTIVE_COMMAND_MAX_DURATION = timedelta(hours=1)
REASONING_SIMILARITY_THRESHOLD = 0.8


@dataclass(frozen=True, slots=True)
class ProgressMarker:
    event_id: str
    timestamp: str
    kind: str  # "reasoning_change" | "file_changed" | "test_improved" | "command_recovered"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)


def _materially_different(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() < REASONING_SIMILARITY_THRESHOLD


def _test_summary_improved(previous: str, current: str) -> bool:
    """Heuristic: a summary with fewer failures (or none) than the previous one improved.

    Detailed pass/fail extraction per test runner is rules/tests.py (Task
    16); this only needs to notice improvement well enough to reset a
    stagnation timer, not to compute a trend.
    """
    prev_failed = "fail" in previous.lower()
    cur_failed = "fail" in current.lower()
    return prev_failed and not cur_failed


def find_progress_markers(events: list[domain.Event]) -> list[ProgressMarker]:
    markers: list[ProgressMarker] = []
    last_reasoning: str | None = None
    last_test_summary: str | None = None
    last_command_exit: dict[str, int] = {}

    for event in events:
        if event.kind == domain.EventKind.reasoning:
            if last_reasoning is None or _materially_different(last_reasoning, event.summary):
                markers.append(ProgressMarker(event.id, event.timestamp, "reasoning_change"))
            last_reasoning = event.summary

        elif event.kind == domain.EventKind.file_changed:
            markers.append(ProgressMarker(event.id, event.timestamp, "file_changed"))

        elif event.kind == domain.EventKind.test_result:
            if last_test_summary is not None and _test_summary_improved(
                last_test_summary, event.summary
            ):
                markers.append(ProgressMarker(event.id, event.timestamp, "test_improved"))
            elif last_test_summary is None:
                markers.append(ProgressMarker(event.id, event.timestamp, "test_improved"))
            last_test_summary = event.summary

        elif event.kind == domain.EventKind.command_result:
            command = event.summary.removeprefix("$ ").split(" -> exit ", 1)[0]
            normalized = normalize_command(command)
            previous_exit = last_command_exit.get(normalized)
            if previous_exit is not None and previous_exit != 0 and event.exit_code == 0:
                markers.append(ProgressMarker(event.id, event.timestamp, "command_recovered"))
            if event.exit_code is not None:
                last_command_exit[normalized] = event.exit_code

    return markers


def detect_stagnation(
    events: list[domain.Event],
    *,
    now: datetime,
    session_reference_time: datetime,
    threshold: timedelta = DEFAULT_STAGNATION_THRESHOLD,
    active_command_started_at: datetime | None = None,
    active_command_max_duration: timedelta = DEFAULT_ACTIVE_COMMAND_MAX_DURATION,
) -> domain.Signal | None:
    if active_command_started_at is not None:
        if now - active_command_started_at <= active_command_max_duration:
            return None  # still within its own budget; time alone is not evidence

    markers = find_progress_markers(events)
    if markers:
        reference_time = max(_parse(m.timestamp) for m in markers)
        evidence_id = max(markers, key=lambda m: _parse(m.timestamp)).event_id
    else:
        reference_time = session_reference_time
        evidence_id = None

    elapsed = now - reference_time
    if elapsed < threshold:
        return None

    minutes = int(elapsed.total_seconds() // 60)
    return domain.Signal(
        id="sig:stagnation",
        kind=domain.SignalKind.stagnation,
        severity=domain.Severity.warning,
        source=domain.SignalSource.local_rule,
        event_ids=[evidence_id] if evidence_id else [],
        observed_at=now.isoformat(),
        freshness=domain.Freshness.current,
        summary=f"No progress marker observed for {minutes} minutes.",
        payload={"minutes_since_marker": minutes},
    )
