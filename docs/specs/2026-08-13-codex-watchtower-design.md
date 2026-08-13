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
~/.codex/sessions/**/rollout-*.jsonl      optional `watchtower run` launcher
                  |                                    |
                  v                                    v
          Session discovery <-------------- process evidence store
                  |                          (PID, exit code, exit time)
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
- classify lifecycle as `active_turn`, `between_turns`, `waiting`, `terminal_completed`, `terminal_completed_unconfirmed`, `terminal_failed`, or `unknown`;
- map a session to a stable Watchtower record.

The first user task message is the default goal. The optional launcher in §5.11 may supply a clearer explicit goal, path boundaries, and process evidence.

### 5.2 Incremental parser

The parser reads only complete newline-terminated JSON records. Its cursor includes file identity, byte position, and a bounded replay checkpoint:

```text
<device>:<inode>:<byte-offset>:<checkpoint-hash>
```

This ingest cursor is internal. It is persisted in `event_cursors` and never leaves the process: device and inode numbers are host details, they change under the rotation and copy-truncate cases this section already handles, and a client holding one would be broken by any of them.

Everything outside the tailer uses a separate **event sequence**: a monotonic per-session integer assigned at normalization and stored with each event. The API `after=` parameter, the observation window `from_cursor`/`to_cursor`, the assessment `event_cursor`, and notification provenance all carry the event sequence. It is stable across restarts, rotation, and replay, because it is derived from the stable event ID ordering rather than from file layout.

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
- `file_read`, aggregated per turn into a count plus a path set rather than one event per read, so read-heavy turns do not consume the packet budget
- `file_changed`
- `test_result`
- `error`
- `turn_lifecycle`
- `process_lifecycle`
- `unknown`

Turn and process lifecycle are separate kinds, not one `lifecycle` kind. §5.8 and §5.11 depend on the distinction: a turn boundary comes from the transcript and can only produce `between_turns`, while a process boundary comes from outside it and is the only thing that can produce a confirmed terminal state.

Each event has a stable ID, timestamp, short factual summary, source type, and optional path/exit code. It must not ask a model to parse events that can be parsed deterministically. Domain validation additionally enforces RFC 3339 timestamps, unique and monotonic event IDs/times, `opened_at <= closed_at`, cursor consistency, event timestamps within the declared window, and `used_characters <= budget_characters` measured on the final serialized request; JSON Schema alone cannot express all of these invariants.

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

- identical normalized command repeated at least three times within the repetition window without a changed outcome;
- same normalized error repeated at least three times within the repetition window;
- worsening focused-test result;
- no progress marker for a configurable interval, default 25 minutes;
- file changes under explicitly forbidden paths;
- unexpected path expansion outside configured expected paths;
- waiting for input or approval;
- failed process or task lifecycle;
- context growth marked critical by AgentLens, only while the adapter in §5.4 is enabled.

Repetition rules are windowed: the default window is 30 minutes or 60 normalized events, whichever is smaller, and both bounds are configurable. Without a window, three occurrences spread across a two-hour session would raise the same signal as three in ninety seconds, and the rule would fire more readily the longer a healthy session ran.

An AgentLens loop signal is deliberately absent. §5.4 records that loop, error, file, tool, and outcome fields are unavailable from the pinned Codex adapter, so a rule consuming them could never fire. It may be added once a version-pinned fixture proves those fields exist, under the same gate as any other enrichment.

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
- system references available for citation;
- redaction-class report, explicitly empty when nothing was removed.

Assessments may cite `ref_type: "system"`, and §5.8 invalidates any unsupported reference, so the citable system IDs must be transmitted rather than assumed. The packet carries an explicit `system_refs` array of `{id, summary}` records covering facts that are neither events nor signals: elapsed time, session state, degraded dependencies, budget exhaustion, and window truncation. IDs use the `sys:` prefix and are stable within a packet. A model citing a `sys:` ID absent from the packet is invalid, exactly as with event and signal IDs.

