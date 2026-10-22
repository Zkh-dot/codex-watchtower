"""Convert raw rollout records into normalized, redacted events (spec 5.3).

The event's public ``id`` *is* its logical event id -- a content-addressed
hash of ``(session_id, record_ordinal, kind, payload_hash)`` -- rather than
a separate identifier layered on top. This keeps identity uniform end to
end: the same value is the repository's dedup key, the packet's citable
event id, and what an assessment's evidence references resolve against.

``record_ordinal`` is the record's absolute zero-based position from the
start of the session file (spec 5.2); it is supplied by the tailer, not
computed here.

Legacy flattened records and the persisted response_item/event_msg envelopes
are both supported. Current envelope shapes were inspected in Codex 0.163.0
rollouts; the committed fixture replaces their contents with synthetic data.
Unknown types remain visible as bounded unknown events rather than crashing ingestion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from codex_watchtower import domain
from codex_watchtower.codex.tailer import RawRecord
from codex_watchtower.privacy.redact import bound_text, redact_text

MAX_SUMMARY_CHARS = 4000
MAX_COMMAND_EXCERPT_CHARS = 2000
MAX_PATH_CHARS = 512

# session_meta is session-level metadata consumed by discovery.py; it is not
# part of the normalized event stream and produces no WatchtowerEvent.
_IGNORED_TYPES = {"session_meta", "turn_context", "token_usage_record", "world_state"}

_KNOWN_TYPES: dict[str, domain.EventKind] = {
    "turn_started": domain.EventKind.turn_lifecycle,
    "turn_complete": domain.EventKind.turn_lifecycle,
    "agent_message": domain.EventKind.message,
    "reasoning": domain.EventKind.reasoning,
    "exec_command_begin": domain.EventKind.command,
    "exec_command_end": domain.EventKind.command_result,
    "file_change": domain.EventKind.file_changed,
    "file_read": domain.EventKind.file_read,
    "test_result": domain.EventKind.test_result,
    "error": domain.EventKind.error,
}


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    event: domain.Event
    payload_hash: str


def compute_logical_event_id(
    session_id: str, record_ordinal: int, kind: str, payload_hash: str
) -> str:
    canonical = f"{session_id}:{record_ordinal}:{kind}:{payload_hash}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _str_field(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _optional_str_field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _valid_timestamp(ts: str, fallback: str) -> str:
    """Return ts if it parses as RFC 3339, otherwise the fallback."""
    try:
        parsed = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
        if parsed.tzinfo is None or len(ts) > 40:
            return fallback
        return ts
    except (ValueError, IndexError):
        return fallback


def _optional_int_field(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _redacted_bounded(text: str, max_length: int) -> str:
    redacted = redact_text(text).text
    bounded, _ = bound_text(redacted, max_length)
    return bounded


def normalize_record(
    session_id: str, record: RawRecord, *, fallback_timestamp: str
) -> NormalizedRecord | None:
    """Normalize one raw record, or return None if it is not an event.

    ``fallback_timestamp`` is used only when the record itself lacks a
    usable timestamp (missing, malformed, or the record is not valid JSON).
    """
    payload_hash = hashlib.sha256(record.raw_line).hexdigest()

    if record.parsed is None:
        return _build(
            session_id=session_id,
            record_ordinal=record.record_ordinal,
            payload_hash=payload_hash,
            kind=domain.EventKind.unknown,
            timestamp=fallback_timestamp,
            summary=_redacted_bounded(
                f"malformed record at ordinal {record.record_ordinal}", MAX_SUMMARY_CHARS
            ),
            source_type="malformed",
        )

    raw_type = record.parsed.get("type")
    if isinstance(raw_type, str) and raw_type in _IGNORED_TYPES:
        return None

    payload_any = record.parsed.get("payload")
    payload: dict[str, Any] = payload_any if isinstance(payload_any, dict) else {}
    timestamp = _valid_timestamp(
        _optional_str_field(record.parsed, "timestamp") or "", fallback_timestamp
    )

    if isinstance(raw_type, str) and raw_type in {"response_item", "event_msg", "compacted"}:
        return _normalize_envelope(session_id, record, payload, timestamp, payload_hash)

    kind = _KNOWN_TYPES.get(raw_type) if isinstance(raw_type, str) else None
    source_type = raw_type if isinstance(raw_type, str) else None

    if kind is None:
        return _build(
            session_id=session_id,
            record_ordinal=record.record_ordinal,
            payload_hash=payload_hash,
            kind=domain.EventKind.unknown,
            timestamp=timestamp,
            summary=_redacted_bounded(f"unrecognized event type: {raw_type!r}", MAX_SUMMARY_CHARS),
            source_type=source_type,
        )

    path: str | None = None
    exit_code: int | None = None

    if kind == domain.EventKind.turn_lifecycle:
        summary = "Turn started" if raw_type == "turn_started" else "Turn complete"
    elif kind in (domain.EventKind.message, domain.EventKind.reasoning):
        summary = _redacted_bounded(_str_field(payload, "text"), MAX_SUMMARY_CHARS)
    elif kind == domain.EventKind.command:
        command = _redacted_bounded(_str_field(payload, "command"), MAX_COMMAND_EXCERPT_CHARS)
        summary = _redacted_bounded(f"$ {command}", MAX_SUMMARY_CHARS)
        source_type = "shell"
    elif kind == domain.EventKind.command_result:
        command = _redacted_bounded(_str_field(payload, "command"), MAX_COMMAND_EXCERPT_CHARS)
        exit_code = _optional_int_field(payload, "exit_code")
        output = _redacted_bounded(_str_field(payload, "stdout_tail"), MAX_COMMAND_EXCERPT_CHARS)
        summary = _redacted_bounded(f"$ {command} -> exit {exit_code}: {output}", MAX_SUMMARY_CHARS)
        source_type = "shell"
    elif kind == domain.EventKind.file_changed:
        raw_path = _optional_str_field(payload, "path")
        path = bound_text(raw_path, MAX_PATH_CHARS)[0] if raw_path else None
        change = _optional_str_field(payload, "change") or "modified"
        summary = _redacted_bounded(f"{change}: {path or 'unknown path'}", MAX_SUMMARY_CHARS)
    elif kind == domain.EventKind.file_read:
        raw_path = _optional_str_field(payload, "path")
        path = bound_text(raw_path, MAX_PATH_CHARS)[0] if raw_path else None
        summary = _redacted_bounded(f"read: {path or 'unknown path'}", MAX_SUMMARY_CHARS)
    elif kind == domain.EventKind.test_result:
        scalar_parts = [
            f"{k}={v}" for k, v in sorted(payload.items()) if isinstance(v, str | int | bool)
        ]
        summary = _redacted_bounded(", ".join(scalar_parts) or "test result", MAX_SUMMARY_CHARS)
    elif kind == domain.EventKind.error:
        summary = _redacted_bounded(_str_field(payload, "message"), MAX_SUMMARY_CHARS)
    else:  # pragma: no cover - exhaustive over _KNOWN_TYPES values
        summary = ""

    return _build(
        session_id=session_id,
        record_ordinal=record.record_ordinal,
        payload_hash=payload_hash,
        kind=kind,
        timestamp=timestamp,
        summary=summary,
        path=path,
        exit_code=exit_code,
        source_type=source_type,
    )


def _build(
    *,
    session_id: str,
    record_ordinal: int,
    payload_hash: str,
    kind: domain.EventKind,
    timestamp: str,
    summary: str,
    path: str | None = None,
    exit_code: int | None = None,
    source_type: str | None = None,
) -> NormalizedRecord:
    event_id = compute_logical_event_id(session_id, record_ordinal, kind.value, payload_hash)
    event = domain.Event(
        id=event_id,
        timestamp=timestamp,
        kind=kind,
        summary=summary,
        path=path,
        exit_code=exit_code,
        source_type=source_type,
    )
    return NormalizedRecord(event=event, payload_hash=payload_hash)


def _normalize_envelope(
    session_id: str, record: RawRecord, payload: dict[str, Any], timestamp: str, payload_hash: str
) -> NormalizedRecord | None:
    """Unwrap persisted CLI events; encrypted compaction content is never evidence."""
    envelope = str((record.parsed or {}).get("type"))
    subtype = str(payload.get("type", envelope))
    kind = domain.EventKind.unknown
    source = f"{envelope}:{subtype}"[:128]
    exit_code = None
    if envelope == "compacted" or subtype == "compaction":
        text = "Compaction boundary; encrypted or omitted history is unavailable."
    elif envelope == "response_item":
        if subtype == "message":
            kind = domain.EventKind.message
            source = str(payload.get("role", "unknown"))[:128]
            content = payload.get("content", [])
            text = (
                "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
                if isinstance(content, list)
                else str(content)
            )
        elif subtype in {"function_call", "custom_tool_call"}:
            kind = domain.EventKind.command
            text = f"{payload.get('name')} [{payload.get('call_id')}]: " + str(
                payload.get("arguments", payload.get("input", ""))
            )
        elif subtype in {"function_call_output", "custom_tool_call_output"}:
            kind = domain.EventKind.command_result
            text = f"Result [{payload.get('call_id')}]: " + json.dumps(
                payload.get("output"), ensure_ascii=False
            )
        elif subtype == "reasoning":
            kind = domain.EventKind.reasoning
            text = json.dumps(payload.get("summary", []), ensure_ascii=False)
        else:
            text = f"Unknown response item: {subtype}"
    elif subtype in {"task_started", "task_complete", "turn_aborted"}:
        kind = domain.EventKind.turn_lifecycle
        source = "turn_started" if subtype == "task_started" else "turn_complete"
        text = f"{subtype}: {payload.get('last_agent_message', '')}"
    elif subtype in {"token_count", "thread_settings_applied"}:
        return None
    elif subtype == "item_completed":
        item = payload.get("item", {})
        if not isinstance(item, dict):
            item = {}
        item_type = str(item.get("type"))
        # Messages are also persisted as response_items; avoid duplicate prose.
        if item_type in {"UserMessage", "AgentMessage", "Reasoning"}:
            return None
        if item_type == "CommandExecution":
            kind = domain.EventKind.command_result
            source = "shell"
            code = item.get("exit_code")
            exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
            if exit_code is not None and not -256 <= exit_code <= 65535:
                exit_code = None
            text = (
                f"$ {item.get('command')} -> exit {exit_code}: {item.get('aggregated_output', '')}"
            )
        elif item_type == "FileChange":
            kind = domain.EventKind.file_changed
            text = json.dumps(item.get("changes", {}), ensure_ascii=False)
        elif item_type in {"Plan", "plan"}:
            kind = domain.EventKind.message
            source = "plan"
            text = json.dumps(item, ensure_ascii=False)
        else:
            text = f"Unknown completed item: {item_type}"
    elif subtype in {"agent_message", "user_message"}:
        kind = domain.EventKind.message
        source = "user" if subtype == "user_message" else "assistant"
        text = str(payload.get("message", ""))
    else:
        text = f"Unrecognized event: {subtype}"
    redacted = redact_text(text).text
    if len(redacted) > MAX_SUMMARY_CHARS:
        redacted = redacted[: MAX_SUMMARY_CHARS - 30] + "\n[TRUNCATED event excerpt]"
    return _build(
        session_id=session_id,
        record_ordinal=record.record_ordinal,
        payload_hash=payload_hash,
        kind=kind,
        timestamp=timestamp,
        summary=redacted,
        source_type=source,
        exit_code=exit_code,
    )
