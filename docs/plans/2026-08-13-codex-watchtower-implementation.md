# Codex Watchtower Implementation Plan

**Goal:** Build a local-first monitor that never mutates Codex or its workspace, explains autonomous Codex CLI progress, detects unhealthy behavior, and escalates ambiguous cases from Luna to Terra.

**Architecture:** A Python service tails persisted Codex rollout JSONL incrementally, normalizes and redacts events before persistence, optionally consumes only version-verified AgentLens fields, applies deterministic rules, and sends bounded observation packets to structured-output model assessors. SQLite persists cursors and redacted assessments; a local HTTP/SSE API and optional Telegram notifier expose reconciled, evidence-backed state without mutating Codex or the workspace.

**Tech stack:** Python 3.12, `uv`, Pydantic v2, SQLite/WAL, FastAPI, `watchfiles`, `httpx`, `mcp`, `jsonschema`, `pytest`, `pytest-asyncio`, `respx`, Ruff, mypy.

**Implementation rule:** Every code task follows red-green-refactor. Commits below are suggested logical checkpoints; merge small adjacent steps when the diff would otherwise be noise.

---

## Phase -1: Contract spikes before interface freeze

### Task 0A: Freeze actual Codex lifecycle and process evidence

**Objective:** Prove the distinction between turn completion and terminal session completion before defining domain enums.

**Files:**

- Create: `spikes/codex-lifecycle/README.md`
- Create: `tests/fixtures/codex/lifecycle/`

**Steps:**

1. Capture redacted fixtures with multiple `TurnStarted`/`TurnComplete` pairs in one rollout.
2. Record launcher PID/exit behavior for `codex exec --json` and persisted interactive sessions.
3. Define terminal completion as process exit plus quiet grace period, or explicit launcher marker; never map wire `task_complete` directly to terminal completion.
4. Commit: `spike: verify codex lifecycle semantics`.

### Task 0B: Freeze AgentLens and Codex Trace compatibility contracts

**Objective:** Prevent speculative third-party interfaces from shaping the core domain.

**Files:**

- Create: `spikes/agentlens/README.md`
- Create: `spikes/agentlens/fixtures/`
- Create: `spikes/codex-trace/README.md`
- Create: `spikes/codex-trace/fixtures/`

**Steps:**

1. Pin AgentLens and record exact `get_recent_sessions`, `get_session_detail`, and `get_efficiency_report` input/output fixtures from `http://127.0.0.1:4316/mcp`.
2. Prove canonical Codex correlation or declare AgentLens enrichment disabled for that version; do not guess from minute-level dates.
3. Pin Codex Trace and record `GET /api/settings`, `POST /api/sessions`, and `POST /api/session/load` request/response fixtures.
4. Confirm that only API base plus session ID is stable; do not promise a clickable deep link without a verified URL contract.
5. Commit: `spike: freeze optional integration contracts`.

### Task 0C: Verify Luna/Terra provider and remote transport

**Objective:** Prove model identifiers, structured output, endpoint trust, and cost behavior before assessor implementation.

**Files:**

- Create: `spikes/models/README.md`
- Create: `spikes/models/redacted-results.json`

**Steps:**

1. Resolve deployment-specific Luna/Terra identifiers and structured-output support.
2. Verify keyword support explicitly rather than structured-output support in general: submit `schemas/assessment.schema.json` unmodified, record which of `const`, `format`, `minLength`, `maxLength`, `minimum`, `maximum`, `minItems`, `maxItems`, `uniqueItems`, `oneOf`, and external `$ref` are rejected, then confirm `schemas/assessment.wire.schema.json` is accepted. Discovering this in Phase 6 instead forces a schema rewrite mid-cascade.
3. Record the result as a capability matrix keyed by provider, model, and API version in `spikes/models/README.md`. The wire schema is conservative pending this matrix; do not assert that a specific keyword is unsupported anywhere until measured here.
4. Smoke one schema-valid request per profile after explicit operator approval.
5. Verify HTTPS certificate validation, redirect rejection, endpoint allowlisting, disabled proxy inheritance, response-size cap, retry budget, and provider retention/logging policy.
6. Record latency and cost without storing prompts or credentials.
7. Commit: `spike: verify assessment model contracts`.

## Phase 0: Repository and quality gates

### Task 1: Create the Python package and CI baseline

**Objective:** Establish a reproducible package with lint, type-check, test, and schema-validation commands.

**Files:**

- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `src/codex_watchtower/__init__.py`
- Create: `src/codex_watchtower/cli.py`
- Create: `tests/test_smoke.py`
- Create: `.github/workflows/ci.yml`
- Modify: `.gitignore`

**Steps:**

1. Add a failing smoke test importing `codex_watchtower` and invoking `--version`.
2. Run `uv run pytest tests/test_smoke.py -q`; expect import failure.
3. Add minimal package metadata and CLI entry point.
4. Add Ruff and mypy configuration.
5. Run:

   ```bash
   uv sync --all-groups
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   uv run pytest -q
   ```

6. Add CI running the same commands on Linux with Python 3.12.
7. Commit: `chore: initialize watchtower service`.

### Task 2: Validate committed JSON Schemas

**Objective:** Make schema correctness and example compatibility part of CI.

**Files:**

- Create: `tests/schemas/test_schemas.py`
- Create: `tests/fixtures/observations/healthy.json`
- Create: `tests/fixtures/assessments/healthy.json`
- Create: `tests/fixtures/reconciled/healthy.json`
- Modify: `pyproject.toml`

**Steps:**

