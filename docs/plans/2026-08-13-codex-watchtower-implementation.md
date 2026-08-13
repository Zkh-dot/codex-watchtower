# Codex Watchtower Implementation Plan

> **For Hermes:** Use the `subagent-driven-development` skill to implement this plan task by task, with specification review before code-quality review.

**Goal:** Build a local-first, read-only monitor that explains autonomous Codex CLI progress, detects unhealthy behavior, and escalates ambiguous cases from Luna to Terra.

**Architecture:** A Python service tails persisted Codex rollout JSONL incrementally, normalizes events, enriches them through optional AgentLens MCP, applies deterministic rules, and sends bounded observation packets to structured-output model assessors. SQLite persists cursors and assessments; a local HTTP/SSE API and optional Telegram notifier expose only reconciled, evidence-backed state.

**Tech stack:** Python 3.12, `uv`, Pydantic v2, SQLite/WAL, FastAPI, `watchfiles`, `httpx`, `mcp`, `jsonschema`, `pytest`, `pytest-asyncio`, `respx`, Ruff, mypy.

**Implementation rule:** Every code task follows red-green-refactor. Commits below are suggested logical checkpoints; merge small adjacent steps when the diff would otherwise be noise.

---

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
- Create: `.gitignore`

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
- Modify: `pyproject.toml`

**Steps:**

1. Write tests loading both schemas and validating healthy examples.
2. Add one deliberately invalid in-test assessment and assert validation failure.
3. Run the focused tests; verify red before fixture/schema wiring and green after.
4. Commit: `test: enforce observation and assessment schemas`.

## Phase 1: Domain model and durable state

### Task 3: Define typed domain models

**Objective:** Represent sessions, events, signals, observations, and assessments without leaking provider-specific records into core logic.

**Files:**

- Create: `src/codex_watchtower/domain.py`
- Create: `tests/unit/test_domain.py`

**Steps:**

1. Write failing tests for enum values, assessment confidence bounds, unique event IDs, and schema serialization.
2. Implement Pydantic models matching the committed JSON Schemas.
3. Verify serialized fixtures validate with `jsonschema`.
4. Commit: `feat: add typed watchtower domain model`.

### Task 4: Create SQLite migrations and repository interface

**Objective:** Persist sessions, cursors, normalized events, signals, assessments, model calls, and deliveries in WAL mode.

**Files:**

- Create: `src/codex_watchtower/storage/migrations/001_initial.sql`
- Create: `src/codex_watchtower/storage/db.py`
- Create: `src/codex_watchtower/storage/repository.py`
- Create: `tests/unit/storage/test_repository.py`

**Steps:**

1. Write tests for migration idempotence and WAL mode.
2. Test atomic insertion of an event and cursor update.
3. Test event deduplication by stable event ID.
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
2. Test that the original file remains byte-identical.
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
2. Parse `session_meta` to obtain ID, workspace, model, origin, and start time.
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
4. Test inode replacement and truncation.
5. Test duplicate suppression using the last event hash.
6. Implement the cursor `<device>:<inode>:<offset>:<hash>`.
7. Commit: `feat: tail codex rollouts incrementally`.

### Task 8: Normalize known and unknown Codex events

**Objective:** Convert version-specific rollout records into bounded provider-neutral events.

**Files:**

- Create: `src/codex_watchtower/codex/normalize.py`
- Create: `tests/unit/codex/test_normalize.py`
- Add fixtures under: `tests/fixtures/codex/events/`

**Steps:**

1. Add fixtures for session lifecycle, messages, commands, command output, patches, token updates, errors, and unknown event types.
2. Write expected normalized snapshots.
3. Implement stable event ID generation from session ID, source offset, type, and payload hash.
4. Bound command output and retain content hash/byte length.
5. Ensure unknown events become `kind=unknown` with source type preserved.
6. Commit: `feat: normalize codex rollout events`.

### Task 9: Classify lifecycle without model inference

**Objective:** Derive running, waiting, completed, and failed states from explicit evidence.

**Files:**

- Create: `src/codex_watchtower/codex/lifecycle.py`
- Create: `tests/unit/codex/test_lifecycle.py`

**Steps:**

1. Test explicit task start/complete/failure sequences.
2. Test a live file with no terminal lifecycle record as running or unknown according to process evidence.
3. Test that prose such as “done” does not mark completion.
4. Commit: `feat: derive codex lifecycle from explicit events`.

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

