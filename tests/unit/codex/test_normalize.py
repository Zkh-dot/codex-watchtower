from __future__ import annotations

from codex_watchtower import domain
from codex_watchtower.codex.normalize import compute_logical_event_id, normalize_record
from codex_watchtower.codex.tailer import RawRecord
from codex_watchtower.storage.repository import SourceLocator

FALLBACK_TS = "2026-08-13T10:00:00Z"


def _raw(ordinal: int, parsed: dict[str, object] | None, raw_line: bytes = b"{}") -> RawRecord:
    return RawRecord(
        record_ordinal=ordinal,
        raw_line=raw_line,
        parsed=parsed,
        locator=SourceLocator(device=1, inode=2, byte_offset=ordinal * 10, record_length=10),
    )


# --- session lifecycle / turn boundaries ------------------------------


def test_session_meta_is_not_a_normalized_event() -> None:
    record = _raw(0, {"type": "session_meta", "payload": {"id": "sess-1"}})
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is None


def test_turn_started_and_complete_normalize_to_turn_lifecycle_only() -> None:
    started = normalize_record(
        "sess-1",
        _raw(1, {"type": "turn_started", "timestamp": "2026-08-13T10:00:05Z", "payload": {}}),
        fallback_timestamp=FALLBACK_TS,
    )
    complete = normalize_record(
        "sess-1",
        _raw(2, {"type": "turn_complete", "timestamp": "2026-08-13T10:00:06Z", "payload": {}}),
        fallback_timestamp=FALLBACK_TS,
    )
    assert started is not None and complete is not None
    assert started.event.kind == domain.EventKind.turn_lifecycle
    assert complete.event.kind == domain.EventKind.turn_lifecycle
    # Never process_lifecycle: process boundaries come from the launcher, not the transcript.
    assert started.event.kind != domain.EventKind.process_lifecycle


# --- messages, reasoning, commands, command results, file changes -----