1. Write tests loading all four schemas and validating healthy examples.
2. Add one deliberately invalid in-test assessment and assert validation failure.
3. Assert `assessment.wire.schema.json` stays aligned with `assessment.schema.json`: identical property sets, enum members, and nullability at every level, `additionalProperties: false` and fully populated `required` everywhere, and no strict-mode-rejected keyword present.
4. Assert every payload valid under the authoritative schema is also valid under the wire schema, so the projection can only be more permissive.
5. Run the focused tests; verify red before fixture/schema wiring and green after.
6. Commit: `test: enforce observation and assessment schemas`.

## Phase 1: Domain model and durable state

### Task 3: Define typed domain models

**Objective:** Represent sessions, events, signals, observations, and assessments without leaking provider-specific records into core logic.

**Files:**

- Create: `src/codex_watchtower/domain.py`
- Create: `tests/unit/test_domain.py`

**Steps:**

1. Write tests for enum values, `confidence_percent` integer bounds, unique evidence references, and schema serialization.
2. Implement Pydantic models matching the committed JSON Schemas.
3. Enforce every domain invariant listed in §5.3, one test each: timestamps and window containment, unique/monotonic event IDs and sequences, `opened_at <= closed_at`, `from_cursor < to_cursor`, `used_characters <= budget_characters`, `signal.event_ids` resolving to packet events, evidence refs resolving to packet event/signal/`system_refs` IDs, `basis_ids` resolving to `evidence[].ref_id`, `supersedes < report_version`, and `run_id`/`execution_epoch` being null or set together.
4. Test the session-state to assessment-status projection: each state permits only its documented statuses, and a model narrowing within `active_turn` is accepted while a cross-row move is rejected.
5. Verify serialized fixtures with `jsonschema[format]` and `FormatChecker`.
6. Commit: `feat: add typed watchtower domain model`.

### Task 4: Create SQLite migrations and repository interface

**Objective:** Persist sessions, executions, process evidence, cursors, normalized events, signals, assessments, model calls, and deliveries in WAL mode.

**Files:**

- Create: `src/codex_watchtower/storage/migrations/001_initial.sql`
- Create: `src/codex_watchtower/storage/db.py`
- Create: `src/codex_watchtower/storage/repository.py`
- Create: `tests/unit/storage/test_repository.py`

**Steps:**

1. Write tests for migration idempotence and WAL mode.
2. Test atomic insertion of an already-redacted event and cursor update; raw rollout payloads must never enter SQLite.
3. Test event deduplication by logical event ID, including concurrent inserts of the same ID, and that the event sequence is assigned exactly once per logical event.
4. Test latest-assessment and pending-delivery queries.
5. Implement the minimal repository using the standard `sqlite3` module and explicit transactions.
6. Run repository tests twice against the same temporary database.
7. Commit: `feat: add durable watchtower state store`.

### Task 5: Add corruption-safe database startup

**Objective:** Refuse silent recreation when the state database is unreadable.

**Files:**

- Modify: `src/codex_watchtower/storage/db.py`
- Create: `tests/unit/storage/test_database_recovery.py`

**Steps:**

1. Seed a corrupt database fixture and assert startup fails with a typed fatal error.
2. Test that the main database file remains byte-identical. Run the check over an immutable URI (`file:...?immutable=1`) so the probe cannot create `-wal` or `-shm` sidecars, and assert none appear.
3. Add `PRAGMA quick_check` startup validation and a clear recovery message.
4. Commit: `feat: fail safely on state database corruption`.

## Phase 2: Codex ingestion

### Task 6: Discover rollout sessions

**Objective:** Discover and identify persisted Codex session files under date-partitioned directories.

**Files:**

- Create: `src/codex_watchtower/codex/discovery.py`
- Create: `tests/unit/codex/test_discovery.py`
- Create: `tests/fixtures/codex/healthy/rollout-sample.jsonl`

**Steps:**

1. Write tests for nested `YYYY/MM/DD` discovery, ordering, ignored files, and missing roots.
2. Parse `session_meta` to obtain ID, workspace, model, origin, and start time; separately capture launcher PID/exit evidence when available.
3. Preserve a session with missing optional metadata as `unknown`, not as a parser failure.
4. Commit: `feat: discover persisted codex sessions`.

### Task 7: Implement incremental complete-line reading

**Objective:** Read only newly appended complete JSONL records and recover safely after restart.

**Files:**

- Create: `src/codex_watchtower/codex/tailer.py`
- Create: `tests/unit/codex/test_tailer.py`

**Steps:**

1. Test an append with a partial final line: no event and no cursor advance beyond the last newline.
2. Complete the line and assert exactly one event appears.
3. Test restart from a stored byte cursor.
4. Test inode replacement, truncation, inode reuse, copy-truncate, fast regrowth beyond the old offset, prefix mismatch, and migration between devices.
5. On a prefix-preserving mismatch, replay from the last verified newline checkpoint or file start, continue the absolute record ordinal, and deduplicate by logical event ID.
6. Test the append-only precondition directly: a prefix-hash mismatch stops ingestion for that session, marks it `identity_broken`, emits a critical `dependency_unavailable` signal naming the file, preserves existing events and their sequences, and re-identifies nothing.
7. Reject symlinks escaping the root, non-regular files, wrong-owner files, FIFO/devices, and configured oversize limits; verify identity again after open.
8. Implement the internal cursor `<device>:<inode>:<offset>:<record-ordinal>:<checkpoint-hash>`, carrying the absolute ordinal so a resumed replay continues the count rather than recomputing it, and assert the cursor is never returned by any API route or embedded in an observation, assessment, or notification.
9. Assign the monotonic per-session event sequence transactionally at first insert, under a unique constraint on the logical event ID; test that a replayed record collides, is ignored, and keeps its original sequence, so notification provenance never changes retroactively.
10. Commit: `feat: tail codex rollouts incrementally`.

