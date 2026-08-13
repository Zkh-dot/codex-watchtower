# Codex Watchtower Architecture Specification

**Date:** 2026-08-13  
**Status:** Draft architecture pending integration spikes, implementation not started
**Repository:** `ya-yara/codex-watchtower`

## 1. Problem

An autonomous Codex CLI run can work unattended for one or two hours. The operator needs a low-noise answer to four questions without repeatedly reopening the full terminal transcript:

1. What is the agent doing now?
2. Is that work plausibly advancing the original goal?
3. Is it looping, stalled, waiting, breaking tests, or expanding scope without justification?
4. Does a human need to intervene?

Raw terminal output is too detailed. A periodic free-form LLM summary alone is too unreliable. Watchtower therefore combines deterministic telemetry with constrained model interpretation.

## 2. Goals

### 2.1 Functional goals

Watchtower shall:

- observe local Codex CLI sessions without modifying them;
- retain the original user goal and optional expected/forbidden path boundaries;
- ingest incremental Codex events with a durable cursor;
- merge only version-verified AgentLens supplemental fields without duplicating raw events;
- generate a concise human assessment with evidence references;
- use Luna for routine assessments and Terra only for escalation;
- notify on material state changes, anomalies, completion, and failure;
- provide links or identifiers for opening the detailed session in Codex Trace;
- produce a final run report after completion.

### 2.2 Quality goals

- A model assessment must never erase or downgrade a deterministic critical signal.
- Every nontrivial claim must cite one or more normalized event, deterministic signal, or system evidence IDs.
- Restarts must not resend already acknowledged events.
- Unknown Codex event types must be preserved and must not crash ingestion.
- A missing AgentLens or Codex Trace instance must degrade capability, not stop monitoring.
- Secrets must be redacted before any non-local model call.

## 3. Non-goals

The MVP shall not:

- steer, interrupt, approve, or send text to Codex;
- replace the Codex Trace user interface;
- claim that code is correct solely from model interpretation;
- infer semantic scope exclusively from directory names;
- support Claude Code, OpenCode, or other agents;
- provide a multi-user hosted control plane;
- install TLS interception or proxy model traffic.

## 4. Architectural decision

### 4.1 Source ownership

There is one source of truth per concern:

- **Codex rollout JSONL:** chronological session evidence;
- **AgentLens:** optional, version-gated supplemental metrics; the inspected Codex adapter does not expose the full timeline/file/loop detail available for other agents;
- **Watchtower state store:** cursors, assessments, delivery state, and operator configuration;
- **Codex Trace:** detailed human visualization, not authoritative storage;
- **Luna/Terra:** interpretation only.

Watchtower does not concatenate complete AgentLens and Codex Trace outputs. Both originate from the same Codex run, so doing so would duplicate evidence and waste context.

### 4.2 Components

```text
~/.codex/sessions/**/rollout-*.jsonl
                  |
                  v
          Session discovery
                  |
                  v
          Incremental parser ------> raw-event cursor/state
                  |
                  v
             Normalizer <---------- AgentLens compatibility adapter
                  |                     (optional, limited)
                  v
          Deterministic rules
                  |
                  v
       Observation packet builder
                  |
          +-------+-------+
          |               |
          v               |
     Luna assessor        |
          |               |
      escalation? --------+
          |
          v
     Terra assessor
          |
          v
     Policy reconciler
          |
      +---+------------+
      |                |
      v                v
 local status API   Telegram notifier
      |
      v
 Codex Trace API base / session ID
```

## 5. Component contracts

### 5.1 Session discovery

Responsibilities:

- watch the configured Codex sessions root;
- identify new and modified `rollout-*.jsonl` files;
- extract session ID, workspace, start time, source, model, and original goal;
- classify lifecycle as `active_turn`, `between_turns`, `waiting`, `terminal_completed`, `terminal_failed`, or `unknown`;
- map a session to a stable Watchtower record.

The first user task message is the default goal. A launcher integration may supply a clearer explicit goal and path boundaries.

### 5.2 Incremental parser

The parser reads only complete newline-terminated JSON records. Its cursor includes file identity, byte position, and a bounded replay checkpoint:

```text
<device>:<inode>:<byte-offset>:<checkpoint-hash>
```

Requirements:

- never process a partial final line;
- detect truncation, inode replacement/reuse, copy-truncate, and prefix mismatch;
- deduplicate after restart using the event hash;
- on any identity/checkpoint mismatch, replay from the last verified newline checkpoint (or file start) and rely on stable event IDs for deduplication;
- preserve only redacted, bounded unknown-event summaries in SQLite; retain source hashes and byte lengths, not raw payloads;
- redact before persistence, cap individual command output before normalization, and retain its hash and original byte length;
- open only regular files owned by the configured user under the resolved sessions root; reject symlink escape, FIFO/device files, and files exceeding configured size limits.