Deterministic signals are structured records with stable ID, kind, severity, source, evidence event IDs, observation time, freshness, summary, and typed payload. Empty acceptance criteria are represented as `[]`; the field is never omitted.

Limits:

- a hard budget of 48,000 characters over the **entire serialized provider request**, including the goal, the previous assessment, signals, system refs, and prompt scaffolding;
- at most 200 normalized events;
- at most 4,000 characters per event summary;
- command outputs reduced to relevant head/tail excerpts plus exit status;
- no full source file contents by default.

The budget covers the whole request because every excluded field is attacker- or user-controlled and unbounded in practice. A single pasted goal, a previous assessment at its own maxima, or an unbounded signal payload could each exceed the context and cost limit before eviction began. Every contributing field therefore carries its own cap in the schema, so a maximum-sized request is finite and computable:

| Field | Cap |
| --- | --- |
| `goal.text` | 8,000 characters |
| `goal.acceptance_criteria` | 32 items x 500 characters |
| `goal.expected_paths`, `goal.forbidden_paths` | 64 items x 512 characters each |
| `events` | 200 items x 4,000 characters |
| `signals` | 64 items, summary 1,000 characters, `payload` 24 scalar entries x 500 characters |
| `system_refs` | 32 items x 1,000 characters |
| `redactions` | 32 items x 100 characters |
| `previous_assessment` | bounded by `assessment.schema.json` |

`signal.payload` is a bounded scalar map rather than a free-form object; nested structure is rendered into the summary instead. `used_characters` is measured on the final serialized request, not on the packet in isolation, and `used_characters <= budget_characters` is a domain invariant enforced in §5.3 validation, since JSON Schema cannot express a cross-field comparison.

The per-event and per-packet caps multiply to roughly 800,000 characters, so the event cap alone bounds nothing useful. The total budget is authoritative and the per-field caps are secondary guards. When the budget is exceeded, the builder evicts in a fixed order and records what it dropped:

1. `file_read` events, collapsed into a count and a path set;
2. `reasoning` events, oldest first;
3. command output excerpts, tightened toward exit status only;
4. remaining events oldest first, folded into counters attributed to the previous assessment.

Goal text is truncated only as a last resort, after every event class has been evicted, and truncation is recorded. Signals are never evicted; a packet that cannot fit its signals and goal within the budget fails closed to a rule-only assessment. The packet records eviction counts per class so an assessment can state that its window was truncated, and so the model is never silently asked to reason from a partial window it believes is complete.

Model spend is bounded independently of packet size. Configuration sets a per-session and a daily ceiling for assessment calls and estimated cost; on breach Watchtower stops invoking Luna and Terra, continues deterministic monitoring, and reports a `dependency_unavailable` signal with reason `budget_exhausted`. Deterministic critical signals still notify.

### 5.7 Model assessors

Both assessors return an assessment via structured output, using two schema artifacts:

- `schemas/assessment.wire.schema.json` is sent to the provider. Strict structured-output modes reject `const`, `format`, `minLength`, `maxLength`, `minimum`, `maximum`, `minItems`, `maxItems`, `uniqueItems`, `oneOf`, and external `$ref`, which covers most of the constraint surface of the authoritative schema. The wire projection drops those keywords, sets `additionalProperties: false`, lists every property in `required`, and carries the removed bounds as prose in `description` so the model still sees them.
- `schemas/assessment.schema.json` remains authoritative. Every response is validated against it after parsing; a response that satisfies the wire schema but violates a bound is invalid and takes the retry path in §5.8.

Nothing is relaxed by this split. The wire schema is a projection, not a second contract: CI regenerates it from the authoritative schema and fails when the two diverge in shape, enum membership, or nullability.

The observation packet is passed as request content, not as a provider-side schema, so it is unaffected.

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