### Task 8: Normalize known and unknown Codex events

**Objective:** Convert version-specific rollout records into bounded provider-neutral events.

**Files:**

- Create: `src/codex_watchtower/codex/normalize.py`
- Create: `tests/unit/codex/test_normalize.py`
- Add fixtures under: `tests/fixtures/codex/events/`

**Steps:**

1. Add fixtures for session lifecycle, messages, commands, command output, patches, token updates, errors, and unknown event types.
2. Write expected normalized snapshots.
3. Implement logical event IDs from session ID, absolute record ordinal, kind, and payload hash. Store device/inode/offset only as a source locator. Test that relocating a record to a different offset, as copy-truncate and replay do, yields the same ID and no duplicate row.
4. Regression-test both failure modes of the relative ordinal this replaces: two identical records where recovery starts after the first must not give the survivor the first record's ID, and a count continued from a stored total must not insert a replayed record twice. Both must hold for replay from the file start and from a checkpoint.
5. Redact before persistence, bound command output, and retain only redacted summary plus source hash/original byte length.
6. Ensure unknown events become `kind=unknown` with source type preserved.
7. Add negative fixtures: a `process_lifecycle` event without `run_id`, `execution_epoch`, or `exit_code`, and one with an empty-string `run_id`, must fail schema validation, since execution scope is what keeps a late exit from terminating a later execution.
8. Test that turn boundaries normalize to `turn_lifecycle` and process boundaries to `process_lifecycle`, and that no single `lifecycle` kind is emitted.
9. Commit: `feat: normalize codex rollout events`.

### Task 9: Classify lifecycle without model inference

**Objective:** Distinguish active turns, between-turn pauses, waiting, and terminal process outcomes from explicit evidence.

**Files:**

- Create: `src/codex_watchtower/codex/lifecycle.py`
- Create: `tests/unit/codex/test_lifecycle.py`

**Steps:**

1. Test multiple turn start/complete sequences remain `between_turns`, not terminally completed.
2. Test terminal completion from zero-exit process evidence (Task 9A) plus quiet grace period, and that no path reaches a terminal state without process evidence.
3. Test terminal failure from non-zero exit and explicit process evidence, and that a terminal transition is accepted only from a `process_lifecycle` event whose `run_id` is the session's current execution. A late exit event from execution 1 must not terminate a resumed execution 2.
4. Test a live file with no process evidence remains active/between-turns/unknown, and that quiet-period expiry without process evidence yields `idle`, which is not terminal.
5. Test the reopen transition from `idle`: any new event returns the session to `active_turn`, keeps its session ID, cursors, and event sequence, re-ingests nothing, and advances `status_epoch` **only**. `execution_epoch` and `run_id` must not move: the same process resuming work after a quiet period is not a new execution. An unobserved session keeps both null rather than reporting a fictitious execution 0.
6. Test reopen from an execution-terminal state: a zero-exit wrapped execution reports `terminal_completed`, then `codex exec resume <same-session-id> --json` opens a new execution, returns the session to `active_turn`, and advances `execution_epoch`. No terminal state may be treated as the end of a persisted session.
7. Test that repeated reopen cycles from either path each produce a new report version and a supersede notice.
8. Test counter exhaustion starting **at** the maximum, not one below it, for each of `status_epoch`, `attention_epoch`, `execution_epoch`, and `report_version`. The session must reach `fatal` with the matching reason and `identity_broken`, both epochs must freeze rather than increment or decrease, and the fatal message must deliver under its own identity. The `status_epoch` case is the one that is unreachable if the fatal path depends on incrementing that counter.
9. Test that prose such as “done” does not mark completion.
10. Commit: `feat: derive codex lifecycle from explicit events`.

### Task 9A: Capture process evidence for terminal states

**Objective:** Provide the out-of-transcript evidence that §5.11 and reconciler rules 6-9 require, without which no session can reach a terminal state.

**Files:**

- Create: `src/codex_watchtower/launcher/run.py`
- Create: `src/codex_watchtower/launcher/evidence.py`
- Create: `tests/unit/launcher/test_run.py`
- Create: `tests/unit/launcher/test_adopt.py`
- Modify: `src/codex_watchtower/storage/migrations/001_initial.sql`

**Steps:**

1. Test that `watchtower run -- <cmd>` passes stdio through unchanged, forwards the child exit code, and forwards SIGINT/SIGTERM to the child.
2. Test the record lifecycle: created before spawn as `state=pending` with `pid=null`, updated atomically to `state=running` with the real PID after a successful spawn, and to `state=exited` on normal exit, non-zero exit, and signal termination, including when Watchtower itself is not running. A PID cannot exist before spawn, so nothing may require one there.
3. Test that a failed spawn leaves the record in `pending` and creates no session.
4. Test canonical correlation: with `codex exec --json`, the launcher tees stdout, forwards every byte unmodified, and binds the session identifier the child itself reported.
5. Test snapshot correlation over both candidate kinds: a newly created rollout and an existing rollout that grows, since a resume appends rather than creating a file. Exactly one matching candidate correlates; zero candidates, two concurrent runs in one workspace, and window expiry all record `correlation_method=none` and leave the run unobserved.
6. Test that a launcher binding an existing session ID opens a new execution with the next `execution_epoch` on that session, and never creates a second logical session.
7. Assert no test relies on a PID appearing in `session_meta`; Codex 0.133.0 does not emit one and the watcher cannot attribute a file to a process.
8. Test process adoption: matching workspace and start order, PID disappearance yielding `idle`, and PID reuse rejected by start time.
9. Test that an unobserved session reaches `idle` from the quiet grace period alone and never a terminal state.
10. Emit process evidence as a `process_lifecycle` event carrying `exit_code`, `run_id`, and `execution_epoch`, which the event contract now includes; assert the launcher writes nothing to the child's stdin.
11. Commit: `feat: capture codex process exit evidence`.

