# Spike 0B (part 2): Codex Trace compatibility contract

**Status:** contract frozen from source inspection recorded in
`docs/references/references.md` (Codex Trace commit
`c0bd7bb8cb99d379056fd7f0e1ed5fd6bfcbc3cf`). A live HTTP round-trip against
a running Codex Trace headless server was not performed in this environment.
Fixtures are synthetic, matching the documented route shapes.

## What is settled without a live capture

- Codex Trace reads the same `~/.codex/sessions` rollout JSONL files
  Watchtower does; it is a viewer, not a second source of truth.
- Headless HTTP API defaults to `127.0.0.1:11424`.
- Relevant routes: `POST /api/sessions`, `POST /api/session/load`,
  `POST /api/session/watch`, `POST /api/session/unwatch`,
  `GET /api/events` (SSE). The design's originally proposed
  `GET /api/settings` was not found in the inspected source; the
  correlation contract instead uses `POST /api/sessions` /
  `POST /api/session/load`.
- No stable clickable deep-link URL format is documented or verified.
  Per spec §5.10/§12, deep-link generation stays **disabled**; only the
  API base and session ID are exposed for manual drill-down.

## Integration decision this spike backs

Codex Trace is optional and unauthenticated-loopback. Watchtower never
proxies it externally, never widens its bind, and never requires it to be
running. Detection is a best-effort `GET`/`POST` probe against the
configured base URL with a short timeout; failure degrades to "Codex Trace
unavailable" without affecting ingestion.

## Fixtures

`fixtures/sessions_response.json` and `fixtures/unavailable.json` are
synthetic responses matching the documented route shapes, used by
`tests/unit/codex_trace/test_client.py`.

## Live verification (fill in when run against a real install)

- [ ] Confirm exact request/response bodies for `POST /api/sessions` and
      `POST /api/session/load` against a running instance.
- [ ] Determine whether a stable deep-link URL scheme exists in a later
      release before enabling deep-link generation.