- any deterministic critical signal is present;
- deterministic evidence conflicts with Luna;
- Luna reports `goal_alignment` of `possibly_aligned` or `misaligned`;
- Luna reports `status` of `stalled`, `looping`, or `off_scope`;
- stagnation exceeds 25 minutes;
- the operator requests a deep assessment.

The trigger list separates `goal_alignment` values from `status` values, which are different fields with different enums and were previously mixed into one list.

Self-reported confidence is not a trigger. A `0.72` threshold on a model's own confidence assumes a calibration that language models generally do not have, and carries more precision than the underlying number supports. Confidence is recorded, surfaced in the API, and used as a tie-breaker when another trigger already fired; a numeric threshold may be added later if calibration data shows the value separates outcomes.

Terra receives the same observation plus Luna's assessment and an explicit request to adjudicate conflicts. It does not receive the entire transcript unless a future operator-authorized forensic mode is added.

### 5.8 Policy reconciler

The reconciler produces the authoritative assessment:

1. deterministic critical signals force `needs_attention=true`;
2. a Terra result supersedes Luna prose but not deterministic evidence;
3. unsupported model evidence references invalidate the response and trigger one retry;
4. a second invalid response falls back to a rule-generated assessment;
5. Codex `TurnComplete`/wire `task_complete` changes state to `between_turns`; it never proves terminal session completion;
6. terminal completion requires zero-exit process evidence from §5.11 plus a configurable quiet grace period with no new turn, or an explicit operator-provided terminal marker;
7. terminal failure requires non-zero process exit evidence from §5.11 or an explicit deterministic process failure rule;
8. for an unobserved session, where no process evidence exists, the quiet grace period alone yields `terminal_completed_unconfirmed`, which notifies and closes the run report but is never reported as a confirmed outcome.

#### Session state and assessment status

The two enums are distinct and neither replaces the other. Session `state` is deterministic lifecycle, owned by §5.1 and §5.11 and derived only from explicit evidence. Assessment `status` is the reconciled operator-facing verdict, owned by the reconciler and consumed by §5.9 and §5.10.

The projection is fixed, and the reconciler applies it before any model output is considered:

| Session state | Assessment status |
| --- | --- |
| `active_turn` | `progressing`, or `investigating`, `stalled`, `looping`, `off_scope` when a rule or model narrows it |
| `between_turns` | `between_turns` |
| `waiting` | `waiting` |
| `terminal_completed` | `terminal_completed` |
| `terminal_completed_unconfirmed` | `terminal_completed_unconfirmed` |
| `terminal_failed` | `terminal_failed` |
| `unknown` | `unknown` |

`progressing`, `investigating`, `stalled`, `looping`, and `off_scope` are refinements of `active_turn` only. A model may narrow within the row its session state permits; it may never move the status to a different row. Terminal and waiting states are therefore never model-assigned, and a reassuring assessment cannot promote a session out of `terminal_failed`.

A third field, `notification_status`, is what §5.10 delivers on. It is computed from deterministic evidence alone and never from model output, so a model narrowing is visible in the API and in message bodies without being able to trigger or suppress a message. The reconciler emits all three: `state` (deterministic lifecycle), `status` (display, may be model-narrowed), and `notification_status` (deterministic, delivery-authoritative).

### 5.9 Status and operator API

Local bind only by default: `127.0.0.1`.

Proposed endpoints:

- `GET /healthz`
- `GET /api/v1/sessions`
- `GET /api/v1/sessions/{id}`
- `GET /api/v1/sessions/{id}/events?after=<event_sequence>`
- `GET /api/v1/sessions/{id}/assessment`
- `POST /api/v1/sessions/{id}/assess` for an authenticated, rate-limited operator-requested Terra escalation
- `GET /api/v1/events` for SSE state updates.

