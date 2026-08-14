# Threat Model

## System boundary

Watchtower is a local-first observer of Codex CLI sessions. It reads
rollout JSONL files from `~/.codex/sessions/`, stores normalized events
in a local SQLite database, optionally sends redacted observation
packets to a configured model endpoint, and optionally delivers
notifications via Telegram. It never mutates the workspace or steers
Codex.

## Assets

1. **Session transcripts** — Codex rollout files containing agent
   messages, commands, file paths, and potentially secrets in command
   output or file diffs.
2. **State database** — SQLite database with normalized events,
   reconciled assessments, signals, and notification delivery records.
3. **Model API keys** — credentials for Luna/Terra model endpoints,
   stored in environment variables.
4. **Telegram bot token** — credential for the Telegram Bot API, stored
   in environment variables.
5. **Observation packets** — redacted summaries sent to model endpoints;
   may contain session summaries, event excerpts, and signal descriptions.

## Threats and mitigations

### T1: Secret leakage into SQLite

**Threat:** Secrets in Codex session transcripts (API keys in command
output, credentials in file diffs) are ingested into the state database.

**Mitigation:** The normalizer (`codex/normalize.py`) applies
`privacy/redact.py` redaction before any text reaches SQLite. Recognized
secret shapes (private key blocks, bearer tokens, API keys, etc.) are
replaced with `[REDACTED:<class>]` markers.

**Residual risk:** Best-effort detection. Unknown secret shapes that do
not match recognized patterns may pass through. The spec acknowledges
this: "arbitrary unknown secrets cannot be guaranteed detectable."

### T2: Secret leakage to model endpoints

**Threat:** Observation packets sent to Luna/Terra endpoints contain
unredacted secrets or source code.

**Mitigation:** Redaction is applied again in the packet builder before
anything leaves the process. The model receives no tools: it cannot
write files or contact Codex. The response-size boundary
(`models/client.py::read_bounded_response`) prevents oversized responses
from being materialized.

**Residual risk:** Same as T1 — unrecognized secret shapes. Remote mode
additionally enforces HTTPS-only, no redirects, no loopback/metadata
destinations, and no inherited proxy environment (`config.py`:
`RemoteTransportPolicy`).

### T3: Telegram bot token persistence

**Threat:** The Telegram bot token is accidentally serialized into the
state database or a log file.

**Mitigation:** The token lives only in configuration/environment and is
passed to `TelegramNotifier` at construction. `PendingDelivery` (the
only delivery record type that is persisted) carries no token field. The
token is never logged.

### T4: Transcript content in metrics/logs

**Threat:** Observability metrics or structured logs expose transcript
content (event summaries, command text, file paths).

**Mitigation:** The telemetry module (`telemetry.py`) enforces the
cardinal rule structurally: metric labels are fixed enum values only,
never free-form text. Structured-log free-form fields are run through
the same redactor the ingestion path uses.

### T5: Malicious rollout file injection

**Threat:** A crafted rollout file is placed in the sessions directory
to crash Watchtower or inject malicious content.

**Mitigation:** The normalizer handles malformed JSON and unknown event
types gracefully (spec 4.1). File-safety enforcement (`enforce_file_safety`)
optionally validates that files are under the sessions root and owned by
the expected UID. The tailer uses prefix-hash verification to detect
file truncation/rotation.

**Residual risk:** A sufficiently sophisticated attacker with filesystem
write access to the sessions directory can craft events that pass
normalization. This is within the trust boundary of a local-first tool.

### T6: Model endpoint compromise

**Threat:** A compromised or malicious model endpoint returns crafted
responses that bypass schema validation or attempt prompt injection.

**Mitigation:** Every model response is re-validated against the
authoritative JSON schema after parsing. The model receives no tools and
cannot execute code. In v0.1.0, model output is advisory and never
influences notification decisions. Deterministic signals remain
authoritative.

### T7: Local API exposure

**Threat:** The local HTTP API is accessible from external interfaces.

**Mitigation:** The API binds to `127.0.0.1` by default. No port is
exposed to external interfaces unless explicitly configured by the
operator.

### T8: Oversized model response

**Threat:** A model endpoint returns an oversized response designed to
exhaust memory or inject an oversized numeric literal.

**Mitigation:** `read_bounded_response` reads at most `cap` bytes into a
retained buffer, plus one bounded overflow probe whose bytes are counted
but never retained. An oversized response is rejected before
`json.loads` ever sees it.

## Trust boundaries

```
Codex rollout files (local filesystem)
    │
    ▼
[Tailer + Normalizer + Redactor]
    │
    ▼
SQLite state DB (local, owner-only)
    │
    ├──► Local API (127.0.0.1 only)
    │
    ├──► Model endpoint (HTTPS, redacted packets, no tools)
    │       └── Response validated against schema
    │
    └──► Telegram Bot API (token in env, no persisted token)
```

Everything inside the Watchtower process is within the local trust
boundary. Model endpoints and Telegram are external trust boundaries
that receive only redacted, bounded data.
