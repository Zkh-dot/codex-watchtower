# Session progress report acceptance

Implementation date: 2026-10-22. Product specification:
[local session progress report](../specs/2026-10-22-session-progress-report.md).

## Automated evidence

| Requirement | Evidence |
| --- | --- |
| Select one session, validate CLI arguments | `test_progress_report.py`: CLI mutual exclusion, exact session lookup, noninteractive selection errors |
| Actual persisted envelopes | Fixture under `tests/fixtures/progress/`; real 0.163.0 envelope shapes with synthetic contents, covering messages, plans, commands, results, compaction and resume |
| Partial lines, restart, dedup and replacement | `test_real_envelopes_partial_restart_and_replacement`; existing tailer tests plus same-descriptor prefix-change regression |
| Separate read/analyzed positions | `test_analysis_evidence_and_cursors_survive_restart`, `test_single_analysis_and_new_events_during_call` |
| Routine cadence survives restart | `test_restart_preserves_interval_even_with_new_events`; configured interval 300–600 seconds, separate initial catch-up boundary |
| Bounded context and visible omissions | `test_packet_budget_and_unknown_malformed_records`; 48,000-character packets and coverage counters |
| Stable tasks, scope changes and unknown estimates | `test_reference_validation_task_identity_and_scope`, schema validation; missing estimates remain null |
| Evidence references | Packet-specific enum in the provider schema, then application subset validation; invalid references cannot be published |
| No model tools | Opt-in installed-CLI probe captured an empty `tools` array for Codex CLI 0.163.0; runtime rejects unverified CLI versions |
| Model timeout and error behavior | Actual subprocess timeout/kill, invalid JSON and stderr suppression tests; engine tests preserve previous report/cursor on model failures |
| Safe HTML and atomic export | Escaped embedded JSON; DOM text nodes; failed replacement leaves previous file intact; output/source collision rejected |
| Free loopback port and HTTP | `test_report_command_http_sse_export_and_shutdown` launches the real CLI/server on OS-assigned loopback port with an offline model stub |
| SSE and reconnect | HTTP integration exercises initial snapshots and reconnect with Last-Event-ID; unit stream tests cover a new connection |
| Frontend rendering and updates | Node self-check exercises all three SVG charts, missing estimates, scope markers, task transition, duplicate/old updates, retained details and scroll; it is a DOM stub, not a visual browser test |
| Packaging | Built sdist/wheel; unpacked-wheel smoke verifies ingestion/export without source resources; HTML, prompt and SQL migration included |

The full test suite passed with 575 tests and one opt-in probe skipped. The probe
was also run explicitly and passed. Ruff lint, Ruff format and mypy passed. Tests
requiring local sockets ran outside the restrictive network sandbox, with a finite
outer timeout. One pre-existing Starlette TestClient deprecation warning remains.

## Model pilot on synthetic data

The installed, authenticated `gpt-6-luna` generated a schema-valid report from the
synthetic fixture through the actual subprocess adapter. All 12 normalized events
were covered, three tasks were produced, and the latest user clarification excluded
publication from scope. One pilot left progress unknown; the final schema pilot estimated 70–80%.
The difference is model judgment, not a measured completion percentage. This checks connectivity and pipeline behavior, not general accuracy.

An earlier pilot produced an invalid reference mixing a sequence number with event
IDs. Application validation rejected it. The provider-facing schema was then narrowed
to the exact allowed event IDs; the subsequent pilot passed.

## Real-journal local ingestion check

A user-selected real journal was read locally without invoking a model or
transmitting its contents. Initial ingestion of 250,997,114 bytes took 101.08
seconds in 434 batches of at most 200 records. The resulting state contained
50,189 normalized events, one malformed record, 605 unknown events and 8,255
truncated excerpts. These gaps remain visible in coverage rather than silently
becoming evidence of completion.

Closing and reopening the database, then reading the source again, preserved
the coverage counts without duplicates. HTML export succeeded. The report version
remained zero with no model assessment, as expected. This verifies local ingestion,
checkpoint recovery and export on a large real journal. This ingestion-only check
does not establish model quality, HTTP/SSE behavior or visual acceptance.

## Authorized real-model pilot (in progress)

After explicit approval of the journal and Codex-provider destination, the
installed `gpt-6-luna` adapter produced seven successive reports without an
analysis error. The seventh covered events through sequence 141 of 50,188;
this is an initial-history catch-up, not a current full-session assessment.
The inspected report cited matching evidence, kept progress and ETA unknown,
and explicitly described its incomplete historical coverage. Successful report
intervals observed early in the run were roughly 13–22 seconds, including
scheduling overhead; model token usage and monetary cost were not measured.

A real HTTP/SSE check observed versions 6 and 7 on one connection, received
version 7 after reconnecting with `Last-Event-ID`, and found version 7 in both
the served page and saved HTML. No browser is attached to the available UI
tool: opening either the in-app browser or Chrome failed as unavailable.
Visual acceptance therefore remains pending, despite browser authorization.

The upstream merge attempt returned a GitHub permissions error for the current
account. The user will complete merge in the UI if that access remains unavailable.

## Compact UI and completion-status correction

The UI revision collapses summaries, evidence, estimate explanations and task
comments into native disclosures, with compact task rows and nonempty status
counts. Disclosure state and scroll position remain stable across updates.

A structured Codex goal result is read independently of the historical model
cursor. A completed goal therefore appears as completed even during initial
catch-up; old task statuses and estimates stay under explicitly historical
disclosures. Completion prose, another thread's goal and a finished turn do not
set this status. A later structured active status replaces completion.
Regression tests cover these boundaries, oversized tool results, restart and the
absence of old in-progress tasks in the visible completed-goal view. The real
journal snapshot returned a completed goal while its model report still covered
older events. Browser tools remain unattached, so visual acceptance is pending.

An additional HTTP lifecycle regression holds SSE open during SIGINT. The server
now bounds graceful shutdown so an attached browser cannot delay restart forever.
All 22 focused report/page/HTTP tests passed after that change.

## Pending acceptance

- [ ] Analyze a specifically authorized real session and inspect the generated
      report against its evidence, including the quality of progress and ETA.
- [ ] Record model latency/token or quota observations for that pilot; no monetary
      cost or continuous-session accuracy estimate is established by the fixture.
- [ ] Inspect the actual rendered page visually, including narrow layout, chart
      labels and updates. No browser has been used; the Node check is not a substitute.
- [ ] Complete GitHub PR checks and upstream merge.

The synthetic pilot generated `/tmp/watchtower-fixture-live/report.html` locally.
That disposable preview is not committed and contains no original session transcript.