No mutation of Codex or the workspace is exposed. The POST endpoint does mutate Watchtower state and can incur model cost. It is disabled unless an operator token is configured and requires bearer authentication, strict Origin/Host allowlists, disabled wildcard CORS, per-session concurrency limits, global rate/cost limits, and idempotency keys. The default remains loopback-only, but loopback is not treated as authentication.

### 5.10 Telegram notifier

#### Notification status

The notifier reads `notification_status`, not the assessment `status`. In advisory mode `notification_status` is derived **only** from deterministic lifecycle and rule signals; model output cannot produce, suppress, or alter it.

This closes a contradiction: §5.8 lets a model narrow `active_turn` to `stalled`, `looping`, or `off_scope`, and this section notifies on exactly those values, so model output would have driven delivery despite the advisory guarantee. Luna reporting `looping` with no corresponding rule signal changes the displayed status and the message body, and sends nothing.

`notification_status` takes `stalled`, `looping`, or `off_scope` only from the matching rule signal (`stagnation`, `repeated_command`/`recurring_error`, `scope_expansion`/`forbidden_path`); every other value comes from the deterministic session state of §5.1 and §5.11. In v0.2.0, promoting model output to a notification input means allowing it to set this field, and that promotion is what the calibration gates in §10.3 authorize.

Delivery policy:

Send when:

- a deterministic signal raises `needs_attention` from false to true;
- `notification_status` enters `waiting`, `stalled`, `looping`, `off_scope`, `terminal_failed`, `terminal_completed`, or `idle`;
- a warning persists and materially changes;
- a configured periodic digest is due, default disabled.

Do not send when only wording changes.

Deduplication key:

```text
session_id + status_epoch + notification_status + signal_fingerprint
```

The key excludes the event cursor. The cursor advances on every ingested event, so including it would make the key unique per assessment and suppress no duplicate at all; it would also re-notify after a restart replayed the same window under a new cursor.

`status_epoch` is a per-session counter incremented every time `notification_status` changes to a different value. Without it the key persists across transitions, so a `waiting → active_turn → waiting` cycle would reuse the key of the first `waiting` and be suppressed, contradicting the rule that entering `waiting` sends. The epoch makes each entry into a state a distinct episode while still collapsing repeats within one episode.

`signal_fingerprint` is the sorted set of `(severity, kind, signal_id)` triples over the **deterministic signals** active at delivery time. It is computed from the rule engine, whose signals carry a stable `id`, `kind`, and `severity` by schema. It is deliberately not computed from assessment concerns: `concerns[].evidence_refs` is a bare string array whose entries may point at event, signal, or system evidence and carry no `ref_type`, so no stable typed identity can be recovered from it, and a concern citing only event or system evidence would yield no signal identity at all.

For each key Watchtower persists the last delivery, its cursor, and its send time. On a repeat key it resends only when a configured cooldown has elapsed and a critical signal is still active, or when a periodic digest is due. The cursor is carried in the message body as provenance, never in the key.

A message includes elapsed time, current action, alignment, concerns, latest test result, redacted changed paths, and session ID/API base when available. Telegram is a separate remote sink: it always applies trusted-remote redaction, never sends prompts or command-output excerpts, validates the configured `chat_id` against an allowlist, escapes Telegram markup, and omits local deep links.

### 5.11 Launcher and process evidence

Rollout JSONL alone cannot distinguish a finished session from an idle one: the last record of a completed run and of a run paused between turns are the same shape. Terminal states in §5.8 therefore require evidence from outside the transcript. Watchtower obtains it in one of three ways, in descending order of confidence.

**Wrapped run (preferred).** `watchtower run -- codex exec ...` spawns Codex as a child, passes stdio through unchanged, and writes a process-evidence record to the state directory:

```text
{launch_id, argv_hash, pid, started_at, workspace, goal, expected_paths, forbidden_paths, exit_code, exited_at}
```

The record is created before spawn and updated on exit, including on signal termination. The launcher never inspects, filters, or alters Codex output; it only observes process lifetime. Correlation to a rollout file uses the first `session_meta` record written by that PID under the sessions root after `started_at`; ambiguity yields no correlation rather than a guess.

