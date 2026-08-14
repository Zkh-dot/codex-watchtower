from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_watchtower import domain
from codex_watchtower.rules.repetition import (
    detect_recurring_errors,
    detect_repeated_commands,
    normalize_command,
)

NOW = datetime(2026, 8, 13, 10, 30, 0, tzinfo=UTC)


def _command_result(idx: int, command: str, exit_code: int, ts: datetime) -> domain.Event:
    return domain.Event(
        id=f"evt:cmd:{idx}",
        timestamp=ts.isoformat(),
        kind=domain.EventKind.command_result,
        summary=f"$ {command} -> exit {exit_code}: output",
        exit_code=exit_code,
        source_type="shell",
    )


def _file_changed(idx: int, path: str, ts: datetime) -> domain.Event:
    return domain.Event(
        id=f"evt:file:{idx}",
        timestamp=ts.isoformat(),
        kind=domain.EventKind.file_changed,
        summary=f"modified: {path}",
        path=path,
    )


def _error(idx: int, message: str, ts: datetime) -> domain.Event:
    return domain.Event(
        id=f"evt:err:{idx}", timestamp=ts.isoformat(), kind=domain.EventKind.error, summary=message
    )


def test_three_identical_commands_unchanged_outcome_triggers_warning() -> None:
    events = [
        _command_result(i, "pytest -q", 1, NOW - timedelta(minutes=20 - i * 2)) for i in range(3)
    ]
    signals = detect_repeated_commands(events, now=NOW)
    assert len(signals) == 1
    assert signals[0].kind == domain.SignalKind.repeated_command
    assert signals[0].severity == domain.Severity.warning
    assert len(signals[0].event_ids) == 3


def test_occurrences_spread_beyond_window_do_not_trigger() -> None:
    events = [
        _command_result(0, "pytest -q", 1, NOW - timedelta(hours=2)),
        _command_result(1, "pytest -q", 1, NOW - timedelta(minutes=10)),
        _command_result(2, "pytest -q", 1, NOW - timedelta(minutes=5)),
    ]
    signals = detect_repeated_commands(events, now=NOW, max_age=timedelta(minutes=30))
    assert signals == []


def test_time_and_event_bounds_are_configurable() -> None:
    events = [
        _command_result(i, "pytest -q", 1, NOW - timedelta(minutes=50 - i * 2)) for i in range(3)
    ]
    # Default 30-minute window excludes these (spread over ~4 minutes but
    # starting 50 minutes ago); a wider configured window includes them.
    assert detect_repeated_commands(events, now=NOW, max_age=timedelta(minutes=30)) == []
    wide = detect_repeated_commands(events, now=NOW, max_age=timedelta(hours=2))
    assert len(wide) == 1


def test_max_events_bound_excludes_old_entries() -> None:
    events = [_command_result(i, "pytest -q", 1, NOW - timedelta(minutes=1)) for i in range(3)]
    signals = detect_repeated_commands(events, now=NOW, max_events=2)
    assert signals == []  # only the last 2 of the 3 survive the event-count bound


def test_same_command_with_changed_outcome_does_not_automatically_trigger() -> None:
    events = [
        _command_result(0, "pytest -q", 1, NOW - timedelta(minutes=20)),
        _command_result(1, "pytest -q", 0, NOW - timedelta(minutes=15)),  # outcome changed
        _command_result(2, "pytest -q", 1, NOW - timedelta(minutes=10)),
    ]
    signals = detect_repeated_commands(events, now=NOW)
    assert signals == []


def test_same_command_with_intervening_file_change_does_not_automatically_trigger() -> None:
    events = [
        _command_result(0, "pytest -q", 1, NOW - timedelta(minutes=20)),
        _file_changed(0, "src/foo.py", NOW - timedelta(minutes=18)),
        _command_result(1, "pytest -q", 1, NOW - timedelta(minutes=15)),
        _file_changed(1, "src/foo.py", NOW - timedelta(minutes=13)),
        _command_result(2, "pytest -q", 1, NOW - timedelta(minutes=10)),
    ]
    signals = detect_repeated_commands(events, now=NOW)
    assert signals == []


def test_normalizes_volatile_timestamps_and_temp_paths() -> None:
    assert normalize_command("touch /tmp/abc123xyz/file.txt") == "touch /tmp/<path>"
    assert normalize_command("log at 2026-08-13T10:00:00Z done") == "log at <ts> done"
    assert normalize_command("kill -9 123456") == "kill -9 <num>"


def test_commands_differing_only_by_volatile_content_are_treated_as_equivalent() -> None:
    events = [
        _command_result(0, "rm /tmp/abc111/x", 1, NOW - timedelta(minutes=20)),
        _command_result(1, "rm /tmp/def222/x", 1, NOW - timedelta(minutes=15)),
        _command_result(2, "rm /tmp/ghi333/x", 1, NOW - timedelta(minutes=10)),
    ]
    signals = detect_repeated_commands(events, now=NOW)
    assert len(signals) == 1


def test_three_equivalent_recurring_errors_trigger_warning() -> None:
    events = [
        _error(i, "connection refused", NOW - timedelta(minutes=20 - i * 2)) for i in range(3)
    ]
    signals = detect_recurring_errors(events, now=NOW)
    assert len(signals) == 1
    assert signals[0].kind == domain.SignalKind.recurring_error
    assert signals[0].severity == domain.Severity.warning


def test_recurring_errors_respect_the_window() -> None:
    events = [
        _error(0, "connection refused", NOW - timedelta(hours=3)),
        _error(1, "connection refused", NOW - timedelta(minutes=5)),
    ]
    signals = detect_recurring_errors(events, now=NOW, max_age=timedelta(minutes=30))
    assert signals == []