### 5.3 Normalizer

The normalizer converts version-specific Codex records into `WatchtowerEvent` values:

- `message`
- `reasoning`
- `command`
- `command_result`
- `file_read`
- `file_changed`
- `test_result`
- `error`
- `lifecycle`
- `unknown`

Each event has a stable ID, timestamp, short factual summary, source type, and optional path/exit code. It must not ask a model to parse events that can be parsed deterministically. Domain validation additionally enforces RFC 3339 timestamps, unique and monotonic event IDs/times, `opened_at <= closed_at`, cursor consistency, and event timestamps within the declared window; JSON Schema alone cannot express all of these invariants.

### 5.4 AgentLens adapter

The inspected AgentLens commit exposes Streamable HTTP MCP at loopback port `4316`, path `/mcp`. Its Codex parser currently exposes prompt/token metadata but empty timeline, tool counts, and file lists. The primary integration is therefore a version-gated compatibility adapter, not a general health-signal contract.

Used tools:

- `get_recent_sessions` for limited discovery metadata;
- `get_session_detail` for fields actually present in the pinned response;
- `get_efficiency_report` only after a compatibility fixture proves meaningful Codex data.

There is no reliable exact-ID or workspace/start-time correlation in the inspected MCP responses: AgentLens uses the rollout filename as its Codex session ID and omits workspace/precise start time. Before enabling enrichment, an integration spike must either land an upstream AgentLens contract carrying canonical rollout path/session metadata, or implement a version-pinned local adapter from explicitly documented fields. Ambiguity returns no match; Watchtower never creates a second logical session from AgentLens data.

Fallback: read-only SQLite snapshot only for explicitly supported AgentLens schema versions and only if a compatibility fixture demonstrates the required canonical correlation fields.

Adapter output is limited to verified fields such as prompt metadata and token/context counters. Loop, error, file, tool, and outcome fields remain unavailable unless a future version-specific compatibility fixture proves them.

AgentLens outages set `agentlens_available=false`; they do not halt ingestion or overwrite the last known signals.

### 5.5 Deterministic rule engine

Rules generate evidence-backed signals before model invocation. Initial rules:

- identical normalized command repeated at least three times without a changed outcome;
- same normalized error repeated at least three times;
- worsening focused-test result;
- no progress marker for a configurable interval, default 25 minutes;
- file changes under explicitly forbidden paths;
- unexpected path expansion outside configured expected paths;
- waiting for input or approval;
- failed process or task lifecycle;
- critical AgentLens loop signal;
- context growth marked critical by AgentLens.

A progress marker is one of:

- a new or materially changed hypothesis/reasoning summary;
- a newly changed file;
- a new test target or improved test outcome;
- a previously failing command succeeding;
- an explicit completion milestone.

Time alone is not evidence of stagnation when a long-running command is still active.

### 5.6 Observation packet builder

The packet conforms to `schemas/observation.schema.json` and always contains:

- explicit goal and acceptance criteria;
- current session metadata;
- previous assessment, explicitly `null` for the first packet;
- only events since the previous cursor;
- compact deterministic signals;
- redaction-class report, explicitly empty when nothing was removed.

Deterministic signals are structured records with stable ID, kind, severity, source, evidence event IDs, observation time, freshness, summary, and typed payload. Empty acceptance criteria are represented as `[]`; the field is never omitted.

Limits:

- at most 200 normalized events;
- at most 4,000 characters per event summary;
- command outputs reduced to relevant head/tail excerpts plus exit status;
- older events folded into the previous assessment and counters;
- no full source file contents by default.

### 5.7 Model assessors

Both assessors return `schemas/assessment.schema.json` using structured output.

#### Luna

Invoked when:

- ten minutes passed since the last assessment; or
- a material progress marker occurred; or
- lifecycle/state changed.

Responsibilities:

- state the current concrete action;
- assess alignment to the explicit goal;
- explain deterministic signals;
- identify uncertainty;
- cite event IDs;
- set confidence.

Luna is not allowed to clear deterministic warnings. It can only contextualize them.

#### Terra

Invoked when any condition holds:

- Luna confidence is below `0.72`;
- Luna reports `possibly_aligned`, `misaligned`, `stalled`, `looping`, or `off_scope`;
- any deterministic critical signal is present;
- deterministic evidence conflicts with Luna;
- stagnation exceeds 25 minutes;
- the operator requests a deep assessment.

