# Verified references

Checked on 2026-08-13. Repository commits are pinned where they were inspected locally.

## OpenAI Codex CLI

- Repository: https://github.com/openai/codex
- Inspected repository HEAD: `4ca1af77a561a8908451ae755d8f2b119ca7c434`
- Local CLI used for verification: `codex-cli 0.133.0`
- CLI reference: https://developers.openai.com/codex/cli/reference
- Advanced configuration and OpenTelemetry: https://developers.openai.com/codex/config-advanced
- App Server: https://developers.openai.com/codex/app-server
- Non-interactive mode: https://developers.openai.com/codex/noninteractive

Verified locally:

- `codex exec --json` emits JSONL events on stdout.
- persisted sessions are stored under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`;
- rollout records include session metadata, task lifecycle, messages, tool activity, and completion records;
- `codex app-server` supports stdio, Unix socket, and WebSocket transports;
- Codex has built-in OpenTelemetry configuration;
- `codex exec` supports `--ephemeral`, which must not be used if Watchtower relies on persisted rollouts.

Codex formats evolve. The implementation must treat unknown JSONL event types as preserved opaque events rather than fatal parser errors.

## AgentLens

- Repository: https://github.com/RogerReed/agentlens
- Inspected commit: `af8797740ccbba6d21748a023a9b448b53847467`
- License: MIT

Verified from source at the pinned commit:

- reads Codex local session files and accepts Codex OpenTelemetry;
- stores normalized sessions and timeline entries in SQLite;
- exposes Streamable HTTP MCP on loopback port `4316`, path `/mcp`;
- MCP tools include `get_recent_sessions`, `get_session_detail`, and `get_efficiency_report`;
- the general schema supports errors, tool counts, loop signals, and changed files;
- the pinned Codex parser supplies empty timeline/tool/file structures, so those general detectors are not useful Codex enrichment;
- `get_recent_sessions` and `get_session_detail` omit canonical workspace and precise start-time fields needed for robust fallback correlation;
- AgentLens uses the rollout filename without `.jsonl` as its Codex session ID, rather than the canonical ID inside `session_meta`.

Integration decision:

- run an early compatibility spike and prefer an upstream canonical correlation field;
- enable MCP enrichment only for fields proven by version-pinned fixtures;
- permit read-only SQLite access only for explicitly supported schema versions and proven canonical correlation;
- never scrape the AgentLens web UI.

## Codex Trace

- Repository: https://github.com/PixelPaw-Labs/codex-trace
- Inspected commit: `c0bd7bb8cb99d379056fd7f0e1ed5fd6bfcbc3cf`
- License: MIT

Verified from source:

- reads `~/.codex/sessions` rollout JSONL files;
- supports live session tailing;
- provides desktop and headless web modes;
- headless HTTP API defaults to `127.0.0.1:11424`;
- API routes include:
  - `POST /api/sessions`
  - `POST /api/session/load`
  - `POST /api/session/watch`
  - `POST /api/session/unwatch`
  - `GET /api/events` for SSE updates.

Integration decision:

- Codex rollout JSONL remains the source of truth for ingestion;
- Codex Trace is an optional manually correlated drill-down and parser/API adapter;
- Watchtower must not require Codex Trace to be running in the first MVP.

This avoids a hard dependency on another young project. A stable clickable deep-link contract is not verified; the MVP may expose only the API base and session identifier.

## Models: Luna and Terra

The names refer to model profiles available through the configured OpenAI Codex provider. Exact model identifiers and availability are deployment configuration, not hard-coded protocol constants.

Proposed responsibilities:

- Luna: frequent incremental summaries and first-pass alignment assessment;
- Terra: escalation judge for anomalies, low confidence, conflicting evidence, or long stagnation.

No quality claim is assumed. Before production use, both profiles must be evaluated against a frozen corpus of completed sessions with human labels. The implementation plan includes this calibration gate.

## Rejected or deferred paths

- Scraping HTML dashboards: unstable and unnecessary.
- Sending complete AgentLens and Codex Trace outputs to the model: duplicates evidence and wastes context.
- AgentOps native Codex integration: previously claimed documentation URLs returned 404 and were not verified.
- `jlowin/codex-dashboard`: repository lookup returned 404 and is not a valid dependency.
- OpenCode as a Codex wrapper: incorrect; OpenCode is a separate coding agent.
- Generic Langfuse/Phoenix adapters: possible through OpenTelemetry, but add infrastructure without solving human summarization directly.
- TLS interception with cctrace: useful for protocol debugging, excessive for routine monitoring.

## Security references

- OpenTelemetry security guidance: https://opentelemetry.io/docs/security/
- Telegram Bot API: https://core.telegram.org/bots/api
- JSON Schema 2020-12: https://json-schema.org/draft/2020-12

Model inputs can contain source paths, commands, error messages, prompts, and command output. Redaction and endpoint trust are therefore part of the security boundary, not optional polish.
