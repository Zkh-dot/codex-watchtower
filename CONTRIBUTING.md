# Contributing

This repository currently contains the approved design and implementation plan. Keep implementation changes aligned with the architecture specification.

## Before opening a pull request

1. Identify the exact implementation-plan task being completed.
2. Use tests first for behavior changes.
3. Keep Codex observation read-only.
4. Do not commit transcripts, prompts, source bodies, credentials, or runtime databases.
5. Preserve unknown Codex events rather than crashing or silently discarding them.
6. Include fresh verification output in the pull request description.

Changes to source ownership, deterministic-alert authority, remote-data policy, or model escalation rules require a specification update before code changes.