Terra receives the same observation plus Luna's assessment and an explicit request to adjudicate conflicts. It does not receive the entire transcript unless a future operator-authorized forensic mode is added.

### 5.8 Policy reconciler

The reconciler produces the authoritative assessment:

1. deterministic critical signals force `needs_attention=true`;
2. a Terra result supersedes Luna prose but not deterministic evidence;
3. unsupported model evidence references invalidate the response and trigger one retry;
4. a second invalid response falls back to a rule-generated assessment;
5. Codex `TurnComplete`/wire `task_complete` changes state to `between_turns`; it never proves terminal session completion;
6. terminal completion requires launcher/process exit evidence plus a configurable quiet grace period with no new turn, or an explicit operator-provided terminal marker;
7. terminal failure requires non-zero launcher/process exit evidence or an explicit deterministic process failure rule.

### 5.9 Status and operator API

Local bind only by default: `127.0.0.1`.

Proposed endpoints:

- `GET /healthz`
- `GET /api/v1/sessions`
- `GET /api/v1/sessions/{id}`
- `GET /api/v1/sessions/{id}/events?after=<cursor>`
- `GET /api/v1/sessions/{id}/assessment`
- `POST /api/v1/sessions/{id}/assess` for an authenticated, rate-limited operator-requested Terra escalation
- `GET /api/v1/events` for SSE state updates.

No mutation of Codex or the workspace is exposed. The POST endpoint does mutate Watchtower state and can incur model cost. It is disabled unless an operator token is configured and requires bearer authentication, strict Origin/Host allowlists, disabled wildcard CORS, per-session concurrency limits, global rate/cost limits, and idempotency keys. The default remains loopback-only, but loopback is not treated as authentication.

### 5.10 Telegram notifier

Delivery policy:

Send when:

- `needs_attention` changes from false to true;
- status enters `waiting`, `stalled`, `looping`, `off_scope`, `terminal_failed`, or `terminal_completed`;
- a warning persists and materially changes;
- a configured periodic digest is due, default disabled.

Do not send when only wording changes.

Deduplication key:

```text
session_id + authoritative_status + concern_fingerprint
```

The key deliberately excludes the event cursor. The cursor advances on every ingested event, so including it would make the key unique per assessment and suppress no duplicate at all; it would also re-notify after a restart replayed the same window under a new cursor.

`concern_fingerprint` is the sorted set of `(severity, kind, evidence_signal_id)` triples from the authoritative assessment. It ignores prose, model identity, confidence, and event cursors, so a reworded explanation of unchanged evidence does not resend.

For each key Watchtower persists the last delivery, its cursor, and its send time. On a repeat key it resends only when a configured cooldown has elapsed and the concern is still `critical`, or when a periodic digest is due. The cursor is carried in the message body as provenance, never in the key.

A message includes elapsed time, current action, alignment, concerns, latest test result, redacted changed paths, and session ID/API base when available. Telegram is a separate remote sink: it always applies trusted-remote redaction, never sends prompts or command-output excerpts, validates the configured `chat_id` against an allowlist, escapes Telegram markup, and omits local deep links.

## 6. State and persistence

Use SQLite in WAL mode.

Core tables:

- `sessions`
- `event_cursors`
- `normalized_events`
- `rule_signals`
- `assessments`
- `deliveries`
- `model_calls`

Raw rollout payloads are never copied into Watchtower SQLite. Redaction occurs before persistence; normalized summaries retain source hashes and original byte lengths for auditability. Bounded retention applies to these redacted summaries.

Watchtower makes a best effort to prevent recognized secret material entering SQLite, backed by seeded-secret tests; arbitrary unknown secrets cannot be guaranteed detectable. Tokens and API credentials come from environment variables or an external secret store. The database and state directory use owner-only permissions.

## 7. Privacy and security

### 7.1 Trust modes

- `local`: model endpoint is loopback/local; source paths and command excerpts may be sent after secret redaction;
- `trusted-remote`: explicitly consented HTTPS endpoint on an allowlist; paths are workspace-relative and sensitive output is aggressively reduced;
- `metadata-only`: no command output or prompt text leaves the machine.

### 7.2 Redaction

Before remote inference, redact:

- authorization headers and bearer tokens;
- API keys and known credential formats;
- environment variable values matching secret-name patterns;
- private keys and certificates;
- cookies and OAuth tokens;
- configured path prefixes and usernames;
- oversized command output.

The packet records which redaction classes were applied, never the removed values.