### Task 10: Add filesystem watcher orchestration

**Objective:** Wake ingestion on changes without relying on fragile polling alone.

**Files:**

- Create: `src/codex_watchtower/ingest.py`
- Create: `tests/integration/test_ingest_watch.py`

**Steps:**

1. Start ingestion against a temporary sessions root.
2. Append events and assert they reach SQLite once.
3. Restart the service and append again; assert no duplicates.
4. Add periodic reconciliation polling as protection against missed filesystem events.
5. Commit: `feat: orchestrate live codex ingestion`.

## Phase 3: AgentLens enrichment

### Task 11: Implement version-gated AgentLens compatibility boundary

**Objective:** Query only fields proven by Task 0B fixtures without coupling core logic to AgentLens UI or speculative general-schema fields.

**Files:**

- Create: `src/codex_watchtower/agentlens/client.py`
- Create: `src/codex_watchtower/agentlens/types.py`
- Create: `tests/unit/agentlens/test_client.py`

**Steps:**

1. Replay the exact Streamable HTTP MCP fixtures captured in Task 0B for `get_recent_sessions` and `get_session_detail`.
2. Test timeout, malformed response, unavailable server, and successful parsing.
3. Reject unknown AgentLens versions/response shapes with typed degraded status.
4. Expose only fields demonstrated by fixtures; do not synthesize loop/file/tool data absent from the Codex adapter.
5. Commit: `feat: add optional agentlens mcp adapter`.

### Task 12: Correlate AgentLens and Codex sessions only with canonical evidence

**Objective:** Enrich the correct session without creating duplicates or cross-project contamination.

**Files:**

- Create: `src/codex_watchtower/agentlens/correlate.py`
- Create: `tests/unit/agentlens/test_correlate.py`

**Steps:**

1. Test canonical rollout path/session metadata added upstream or proven by the version-pinned local adapter.
2. Test the current filename-vs-session-meta mismatch explicitly.
3. Test absent canonical evidence and ambiguity returning no match instead of guessing.
4. Record correlation method and compatibility version; never use minute-level date/model heuristics.
5. Commit: `feat: correlate codex and agentlens sessions safely`.

### Task 13: Add explicit, schema-pinned SQLite fallback only if Task 0B proves it

**Objective:** Permit read-only AgentLens database snapshots only when configured.

**Files:**

- Create: `src/codex_watchtower/agentlens/sqlite_fallback.py`
- Create: `tests/unit/agentlens/test_sqlite_fallback.py`

**Steps:**

1. Create a minimal AgentLens-compatible fixture database.
2. Open it read-only and assert no WAL/schema mutation.
3. Map only documented fields used by Watchtower.
4. Reject unknown schema versions with degraded status rather than best-effort guessing.
5. If canonical correlation cannot be proven, omit this task and keep AgentLens disabled for the pinned version.
6. Commit: `feat: add guarded agentlens sqlite fallback`.

## Phase 4: Deterministic health signals

### Task 14: Implement command and error repetition rules

**Objective:** Detect loops independently of model output.

**Files:**

- Create: `src/codex_watchtower/rules/repetition.py`
- Create: `tests/unit/rules/test_repetition.py`

**Steps:**

1. Test three identical commands with unchanged outcomes inside the repetition window trigger a warning.
2. Test that the same three occurrences spread beyond the window do not trigger, and that both the time and event bounds are configurable.
3. Test the same command after changed files or changed result does not automatically trigger.
4. Test normalization of volatile timestamps/temp paths.
5. Test three equivalent recurring errors within the window trigger a warning.
6. Commit: `feat: detect repeated command and error loops`.

### Task 15: Implement progress and stagnation rules

**Objective:** Distinguish inactivity from legitimate long-running work.

**Files:**

- Create: `src/codex_watchtower/rules/progress.py`
- Create: `tests/unit/rules/test_progress.py`

**Steps:**

1. Test each documented progress marker.
2. Test 25 minutes without a marker triggers stagnation.
3. Test an active long-running command suppresses the timer until it exits or exceeds its configured maximum.
4. Test a changed hypothesis counts only when materially different.
5. Commit: `feat: detect evidence-based stagnation`.

### Task 16: Implement path-scope and test-trend rules

**Objective:** Detect explicit scope violations and worsening test evidence.

**Files:**

- Create: `src/codex_watchtower/rules/scope.py`
- Create: `src/codex_watchtower/rules/tests.py`
- Create: `tests/unit/rules/test_scope.py`
- Create: `tests/unit/rules/test_test_trend.py`

**Steps:**

1. Test workspace-relative path canonicalization and symlink-safe comparison.
2. Test forbidden paths as critical and unexpected paths as warnings.
3. Test pass/fail count extraction from pytest, cargo test, npm/vitest, and generic exit codes.
4. Test improving, stable, worsening, and unknown trends.
5. Commit: `feat: detect scope and test regressions`.

### Task 17: Reconcile AgentLens signals with local rules

**Objective:** Merge signals without double-counting or reducing severity.