### Task 11: Implement AgentLens MCP client boundary

**Objective:** Query AgentLens through its public MCP tools without coupling core logic to its UI or database.

**Files:**

- Create: `src/codex_watchtower/agentlens/client.py`
- Create: `src/codex_watchtower/agentlens/types.py`
- Create: `tests/unit/agentlens/test_client.py`

**Steps:**

1. Mock Streamable HTTP MCP responses for `get_recent_sessions` and `get_session_detail`.
2. Test timeout, malformed response, unavailable server, and successful parsing.
3. Implement bounded retries and typed degraded status.
4. Commit: `feat: add optional agentlens mcp adapter`.

### Task 12: Correlate AgentLens and Codex sessions

**Objective:** Enrich the correct session without creating duplicates or cross-project contamination.

**Files:**

- Create: `src/codex_watchtower/agentlens/correlate.py`
- Create: `tests/unit/agentlens/test_correlate.py`

**Steps:**

1. Test exact session-ID match.
2. Test workspace/start-time/model fallback correlation.
3. Test ambiguity returning no match instead of guessing.
4. Record correlation confidence and method.
5. Commit: `feat: correlate codex and agentlens sessions safely`.

### Task 13: Add explicit SQLite fallback

**Objective:** Permit read-only AgentLens database snapshots only when configured.

**Files:**

- Create: `src/codex_watchtower/agentlens/sqlite_fallback.py`
- Create: `tests/unit/agentlens/test_sqlite_fallback.py`

**Steps:**

1. Create a minimal AgentLens-compatible fixture database.
2. Open it read-only and assert no WAL/schema mutation.
3. Map only documented fields used by Watchtower.
4. Reject unknown schema versions with degraded status rather than best-effort guessing.
5. Commit: `feat: add guarded agentlens sqlite fallback`.

## Phase 4: Deterministic health signals

### Task 14: Implement command and error repetition rules

**Objective:** Detect loops independently of model output.

**Files:**

- Create: `src/codex_watchtower/rules/repetition.py`
- Create: `tests/unit/rules/test_repetition.py`

**Steps:**

1. Test three identical commands with unchanged outcomes trigger a warning.
2. Test the same command after changed files or changed result does not automatically trigger.
3. Test normalization of volatile timestamps/temp paths.
4. Test three equivalent recurring errors trigger a warning.
5. Commit: `feat: detect repeated command and error loops`.

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

1. Test identical loop evidence from both sources becomes one signal with two sources.
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
3. Test event/item/character limits.
4. Test overflow folding and no source-file bodies by default.
5. Validate generated packets against `observation.schema.json`.
6. Commit: `feat: build bounded observation packets`.

## Phase 6: Luna/Terra assessment cascade

### Task 20: Define provider-neutral structured model client

**Objective:** Keep model names and endpoint mechanics out of assessment policy.

**Files:**

- Create: `src/codex_watchtower/models/client.py`
- Create: `src/codex_watchtower/models/config.py`
- Create: `tests/unit/models/test_client.py`

**Steps:**

1. Define `assess(model_profile, observation, schema)` interface.
2. Test OpenAI-compatible structured-output request construction with `respx`.
3. Test timeout, invalid JSON, schema mismatch, and retry classification.
4. Ensure no tools are supplied to the assessment model.
5. Commit: `feat: add structured model assessment client`.

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

**Steps:**

1. Parametrize every escalation trigger from the specification.
2. Verify healthy/high-confidence Luna results do not call Terra.
3. Verify deterministic critical signals force attention despite reassuring model output.
4. Verify Terra prose supersedes Luna only when valid.
5. Verify timeout falls back to deterministic assessment.
6. Commit: `feat: add terra escalation policy`.

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

### Task 24: Expose read-only HTTP and SSE API

**Objective:** Provide machine-readable current state without exposing Codex mutation.

**Files:**

- Create: `src/codex_watchtower/api/app.py`
- Create: `src/codex_watchtower/api/routes.py`
- Create: `tests/integration/test_api.py`

**Steps:**

1. Write failing tests for all specified endpoints.
2. Implement pagination/cursors and JSON schema-compatible responses.
3. Implement SSE state transitions with reconnect cursor support.
4. Assert the default bind is `127.0.0.1`.
5. Assert no endpoint can submit input or mutate Codex.
6. Commit: `feat: expose local watchtower status api`.

