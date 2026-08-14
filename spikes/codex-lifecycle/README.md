# Spike 0A: Codex lifecycle semantics

**Status:** contract frozen from documented/inspected evidence; live multi-hour
capture against a running `codex exec` session was not performed in this
environment (no interactive Codex CLI session available at implementation
time). The distinction below is derived from `docs/references/references.md`
(verified locally against `codex-cli 0.133.0` and the pinned upstream commit)
and from the architecture specification, §5.1, §5.3, and §5.11. Any operator
running this spike against a live install should replace
`tests/fixtures/codex/lifecycle/*.jsonl` with fresh captures and update this
file's "Live verification" section.

## What is settled without a live capture

These facts come from `docs/references/references.md`, itself checked
against a local `codex-cli 0.133.0` binary and the pinned upstream commit
`4ca1af77a561a8908451ae755d8f2b119ca7c434`:

- `codex exec --json` emits JSONL events on stdout, including a session
  identifier record.
- persisted sessions live under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.
- `codex exec resume <SESSION_ID> --json` starts a **new process** that
  **adopts an existing session ID** and **appends to the same rollout file**.
  A persisted session therefore has no defined end.
- rollout records carry `session_meta` with `id`, `timestamp`, `cwd`,
  `originator`, `cli_version`, `source`, `model_provider`,
  `base_instructions`, `git`, `thread_source` — **no PID**.
- turn boundaries (`TurnStarted`/`TurnComplete`, wire `task_complete`) are
  transcript events; process boundaries are not observable from the
  transcript at all.

## The distinction this spike exists to fix in place

`TurnComplete` / wire `task_complete` marks the end of **one turn**, not the
end of the session and not the end of the process. Codex can start another
turn immediately after (interactive follow-up) or after a long pause
(operator steps away and comes back). The trailing records of a finished
run and of a run that is merely between turns are indistinguishable from
the transcript alone — both end in a quiescent state with no further
records for an arbitrary interval.

Therefore:

- **Terminal completion/failure is never derived from `TurnComplete`/
  `task_complete`.** It requires process evidence external to the
  transcript: a zero/non-zero exit code from a `watchtower run`-wrapped
  child process (preferred), or an adopted PID's disappearance (weaker,
  never yields `terminal_failed` since exit status is unknown), per
  spec §5.11.
- **Silence alone never produces a terminal state.** It produces the
  reversible `idle` state after a configurable quiet grace period, because
  a paused interactive session and a finished one produce the same
  trailing records.
- **`codex exec resume` reopens a session that already reported
  terminal_completed/terminal_failed.** The prior report is not retracted;
  it is superseded by a new report version, and the session returns to
  `active_turn` with a new `execution_epoch`. See spec §5.8
  "Executions and session lifetime".

## Fixtures

`tests/fixtures/codex/lifecycle/` contains synthetic (not captured)
rollout JSONL fixtures constructed to match the documented `session_meta`
and turn-record shapes, covering:

- `multi_turn_no_exit.jsonl` — three `TurnStarted`/`TurnComplete` pairs,
  no process evidence: must classify as `between_turns`, never terminal.
- `quiet_then_resume.jsonl` plus a paired process-evidence record — quiet
  period expires to `idle`, then a new turn appended by a `resume` process
  returns the session to `active_turn` with `execution_epoch` incremented.
- `wrapped_zero_exit.jsonl` plus process evidence with `exit_code: 0` —
  reaches `terminal_completed` for that execution.
- `wrapped_nonzero_exit.jsonl` plus process evidence with `exit_code: 1` —
  reaches `terminal_failed` for that execution.

These are consumed by `tests/unit/codex/test_lifecycle.py`.

## Live verification (fill in when run against a real install)

- [ ] Capture a real multi-turn interactive session and confirm no
      terminal state is inferred before process exit.
- [ ] Capture a real `codex exec --json` run to confirm the emitted
      stdout JSONL shape matches the fixtures' `session_meta` record.
- [ ] Capture a real `codex exec resume <id> --json` run against a
      previously "completed" wrapped run and confirm it appends to the
      same rollout file rather than creating a new one.