### 7.3 Prompt-injection boundary

Repository files, command output, logs, and agent messages are untrusted data. The assessment prompt states that embedded instructions are evidence, not commands. The model receives no tools and cannot write files or contact Codex.

Remote model transport requires valid HTTPS certificates, rejects redirects, link-local/loopback/metadata destinations and proxy-environment inheritance by default, caps request/response sizes and retry budgets, and records provider retention/logging policy plus explicit operator consent before first transmission. Local mode may use loopback HTTP.

## 8. Failure handling

- Malformed JSONL line: wait if partial; quarantine and continue if newline-terminated but invalid.
- Unknown event: store as `unknown`, increment parser metric, continue.
- AgentLens unavailable: use Codex events and local rules; show degraded status.
- Codex Trace unavailable: omit drill-down identifiers; monitoring continues.
- Luna timeout: retry once, then rule-only assessment and optional Terra escalation.
- Terra timeout: preserve deterministic `needs_attention`; send rule-only alert.
- Telegram failure: durable retry with exponential backoff and deduplication.
- State database corruption: stop writes, preserve evidence files, report critical local error; never recreate silently.

## 9. Observability

Watchtower emits structured logs and Prometheus-compatible counters for:

- sessions observed;
- events ingested and rejected;
- unknown Codex event types;
- AgentLens availability;
- model call count, latency, failures, and escalation rate;
- token/input size per assessment;
- notifications sent, deduplicated, and failed;
- cursor recovery and parser lag.

It must not export transcript content through metrics.

## 10. Testing strategy

### 10.1 Fixture corpus

Commit synthetic and redacted JSONL fixtures covering:

- healthy progress;
- repeated command loop;
- repeated error loop;
- legitimate long-running test/build;
- scope expansion with and without justification;
- waiting for input;
- partial line and file rotation;
- unknown future event type;
- successful completion;
- process/task failure.

### 10.2 Deterministic tests

- parser and cursor recovery;
- normalization;
- rule thresholds;
- redaction;
- assessment reconciliation;
- notification deduplication;
- API schemas.

### 10.3 Model calibration

Build a frozen evaluation set of at least 30 completed sessions, sampled across healthy, ambiguous, stalled, looping, off-scope, failed, and completed outcomes.

Human labels include:

- status;
- goal alignment;
- attention required;
- evidence event IDs;
- acceptable one-sentence summary.

Measure Luna and Terra independently:

- attention precision and recall;
- status macro-F1;
- evidence citation validity;
- false escalation rate;
- summary factuality;
- latency and input tokens.

Initial release gates:

- no unsupported evidence citations in the acceptance corpus;
- attention recall at least 0.90 on deterministic anomaly cases;
- false attention rate at most 0.15 on healthy sessions;
- Terra improves ambiguous-case accuracy over Luna by at least 10 percentage points, otherwise remove the cascade complexity.

## 11. MVP acceptance criteria

The MVP is accepted when:

1. A two-hour replay fixture can be ingested incrementally without rereading the full file.
2. Restarting midway produces no duplicate events or Telegram messages.
3. Monitoring works with both AgentLens and Codex Trace stopped.
4. A fixture-compatible AgentLens enriches the same session through canonical correlation without creating a duplicate; incompatible versions remain disabled with explicit degraded status.
5. Rule-based loops, repeated errors, waiting, failure, and forbidden-path changes are detected in fixtures.
6. Luna assessments validate against the JSON schema and cite existing event, deterministic signal, or system evidence IDs.
7. Terra runs only under documented escalation conditions.
8. Deterministic critical signals survive contradictory model output.
9. Remote-mode packets pass redaction tests with seeded secrets.
10. Completion notification contains the final status, elapsed time, changed files, tests observed, and session identifier.
11. The local API and SSE stream survive malformed and unknown Codex events.
12. The frozen calibration report is committed and release gates are met or explicitly fail closed to rule-only mode.

## 12. Open design choices deferred to implementation

These are configuration choices, not missing requirements:

- Python versus Rust implementation: the plan starts with Python for parser/model iteration speed and leaves a measured rewrite threshold.
- Exact Luna/Terra identifiers: resolved and verified by the mandatory model contract spike before implementation.
- Telegram transport: direct Bot API or existing Hermes delivery adapter; both must implement the same notifier and remote-sink privacy interface.
- Codex Trace deep-link format: absent from MVP unless the mandatory compatibility spike verifies a stable URL contract.

These decisions must be resolved by Phase -1 spikes before affected interfaces are frozen; unsupported integrations fail closed rather than being guessed.
