# Contributing

This repository currently contains a draft design and implementation plan. Resolve the early integration spikes before freezing interfaces, then keep implementation changes aligned with the validated architecture.

## Before opening a pull request

1. Identify the exact implementation-plan task being completed.
2. Use tests first for behavior changes.
3. Never mutate Codex or the observed workspace.
4. Do not commit transcripts, prompts, source bodies, credentials, or runtime databases.
5. Preserve unknown Codex events rather than crashing or silently discarding them.
6. Include fresh verification output in the pull request description.

Changes to source ownership, deterministic-alert authority, remote-data policy, or model escalation rules require a specification update before code changes.