def test_agent_message_normalizes_to_message() -> None:
    record = _raw(
        1,
        {
            "type": "agent_message",
            "timestamp": "2026-08-13T10:01:00Z",
            "payload": {"text": "Fixed the bug."},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.message
    assert result.event.summary == "Fixed the bug."


def test_reasoning_normalizes_to_reasoning() -> None:
    record = _raw(
        1,
        {
            "type": "reasoning",
            "timestamp": "2026-08-13T10:01:00Z",
            "payload": {"text": "I should check the logs first."},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.reasoning


def test_exec_command_begin_normalizes_to_command() -> None:
    record = _raw(
        1,
        {
            "type": "exec_command_begin",
            "timestamp": "2026-08-13T10:01:00Z",
            "payload": {"command": "pytest -q", "cwd": "/w"},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.command
    assert "pytest -q" in result.event.summary
    assert result.event.source_type == "shell"


def test_exec_command_end_normalizes_to_command_result_with_exit_code() -> None:
    record = _raw(
        1,
        {
            "type": "exec_command_end",
            "timestamp": "2026-08-13T10:01:05Z",
            "payload": {
                "command": "pytest -q",
                "exit_code": 1,
                "stdout_tail": "1 failed",
                "stderr_tail": "",
            },
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.command_result
    assert result.event.exit_code == 1
    assert "1 failed" in result.event.summary


def test_file_change_normalizes_with_path() -> None:
    record = _raw(
        1,
        {
            "type": "file_change",
            "timestamp": "2026-08-13T10:01:05Z",
            "payload": {"path": "src/tailer.py", "change": "modified"},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.file_changed
    assert result.event.path == "src/tailer.py"


def test_file_read_normalizes_with_path() -> None:
    record = _raw(
        1,
        {
            "type": "file_read",
            "timestamp": "2026-08-13T10:01:05Z",
            "payload": {"path": "src/normalize.py"},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.file_read
    assert result.event.path == "src/normalize.py"


def test_error_normalizes_to_error() -> None:
    record = _raw(
        1,
        {
            "type": "error",
            "timestamp": "2026-08-13T10:01:05Z",
            "payload": {"message": "connection refused"},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.error


# --- unknown event types ------------------------------------------------


def test_unrecognized_type_becomes_unknown_kind_source_type_preserved() -> None:
    record = _raw(
        1,
        {
            "type": "some_future_event_type",
            "timestamp": "2026-08-13T10:01:05Z",
            "payload": {"whatever": "future-shaped"},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.unknown
    assert result.event.source_type == "some_future_event_type"


def test_malformed_json_line_becomes_unknown_and_does_not_crash() -> None:
    record = _raw(1, None, raw_line=b"not json")
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.kind == domain.EventKind.unknown
    assert result.event.source_type == "malformed"


def test_missing_timestamp_falls_back() -> None:
    record = _raw(1, {"type": "agent_message", "payload": {"text": "hi"}})
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert result.event.timestamp == FALLBACK_TS


# --- redaction and bounding before persistence --------------------------


def test_secrets_in_message_text_are_redacted() -> None:
    record = _raw(
        1,
        {
            "type": "agent_message",
            "timestamp": "2026-08-13T10:01:00Z",
            "payload": {"text": "Using token: Bearer sk-abcdefghijklmnopqrstuvwx to call the API"},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert "sk-abcdefghijklmnopqrstuvwx" not in result.event.summary
    assert "REDACTED" in result.event.summary


def test_secrets_in_command_output_are_redacted() -> None:
    record = _raw(
        1,
        {
            "type": "exec_command_end",
            "timestamp": "2026-08-13T10:01:05Z",
            "payload": {
                "command": "printenv",
                "exit_code": 0,
                "stdout_tail": "AWS_KEY=AKIAABCDEFGHIJKLMNOP",
            },
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert "AKIAABCDEFGHIJKLMNOP" not in result.event.summary


def test_long_command_output_is_bounded() -> None:
    huge_output = "x" * 10_000
    record = _raw(
        1,
        {
            "type": "exec_command_end",
            "timestamp": "2026-08-13T10:01:05Z",
            "payload": {"command": "cat bigfile", "exit_code": 0, "stdout_tail": huge_output},
        },
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert len(result.event.summary) <= 4000


def test_normalized_record_retains_payload_hash_not_raw_payload() -> None:
    record = _raw(
        1, {"type": "agent_message", "payload": {"text": "secret stuff: sk-abc123456789012345"}}
    )
    result = normalize_record("sess-1", record, fallback_timestamp=FALLBACK_TS)
    assert result is not None
    assert isinstance(result.payload_hash, str)
    assert len(result.payload_hash) == 64  # sha256 hex digest
    assert not hasattr(result, "raw_payload")


# --- logical event id identity -------------------------------------------


def test_logical_event_id_stable_for_same_ordinal_kind_payload() -> None:
    a = compute_logical_event_id("sess-1", 5, "command", "deadbeef")
    b = compute_logical_event_id("sess-1", 5, "command", "deadbeef")
    assert a == b


def test_logical_event_id_changes_with_ordinal() -> None:
    a = compute_logical_event_id("sess-1", 5, "command", "deadbeef")
    b = compute_logical_event_id("sess-1", 6, "command", "deadbeef")
    assert a != b


def test_logical_event_id_independent_of_source_locator() -> None:
    """Relocating a record to a different byte offset must not change its identity.

    This is the property copy-truncate and replay rely on: the locator
    (device/inode/offset/length) is provenance, never identity.
    """
    record_a = normalize_record(
        "sess-1",
        RawRecord(
            record_ordinal=3,
            raw_line=b'{"type": "agent_message", "payload": {"text": "hi"}}',
            parsed={"type": "agent_message", "payload": {"text": "hi"}},
            locator=SourceLocator(device=1, inode=2, byte_offset=30, record_length=10),
        ),
        fallback_timestamp=FALLBACK_TS,
    )
    record_b = normalize_record(
        "sess-1",
        RawRecord(
            record_ordinal=3,
            raw_line=b'{"type": "agent_message", "payload": {"text": "hi"}}',
            parsed={"type": "agent_message", "payload": {"text": "hi"}},
            locator=SourceLocator(device=99, inode=999, byte_offset=99999, record_length=999),
        ),
        fallback_timestamp=FALLBACK_TS,
    )
    assert record_a is not None and record_b is not None
    assert record_a.event.id == record_b.event.id


def test_logical_event_id_does_not_depend_on_prior_duplicate_count() -> None:
    """Regression test for the relative-ordinal bug this design replaces.

    A relative ordinal derived from "how many identical records came
    before" breaks in both directions when recovery restarts partway
    through a run of duplicates. Because record_ordinal here is the
    absolute position from file start (supplied by the tailer, not derived
    from content repetition), replaying the same absolute ordinal always
    reproduces the same id regardless of how many identical records exist
    elsewhere in the file.
    """
    payload = {"type": "agent_message", "payload": {"text": "same text twice"}}
    raw_line = b'{"type": "agent_message", "payload": {"text": "same text twice"}}'

    first_pass_ordinal_5 = normalize_record(
        "sess-1",
        RawRecord(5, raw_line, payload, SourceLocator(device=1, inode=2, byte_offset=50)),
        fallback_timestamp=FALLBACK_TS,
    )
    replayed_ordinal_5 = normalize_record(
        "sess-1",
        RawRecord(5, raw_line, payload, SourceLocator(device=1, inode=2, byte_offset=50)),
        fallback_timestamp=FALLBACK_TS,
    )
    different_ordinal_9 = normalize_record(
        "sess-1",
        RawRecord(9, raw_line, payload, SourceLocator(device=1, inode=2, byte_offset=90)),
        fallback_timestamp=FALLBACK_TS,
    )
    assert first_pass_ordinal_5 is not None
    assert replayed_ordinal_5 is not None
    assert different_ordinal_9 is not None
    assert first_pass_ordinal_5.event.id == replayed_ordinal_5.event.id
    assert first_pass_ordinal_5.event.id != different_ordinal_9.event.id
