"""Typed domain model matching the committed JSON Schema contracts.

Field-level shape (types, enums, length/count bounds) mirrors
``schemas/*.json`` and is enforced by Pydantic at construction time.
Cross-field and cross-collection invariants that JSON Schema's `if`/`then`
cannot express -- because they compare two sibling values, recompute a
digest, or resolve a reference against a sibling collection -- are listed
in specification section 5.3 and are each enforced by a validator here,
with a dedicated test in tests/unit/test_domain.py.

Everything else the schema *can* express (the state/status/notification_status
projection, fatal/identity_broken coupling, signal-kind-to-notification-status
matching) is authoritative in ``schemas/reconciled_assessment.schema.json``
and is exercised via ``codex_watchtower.schemas.validate`` rather than
re-implemented here by hand, so the two cannot drift apart silently.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

# --- enums -------------------------------------------------------------


class SessionState(StrEnum):
    active_turn = "active_turn"
    between_turns = "between_turns"
    waiting = "waiting"
    terminal_completed = "terminal_completed"
    terminal_failed = "terminal_failed"
    idle = "idle"
    identity_broken = "identity_broken"
    unknown = "unknown"


class AssessmentStatus(StrEnum):
    progressing = "progressing"
    investigating = "investigating"
    between_turns = "between_turns"
    waiting = "waiting"
    stalled = "stalled"
    looping = "looping"
    off_scope = "off_scope"
    terminal_completed = "terminal_completed"
    terminal_failed = "terminal_failed"
    idle = "idle"
    unknown = "unknown"


class NotificationStatus(StrEnum):
    progressing = "progressing"
    between_turns = "between_turns"
    waiting = "waiting"
    stalled = "stalled"
    looping = "looping"
    off_scope = "off_scope"
    terminal_completed = "terminal_completed"
    terminal_failed = "terminal_failed"
    idle = "idle"
    identity_broken = "identity_broken"
    unknown = "unknown"


class GoalAlignment(StrEnum):
    aligned = "aligned"
    possibly_aligned = "possibly_aligned"
    misaligned = "misaligned"
    unknown = "unknown"


class AssessedBy(StrEnum):
    luna = "luna"
    terra = "terra"
    rules = "rules"


class EventKind(StrEnum):
    message = "message"
    reasoning = "reasoning"
    command = "command"
    command_result = "command_result"
    file_read = "file_read"
    file_changed = "file_changed"
    test_result = "test_result"
    error = "error"
    turn_lifecycle = "turn_lifecycle"
    process_lifecycle = "process_lifecycle"
    unknown = "unknown"


class SignalKind(StrEnum):
    repeated_command = "repeated_command"
    recurring_error = "recurring_error"
    stagnation = "stagnation"
    scope_expansion = "scope_expansion"
    forbidden_path = "forbidden_path"
    waiting = "waiting"
    turn_failed = "turn_failed"
    process_failed = "process_failed"
    test_regression = "test_regression"
    context_growth = "context_growth"
    agentlens_finding = "agentlens_finding"
    dependency_unavailable = "dependency_unavailable"
    other = "other"


class Severity(StrEnum):
    info = "info"
    warning = "warning"
    critical = "critical"


class SignalSource(StrEnum):
    local_rule = "local_rule"
    agentlens = "agentlens"
    system = "system"


class Freshness(StrEnum):
    current = "current"
    stale = "stale"
    unknown = "unknown"


class RefType(StrEnum):
    event = "event"
    signal = "signal"
    system = "system"


class ConcernKind(StrEnum):
    loop = "loop"
    stagnation = "stagnation"
    scope = "scope"
    tests = "tests"
    errors = "errors"
    waiting = "waiting"
    resource = "resource"
    uncertainty = "uncertainty"
    infrastructure = "infrastructure"
    other = "other"


class FatalReason(StrEnum):
    prefix_mismatch = "prefix_mismatch"
    status_epoch_exhausted = "status_epoch_exhausted"
    attention_epoch_exhausted = "attention_epoch_exhausted"
    execution_epoch_exhausted = "execution_epoch_exhausted"
    report_version_exhausted = "report_version_exhausted"


# --- shared helpers ------------------------------------------------------

_STRICT_MODEL = ConfigDict(extra="forbid", validate_assignment=True)

PayloadValue = StrictStr | StrictInt | StrictBool | None


def _parse_rfc3339(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not an RFC 3339 timestamp: {value!r}") from exc


def _check_run_pair(run_id: str | None, execution_epoch: int | None) -> None:
    if (run_id is None) != (execution_epoch is None):
        raise ValueError("run_id and execution_epoch must both be null or both be set")


def canonical_signal_fingerprint(active_signals: list[ActiveSignal]) -> str:
    """Stable digest of the sorted (severity, kind, id) triples.

    This is the "signal_fingerprint equal to the canonical digest of
    active_signals" invariant from spec section 5.3/5.10, which JSON Schema
    cannot recompute. The exact digest algorithm is internal to Watchtower;
    what matters is that it is deterministic and that the reconciler and
    validator agree on it, which this shared function guarantees.
    """
    triples = sorted((s.severity.value, s.kind.value, s.id) for s in active_signals)
    canonical = json.dumps(triples, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- leaf models -----------------------------------------------------------


class Evidence(BaseModel):
    model_config = _STRICT_MODEL

    ref_type: RefType
    ref_id: Annotated[str, Field(min_length=1, max_length=128)]
    claim: Annotated[str, Field(min_length=1, max_length=500)]


class Concern(BaseModel):
    model_config = _STRICT_MODEL

    severity: Severity
    kind: ConcernKind
    explanation: Annotated[str, Field(min_length=1, max_length=1000)]
    evidence_refs: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128)]],
        Field(min_length=1, max_length=12),
    ]

    @field_validator("evidence_refs")
    @classmethod
    def _unique_refs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must be unique")
        return value


class SystemRef(BaseModel):
    model_config = _STRICT_MODEL

    id: Annotated[str, Field(pattern=r"^sys:[a-z0-9_.-]+$", max_length=128)]
    summary: Annotated[str, Field(min_length=1, max_length=1000)]


class Signal(BaseModel):
    model_config = _STRICT_MODEL

    id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: SignalKind
    severity: Severity
    source: SignalSource
    event_ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=64)
    ]
    observed_at: Annotated[str, Field(max_length=40)]
    freshness: Freshness
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    payload: dict[str, PayloadValue]

    @field_validator("observed_at")
    @classmethod
    def _rfc3339(cls, value: str) -> str:
        _parse_rfc3339(value)
        return value

    @field_validator("event_ids")
    @classmethod
    def _unique_event_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("event_ids must be unique")
        return value

    @field_validator("payload")
    @classmethod
    def _bounded_payload(cls, value: dict[str, PayloadValue]) -> dict[str, PayloadValue]:
        if len(value) > 24:
            raise ValueError("payload must have at most 24 entries")
        for v in value.values():
            if isinstance(v, str) and len(v) > 500:
                raise ValueError("payload string values must be at most 500 characters")
            if isinstance(v, int) and not isinstance(v, bool):
                if not (-1_000_000_000_000 <= v <= 1_000_000_000_000):
                    raise ValueError("payload integer values must fit the committed bound")
        return value


class ActiveSignal(BaseModel):
    model_config = _STRICT_MODEL

    id: Annotated[str, Field(min_length=1, max_length=128)]
    kind: SignalKind
    severity: Severity


class Event(BaseModel):
    model_config = _STRICT_MODEL

    id: Annotated[str, Field(min_length=1, max_length=128)]
    timestamp: Annotated[str, Field(max_length=40)]
    kind: EventKind
    summary: Annotated[str, Field(max_length=4000)] = ""
    path: Annotated[str | None, Field(max_length=512)] = None
    exit_code: Annotated[int | None, Field(ge=-256, le=65535)] = None
    source_type: Annotated[str | None, Field(max_length=128)] = None
    run_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    execution_epoch: Annotated[int | None, Field(ge=0, le=100_000)] = None

    @field_validator("timestamp")
    @classmethod
    def _rfc3339(cls, value: str) -> str:
        _parse_rfc3339(value)
        return value

    @model_validator(mode="after")
    def _run_pair(self) -> Event:
        _check_run_pair(self.run_id, self.execution_epoch)
        return self

    @model_validator(mode="after")
    def _process_lifecycle_requires_evidence(self) -> Event:
        if self.kind == EventKind.process_lifecycle:
            if self.run_id is None or self.execution_epoch is None or self.exit_code is None:
                raise ValueError(
                    "process_lifecycle events must carry run_id, execution_epoch, and exit_code"
                )
        return self


# --- assessment --------------------------------------------------------


class Assessment(BaseModel):
    model_config = _STRICT_MODEL

    schema_version: Literal["1.0"] = "1.0"
    status: AssessmentStatus
    current_action: Annotated[str, Field(min_length=1, max_length=500)]
    goal_alignment: GoalAlignment
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=12)]
    basis_ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128)]],
        Field(min_length=1, max_length=12),
    ]
    concerns: Annotated[list[Concern], Field(max_length=8)] = Field(default_factory=list)
    needs_attention: StrictBool
    recommended_human_action: Annotated[str | None, Field(max_length=1000)] = None
    confidence_percent: Annotated[StrictInt, Field(ge=0, le=100)]
    assessed_by: AssessedBy
    escalation_reason: Annotated[str | None, Field(max_length=500)] = None
    event_cursor: Annotated[StrictInt | None, Field(ge=0, le=1_000_000_000)] = None
    assessed_at: Annotated[str, Field(max_length=40)]

    @field_validator("assessed_at")
    @classmethod
    def _rfc3339(cls, value: str) -> str:
        _parse_rfc3339(value)
        return value

    @field_validator("basis_ids")
    @classmethod
    def _unique_basis_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("basis_ids must be unique")
        return value

    @model_validator(mode="after")
    def _basis_ids_resolve_to_evidence(self) -> Assessment:
        evidence_ids = {e.ref_id for e in self.evidence}
        missing = [b for b in self.basis_ids if b not in evidence_ids]
        if missing:
            raise ValueError(f"basis_ids not present in evidence[].ref_id: {missing}")
        return self


def validate_evidence_against_packet(assessment: Assessment, observation: Observation) -> list[str]:
    """Return unresolved evidence ref_ids, or [] if every citation resolves.

    Implements the spec 5.3 invariant "every assessment evidence ref_id
    resolving to a packet event, signal, or system_refs ID". This must be
    checked against the packet the assessment was produced from, so it is a
    function over both objects rather than a validator on Assessment alone.
    """
    event_ids = {e.id for e in observation.events}
    signal_ids = {s.id for s in observation.signals}
    system_ids = {r.id for r in observation.system_refs}
    by_type = {
        RefType.event: event_ids,
        RefType.signal: signal_ids,
        RefType.system: system_ids,
    }
    unresolved = []
    for ev in assessment.evidence:
        if ev.ref_id not in by_type[ev.ref_type]:
            unresolved.append(ev.ref_id)
    return unresolved


# --- observation ---------------------------------------------------------


class SessionInfo(BaseModel):
    model_config = _STRICT_MODEL

    id: Annotated[str, Field(min_length=1, max_length=200)]
    workspace: Annotated[str, Field(max_length=512)] = ""
    started_at: Annotated[str, Field(max_length=40)]
    elapsed_seconds: Annotated[int, Field(ge=0, le=31_536_000)]
    state: SessionState
    model: Annotated[str | None, Field(max_length=200)] = None

    @field_validator("started_at")
    @classmethod
    def _rfc3339(cls, value: str) -> str:
        _parse_rfc3339(value)
        return value


class Goal(BaseModel):
    model_config = _STRICT_MODEL

    text: Annotated[str, Field(min_length=1, max_length=8000)]
    expected_paths: Annotated[list[Annotated[str, Field(max_length=512)]], Field(max_length=64)] = (
        Field(default_factory=list)
    )
    forbidden_paths: Annotated[
        list[Annotated[str, Field(max_length=512)]], Field(max_length=64)
    ] = Field(default_factory=list)
    acceptance_criteria: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=32)
    ]

    @field_validator("expected_paths", "forbidden_paths")
    @classmethod
    def _unique_paths(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("paths must be unique")
        return value


class Window(BaseModel):
    model_config = _STRICT_MODEL

    from_cursor: Annotated[StrictInt | None, Field(ge=0, le=1_000_000_000)] = None
    to_cursor: Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
    opened_at: Annotated[str, Field(max_length=40)]
    closed_at: Annotated[str, Field(max_length=40)]

    @field_validator("opened_at", "closed_at")
    @classmethod
    def _rfc3339(cls, value: str) -> str:
        _parse_rfc3339(value)
        return value

    @model_validator(mode="after")
    def _cursor_order(self) -> Window:
        if self.from_cursor is not None and not (self.from_cursor < self.to_cursor):
            raise ValueError("from_cursor must be strictly less than to_cursor")
        return self

    @model_validator(mode="after")
    def _time_order(self) -> Window:
        if _parse_rfc3339(self.opened_at) > _parse_rfc3339(self.closed_at):
            raise ValueError("opened_at must be <= closed_at")
        return self


class Evicted(BaseModel):
    model_config = _STRICT_MODEL

    file_read: Annotated[int, Field(ge=0, le=100_000)] = 0
    reasoning: Annotated[int, Field(ge=0, le=100_000)] = 0
    command_output: Annotated[int, Field(ge=0, le=100_000)] = 0
    events: Annotated[int, Field(ge=0, le=100_000)] = 0


class Truncation(BaseModel):
    model_config = _STRICT_MODEL

    budget_characters: Annotated[int, Field(ge=1, le=10_000_000)]
    used_characters: Annotated[int, Field(ge=0, le=10_000_000)]
    evicted: Evicted

    @model_validator(mode="after")
    def _within_budget(self) -> Truncation:
        if self.used_characters > self.budget_characters:
            raise ValueError("used_characters must be <= budget_characters")
        return self


class Observation(BaseModel):
    model_config = _STRICT_MODEL

    schema_version: Literal["1.0"] = "1.0"
    session: SessionInfo
    goal: Goal
    window: Window
    previous_assessment: Assessment | None
    events: Annotated[list[Event], Field(max_length=200)]
    signals: Annotated[list[Signal], Field(max_length=64)]
    system_refs: Annotated[list[SystemRef], Field(max_length=32)]
    redactions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=32)
    ]
    truncation: Truncation

    @field_validator("system_refs")
    @classmethod
    def _unique_system_refs(cls, value: list[SystemRef]) -> list[SystemRef]:
        ids = [r.id for r in value]
        if len(set(ids)) != len(ids):
            raise ValueError("system_refs ids must be unique")
        return value

    @field_validator("redactions")
    @classmethod
    def _unique_redactions(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("redactions must be unique")
        return value

    @model_validator(mode="after")
    def _unique_event_ids(self) -> Observation:
        ids = [e.id for e in self.events]
        if len(set(ids)) != len(ids):
            raise ValueError("event ids must be unique within a packet")
        return self

    @model_validator(mode="after")
    def _events_within_window(self) -> Observation:
        opened = _parse_rfc3339(self.window.opened_at)
        closed = _parse_rfc3339(self.window.closed_at)
        for event in self.events:
            ts = _parse_rfc3339(event.timestamp)
            if not (opened <= ts <= closed):
                raise ValueError(
                    f"event {event.id} timestamp {event.timestamp} falls outside "
                    f"window [{self.window.opened_at}, {self.window.closed_at}]"
                )
        return self

    @model_validator(mode="after")
    def _signal_event_ids_resolve(self) -> Observation:
        packet_event_ids = {e.id for e in self.events}
        for signal in self.signals:
            missing = [eid for eid in signal.event_ids if eid not in packet_event_ids]
            if missing:
                raise ValueError(
                    f"signal {signal.id} references event ids not present in packet: {missing}"
                )
        return self


# --- reconciled assessment ------------------------------------------------


class Fatal(BaseModel):
    model_config = _STRICT_MODEL

    reason: FatalReason
    detected_at: Annotated[str, Field(max_length=40)]
    detail: Annotated[str | None, Field(max_length=500)] = None

    @field_validator("detected_at")
    @classmethod
    def _rfc3339(cls, value: str) -> str:
        _parse_rfc3339(value)
        return value


class Report(BaseModel):
    model_config = _STRICT_MODEL

    report_version: Annotated[int, Field(ge=1, le=100_000)]
    provisional: StrictBool
    supersedes: Annotated[int | None, Field(ge=1, le=100_000)] = None

    @model_validator(mode="after")
    def _supersedes_order(self) -> Report:
        if self.supersedes is not None and not (self.supersedes < self.report_version):
            raise ValueError("supersedes must be strictly less than report_version")
        if self.report_version == 1 and self.supersedes is not None:
            raise ValueError("the first report (version 1) cannot supersede anything")
        return self


# State -> allowed {status, notification_status} projection. Encodes the
# table in spec section 5.8; also expressed (more completely, including the
# fatal/signal-kind coupling this table omits) in
# schemas/reconciled_assessment.schema.json's allOf block, which is what
# ReconciledAssessment.validate_against_schema exercises.
STATUS_PROJECTION: dict[SessionState, frozenset[AssessmentStatus]] = {
    SessionState.active_turn: frozenset(
        {
            AssessmentStatus.progressing,
            AssessmentStatus.investigating,
            AssessmentStatus.stalled,
            AssessmentStatus.looping,
            AssessmentStatus.off_scope,
        }
    ),
    SessionState.between_turns: frozenset({AssessmentStatus.between_turns}),
    SessionState.waiting: frozenset({AssessmentStatus.waiting}),
    SessionState.terminal_completed: frozenset({AssessmentStatus.terminal_completed}),
    SessionState.terminal_failed: frozenset({AssessmentStatus.terminal_failed}),
    SessionState.idle: frozenset({AssessmentStatus.idle}),
    SessionState.identity_broken: frozenset({AssessmentStatus.unknown}),
    SessionState.unknown: frozenset({AssessmentStatus.unknown}),
}

NOTIFICATION_PROJECTION: dict[SessionState, frozenset[NotificationStatus]] = {
    SessionState.active_turn: frozenset(
        {
            NotificationStatus.progressing,
            NotificationStatus.stalled,
            NotificationStatus.looping,
            NotificationStatus.off_scope,
        }
    ),
    SessionState.between_turns: frozenset({NotificationStatus.between_turns}),
    SessionState.waiting: frozenset({NotificationStatus.waiting}),
    SessionState.terminal_completed: frozenset({NotificationStatus.terminal_completed}),
    SessionState.terminal_failed: frozenset({NotificationStatus.terminal_failed}),
    SessionState.idle: frozenset({NotificationStatus.idle}),
    SessionState.identity_broken: frozenset({NotificationStatus.identity_broken}),
    SessionState.unknown: frozenset({NotificationStatus.unknown}),
}


class ReconciledAssessment(BaseModel):
    model_config = _STRICT_MODEL

    schema_version: Literal["1.0"] = "1.0"
    session_id: Annotated[str, Field(min_length=1, max_length=200)]
    run_id: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    execution_epoch: Annotated[int | None, Field(ge=0, le=100_000)] = None
    state: SessionState
    status: AssessmentStatus
    notification_status: NotificationStatus
    status_epoch: Annotated[int, Field(ge=0, le=1_000_000)]
    attention_epoch: Annotated[int, Field(ge=0, le=1_000_000)]
    fatal: Fatal | None = None
    needs_attention: StrictBool
    active_signals: Annotated[list[ActiveSignal], Field(max_length=64)]
    signal_fingerprint: Annotated[str, Field(min_length=1, max_length=128)]
    model_assessment: Assessment | None
    report: Report | None
    event_cursor: Annotated[StrictInt | None, Field(ge=0, le=1_000_000_000)] = None
    reconciled_at: Annotated[str, Field(max_length=40)]

    @field_validator("reconciled_at")
    @classmethod
    def _rfc3339(cls, value: str) -> str:
        _parse_rfc3339(value)
        return value

    @field_validator("active_signals")
    @classmethod
    def _unique_active_signals(cls, value: list[ActiveSignal]) -> list[ActiveSignal]:
        ids = [s.id for s in value]
        if len(set(ids)) != len(ids):
            raise ValueError("active_signals ids must be unique")
        return value

    @model_validator(mode="after")
    def _run_pair(self) -> ReconciledAssessment:
        _check_run_pair(self.run_id, self.execution_epoch)
        return self

    @model_validator(mode="after")
    def _status_projection(self) -> ReconciledAssessment:
        if self.status not in STATUS_PROJECTION[self.state]:
            raise ValueError(
                f"status {self.status} is not permitted for state {self.state}; "
                f"allowed: {sorted(s.value for s in STATUS_PROJECTION[self.state])}"
            )
        if self.notification_status not in NOTIFICATION_PROJECTION[self.state]:
            raise ValueError(
                f"notification_status {self.notification_status} is not permitted for "
                f"state {self.state}; allowed: "
                f"{sorted(s.value for s in NOTIFICATION_PROJECTION[self.state])}"
            )
        return self

    @model_validator(mode="after")
    def _fingerprint_matches_digest(self) -> ReconciledAssessment:
        expected = canonical_signal_fingerprint(self.active_signals)
        if self.signal_fingerprint != expected:
            raise ValueError(
                f"signal_fingerprint {self.signal_fingerprint!r} does not match the "
                f"canonical digest of active_signals ({expected!r})"
            )
        return self

    def validate_against_schema(self) -> None:
        """Exercise the schema's allOf projection this model does not restate.

        Covers fatal/identity_broken coupling, notification_status-to-signal-kind
        matching, critical-signal-forces-needs_attention, report provisionality,
        and the *_exhausted-reason-matches-counter-at-max rules -- all of which
        are already correctly expressed in
        schemas/reconciled_assessment.schema.json and are not duplicated here.
        """
        from codex_watchtower.schemas import validate

        validate("reconciled_assessment", self.model_dump(mode="json"))