**Files:**

- Create: `src/codex_watchtower/rules/reconcile.py`
- Create: `tests/unit/rules/test_reconcile.py`

**Steps:**

1. Test identical evidence from both sources becomes one signal with two sources, using the context-growth signal, the only field the pinned adapter exposes.
2. Test highest severity wins.
3. Test stale AgentLens data is tagged and cannot overwrite newer local evidence.
4. Commit: `feat: reconcile deterministic health evidence`.

## Phase 5: Redaction and observation packets

### Task 18: Implement secret and path redaction

**Objective:** Prevent sensitive data from reaching remote models or notifications.

**Files:**

- Create: `src/codex_watchtower/privacy/redact.py`
- Create: `tests/unit/privacy/test_redact.py`
- Create: `tests/fixtures/privacy/seeded_secrets.json`

**Steps:**

1. Seed representative bearer tokens, API keys, cookies, private keys, environment secrets, and usernames.
2. Assert no seeded value survives local/trusted-remote/metadata-only policies where prohibited.
3. Add configurable literal and regex redactions.
4. Ensure redaction reports contain classes only.
5. Commit: `feat: redact model and notification payloads`.

### Task 19: Build bounded incremental observations

**Objective:** Construct schema-valid model inputs from only new evidence and prior assessment state.

**Files:**

- Create: `src/codex_watchtower/observations.py`
- Create: `tests/unit/test_observations.py`

**Steps:**

1. Test first packet with no prior assessment.
2. Test subsequent packet starts after the prior cursor.
3. Test event/item/character limits and the total budget measured on the final serialized provider request, not on the packet alone.
4. Prove finiteness structurally: walk both schemas and assert every string has `maxLength`, every array `maxItems`, every numeric `maximum`, and every object with `additionalProperties` a `maxProperties`. This test fails when a future field is added without a cap, which is how the previous gap appeared.
5. Compute the theoretical worst-case serialized request from the schema caps and assert it is finite. Do not assert it fits the budget: 200 events at 4,000 characters already exceed 48,000, so that assertion is unsatisfiable by construction.
6. Prove the runtime bound instead: build a request from maximal inputs (goal at `maxLength`, a previous assessment at its own maxima, 200 maximal events, 64 maximal signals with payloads at `maxProperties`) and assert the builder either emits `serialized_size <= 48_000` after eviction or fails closed to a rule-only assessment with no model call. Assert on the bytes handed to the transport, including prompt scaffolding.
7. Test that every parsed field, `confidence_percent` included, re-serializes within its bound, and that no provider text is passed through verbatim. The pre-parse byte cap is the model client's boundary and is tested in Task 20, where the response is actually read.
8. Test the documented eviction order, that signals are never evicted, that goal text truncates only after all event classes, and that a packet whose signals and goal exceed the budget fails closed to a rule-only assessment.
9. Test that `truncation` counts are populated on eviction and zeroed on a complete window.
10. Test that `system_refs` carries every citable non-event fact and that a `sys:` ID absent from the packet is rejected downstream.
11. Test overflow folding and no source-file bodies by default.
12. Validate generated packets against `observation.schema.json`.
13. Commit: `feat: build bounded observation packets`.

## Phase 6: Luna/Terra assessment cascade

### Task 20: Define provider-neutral structured model client

**Objective:** Keep model names and endpoint mechanics out of assessment policy.

**Files:**

- Create: `src/codex_watchtower/models/client.py`
- Create: `src/codex_watchtower/models/config.py`
- Create: `tests/unit/models/test_client.py`

**Steps:**

1. Define `assess(model_profile, observation, schema)` interface.
2. Test OpenAI-compatible structured-output request construction with `respx`, asserting the request carries the wire schema and never the authoritative one.
3. Test the response transport boundary here, where the client actually reads it. Assert on retained bytes, not on bytes read: at most `model.max_response_bytes` (default 1 MiB, configurable 64 KiB to 8 MiB) reaches the parser, at most one bounded overflow probe is read beyond it, probe bytes are never retained or parsed, and total bytes read in an attempt are at most `cap + chunk_size`. Use responses of exactly `cap` and `cap + 1` bytes with no `Content-Length`, since those are indistinguishable until the probe.
4. Test that an oversized response is classified non-retryable and fails closed to a rule-only assessment, and that the cumulative ceiling `(retry_budget + 1) x (cap + chunk_size)` bounds all attempts together.
5. Test that a response valid under the wire schema but violating an authoritative bound is rejected and takes the retry path.
6. Test timeout, invalid JSON, schema mismatch, and retry classification.
7. Ensure no tools are supplied to the assessment model.
8. Commit: `feat: add structured model assessment client`.

### Task 21: Implement Luna assessment

**Objective:** Produce routine evidence-backed status summaries.

**Files:**

- Create: `src/codex_watchtower/models/prompts/luna.txt`
- Create: `src/codex_watchtower/assess/luna.py`
- Create: `tests/unit/assess/test_luna.py`

**Steps:**

1. Test prompt-injection content is framed as untrusted evidence.
2. Test valid assessment parsing and evidence-ID validation.
3. Test unsupported event IDs invalidate the response.
4. Test one retry followed by rule-only fallback.
5. Commit: `feat: add luna incremental assessor`.

### Task 22: Implement Terra escalation and policy reconciliation

**Objective:** Escalate only documented cases and preserve deterministic authority.

**Files:**

- Create: `src/codex_watchtower/models/prompts/terra.txt`
- Create: `src/codex_watchtower/assess/terra.py`
- Create: `src/codex_watchtower/assess/policy.py`
- Create: `tests/unit/assess/test_policy.py`
- Create: `tests/unit/assess/test_reconciled_schema.py`

