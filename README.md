# Codex Watchtower

A local-first observer of autonomous OpenAI Codex CLI runs.

Watchtower combines:

- **Codex Trace** as an optional, manually correlated live drill-down;
- **AgentLens** as an optional, version-gated source of the limited metrics its Codex adapter actually exposes;
- a small **Luna** model for incremental human-readable summaries;
- a stronger **Terra** model only when evidence is ambiguous or unhealthy;
- optional Telegram delivery when state changes or human attention is required.

## Quick start

```bash
# Install
uv sync

# Verify
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q

# Run the doctor check
uv run watchtower doctor

# Start serving (ingestion + local API)
uv run watchtower serve
```

## Documents

- [Architecture specification](docs/specs/2026-08-13-codex-watchtower-design.md)
- [Implementation plan](docs/plans/2026-08-13-codex-watchtower-implementation.md)
- [Verified references and research notes](docs/references/references.md)
- [Operations guide](docs/operations.md)
- [Acceptance checklist](docs/release/acceptance-checklist.md)
- [Threat model](docs/release/threat-model.md)
- [Corpus format](docs/evaluation/corpus-format.md)
- [Calibration report template](docs/evaluation/report-template.md)
- [Normalized observation schema](schemas/observation.schema.json)
- [Model assessment schema](schemas/assessment.schema.json)
- [Provider-facing wire projection of the assessment schema](schemas/assessment.wire.schema.json)
- [Reconciled assessment schema](schemas/reconciled_assessment.schema.json)

## Design principles

1. **No Codex/workspace mutation.** Watchtower never edits the workspace or silently steers Codex; authenticated operator actions may trigger an assessment and consume model quota.
2. **Evidence before prose.** Deterministic signals remain authoritative; an LLM explains them but does not invent alerts.
3. **Incremental context.** Models receive the goal, previous assessment, and only events since the last cursor.
4. **Local first.** Session transcripts and source code stay local unless an explicitly configured model endpoint receives a redacted observation packet.
5. **Quiet by default.** Telegram reports state transitions and anomalies, not a metronomic stream of noise.
6. **Inspectability.** Every assessment links back to its source session, event cursor, and deterministic evidence.

## Intended MVP

The first usable version will:

- discover active `~/.codex/sessions/**/rollout-*.jsonl` sessions;
- normalize new Codex events;
- record process exit evidence for runs started through `watchtower run`, and fall back to a reversible `idle` state otherwise;
- treat every terminal state as describing one execution, so a session resumed with `codex exec resume` reopens and supersedes its earlier report;
- query only verified AgentLens Codex fields when a compatible adapter is available;
- produce a Luna summary every 10 minutes or on significant change;
- escalate anomalous or conflicting cases to Terra;
- expose a local JSON/HTTP status endpoint;
- optionally send Telegram notifications;
- expose a Codex Trace API base and session identifier for manual drill-down when available.

## Non-goals for MVP

- replacing Codex CLI or Codex Trace;
- autonomous intervention in a running session;
- judging source-code correctness without tests or repository evidence;
- uploading complete transcripts to a hosted observability platform;
- supporting every coding agent from day one.

## Status

v0.1.0 is implemented in advisory mode: deterministic rules are
authoritative and notify on their own, and model assessments are
displayed without influencing any notification decision. All 11 MVP
acceptance criteria are met with evidence (see the [acceptance
checklist](docs/release/acceptance-checklist.md)). Model output is
promoted to a notification input in v0.2.0, once the frozen calibration
corpus and its gates exist.

### Implemented

- Date-partitioned session discovery and incremental, restart-safe tailing
- Content-addressed exactly-once event ingestion (SHA-256 logical event IDs)
- Deterministic rule engine: repeated commands, recurring errors,
  stagnation, scope/forbidden-path violations, test regression
- Session lifecycle: idle timeout, process exit, resume with
  execution-epoch advancement, report version chains
- Version-gated AgentLens MCP adapter (degrades gracefully when absent)
- Provider-neutral structured model client with bounded response reads
- Luna/Terra assessment cascade with escalation conditions
- Observation packet builder with redaction and truncation budgets
- Reconciliation policy: deterministic signals are authoritative
- Telegram notifier with durable retry, dedup, and rate-limit backoff
- Local FastAPI JSON/HTTP API with SSE stream
- Structured telemetry (counters + histograms) with no transcript
  content in metrics
- Redacted rollout fixture tooling (`tools/redact_rollout.py`)
- Evaluation metrics (macro-F1, attention, citation, latency, tokens)
- Two-hour accelerated replay e2e tests
- Degraded-dependency e2e tests

### Known limitations

1. No real Codex rollout corpus has been ingested; all tests use
   synthetic fixtures.
2. No frozen labeled evaluation corpus exists; calibration gates v0.2.0
   are not yet met.
3. Model output is advisory only; no notification depends on it.
4. Telegram delivery is tested against mock endpoints.
5. AgentLens integration is tested against synthetic fixtures.

## License

MIT. See [LICENSE](LICENSE).
