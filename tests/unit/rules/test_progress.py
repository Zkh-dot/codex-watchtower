from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_watchtower import domain
from codex_watchtower.rules.progress import detect_stagnation, find_progress_markers

NOW = datetime(2026, 8, 13, 11, 0, 0, tzinfo=UTC)
START = NOW - timedelta(hours=1)


def _reasoning(idx: int, text: str, ts: datetime) -> domain.Event:
    return domain.Event(
        id=f"evt:reason:{idx}",
        timestamp=ts.isoformat(),
        kind=domain.EventKind.reasoning,
        summary=text,
    )


def _file_changed(idx: int, path: str, ts: datetime) -> domain.Event:
    return domain.Event(
        id=f"evt:file:{idx}",
        timestamp=ts.isoformat(),
        kind=domain.EventKind.file_changed,
        summary=f"modified: {path}",
        path=path,
    )


def _test_result(idx: int, summary: str, ts: datetime) -> domain.Event:
    return domain.Event(
        id=f"evt:test:{idx}",
        timestamp=ts.isoformat(),
        kind=domain.EventKind.test_result,
        summary=summary,
    )


def _command_result(idx: int, command: str, exit_code: int, ts: datetime) -> domain.Event:
    return domain.Event(
        id=f"evt:cmd:{idx}",
        timestamp=ts.isoformat(),
        kind=domain.EventKind.command_result,
        summary=f"$ {command} -> exit {exit_code}: output",
        exit_code=exit_code,
    )


# --- each documented progress marker ------------------------------------


def test_marker_new_reasoning_summary() -> None:
    events = [_reasoning(0, "I should check the failing test first.", NOW - timedelta(minutes=5))]
    markers = find_progress_markers(events)
    assert len(markers) == 1
    assert markers[0].kind == "reasoning_change"


def test_marker_newly_changed_file() -> None:
    events = [_file_changed(0, "src/foo.py", NOW - timedelta(minutes=5))]
    markers = find_progress_markers(events)
    assert len(markers) == 1
    assert markers[0].kind == "file_changed"


def test_marker_improved_test_outcome() -> None:
    events = [
        _test_result(0, "3 failed, 2 passed", NOW - timedelta(minutes=10)),
        _test_result(1, "0 failed, 5 passed", NOW - timedelta(minutes=5)),
    ]
    markers = find_progress_markers(events)
    kinds = [m.kind for m in markers]
    assert "test_improved" in kinds


def test_marker_new_test_target_first_result_counts() -> None:
    events = [_test_result(0, "3 failed, 2 passed", NOW - timedelta(minutes=5))]
    markers = find_progress_markers(events)
    assert len(markers) == 1
    assert markers[0].kind == "test_improved"


def test_marker_previously_failing_command_succeeding() -> None:
    events = [
        _command_result(0, "pytest -q", 1, NOW - timedelta(minutes=10)),
        _command_result(1, "pytest -q", 0, NOW - timedelta(minutes=5)),
    ]
    markers = find_progress_markers(events)
    kinds = [m.kind for m in markers]
    assert "command_recovered" in kinds


def test_no_marker_for_repeated_failing_command() -> None:
    events = [
        _command_result(0, "pytest -q", 1, NOW - timedelta(minutes=10)),
        _command_result(1, "pytest -q", 1, NOW - timedelta(minutes=5)),
    ]
    markers = find_progress_markers(events)
    assert markers == []


# --- stagnation timing ----------------------------------------------------


def test_25_minutes_without_a_marker_triggers_stagnation() -> None:
    events = [_reasoning(0, "starting work", NOW - timedelta(minutes=26))]
    signal = detect_stagnation(events, now=NOW, session_reference_time=START)
    assert signal is not None
    assert signal.kind == domain.SignalKind.stagnation


def test_under_25_minutes_since_marker_does_not_trigger() -> None:
    events = [_reasoning(0, "starting work", NOW - timedelta(minutes=10))]
    signal = detect_stagnation(events, now=NOW, session_reference_time=START)
    assert signal is None


def test_no_markers_at_all_uses_session_reference_time() -> None:
    signal = detect_stagnation([], now=NOW, session_reference_time=NOW - timedelta(minutes=30))
    assert signal is not None


def test_no_markers_within_threshold_of_reference_time_does_not_trigger() -> None:
    signal = detect_stagnation([], now=NOW, session_reference_time=NOW - timedelta(minutes=5))
    assert signal is None


# --- active long-running command suppresses the timer ---------------------


def test_active_long_running_command_suppresses_stagnation() -> None:
    signal = detect_stagnation(
        [],
        now=NOW,
        session_reference_time=START,
        active_command_started_at=NOW - timedelta(minutes=40),
        active_command_max_duration=timedelta(hours=1),
    )
    assert signal is None


def test_command_exceeding_its_own_maximum_no_longer_suppresses() -> None:
    signal = detect_stagnation(
        [],
        now=NOW,
        session_reference_time=START,
        active_command_started_at=NOW - timedelta(hours=2),
        active_command_max_duration=timedelta(hours=1),
    )
    assert signal is not None


# --- changed hypothesis counts only when materially different -------------


def test_reasoning_repeated_near_verbatim_does_not_count_as_new_marker() -> None:
    events = [
        _reasoning(0, "I should check the failing test first.", NOW - timedelta(minutes=30)),
        _reasoning(1, "I should check the failing test first!", NOW - timedelta(minutes=5)),
    ]
    markers = find_progress_markers(events)
    assert len(markers) == 1  # only the first counts; the second is materially the same


def test_reasoning_materially_different_counts_as_new_marker() -> None:
    events = [
        _reasoning(0, "I should check the failing test first.", NOW - timedelta(minutes=30)),
        _reasoning(
            1,
            "Actually the real issue is a race condition in the tailer's cursor handling.",
            NOW - timedelta(minutes=5),
        ),
    ]
    markers = find_progress_markers(events)
    assert len(markers) == 2