**Steps:**

1. Parametrize every escalation trigger from the specification, keeping `goal_alignment` and `status` triggers distinct.
2. Verify healthy Luna results do not call Terra, and that `confidence_percent` alone never triggers escalation.
3. Verify advisory mode structurally against `schemas/reconciled_assessment.schema.json`: the reconciler emits `state`, `status`, and `notification_status`; only the first and third are derivable without model output, and no model-narrowed status reaches the notifier. Assert the reconciled result validates and that `assessment.schema.json` alone cannot represent it, so the boundary stays a contract rather than a convention.
4. Test that a reconciled result with `model_assessment: null` is valid and fully populated, which is the rule-only and budget-exhausted path.
5. Add negative fixtures for every combination the projection forbids, each asserted invalid against the committed schema: `state=idle` with `status=terminal_failed` or `notification_status=identity_broken`; `state=idle` with a non-provisional report; a terminal state with `run_id=null`; and `report_version=1` with a non-null `supersedes`.
6. Test the domain invariants JSON Schema cannot express: `supersedes < report_version` with a chain that never reuses or decreases a version, and `signal_fingerprint` equal to the canonical digest of `active_signals`, including that a mismatched digest is rejected.
7. Add negative fixtures for the relations now encoded in the schema: `notification_status` of `stalled`, `looping`, or `off_scope` with no matching signal kind in `active_signals`; an active critical signal with `needs_attention: false`; `fatal` set without `identity_broken`; `identity_broken` without `fatal`; an empty-string `run_id` on a terminal result; a `fatal` with no active critical signal; a `prefix_mismatch` without a critical `dependency_unavailable` signal or without a detail naming the file; and each `*_exhausted` reason paired with a counter below its maximum.
8. Test the fatal path end to end from a saturated `status_epoch`: the session enters `fatal` out of band, epochs freeze, and the message deduplicates on `session_id + "fatal" + reason` with no epoch in the key.
9. Verify deterministic critical signals force attention despite reassuring model output.
10. Verify Terra prose supersedes Luna only when valid.
11. Verify timeout falls back to deterministic assessment.
12. Verify both epochs: `status_epoch` advances only on a `notification_status` change, `attention_epoch` only on a deterministic `needs_attention` false-to-true transition, and neither is advanced by model output.
13. Commit: `feat: add terra escalation policy`.

### Task 23: Schedule assessments by evidence change

**Objective:** Avoid calling models on every filesystem event or fixed noisy interval.

**Files:**

- Create: `src/codex_watchtower/assess/scheduler.py`
- Create: `tests/unit/assess/test_scheduler.py`

**Steps:**

1. Use a fake clock to test ten-minute routine cadence.
2. Test material progress and lifecycle changes trigger immediate assessment.
3. Test wording-only and duplicate signal changes do not trigger.
4. Test per-session concurrency lock and coalescing.
5. Commit: `feat: schedule low-noise assessments`.

## Phase 7: Local API and Codex Trace integration

### Task 24: Expose read-only status API and guarded operator assessment action

**Objective:** Provide machine-readable current state without exposing Codex mutation.

**Files:**

- Create: `src/codex_watchtower/api/app.py`
- Create: `src/codex_watchtower/api/routes.py`
- Create: `tests/integration/test_api.py`

**Steps:**

1. Write failing tests for all specified endpoints.
2. Implement pagination over the per-session event sequence, and return session and assessment routes as `reconciled_assessment.schema.json` payloads; assert no route exposes a tailer file cursor.
3. Implement SSE state transitions with reconnect cursor support.
4. Assert the default bind is `127.0.0.1`.
5. Disable POST assessment unless an operator token is configured; test bearer auth, strict Origin/Host checks, no wildcard CORS, idempotency, rate/cost budgets, and concurrency limits.
6. Assert no endpoint can submit input or mutate Codex/workspace; document that assessment POST mutates Watchtower state and consumes quota.
7. Commit: `feat: expose local watchtower status api`.

### Task 25: Add optional Codex Trace adapter and manual drill-down identifiers

**Objective:** Detect Codex Trace and attach a drill-down target without making it required.

**Files:**

- Create: `src/codex_watchtower/codex_trace/client.py`
- Create: `tests/unit/codex_trace/test_client.py`

**Steps:**

1. Mock `GET /api/settings`, `POST /api/sessions`, and `POST /api/session/load` with Task 0B fixtures.
2. Test unavailable and incompatible responses degrade cleanly.
3. Verify session correlation by path/session ID.
4. Keep deep-link generation disabled until a stable URL contract is verified; expose API base and session ID meanwhile.
5. Treat Codex Trace as an unauthenticated loopback service with permissive CORS: never proxy it externally or widen its bind.
6. Commit: `feat: integrate optional codex trace drill-down`.

## Phase 8: Telegram delivery

### Task 26: Implement notification rendering and fingerprinting

**Objective:** Render concise messages and avoid wording-only duplicates.

**Files:**

- Create: `src/codex_watchtower/notify/render.py`
- Create: `src/codex_watchtower/notify/policy.py`
- Create: `tests/unit/notify/test_policy.py`
- Create: `tests/golden/telegram/*.md`

**Steps:**

