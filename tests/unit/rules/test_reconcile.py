from __future__ import annotations

from codex_watchtower import domain
from codex_watchtower.agentlens.types import AgentLensSessionDetail
from codex_watchtower.rules.reconcile import (
    context_growth_signal_from_agentlens,
    reconcile_signals,
)


def _local_context_signal(severity: domain.Severity) -> domain.Signal:
    return domain.Signal(
        id="sig:context_growth:local",
        kind=domain.SignalKind.context_growth,
        severity=severity,
        source=domain.SignalSource.local_rule,
        event_ids=["evt:1"],
        observed_at="2026-08-13T10:00:00Z",
        freshness=domain.Freshness.current,
        summary="local context growth reading",
        payload={"ratio_percent": 80},
    )


def _agentlens_context_signal(
    severity: domain.Severity, freshness: domain.Freshness = domain.Freshness.current
) -> domain.Signal:
    return domain.Signal(
        id="sig:context_growth:agentlens",
        kind=domain.SignalKind.context_growth,
        severity=severity,
        source=domain.SignalSource.agentlens,
        event_ids=[],
        observed_at="2026-08-13T10:00:00Z",
        freshness=freshness,
        summary="agentlens context growth reading",
        payload={"ratio_percent": 92},
    )


# --- context_growth signal derivation ------------------------------------


def test_context_growth_critical_above_threshold() -> None:
    detail = AgentLensSessionDetail(
        agentlens_session_id="rollout-x",
        prompt_tokens=1000,
        completion_tokens=200,
        context_tokens=120000,
        context_limit=128000,
    )
    signal = context_growth_signal_from_agentlens(detail, observed_at="2026-08-13T10:00:00Z")
    assert signal is not None
    assert signal.severity == domain.Severity.critical


def test_context_growth_warning_band() -> None:
    detail = AgentLensSessionDetail(
        agentlens_session_id="rollout-x",
        prompt_tokens=1000,
        completion_tokens=200,
        context_tokens=100000,
        context_limit=128000,
    )
    signal = context_growth_signal_from_agentlens(detail, observed_at="2026-08-13T10:00:00Z")
    assert signal is not None
    assert signal.severity == domain.Severity.warning


def test_context_growth_below_threshold_no_signal() -> None:
    detail = AgentLensSessionDetail(
        agentlens_session_id="rollout-x",
        prompt_tokens=1000,
        completion_tokens=200,
        context_tokens=10000,
        context_limit=128000,
    )
    assert context_growth_signal_from_agentlens(detail, observed_at="2026-08-13T10:00:00Z") is None


def test_context_growth_missing_fields_no_signal() -> None:
    detail = AgentLensSessionDetail(
        agentlens_session_id="rollout-x",
        prompt_tokens=None,
        completion_tokens=None,
        context_tokens=None,
        context_limit=None,
    )
    assert context_growth_signal_from_agentlens(detail, observed_at="2026-08-13T10:00:00Z") is None


# --- reconciliation: merge, severity, staleness ---------------------------


def test_identical_evidence_from_both_sources_becomes_one_signal_with_corroboration() -> None:
    local = _local_context_signal(domain.Severity.warning)
    agentlens = _agentlens_context_signal(domain.Severity.warning)
    merged = reconcile_signals([local], [agentlens])
    assert len(merged) == 1
    assert merged[0].kind == domain.SignalKind.context_growth
    assert merged[0].payload.get("corroborated_by_agentlens") is True


def test_highest_severity_wins_agentlens_higher() -> None:
    local = _local_context_signal(domain.Severity.warning)
    agentlens = _agentlens_context_signal(domain.Severity.critical)
    merged = reconcile_signals([local], [agentlens])
    assert len(merged) == 1
    assert merged[0].severity == domain.Severity.critical


def test_highest_severity_wins_local_higher() -> None:
    local = _local_context_signal(domain.Severity.critical)
    agentlens = _agentlens_context_signal(domain.Severity.warning)
    merged = reconcile_signals([local], [agentlens])
    assert len(merged) == 1
    assert merged[0].severity == domain.Severity.critical


def test_stale_agentlens_data_cannot_overwrite_newer_local_evidence() -> None:
    local = _local_context_signal(domain.Severity.warning)
    stale_agentlens = _agentlens_context_signal(
        domain.Severity.critical, freshness=domain.Freshness.stale
    )
    merged = reconcile_signals([local], [stale_agentlens])
    assert len(merged) == 1
    assert merged[0].severity == domain.Severity.warning  # not upgraded by stale data
    assert merged[0].source == domain.SignalSource.local_rule


def test_stale_agentlens_data_with_no_local_counterpart_is_still_surfaced() -> None:
    stale_agentlens = _agentlens_context_signal(
        domain.Severity.critical, freshness=domain.Freshness.stale
    )
    merged = reconcile_signals([], [stale_agentlens])
    assert len(merged) == 1
    assert merged[0].freshness == domain.Freshness.stale


def test_signals_of_different_kinds_are_not_merged() -> None:
    local = domain.Signal(
        id="sig:stagnation",
        kind=domain.SignalKind.stagnation,
        severity=domain.Severity.warning,
        source=domain.SignalSource.local_rule,
        event_ids=[],
        observed_at="2026-08-13T10:00:00Z",
        freshness=domain.Freshness.current,
        summary="no progress",
        payload={},
    )
    agentlens = _agentlens_context_signal(domain.Severity.critical)
    merged = reconcile_signals([local], [agentlens])
    assert len(merged) == 2
    kinds = {s.kind for s in merged}
    assert kinds == {domain.SignalKind.stagnation, domain.SignalKind.context_growth}
