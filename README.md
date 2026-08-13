# Codex Watchtower

A design and implementation plan for a local-first observer of autonomous OpenAI Codex CLI runs.

Watchtower combines:

- **Codex Trace** as an optional, manually correlated live drill-down;
- **AgentLens** as an optional, version-gated source of the limited metrics its Codex adapter actually exposes;
- a small **Luna** model for incremental human-readable summaries;
- a stronger **Terra** model only when evidence is ambiguous or unhealthy;
- optional Telegram delivery when state changes or human attention is required.

The project is documentation-first. No runtime implementation has been committed yet.

## Documents

- [Architecture specification](docs/specs/2026-08-13-codex-watchtower-design.md)
- [Implementation plan](docs/plans/2026-08-13-codex-watchtower-implementation.md)
- [Verified references and research notes](docs/references/references.md)
- [Normalized observation schema](schemas/observation.schema.json)
- [Model assessment schema](schemas/assessment.schema.json)

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
- query only verified AgentLens Codex fields when a compatible adapter is available;
- produce a Luna summary every 10 minutes or on significant change;
- escalate suspicious or low-confidence cases to Terra;
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

Draft architecture and execution plan pending integration spikes and implementation validation. No runtime implementation has started.

## License

MIT. See [LICENSE](LICENSE).
