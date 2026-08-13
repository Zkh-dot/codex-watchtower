# Codex Watchtower Architecture Specification

**Date:** 2026-08-13  
**Status:** Approved design, implementation not started  
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
- merge AgentLens-derived health signals without duplicating raw events;
- generate a concise human assessment with evidence references;
- use Luna for routine assessments and Terra only for escalation;
- notify on material state changes, anomalies, completion, and failure;
- provide links or identifiers for opening the detailed session in Codex Trace;
- produce a final run report after completion.

### 2.2 Quality goals

- A model assessment must never erase or downgrade a deterministic critical signal.
- Every nontrivial claim must cite one or more normalized event IDs.
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
- **AgentLens:** deterministic derived health and efficiency signals;
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
             Normalizer <---------- AgentLens MCP adapter
                  |                     (optional)
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
 Codex Trace deep link / session ID
```

## 5. Component contracts

### 5.1 Session discovery

Responsibilities:

- watch the configured Codex sessions root;
- identify new and modified `rollout-*.jsonl` files;
- extract session ID, workspace, start time, source, model, and original goal;
- classify lifecycle as `running`, `waiting`, `completed`, `failed`, or `unknown`;
- map a session to a stable Watchtower record.

The first user task message is the default goal. A launcher integration may supply a clearer explicit goal and path boundaries.

### 5.2 Incremental parser

The parser reads only complete newline-terminated JSON records. Its cursor is:

```text
<device>:<inode>:<byte-offset>:<last-event-hash>
```

Requirements:

- never process a partial final line;
- detect truncation or inode replacement;
- deduplicate after restart using the event hash;
- preserve unknown event payloads in bounded raw storage;
- cap individual command output before normalization while retaining its hash and byte length.

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

Each event has a stable ID, timestamp, short factual summary, source type, and optional path/exit code. It must not ask a model to parse events that can be parsed deterministically.

### 5.4 AgentLens adapter

Primary interface: Streamable HTTP MCP.

Used tools:

- `get_recent_sessions` to correlate Watchtower and AgentLens sessions;
- `get_session_detail` to obtain the current timeline and session signals;
- `get_efficiency_report` for final or operator-requested analysis, not every assessment.

Fallback: read-only SQLite snapshot if MCP is unavailable and a database path is explicitly configured.

Adapter output is limited to:

- loop signal names and severities;
- error count and recurring error classes;
- changed/read files;
- tool repetition;
- token/context growth;
- elapsed time and latest activity;
- outcome, when known.

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

The packet conforms to `schemas/observation.schema.json` and contains:

- explicit goal and acceptance criteria;
- current session metadata;
- previous assessment;
- only events since the previous cursor;
- compact deterministic signals;
- redaction report.

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
5. completion requires a Codex lifecycle completion event, not model inference;
6. failure requires process/task evidence or an explicit deterministic failure rule.

### 5.9 Status API

Local bind only by default: `127.0.0.1`.

Proposed endpoints:

- `GET /healthz`
- `GET /api/v1/sessions`
- `GET /api/v1/sessions/{id}`
- `GET /api/v1/sessions/{id}/events?after=<cursor>`
- `GET /api/v1/sessions/{id}/assessment`
- `POST /api/v1/sessions/{id}/assess` for operator-requested Terra escalation
- `GET /api/v1/events` for SSE state updates.

No mutation of Codex is exposed.

### 5.10 Telegram notifier

Delivery policy:

Send when:

- `needs_attention` changes from false to true;
- status enters `waiting`, `stalled`, `looping`, `off_scope`, `failed`, or `completed`;
- a warning persists and materially changes;
- a configured periodic digest is due, default disabled.

Do not send when only wording changes.

Deduplication key:

```text
session_id + authoritative_status + concern_fingerprint + event_cursor
```

A message includes elapsed time, current action, alignment, concerns, latest test result, changed files, and session ID/deep link when available.

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

Raw event retention is bounded and configurable. Normalized events and assessments retain their source hashes for auditability.

Secrets never enter SQLite. Tokens and API credentials come from environment variables or an external secret store.

## 7. Privacy and security

### 7.1 Trust modes

- `local`: model endpoint is loopback/local; source paths and command excerpts may be sent after secret redaction;
- `trusted-remote`: configured remote endpoint; paths are workspace-relative and sensitive output is aggressively reduced;
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

## 8. Failure handling

- Malformed JSONL line: wait if partial; quarantine and continue if newline-terminated but invalid.
- Unknown event: store as `unknown`, increment parser metric, continue.
- AgentLens unavailable: use Codex events and local rules; show degraded status.
- Codex Trace unavailable: omit deep link; monitoring continues.
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
4. Starting AgentLens enriches the same session without creating a duplicate session.
5. Rule-based loops, repeated errors, waiting, failure, and forbidden-path changes are detected in fixtures.
6. Luna assessments validate against the JSON schema and cite existing event IDs.
7. Terra runs only under documented escalation conditions.
8. Deterministic critical signals survive contradictory model output.
9. Remote-mode packets pass redaction tests with seeded secrets.
10. Completion notification contains the final status, elapsed time, changed files, tests observed, and session identifier.
11. The local API and SSE stream survive malformed and unknown Codex events.
12. The frozen calibration report is committed and release gates are met or explicitly fail closed to rule-only mode.

## 12. Open design choices deferred to implementation

These are configuration choices, not missing requirements:

- Python versus Rust implementation: the plan starts with Python for parser/model iteration speed and leaves a measured rewrite threshold.
- Exact Luna/Terra identifiers: resolved from runtime configuration.
- Telegram transport: direct Bot API or existing Hermes delivery adapter; both must implement the same notifier interface.
- Codex Trace deep-link format: adapter-specific and optional until its stable URL contract is verified.

No other MVP behavior is intentionally left undefined.