### Task 25: Add optional Codex Trace adapter and deep links

**Objective:** Detect Codex Trace and attach a drill-down target without making it required.

**Files:**

- Create: `src/codex_watchtower/codex_trace/client.py`
- Create: `tests/unit/codex_trace/test_client.py`

**Steps:**

1. Mock `/api/settings`, `/api/sessions`, and `/api/session/load`.
2. Test unavailable and incompatible responses degrade cleanly.
3. Verify session correlation by path/session ID.
4. Keep deep-link generation disabled until a stable URL contract is verified; expose API base and session ID meanwhile.
5. Commit: `feat: integrate optional codex trace drill-down`.

## Phase 8: Telegram delivery

### Task 26: Implement notification rendering and fingerprinting

**Objective:** Render concise messages and avoid wording-only duplicates.

**Files:**

- Create: `src/codex_watchtower/notify/render.py`
- Create: `src/codex_watchtower/notify/policy.py`
- Create: `tests/unit/notify/test_policy.py`
- Create: `tests/golden/telegram/*.md`

**Steps:**

1. Add golden messages for warning, waiting, failed, and completed states.
2. Test transition policy and concern fingerprinting.
3. Test changed prose with identical evidence is suppressed.
4. Test critical evidence always includes a factual reason and event cursor.
5. Commit: `feat: render deduplicated operator notifications`.

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
4. Commit: `feat: add safe watchtower configuration`.

### Task 29: Add service CLI and systemd unit

**Objective:** Run discovery, ingestion, assessment, API, and notifier as one supervised process.

**Files:**

- Modify: `src/codex_watchtower/cli.py`
- Create: `deploy/codex-watchtower.service`
- Create: `docs/operations.md`
- Create: `tests/integration/test_cli.py`

**Steps:**

1. Add `watchtower serve`, `watchtower inspect`, `watchtower assess`, and `watchtower doctor`.
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
5. Commit: `tools: add safe rollout fixture redaction`.

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
4. Add human labels with evidence event IDs.
5. Freeze the manifest with fixture SHA-256 hashes.
6. Commit: `eval: add frozen watchtower assessment corpus`.

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
7. If Terra fails to improve ambiguous cases by ten percentage points, disable automatic escalation and document the decision.
8. Commit: `eval: calibrate luna and terra assessment cascade`.

## Phase 11: End-to-end verification and release

### Task 34: Add two-hour accelerated replay test

**Objective:** Prove incremental behavior, restart safety, and notification deduplication.

**Files:**

- Create: `tests/e2e/test_two_hour_replay.py`
- Create: `tests/fixtures/replays/two_hour_manifest.json`

**Steps:**

1. Replay timestamped events under a fake clock.
2. Restart Watchtower midway.
3. Verify each normalized event and notification appears exactly once.
4. Verify only incremental windows reach the model client.
5. Verify completion report contents.
6. Commit: `test: verify restart-safe autonomous run replay`.

### Task 35: Test degraded dependencies

**Objective:** Demonstrate core monitoring survives missing optional services.

**Files:**

- Create: `tests/e2e/test_degraded_services.py`

**Steps:**

1. Run with AgentLens and Codex Trace absent.
2. Start AgentLens midway and verify enrichment without duplicate session creation.
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
2. Create `v0.1.0` release notes referencing the calibration and acceptance reports.
3. Tag and publish the release.
4. Install from the released artifact in a clean environment.
5. Run `watchtower doctor` and one synthetic replay.

## Execution order and review gates

- Phases 0–5 can proceed without paid inference.
- Before Phase 6, verify the actual Luna/Terra provider interface and structured-output support with isolated smoke requests.
- Before Task 32 full corpus generation or Task 33 full evaluation, run the five-session smoke and obtain approval for the long/paid run.
- Before enabling Telegram against a real chat, test against a local/mock endpoint and inspect rendered messages.
- Before release, a separate reviewer must perform specification compliance review and security review.

## Completion definition

Implementation is complete only when:

- every MVP acceptance criterion has fresh evidence;
- the full deterministic suite passes;
- the model calibration report passes its gates or the system fails closed to rule-only mode;
- restart and degraded-service E2E tests pass;
- CI is green on the pushed commit;
- a clean install of the release reproduces the smoke test.
