from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from codex_watchtower import domain, schemas

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(*parts: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(FIXTURES.joinpath(*parts).read_text())
    return result


def _healthy_observation() -> dict[str, Any]:
    return copy.deepcopy(_load("observations", "healthy.json"))


def _healthy_assessment() -> dict[str, Any]:
    return copy.deepcopy(_load("assessments", "healthy.json"))


def _healthy_reconciled() -> dict[str, Any]:
    return copy.deepcopy(_load("reconciled", "healthy.json"))


# --- basic construction, enums, bounds ------------------------------------


def test_enum_values_round_trip() -> None:
    event = domain.Event(
        id="e1", timestamp="2026-08-13T10:00:00Z", kind=domain.EventKind.command, summary="ls"
    )
    assert event.model_dump(mode="json")["kind"] == "command"


@pytest.mark.parametrize("value", [-1, 101, 1000])
def test_confidence_percent_out_of_bounds_rejected(value: int) -> None:
    payload = _healthy_assessment()
    payload["confidence_percent"] = value
    with pytest.raises(ValidationError):
        domain.Assessment.model_validate(payload)


def test_confidence_percent_rejects_float() -> None:
    payload = _healthy_assessment()
    payload["confidence_percent"] = 82.5
    with pytest.raises(ValidationError):
        domain.Assessment.model_validate(payload)


def test_confidence_percent_accepts_boundary_values() -> None:
    for value in (0, 100):
        payload = _healthy_assessment()
        payload["confidence_percent"] = value
        domain.Assessment.model_validate(payload)


def test_signal_payload_rejects_float_value() -> None:
    payload = _healthy_observation()
    payload["signals"] = [
        {
            "id": "sig:1",
            "kind": "context_growth",
            "severity": "info",
            "source": "agentlens",
            "event_ids": [],
            "observed_at": "2026-08-13T10:00:30Z",
            "freshness": "current",
            "summary": "context growth ratio",
            "payload": {"ratio": 0.42},
        }
    ]
    with pytest.raises(ValidationError):
        domain.Observation.model_validate(payload)


def test_signal_payload_rejects_too_many_entries() -> None:
    payload = _healthy_observation()
    payload["signals"] = [
        {
            "id": "sig:1",
            "kind": "context_growth",
            "severity": "info",
            "source": "agentlens",
            "event_ids": [],
            "observed_at": "2026-08-13T10:00:30Z",
            "freshness": "current",
            "summary": "too many keys",
            "payload": {f"k{i}": i for i in range(25)},
        }
    ]
    with pytest.raises(ValidationError):
        domain.Observation.model_validate(payload)


def test_evidence_ref_type_and_bounds_enforced() -> None:
    payload = _healthy_assessment()
    payload["evidence"] = []
    with pytest.raises(ValidationError):
        domain.Assessment.model_validate(payload)


def test_serialized_observation_validates_against_schema() -> None:
    obs = domain.Observation.model_validate(_healthy_observation())
    schemas.validate("observation", obs.model_dump(mode="json"))


def test_serialized_assessment_validates_against_schema() -> None:
    asm = domain.Assessment.model_validate(_healthy_assessment())
    schemas.validate("assessment", asm.model_dump(mode="json"))


# --- domain invariants listed in spec section 5.3 as not JSON-Schema-expressible ---


def test_invariant_rfc3339_timestamps_rejected_when_malformed() -> None:
    payload = _healthy_observation()
    payload["window"]["opened_at"] = "not-a-timestamp"
    with pytest.raises(ValidationError):
        domain.Observation.model_validate(payload)


def test_invariant_event_timestamp_must_fall_inside_window() -> None:
    payload = _healthy_observation()
    payload["events"][0]["timestamp"] = "2026-08-13T09:00:00Z"  # before window.opened_at
    with pytest.raises(ValidationError, match="outside window"):
        domain.Observation.model_validate(payload)


def test_invariant_event_ids_unique_within_packet() -> None:
    payload = _healthy_observation()
    duplicate = copy.deepcopy(payload["events"][0])
    payload["events"].append(duplicate)
    with pytest.raises(ValidationError, match="unique"):
        domain.Observation.model_validate(payload)


def test_invariant_opened_at_must_not_exceed_closed_at() -> None:
    payload = _healthy_observation()
    payload["window"]["opened_at"] = "2026-08-13T11:00:00Z"
    payload["window"]["closed_at"] = "2026-08-13T10:00:00Z"
    with pytest.raises(ValidationError, match="opened_at"):
        domain.Observation.model_validate(payload)


def test_invariant_from_cursor_must_be_less_than_to_cursor() -> None:
    payload = _healthy_observation()
    payload["window"]["from_cursor"] = 5
    payload["window"]["to_cursor"] = 5
    with pytest.raises(ValidationError, match="from_cursor"):
        domain.Observation.model_validate(payload)


def test_invariant_used_characters_le_budget_characters() -> None:
    payload = _healthy_observation()
    payload["truncation"]["used_characters"] = payload["truncation"]["budget_characters"] + 1
    with pytest.raises(ValidationError, match="used_characters"):
        domain.Observation.model_validate(payload)


def test_invariant_signal_event_ids_resolve_to_packet_events() -> None:
    payload = _healthy_observation()
    payload["signals"] = [
        {
            "id": "sig:1",
            "kind": "stagnation",
            "severity": "warning",
            "source": "local_rule",
            "event_ids": ["evt:does-not-exist"],
            "observed_at": "2026-08-13T10:00:30Z",
            "freshness": "current",
            "summary": "no matching event",
            "payload": {},
        }
    ]
    with pytest.raises(ValidationError, match="not present in packet"):
        domain.Observation.model_validate(payload)


def test_invariant_evidence_refs_resolve_to_packet_ids() -> None:
    observation = domain.Observation.model_validate(_healthy_observation())
    assessment_payload = _healthy_assessment()
    assessment_payload["evidence"] = [
        {"ref_type": "event", "ref_id": "evt:1", "claim": "resolves"},
        {"ref_type": "system", "ref_id": "sys:does-not-exist", "claim": "does not resolve"},
    ]
    assessment_payload["basis_ids"] = ["evt:1", "sys:does-not-exist"]
    assessment = domain.Assessment.model_validate(assessment_payload)
    unresolved = domain.validate_evidence_against_packet(assessment, observation)
    assert unresolved == ["sys:does-not-exist"]


def test_invariant_evidence_refs_all_resolve_when_valid() -> None:
    observation = domain.Observation.model_validate(_healthy_observation())
    assessment_payload = _healthy_assessment()
    assessment_payload["evidence"] = [
        {"ref_type": "event", "ref_id": "evt:1", "claim": "resolves"},
        {"ref_type": "system", "ref_id": "sys:elapsed", "claim": "also resolves"},
    ]
    assessment_payload["basis_ids"] = ["evt:1"]
    assessment = domain.Assessment.model_validate(assessment_payload)
    assert domain.validate_evidence_against_packet(assessment, observation) == []


def test_invariant_basis_ids_must_resolve_to_evidence_ref_ids() -> None:
    payload = _healthy_assessment()
    payload["basis_ids"] = ["evt:not-in-evidence"]
    with pytest.raises(ValidationError, match="basis_ids"):
        domain.Assessment.model_validate(payload)


def test_invariant_report_supersedes_less_than_report_version() -> None:
    with pytest.raises(ValidationError, match="supersedes"):
        domain.Report(report_version=2, provisional=False, supersedes=2)


def test_invariant_first_report_cannot_supersede() -> None:
    with pytest.raises(ValidationError, match="supersedes"):
        domain.Report(report_version=1, provisional=False, supersedes=1)


def test_invariant_report_chain_valid_case() -> None:
    report = domain.Report(report_version=3, provisional=False, supersedes=2)
    assert report.supersedes == 2


def test_invariant_run_id_and_execution_epoch_both_or_neither_on_event() -> None:
    with pytest.raises(ValidationError, match="run_id and execution_epoch"):
        domain.Event(
            id="e1",
            timestamp="2026-08-13T10:00:00Z",
            kind=domain.EventKind.command,
            run_id="run-1",
            execution_epoch=None,
        )


def test_invariant_run_id_and_execution_epoch_both_set_ok() -> None:
    event = domain.Event(
        id="e1",
        timestamp="2026-08-13T10:00:00Z",
        kind=domain.EventKind.process_lifecycle,
        run_id="run-1",
        execution_epoch=0,
        exit_code=0,
    )
    assert event.run_id == "run-1"


def test_invariant_process_lifecycle_requires_exit_code() -> None:
    with pytest.raises(ValidationError):
        domain.Event(
            id="e1",
            timestamp="2026-08-13T10:00:00Z",
            kind=domain.EventKind.process_lifecycle,
            run_id="run-1",
            execution_epoch=0,
        )


def test_invariant_signal_fingerprint_matches_digest() -> None:
    payload = _healthy_reconciled()
    payload["active_signals"] = [{"id": "sig:1", "kind": "stagnation", "severity": "warning"}]
    payload["signal_fingerprint"] = "deadbeef" * 8
    with pytest.raises(ValidationError, match="signal_fingerprint"):
        domain.ReconciledAssessment.model_validate(payload)


def test_invariant_signal_fingerprint_correct_digest_accepted() -> None:
    payload = _healthy_reconciled()
    active_signals = [{"id": "sig:1", "kind": "stagnation", "severity": "warning"}]
    payload["active_signals"] = active_signals
    payload["state"] = "active_turn"
    payload["status"] = "stalled"
    payload["notification_status"] = "stalled"
    payload["needs_attention"] = False
    payload["signal_fingerprint"] = domain.canonical_signal_fingerprint(
        [domain.ActiveSignal(**s) for s in active_signals]
    )
    reconciled = domain.ReconciledAssessment.model_validate(payload)
    reconciled.validate_against_schema()


def test_invariant_run_id_and_execution_epoch_both_or_neither_on_reconciled() -> None:
    payload = _healthy_reconciled()
    payload["run_id"] = "run-1"
    payload["execution_epoch"] = None
    with pytest.raises(ValidationError, match="run_id and execution_epoch"):
        domain.ReconciledAssessment.model_validate(payload)


# --- session-state -> assessment-status / notification_status projection ---


def test_projection_active_turn_permits_narrowed_statuses() -> None:
    for status in ("progressing", "investigating", "stalled", "looping", "off_scope"):
        payload = _healthy_reconciled()
        payload["status"] = status
        if status in ("stalled", "looping", "off_scope"):
            # notification_status narrowing requires a matching active signal
            # per the schema's allOf block; exercised fully in test_policy.py.
            # Here we only check the state/status pairing itself is legal by
            # keeping notification_status at a value valid for active_turn.
            payload["notification_status"] = "progressing"
        domain.ReconciledAssessment.model_validate(payload)


def test_projection_rejects_cross_row_status() -> None:
    payload = _healthy_reconciled()
    payload["state"] = "active_turn"
    payload["status"] = "terminal_completed"  # belongs to a different row
    with pytest.raises(ValidationError, match="not permitted for state"):
        domain.ReconciledAssessment.model_validate(payload)


def test_projection_between_turns_only_permits_between_turns_status() -> None:
    payload = _healthy_reconciled()
    payload["state"] = "between_turns"
    payload["status"] = "progressing"
    payload["notification_status"] = "between_turns"
    with pytest.raises(ValidationError, match="not permitted for state"):
        domain.ReconciledAssessment.model_validate(payload)


def test_projection_identity_broken_status_is_unknown() -> None:
    payload = _healthy_reconciled()
    payload["state"] = "identity_broken"
    payload["status"] = "unknown"
    payload["notification_status"] = "identity_broken"
    payload["needs_attention"] = True
    payload["fatal"] = {
        "reason": "prefix_mismatch",
        "detected_at": "2026-08-13T10:02:00Z",
        "detail": "rollout-2026-08-13.jsonl",
    }
    payload["active_signals"] = [
        {"id": "sig:dep", "kind": "dependency_unavailable", "severity": "critical"}
    ]
    payload["signal_fingerprint"] = domain.canonical_signal_fingerprint(
        [domain.ActiveSignal(**s) for s in payload["active_signals"]]
    )
    reconciled = domain.ReconciledAssessment.model_validate(payload)
    reconciled.validate_against_schema()


def test_schema_projection_rejects_notification_status_without_matching_signal() -> None:
    payload = _healthy_reconciled()
    payload["status"] = "stalled"
    payload["notification_status"] = "stalled"
    payload["active_signals"] = []
    reconciled = domain.ReconciledAssessment.model_validate(payload)
    with pytest.raises(jsonschema.ValidationError):
        reconciled.validate_against_schema()
