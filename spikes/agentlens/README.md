# Spike 0B (part 1): AgentLens compatibility contract

**Status:** contract frozen from source inspection recorded in
`docs/references/references.md` (AgentLens commit
`af8797740ccbba6d21748a023a9b448b53847467`). A live MCP round-trip against a
running AgentLens instance on `127.0.0.1:4316` was not performed in this
environment (no AgentLens instance available at implementation time). The
fixtures below are **synthetic**, built to match the documented response
shapes, not captured from a live server. They are sufficient to implement
and test the version-gated compatibility boundary described in spec §5.4,
but they are not a substitute for a real capture.

## What is settled without a live capture

From source inspection at the pinned commit:

- AgentLens exposes Streamable HTTP MCP at loopback `127.0.0.1:4316`,
  path `/mcp`.
- Relevant tools: `get_recent_sessions`, `get_session_detail`,
  `get_efficiency_report`.
- The pinned Codex parser populates prompt/token metadata but leaves
  timeline, tool-count, and file-list fields **empty** for Codex sessions
  specifically (other agents get richer data from AgentLens; Codex does
  not, as of the pinned commit).
- AgentLens identifies a Codex session by the **rollout filename without
  `.jsonl`**, not by the canonical `id` inside `session_meta`. The two are
  not guaranteed equal in general (a future AgentLens version could derive
  the filename differently), so filename-based matching is a heuristic,
  not a proof of identity.
- Neither `get_recent_sessions` nor `get_session_detail` exposes a
  canonical workspace path or a precise (sub-minute) start time, which is
  what would be needed for a robust independent correlation fallback.

## Integration decision this spike backs

Per spec §5.4: correlate **only** when the filename-derived session
identifier is present and matches exactly one Watchtower session; treat any
ambiguity as no match rather than guessing. Expose only the fields the
pinned adapter actually returns for Codex (prompt/token counters); do not
synthesize loop, error, file, or tool signals AgentLens does not supply for
Codex.

## Fixtures

`fixtures/get_recent_sessions.json`, `fixtures/get_session_detail.json`,
and `fixtures/unavailable.json` are synthetic MCP tool-result payloads
matching the documented shape (session identified by rollout filename,
empty timeline/tool/file arrays, populated prompt/token counters). Used by
`tests/unit/agentlens/test_client.py` and
`tests/unit/agentlens/test_correlate.py`.

## Live verification (fill in when run against a real install)

- [ ] Confirm `get_recent_sessions`/`get_session_detail` field names and
      types against a live server response.
- [ ] Confirm whether a newer AgentLens version has added a canonical
      rollout path/session-id field, which would let correlation move off
      the filename heuristic.
- [ ] Record actual latency for `get_session_detail` under a realistic
      session count.