1. Add golden messages for warning, waiting, terminal failure, and terminal completion states.
2. Test transition policy and `signal_fingerprint` over `(severity, kind, signal_id)` triples taken from the rule engine, not from assessment concerns.
3. Test the advisory guarantee: Luna reporting `looping` with no corresponding rule signal changes the displayed status and message body but sends nothing.
4. Test `status_epoch`: a `waiting -> active_turn -> waiting` cycle sends twice, while repeats inside one episode send once.
5. Test `attention_epoch`: a signal that activates, clears, and reactivates while `notification_status` stays `progressing` sends twice, since each is a distinct `needs_attention` false-to-true transition. This is the case `status_epoch` alone does not cover.
6. Test that entering `identity_broken` always sends, including when `needs_attention` was already true from an unrelated signal so no false-to-true transition occurs. Ingestion has stopped, and an operator must never silently lose monitoring of a session.
7. Test changed prose with identical evidence is suppressed.
8. Test that an advanced event cursor alone does not change the deduplication key, and that a restart replaying the same window sends nothing.
9. Test the critical-signal cooldown resend and the digest path.
10. Test critical evidence always includes a factual reason and event cursor in the body.
11. Apply trusted-remote redaction, path minimization, Telegram markup escaping, `chat_id` allowlisting, and omission of prompts/output/local links.
12. Commit: `feat: render deduplicated operator notifications`.

### Task 27: Add Telegram Bot API notifier

**Objective:** Deliver durable optional notifications without storing credentials.

**Files:**

- Create: `src/codex_watchtower/notify/telegram.py`
- Create: `tests/unit/notify/test_telegram.py`

**Steps:**

1. Mock successful, rate-limited, transient, and permanent Bot API responses.
2. Store pending delivery and retry state, not the token.
3. Implement exponential backoff with Telegram `retry_after` support.
4. Mark sent only after confirmed API success.
5. Commit: `feat: deliver watchtower alerts to telegram`.

## Phase 9: Configuration and operations

### Task 28: Add validated configuration and sample file

**Objective:** Make trust mode, paths, thresholds, models, endpoints, and delivery explicit.

**Files:**

- Create: `src/codex_watchtower/config.py`
- Create: `config.example.toml`
- Create: `tests/unit/test_config.py`
- Modify: `README.md`

**Steps:**

1. Test safe defaults: loopback bind, Telegram off, AgentLens optional, remote model trust not assumed.
2. Test invalid threshold/model/endpoint combinations fail at startup.
3. Document Luna/Terra profile mapping without hard-coding deployment-specific names.
4. Test the packet character budget, `model.max_response_bytes` and its 64 KiB to 8 MiB range, the per-session assessment ceiling, and the daily cost ceiling, including that a breach stops model calls, emits `dependency_unavailable` with reason `budget_exhausted`, and leaves deterministic notification intact.
5. Require explicit consent before first remote transmission and validate HTTPS allowlist, redirect/SSRF policy, proxy handling, response limits, retry budget, and provider retention policy.
6. Commit: `feat: add safe watchtower configuration`.

### Task 29: Add service CLI and systemd unit

**Objective:** Run discovery, ingestion, assessment, API, and notifier as one supervised process.

**Files:**

- Modify: `src/codex_watchtower/cli.py`
- Create: `deploy/codex-watchtower.service`
- Create: `docs/operations.md`
- Create: `tests/integration/test_cli.py`

**Steps:**

1. Add `watchtower serve`, `watchtower run`, `watchtower inspect`, `watchtower assess`, and `watchtower doctor`.
2. Test `doctor` against missing Codex root, unavailable AgentLens, invalid model config, and healthy local setup.
3. Ensure service hardening includes no root user, private temp, restart policy, and explicit environment file.
4. Document backup/recovery of the SQLite state.
5. Commit: `feat: add service lifecycle and diagnostics`.

### Task 30: Add structured internal observability

**Objective:** Diagnose Watchtower without leaking transcript content.

**Files:**

- Create: `src/codex_watchtower/telemetry.py`
- Create: `tests/unit/test_telemetry.py`
- Modify: `docs/operations.md`

**Steps:**

1. Test structured log fields and content redaction.
2. Add counters and latency histograms listed in the specification.
3. Assert raw prompts, output, tokens, and paths are absent from metric labels.
4. Commit: `feat: instrument watchtower safely`.

## Phase 10: Replay corpus and model calibration

Phase 10 gates v0.2.0, not v0.1.0. v0.1.0 ships in advisory mode per specification section 10.3: deterministic rules notify on their own and no notification decision depends on model output, so the corpus is not on the first release's critical path. Tasks 31 and 32 may begin as soon as completed sessions accumulate.

### Task 31: Build redacted replay fixture tooling

**Objective:** Convert completed rollouts into reviewable fixtures without committing secrets or proprietary source content.

**Files:**

- Create: `tools/redact_rollout.py`
- Create: `tests/unit/tools/test_redact_rollout.py`
- Create: `docs/evaluation/corpus-format.md`

**Steps:**

1. Test deterministic pseudonymization of workspace paths and filenames.
2. Test stripping source bodies, tokens, and command secrets.
3. Preserve event order, exit codes, test counts, and hashes required for behavior analysis.
4. Require an explicit output path and refuse overwrite by default.
5. Emit provenance metadata: source owner, repository visibility/classification, collection authority, license/usage basis, reviewer, and deletion contact.
6. Refuse private/third-party repository material unless written inclusion authority is recorded; document history-rewrite and credential-rotation procedure for a leaked fixture.
7. Commit: `tools: add safe rollout fixture redaction`.

### Task 32: Create the frozen labeled evaluation set

**Objective:** Calibrate Luna/Terra using real behavior rather than intuition.

**Files:**

- Create: `evaluation/corpus/README.md`
- Create: `evaluation/labels.schema.json`
- Create: `evaluation/labels/*.json`
- Create: `evaluation/manifest.json`