**Adopted run.** For a session started outside the wrapper, Watchtower may adopt it when the operator enables process adoption: it matches a live Codex process whose working directory equals the session workspace and whose start time precedes the first rollout record, then watches for that PID to disappear. Disappearance without a recorded exit code yields `terminal_completed_unconfirmed`, never `terminal_failed`, because the exit status is unknown.

**Unobserved run.** With neither wrapper nor adoption, only rule 8 of §5.8 applies: after the quiet grace period the session becomes `terminal_completed_unconfirmed`.

Process evidence is a `process_lifecycle` event with `exit_code` set. The launcher is optional; its absence degrades terminal-state confidence and nothing else. It performs no Codex mutation and satisfies the non-goal in §3, since it neither steers nor sends text to Codex.

## 6. State and persistence

Use SQLite in WAL mode.

Core tables:

- `sessions`
- `process_evidence`
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

Calibration gates v0.2.0, not v0.1.0. It requires at least 30 completed real sessions covering looping, off-scope, and failed outcomes, plus hand labeling with evidence IDs; that corpus does not exist yet and cannot be manufactured honestly. Blocking the first release on it would withhold the deterministic rules, which are useful without any model at all.

v0.1.0 therefore ships in **advisory mode**: deterministic rules are authoritative and notify on their own, Luna assessments are produced and shown in the API and in message bodies, and no notification decision depends on model output. The guarantee is structural, not a convention: the notifier reads `notification_status`, which §5.8 computes from deterministic evidence only, so there is no path from model output to a delivery decision. Terra escalation is available but off by default. Advisory mode is not a degraded fallback; it is the supported first release, and the failure modes calibration protects against cannot occur while notification is rule-driven.

v0.2.0 promotes model output to a notification input only after the gates below pass.

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

Gates for promoting model output out of advisory mode in v0.2.0:

- no unsupported evidence citations in the acceptance corpus;
- attention recall at least 0.90 on deterministic anomaly cases;
- false attention rate at most 0.15 on healthy sessions;
- Terra measurably improves ambiguous-case accuracy over Luna.

The Terra gate is qualitative on purpose. A fixed 10-percentage-point threshold measured on the ambiguous subset of a 30-session corpus is decided by one or two labels, which is noise rather than evidence. Either collect at least 30 ambiguous cases before applying a numeric threshold, or make the keep-or-remove decision from the reviewed disagreement set with recorded rationale and reviewer sign-off. Do not report a percentage-point improvement from a handful of labels as if it were a measurement.

## 11. MVP acceptance criteria

Criteria 1-11 gate v0.1.0 in advisory mode. Criterion 12 gates v0.2.0, when model output first influences notification.

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
10. A wrapped run reaches `terminal_completed` or `terminal_failed` from process evidence, an unobserved run reaches `terminal_completed_unconfirmed` from the quiet grace period, and both emit a completion notification containing the final status, elapsed time, changed files, tests observed, and session identifier.
11. The local API and SSE stream survive malformed and unknown Codex events.
12. For v0.2.0 only: the frozen calibration report is committed and the promotion gates are met, or the system stays in advisory mode.

## 12. Open design choices deferred to implementation

These are configuration choices, not missing requirements:

- Python versus Rust implementation: the plan starts with Python for parser/model iteration speed and leaves a measured rewrite threshold.
- Exact Luna/Terra identifiers: resolved and verified by the mandatory model contract spike before implementation.
- Telegram transport: direct Bot API or existing Hermes delivery adapter; both must implement the same notifier and remote-sink privacy interface.
- Codex Trace deep-link format: absent from MVP unless the mandatory compatibility spike verifies a stable URL contract.

These decisions must be resolved by Phase -1 spikes before affected interfaces are frozen; unsupported integrations fail closed rather than being guessed.
