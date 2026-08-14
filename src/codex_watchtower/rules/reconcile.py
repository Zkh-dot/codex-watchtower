"""Reconcile AgentLens-derived signals with local rule signals (spec 5.4/5.5).

Merge rules:

- identical evidence from both sources (same ``kind``) becomes one signal;
  the schema's ``source`` field can only hold a single value, so the local
  rule's source wins when both exist (local rules are the deterministic
  authority per spec 4.1's "evidence before prose"), the higher of the two
  severities is kept, and the merge is recorded in the summary and payload
  rather than silently dropped;
- a signal's severity is never reduced by merging -- the winner is always
  ``max(local.severity, agentlens.severity)``;
- stale AgentLens data (``freshness != current``) cannot overwrite or
  upgrade a same-kind local signal; it is only surfaced on its own, tagged
  as stale, when local evidence of that kind does not exist at all.

``context_growth_signal_from_agentlens`` is the only local signal this
adapter derives, since spec 5.4 records that loop, error, file, and tool
fields are unavailable from the pinned Codex adapter -- there is nothing
else to build a rule on top of yet.
"""

from __future__ import annotations

from codex_watchtower import domain
from codex_watchtower.agentlens.types import AgentLensSessionDetail

CONTEXT_GROWTH_CRITICAL_RATIO = 0.9
CONTEXT_GROWTH_WARNING_RATIO = 0.75

_SEVERITY_ORDER = {
    domain.Severity.info: 0,
    domain.Severity.warning: 1,
    domain.Severity.critical: 2,
}


def context_growth_signal_from_agentlens(
    detail: AgentLensSessionDetail,
    *,
    observed_at: str,
    freshness: domain.Freshness = domain.Freshness.current,
) -> domain.Signal | None:
    if not detail.context_tokens or not detail.context_limit:
        return None
    ratio = detail.context_tokens / detail.context_limit
    if ratio >= CONTEXT_GROWTH_CRITICAL_RATIO:
        severity = domain.Severity.critical
    elif ratio >= CONTEXT_GROWTH_WARNING_RATIO:
        severity = domain.Severity.warning
    else:
        return None
    return domain.Signal(
        id="sig:context_growth:agentlens",
        kind=domain.SignalKind.context_growth,
        severity=severity,
        source=domain.SignalSource.agentlens,
        event_ids=[],
        observed_at=observed_at,
        freshness=freshness,
        summary=(
            f"Context usage at {int(ratio * 100)}% of limit "
            f"({detail.context_tokens}/{detail.context_limit} tokens)."
        ),
        payload={
            "context_tokens": detail.context_tokens,
            "context_limit": detail.context_limit,
            "ratio_percent": int(ratio * 100),
        },
    )


def _merge_same_kind(local: domain.Signal, agentlens: domain.Signal) -> domain.Signal:
    winner_severity = (
        local.severity
        if _SEVERITY_ORDER[local.severity] >= _SEVERITY_ORDER[agentlens.severity]
        else agentlens.severity
    )
    merged_event_ids = sorted(set(local.event_ids) | set(agentlens.event_ids))[:64]
    payload = dict(local.payload)
    payload["corroborated_by_agentlens"] = True
    return domain.Signal(
        id=local.id,
        kind=local.kind,
        severity=winner_severity,
        source=domain.SignalSource.local_rule,
        event_ids=merged_event_ids,
        observed_at=local.observed_at,
        freshness=domain.Freshness.current,
        summary=f"{local.summary} (corroborated by AgentLens)",
        payload=payload,
    )


def reconcile_signals(
    local_signals: list[domain.Signal], agentlens_signals: list[domain.Signal]
) -> list[domain.Signal]:
    """Merge local and AgentLens signals without collapsing independent findings.

    Local signals are keyed by their ID (not kind), so two distinct
    ``forbidden_path`` violations remain as separate signals. An AgentLens
    signal of the same kind as a local signal merges with the *first*
    matching local signal (corroboration); it does not replace or
    collapse other local signals of the same kind.
    """
    result: list[domain.Signal] = list(local_signals)
    local_by_kind: dict[domain.SignalKind, domain.Signal] = {}
    for s in local_signals:
        # Only track the first local signal per kind for AgentLens merge.
        if s.kind not in local_by_kind:
            local_by_kind[s.kind] = s

    for agentlens_signal in agentlens_signals:
        existing = local_by_kind.get(agentlens_signal.kind)
        if existing is None:
            result.append(agentlens_signal)
            continue
        if existing.source == agentlens_signal.source:
            continue  # identical source already covers this kind
        if agentlens_signal.freshness != domain.Freshness.current:
            continue  # stale AgentLens evidence cannot overwrite/upgrade newer local evidence
        merged = _merge_same_kind(existing, agentlens_signal)
        # Replace the original local signal in the result with the merged version.
        idx = result.index(existing)
        result[idx] = merged
        local_by_kind[agentlens_signal.kind] = merged

    return result