**Steps:**

1. Smoke first with five sessions and manually inspect every redaction.
2. Obtain operator approval before processing the remaining sessions if model calls are paid or long-running.
3. Expand to at least 30 sessions across all specified outcome classes.
4. Add human labels with evidence event IDs and reviewer sign-off.
5. Define class-distribution targets before collection; record provenance/license metadata for every session.
6. Freeze the manifest with fixture SHA-256 hashes.
7. Commit: `eval: add frozen watchtower assessment corpus`.

### Task 33: Implement Luna/Terra evaluation harness

**Objective:** Measure whether the cascade earns its complexity.

**Files:**

- Create: `evaluation/run.py`
- Create: `evaluation/metrics.py`
- Create: `tests/unit/evaluation/test_metrics.py`
- Create: `docs/evaluation/report-template.md`

**Steps:**

1. Unit-test macro-F1, attention precision/recall, false-attention rate, citation validity, latency, and token metrics.
2. Add deterministic replay with cached model outputs.
3. Add `--smoke 5` and `--full` modes.
4. Run smoke only; review cost/latency/results with the operator.
5. After approval, run full evaluation and write a dated report.
6. Enforce the release gates from the specification.
7. Report the ambiguous-case count alongside any accuracy comparison, and refuse to emit a percentage-point verdict below 30 ambiguous cases.
8. Decide keep-or-remove for the cascade from the reviewed disagreement set with recorded rationale and reviewer sign-off; if Terra does not improve ambiguous cases, disable automatic escalation and document it.
9. Commit: `eval: calibrate luna and terra assessment cascade`.

## Phase 11: End-to-end verification and release

### Task 34: Add two-hour accelerated replay test

**Objective:** Prove incremental behavior, restart safety, and notification deduplication.

**Files:**

- Create: `tests/e2e/test_two_hour_replay.py`
- Create: `tests/fixtures/replays/two_hour_manifest.json`

**Steps:**

1. Replay timestamped events under a fake clock.
2. Replay the idle/reopen case end to end: the grace period expires, a provisional report is sent, a new turn is appended, the session reopens exactly once, the supersede notice references the superseded report version, and the stale idle dedup entry suppresses no alert in the reopened episode.
3. Replay the resume case end to end: a wrapped `codex exec --json` exits 0 and reports `terminal_completed` for that execution, then a wrapped `codex exec resume <same-session-id> --json` appends to the same rollout. Assert no event is lost or duplicated, the session reopens to `active_turn` with the next `execution_epoch`, and the earlier report is superseded by version rather than left standing as final.
4. Restart Watchtower midway.
5. Verify each normalized event and notification appears exactly once.
6. Verify only incremental windows reach the model client.
7. Verify report contents and version chain across both reopen paths.
8. Commit: `test: verify restart-safe autonomous run replay`.

### Task 35: Test degraded dependencies

**Objective:** Demonstrate core monitoring survives missing optional services.

**Files:**

- Create: `tests/e2e/test_degraded_services.py`

**Steps:**

1. Run with AgentLens and Codex Trace absent.
2. Start a fixture-compatible AgentLens midway and verify only canonical enrichment without duplicate session creation; otherwise verify it remains disabled and degraded.
3. Stop model endpoints and verify rule-only alerts.
4. Stop Telegram endpoint and verify durable retry.
5. Commit: `test: verify degraded watchtower operation`.

### Task 36: Perform security and acceptance review

**Objective:** Close the MVP only with evidence for every acceptance criterion.

**Files:**

- Create: `docs/release/acceptance-checklist.md`
- Create: `docs/release/threat-model.md`
- Modify: `README.md`

**Steps:**

1. Map all 12 specification acceptance criteria to commands and captured output.
2. Run the full suite:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src
   uv run pytest -q
   uv run python evaluation/run.py --cached --full
   ```

3. Run a seeded-secret scan across generated observation packets and logs.
4. Verify local ports bind only to loopback by default.
5. Review dependencies and licenses.
6. Document every known limitation; do not waive failed release gates silently.
7. Commit: `docs: record mvp acceptance evidence`.

### Task 37: Tag the first release

**Objective:** Publish only after CI and acceptance evidence are green.

**Steps:**

1. Verify clean worktree and remote CI success.
2. Create `v0.1.0` release notes referencing the acceptance report, stating that model output is advisory and that calibration gates v0.2.0.
3. Tag and publish the release.
4. Install from the released artifact in a clean environment.
5. Run `watchtower doctor` and one synthetic replay.

## Execution order and review gates

- Phases 0–5 can proceed without paid inference.
- v0.1.0 requires Phases -1 through 9 in advisory mode. Phase 10 and acceptance criterion 12 gate v0.2.0.
- Before Phase 6, verify the actual Luna/Terra provider interface and structured-output support with isolated smoke requests.
- Before Task 32 full corpus generation or Task 33 full evaluation, run the five-session smoke and obtain approval for the long/paid run.
- Before enabling Telegram against a real chat, test against a local/mock endpoint and inspect rendered messages.
- Before release, a separate reviewer must perform specification compliance review and security review.

## Completion definition

Implementation is complete only when:

- every MVP acceptance criterion has fresh evidence;
- the full deterministic suite passes;
- for v0.1.0, model output is advisory and no notification depends on it;
- for v0.2.0, the model calibration report passes its promotion gates or the system stays advisory;
- restart and degraded-service E2E tests pass;
- CI is green on the pushed commit;
- a clean install of the release reproduces the smoke test.
