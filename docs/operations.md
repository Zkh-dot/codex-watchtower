# Operating Codex Watchtower

## Installation

```bash
uv sync --all-groups
cp config.example.toml config.toml
# edit config.toml: at minimum, confirm sessions_root points at your
# ~/.codex/sessions, and review the loopback bind before changing it
uv run watchtower doctor
```

`watchtower doctor` checks, in order: whether `config.toml` loads and
validates, whether `sessions_root` exists, whether AgentLens is reachable
(only if enabled), and whether the state directory/database can be opened.
It exits 0 only when every check passes.

## Running

- `watchtower serve [--config PATH]` runs discovery, ingestion, the
  deterministic rule engine, reconciliation, the local status API, and (once
  configured) the Telegram notifier as one process. The API binds
  `127.0.0.1` by default.
- `watchtower run -- codex exec ...` (or any Codex invocation) wraps the
  child process, capturing process exit evidence with stdio passed through
  unchanged. This is the preferred way to get confident terminal-state
  reporting (spec 5.11); without it, a session degrades to the reversible
  `idle` state after the quiet grace period instead of a confirmed
  completion/failure.
- `watchtower inspect SESSION_ID [--config PATH]` prints the latest
  reconciled assessment for a session as JSON.
- `watchtower assess SESSION_ID [--config PATH]` requests an on-demand
  Terra escalation via the local API. Requires `operator_token` to be set
  in `config.toml` and a running `watchtower serve`.

## systemd

`deploy/codex-watchtower.service` runs `watchtower serve` under a
dedicated, unprivileged `watchtower` user with standard hardening
(`ProtectSystem=strict`, `NoNewPrivileges=true`, no capabilities, private
tmp). Secrets (Telegram bot token, model API keys) belong in
`/etc/codex-watchtower/environment` (mode `0600`, referenced via
`EnvironmentFile=-`), never in `config.toml` itself -- `config.toml` only
ever names the *environment variable* a secret lives in
(`bot_token_env_var`, `api_key_env_var`).

```bash
sudo useradd --system --home-dir /var/lib/codex-watchtower --shell /usr/sbin/nologin watchtower
sudo mkdir -p /var/lib/codex-watchtower /etc/codex-watchtower
sudo cp config.toml /etc/codex-watchtower/config.toml
sudo chown -R watchtower:watchtower /var/lib/codex-watchtower
sudo cp deploy/codex-watchtower.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-watchtower
```

## Backup and recovery of state

All durable state lives in one SQLite database at `state_dir/state.db`
(WAL mode; `state.db-wal`/`state.db-shm` sidecars may exist alongside it
during normal operation). To back up safely while the service is running,
use SQLite's own online backup rather than copying the files directly:

```bash
sqlite3 /var/lib/codex-watchtower/state.db ".backup '/path/to/backup.db'"
```

To restore, stop the service, replace `state.db` (and remove any stale
`-wal`/`-shm` sidecars) with the backup, and restart. Startup refuses to
open a corrupt database rather than silently recreating it (spec section
8); if `watchtower doctor` or `serve` reports a corruption error, restore
from the most recent backup rather than deleting the file.

Nothing in the database is itself a secret (tokens are read from the
environment at call time and never persisted), but redacted session
summaries are still operational data worth protecting; the state directory
should remain owner-only readable, matching what `systemd`'s
`ReadWritePaths=/var/lib/codex-watchtower` plus standard directory
permissions already enforce.

## Redacted secret scanning

Before deploying or sharing generated observation packets, logs, or the
state database, run a scan for accidentally-retained secret material. The
seeded-secret regression tests (`tests/unit/privacy/test_redact.py`) are
the source of truth for which patterns Watchtower recognizes; treat that
suite, not a one-off manual grep, as the acceptance gate.

## Internal observability

Watchtower emits structured logs and Prometheus-compatible counters
(spec section 9) through `codex_watchtower.telemetry.Telemetry`. The
cardinal rule is that **no transcript content is exported through
metrics**: metric labels are drawn only from fixed enum values, session
ids, signal kinds/severities, and outcome categories, never from event
summaries, model output, command text, or file paths. Structured-log
fields may carry a bounded `session_id` and outcome, but any free-form
payload is run through the same `privacy.redact` redactor the ingestion
path uses before it reaches a log record.

The telemetry sink exposes:

- **Counters:** sessions observed, events ingested/rejected, unknown
  Codex event types, AgentLens availability, model call
  count/failures/escalations, notifications sent/deduplicated/failed,
  cursor recovery outcomes, and cumulative parser lag.
- **Histograms:** model-call latency (seconds) and input-packet size
  (characters), with cumulative bucket counts matching Prometheus
  semantics. No individual sample or raw packet content is exported --
  only aggregate bucket counts and totals.

`watchtower doctor` does not require telemetry to be configured; the
no-op default keeps the service fully functional without a logger
attached.
