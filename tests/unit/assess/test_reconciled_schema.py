"""Negative fixtures for schema projection rules not already covered by test_domain.py.

Each of these asserts invalid against schemas/reconciled_assessment.schema.json
directly (not through pydantic construction), since these are the allOf
conditionals that ARE expressible in JSON Schema and are therefore
authoritative there rather than re-implemented in Python (see
assess/policy.py's module docstring and domain.py's module docstring for
the split).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from codex_watchtower import domain, schemas

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "reconciled" / "healthy.json"


def _healthy() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(FIXTURES.read_text())
    return copy.deepcopy(result)


def _assert_invalid(instance: dict[str, Any]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate("reconciled_assessment", instance)


def test_idle_state_with_terminal_failed_status_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "idle"
    instance["status"] = "terminal_failed"
    instance["notification_status"] = "idle"
    _assert_invalid(instance)


def test_idle_state_with_identity_broken_notification_status_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "idle"
    instance["status"] = "idle"
    instance["notification_status"] = "identity_broken"
    _assert_invalid(instance)


def test_idle_state_with_non_provisional_report_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "idle"
    instance["status"] = "idle"
    instance["notification_status"] = "idle"
    instance["report"] = {"report_version": 1, "provisional": False, "supersedes": None}
    _assert_invalid(instance)


def test_terminal_state_with_null_run_id_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "terminal_completed"
    instance["status"] = "terminal_completed"
    instance["notification_status"] = "terminal_completed"
    instance["run_id"] = None
    instance["execution_epoch"] = None
    _assert_invalid(instance)


def test_notification_status_stalled_without_matching_signal_is_invalid() -> None:
    instance = _healthy()
    instance["notification_status"] = "stalled"
    instance["active_signals"] = []
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint([])
    _assert_invalid(instance)


def test_notification_status_looping_without_matching_signal_is_invalid() -> None:
    instance = _healthy()
    instance["notification_status"] = "looping"
    instance["active_signals"] = [{"id": "sig:1", "kind": "stagnation", "severity": "warning"}]
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint(
        [domain.ActiveSignal(**s) for s in instance["active_signals"]]
    )
    _assert_invalid(instance)


def test_notification_status_off_scope_without_matching_signal_is_invalid() -> None:
    instance = _healthy()
    instance["notification_status"] = "off_scope"
    instance["active_signals"] = []
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint([])
    _assert_invalid(instance)


def test_active_critical_signal_with_needs_attention_false_is_invalid() -> None:
    instance = _healthy()
    instance["active_signals"] = [
        {"id": "sig:crit", "kind": "forbidden_path", "severity": "critical"}
    ]
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint(
        [domain.ActiveSignal(**s) for s in instance["active_signals"]]
    )
    instance["needs_attention"] = False
    _assert_invalid(instance)


def test_fatal_set_without_identity_broken_state_is_invalid() -> None:
    instance = _healthy()
    instance["fatal"] = {
        "reason": "prefix_mismatch",
        "detected_at": "2026-08-13T10:02:00Z",
        "detail": "x",
    }
    # state left as active_turn -- must be identity_broken when fatal is set
    _assert_invalid(instance)


def test_identity_broken_state_without_fatal_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "identity_broken"
    instance["status"] = "unknown"
    instance["notification_status"] = "identity_broken"
    instance["fatal"] = None
    _assert_invalid(instance)


def test_empty_string_run_id_on_terminal_result_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "terminal_completed"
    instance["status"] = "terminal_completed"
    instance["notification_status"] = "terminal_completed"
    instance["run_id"] = ""
    _assert_invalid(instance)


def test_fatal_with_no_active_critical_signal_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "identity_broken"
    instance["status"] = "unknown"
    instance["notification_status"] = "identity_broken"
    instance["needs_attention"] = True
    instance["fatal"] = {
        "reason": "execution_epoch_exhausted",
        "detected_at": "2026-08-13T10:02:00Z",
        "detail": None,
    }
    instance["execution_epoch"] = 100000
    instance["active_signals"] = []  # no critical signal to justify the fatal condition
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint([])
    _assert_invalid(instance)


def test_prefix_mismatch_without_dependency_unavailable_signal_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "identity_broken"
    instance["status"] = "unknown"
    instance["notification_status"] = "identity_broken"
    instance["needs_attention"] = True
    instance["fatal"] = {
        "reason": "prefix_mismatch",
        "detected_at": "2026-08-13T10:02:00Z",
        "detail": "rollout-x.jsonl",
    }
    # critical, but wrong kind -- not dependency_unavailable
    instance["active_signals"] = [{"id": "sig:1", "kind": "forbidden_path", "severity": "critical"}]
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint(
        [domain.ActiveSignal(**s) for s in instance["active_signals"]]
    )
    _assert_invalid(instance)


@pytest.mark.parametrize(
    ("reason", "field", "below_max_value"),
    [
        ("status_epoch_exhausted", "status_epoch", 999_999),
        ("attention_epoch_exhausted", "attention_epoch", 999_999),
        ("execution_epoch_exhausted", "execution_epoch", 99_999),
    ],
)
def test_exhausted_reason_paired_with_counter_below_max_is_invalid(
    reason: str, field: str, below_max_value: int
) -> None:
    instance = _healthy()
    instance["state"] = "identity_broken"
    instance["status"] = "unknown"
    instance["notification_status"] = "identity_broken"
    instance["needs_attention"] = True
    instance["fatal"] = {"reason": reason, "detected_at": "2026-08-13T10:02:00Z", "detail": None}
    instance["active_signals"] = [
        {"id": "sig:1", "kind": "dependency_unavailable", "severity": "critical"}
    ]
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint(
        [domain.ActiveSignal(**s) for s in instance["active_signals"]]
    )
    if field == "execution_epoch":
        instance["run_id"] = "run-1"
    instance[field] = below_max_value  # not actually at the maximum
    _assert_invalid(instance)


def test_report_version_exhausted_with_report_below_max_is_invalid() -> None:
    instance = _healthy()
    instance["state"] = "identity_broken"
    instance["status"] = "unknown"
    instance["notification_status"] = "identity_broken"
    instance["needs_attention"] = True
    instance["fatal"] = {
        "reason": "report_version_exhausted",
        "detected_at": "2026-08-13T10:02:00Z",
        "detail": None,
    }
    instance["active_signals"] = [
        {"id": "sig:1", "kind": "dependency_unavailable", "severity": "critical"}
    ]
    instance["signal_fingerprint"] = domain.canonical_signal_fingerprint(
        [domain.ActiveSignal(**s) for s in instance["active_signals"]]
    )
    instance["report"] = {"report_version": 99_999, "provisional": False, "supersedes": 99_998}
    _assert_invalid(instance)


def test_report_chain_never_reuses_or_decreases_version() -> None:
    from datetime import UTC, datetime, timedelta

    from codex_watchtower.codex import lifecycle

    now = datetime.now(UTC)
    state = lifecycle.LifecycleState.initial("sess-1")
    bound = lifecycle.bind_execution(state, run_id="run-1", now=now, first_binding=True)
    exited = lifecycle.on_process_exit(
        bound, run_id="run-1", execution_epoch=0, exit_code=0, now=now
    )
    completed = lifecycle.tick(exited, now=now + timedelta(minutes=11))
    first = lifecycle.report_for_terminal_or_idle(completed)
    assert first.report is not None
    assert first.report.report_version == 1

    resumed = lifecycle.bind_execution(first.state, run_id="run-2", now=now, first_binding=False)
    exited2 = lifecycle.on_process_exit(
        resumed, run_id="run-2", execution_epoch=1, exit_code=0, now=now
    )
    completed2 = lifecycle.tick(exited2, now=now + timedelta(minutes=30))
    second = lifecycle.report_for_terminal_or_idle(completed2)
    assert second.report is not None
    assert second.report.report_version == 2
    assert second.report.supersedes == 1
    assert second.report.report_version > first.report.report_version
