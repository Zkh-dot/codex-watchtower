# Corpus format

Redacted replay fixtures are JSONL files produced by `tools/redact_rollout.py`
from real Codex rollouts. Each fixture preserves event order, exit codes,
test counts, and metadata hashes required for behavior analysis, while
stripping secrets, source bodies, and proprietary content.

## Structure

Each line is a JSON object with the following top-level keys:

- `type` — the Codex event type (preserved verbatim).
- `timestamp` — the event timestamp (preserved verbatim).
- `payload` — a redacted payload object.

### Payload fields

**Preserved** (behaviorally relevant):

| Field | Description |
|-------|-------------|
| `id` | Session id |
| `cwd` | Pseudonymized workspace path |
| `exit_code` | Command exit code |
| `path` | File path (redacted for secrets) |
| `change` | File change type |
| `command` | Command excerpt (redacted) |
| `passed` / `failed` / `total` | Test counts |

**Stripped** (content that could carry secrets or proprietary source):

`text`, `stdout_tail`, `stderr_tail`, `stdout`, `stderr`, `diff`, `content`,
`body`, `source`, and any unrecognized payload key.

Stripped fields are replaced with the literal string `[STRIPPED]`.

Recognized secret shapes (API keys, bearer tokens, private key blocks, etc.)
are replaced with `[REDACTED:<class>]` markers by the same redactor the
ingestion pipeline uses.

## Provenance sidecar

Each fixture is accompanied by a `<filename>.provenance.json` sidecar
recording:

- `source_owner` — who owns the original rollout.
- `repository_visibility` — `public`, `private`, or `internal`.
- `collection_authority` — the approval basis for collecting this rollout.
- `license_usage_basis` — the license or usage terms.
- `reviewer` — who reviewed the redacted output.
- `deletion_contact` — who to contact for fixture deletion.
- `inclusion_authority` — written approval (required for private/internal repos).
- `output_sha256` — SHA-256 hash of the fixture file for integrity verification.

## Access control

Private or internal repository material requires `--inclusion-authority`
(written approval to include the material in a shared fixture). The tool
refuses to process such material without it. If a fixture is found to
contain leaked material after the fact, the procedure is:

1. Delete the fixture and its provenance sidecar.
2. Rotate any credentials that may have been in the original rollout.
3. Rewrite history in any repository where the fixture was committed.
4. Notify the `deletion_contact` from the provenance sidecar.
