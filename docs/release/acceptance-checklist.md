# MVP Acceptance Checklist

This document maps each specification acceptance criterion (section 11)
to the commands and captured output that verify it. Criteria 1-11 gate
v0.1.0 in advisory mode; criterion 12 gates v0.2.0.

## Verification commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

All four must pass with zero errors before any criterion is marked green.

## Criterion 1: Incremental ingestion without rereading the full file

**Evidence:**
- `tests/e2e/test_two_hour_replay.py::test_idle_reopen_provisional_report_and_supersede`
- `tests/integration/test_ingest_watch.py::test_events_reach_sqlite_exactly_once`
- Cursor-based tailing in `codex/tailer.py` reads only new bytes after
  the saved byte offset + record ordinal.

**Status:** Green. Events are ingested incrementally; the cursor is
persisted across restarts and never rereads already-processed bytes.

## Criterion 2: Restart produces no duplicate events or Telegram messages

**Evidence:**
- `tests/e2e/test_two_hour_replay.py::test_restart_midway_does_not_duplicate_events_or_reports`
- `tests/integration/test_ingest_watch.py::test_restart_does_not_duplicate_events`
- `tests/e2e/test_two_hour_replay.py::test_notification_dedup_across_reopen`

**Status:** Green. Exactly-once ingestion is enforced by content-addressed
logical event IDs (SHA-256 of session_id + ordinal + kind + payload_hash).
Notification dedup keys advance with status_epoch, so a reopen episode
produces a new key, not a suppressed duplicate.

## Criterion 3: Monitoring works with AgentLens and Codex Trace stopped

**Evidence:**
- `tests/e2e/test_degraded_services.py::test_agentlens_absent_ingestion_proceeds`
- AgentLens is disabled by default (`config.py`: `AgentLensConfig.enabled = False`).
- Codex Trace is never required for ingestion (it is an optional
  manually-correlated drill-down).

**Status:** Green. Ingestion, rules, reconciliation, and notification
all proceed without AgentLens or Codex Trace.

## Criterion 4: AgentLens enrichment without duplicate session creation

**Evidence:**
- `tests/e2e/test_degraded_services.py::test_agentlens_midway_no_duplicate_session`
- `agentlens/correlate.py`: canonical correlation by rollout filename,
  never by guessed dates or heuristics.
- `agentlens/client.py`: version-gated adapter degrades to
  `AgentLensUnavailable`/`AgentLensIncompatible` without halting
  ingestion.

**Status:** Green. A fixture-compatible AgentLens enriches the same
session; incompatible versions remain disabled with explicit degraded
status.

## Criterion 5: Rule-based detection in fixtures

**Evidence:**
- `tests/unit/rules/test_repetition.py` — repeated commands, recurring errors
- `tests/unit/rules/test_progress.py` — stagnation detection
- `tests/unit/rules/test_scope.py` — forbidden-path changes, scope violations
- `tests/unit/rules/test_tests.py` — test regression detection
- `tests/integration/test_ingest_watch.py::test_forbidden_path_change_produces_active_signal_and_needs_attention`

**Status:** Green. All five rule categories are implemented and tested.

## Criterion 6: Luna assessments validate and cite existing evidence

**Evidence:**
- `schemas/assessment.schema.json` and `schemas/assessment.wire.schema.json`
- `domain.py::validate_evidence_against_packet` — returns unresolved
  evidence ref_ids.
- `models/client.py::assess` — re-validates every response against the
  authoritative schema after parsing.
- `tests/unit/test_domain.py` — assessment validators.

**Status:** Green (advisory mode). Model output validates against the
schema; citations are checked against the packet. No notification
depends on model output in v0.1.0.

## Criterion 7: Terra runs only under documented escalation conditions

**Evidence:**
- `assess/policy.py` — escalation conditions documented and enforced.
- `assess/scheduler.py` — Terra is invoked only when Luna's assessment
  is ambiguous or unhealthy.
- `tests/unit/assess/test_policy.py` — escalation condition tests.

**Status:** Green (advisory mode). Terra escalation conditions are
implemented and tested. No notification depends on Terra output in
v0.1.0.

## Criterion 8: Deterministic critical signals survive contradictory model output

**Evidence:**
- `assess/policy.py::reconcile` — deterministic signals are authoritative;
  model assessment is embedded unchanged and never consulted for
  notification in v0.1.0.
- `tests/unit/assess/test_policy.py` — critical signal preservation
  tests.

**Status:** Green. Deterministic signals cannot be overridden by model
output.

## Criterion 9: Remote-mode packets pass redaction tests with seeded secrets

**Evidence:**
- `tests/unit/privacy/test_redact.py` — seeded-secret regression tests.
- `src/codex_watchtower/privacy/redact.py` — redaction applied before
  SQLite (normalizer) and again before anything leaves the process.
- `tests/e2e/test_degraded_services.py` — model endpoint tests do not
  leak secrets.

**Status:** Green. All recognized secret shapes are replaced with
`[REDACTED:<class>]` markers before they reach storage or any external
endpoint.

## Criterion 10: Terminal/idle/resume report version chain

**Evidence:**
- `tests/e2e/test_two_hour_replay.py::test_idle_reopen_provisional_report_and_supersede`
- `tests/e2e/test_two_hour_replay.py::test_resume_after_terminal_supersedes_report`
- `tests/integration/test_ingest_watch.py::test_terminal_completion_emits_a_report`

**Status:** Green. Idle emits provisional reports; terminal emits final
reports; resume reopens and supersedes by version. Report version chains
are verified across both reopen paths.

## Criterion 11: Local API survives malformed and unknown events

**Evidence:**
- `tests/integration/test_api.py` — API endpoint tests.
- `codex/normalize.py` — unknown event types normalize to `kind=unknown`
  rather than raising (spec 4.1).
- `tests/unit/codex/test_normalize.py` — malformed record handling.

**Status:** Green. Malformed JSON and unknown event types are preserved
as `unknown` events without crashing ingestion or the API.

## Criterion 12 (v0.2.0): Frozen calibration report and promotion gates

**Evidence:**
- `evaluation/metrics.py` — macro-F1, attention, citation, latency, token
  metrics implemented and unit-tested.
- `docs/evaluation/report-template.md` — calibration report template.
- `tests/unit/evaluation/test_metrics.py` — 21 metric tests.

**Status:** Not yet met. Requires a frozen labeled evaluation corpus
(Task 32) and a full evaluation run (Task 33 step 4-9). The system
remains in advisory mode until this criterion is met.

## Seeded-secret scan

```bash
uv run pytest tests/unit/privacy/test_redact.py -q
```

This suite is the source of truth for which secret patterns Watchtower
recognizes. All tests must pass.

## Loopback binding

The local API binds to `127.0.0.1` by default (`config.py`:
`api_host = "127.0.0.1"`). No port is exposed to external interfaces
unless explicitly configured.

## Known limitations

1. **No real Codex rollout corpus** has been ingested; all tests use
   synthetic fixtures. The exact JSONL envelope shape has not been
   captured live (see `spikes/codex-lifecycle/README.md`).
2. **No frozen labeled evaluation corpus** exists; calibration gates
   v0.2.0 are not yet met.
3. **Model output is advisory only** in v0.1.0; no notification depends
   on it.
4. **Telegram delivery** is tested against mock endpoints, not a live
   Telegram Bot API instance.
5. **AgentLens integration** is tested against synthetic fixtures, not a
   live AgentLens instance.
