"""Tests for the rollout redaction tooling (Task 31)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.redact_rollout import Provenance, redact_rollout

PUBLIC_PROV = Provenance(
    source_owner="owner@example.com",
    repository_visibility="public",
    collection_authority="operator-approval-2026-08-14",
    license_usage_basis="MIT",
    reviewer="reviewer@example.com",
    deletion_contact="privacy@example.com",
)

PRIVATE_PROV = Provenance(
    source_owner="owner@example.com",
    repository_visibility="private",
    collection_authority="operator-approval-2026-08-14",
    license_usage_basis="internal-use-only",
    reviewer="reviewer@example.com",
    deletion_contact="privacy@example.com",
    inclusion_authority="written-approval-2026-08-14",
)


def _write_rollout(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _read_output(path: Path) -> list[dict[str, object]]:
    results = []
    for line in path.read_text().splitlines():
        if line.strip():
            results.append(json.loads(line))
    return results


def test_secrets_are_stripped(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp,
        [
            {
                "type": "agent_message",
                "timestamp": "2026-08-14T10:00:00Z",
                "payload": {"text": "Here is my key: sk-abcdefghijklmnop123456"},
            },
            {
                "type": "exec_command_begin",
                "timestamp": "2026-08-14T10:00:01Z",
                "payload": {"command": "curl -H 'Authorization: Bearer secret_token_value'"},
            },
        ],
    )
    report = redact_rollout(inp, out, PUBLIC_PROV)
    records = _read_output(out)

    assert len(records) == 2
    raw_output = out.read_text()
    assert "sk-abcdefghijklmnop123456" not in raw_output
    assert "secret_token_value" not in raw_output
    assert "curl" not in raw_output
    # Text and command are pseudonymized, not preserved.
    assert records[0]["payload"]["text"].startswith("psuedo-text-")
    assert records[1]["payload"]["command"].startswith("psuedo-command-")
    assert report.records_processed == 2
    assert report.records_redacted >= 1


def test_event_order_preserved(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    records_in = [
        {"type": "turn_started", "timestamp": f"2026-08-14T10:00:0{i}Z", "payload": {}}
        for i in range(5)
    ]
    _write_rollout(inp, records_in)
    redact_rollout(inp, out, PUBLIC_PROV)
    records = _read_output(out)
    assert [r["timestamp"] for r in records] == [r["timestamp"] for r in records_in]


def test_exit_codes_and_test_counts_preserved(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp,
        [
            {
                "type": "exec_command_end",
                "timestamp": "2026-08-14T10:00:00Z",
                "payload": {"command": "pytest", "exit_code": 1, "stdout_tail": "FAIL"},
            },
            {
                "type": "test_result",
                "timestamp": "2026-08-14T10:00:01Z",
                "payload": {"passed": 10, "failed": 2, "total": 12},
            },
        ],
    )
    redact_rollout(inp, out, PUBLIC_PROV)
    records = _read_output(out)

    assert records[0]["payload"]["exit_code"] == 1
    assert records[1]["payload"]["passed"] == 10
    assert records[1]["payload"]["failed"] == 2
    assert records[1]["payload"]["total"] == 12
    # command and stdout_tail are pseudonymized/stripped, not preserved.
    assert "pytest" not in out.read_text()
    assert "FAIL" not in out.read_text()


def test_workspace_path_pseudonymized(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-14T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/home/user/secret-project"},
            },
        ],
    )
    report = redact_rollout(inp, out, PUBLIC_PROV)
    records = _read_output(out)

    assert records[0]["payload"]["cwd"] != "/home/user/secret-project"
    assert "secret-project" not in out.read_text()
    assert records[0]["payload"]["id"] != "sess-1"
    assert "sess-1" not in out.read_text()
    assert report.paths_pseudonymized >= 1


def test_pseudonymization_is_deterministic(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out1 = tmp_path / "output1.jsonl"
    out2 = tmp_path / "output2.jsonl"
    _write_rollout(
        inp,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-14T10:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/home/user/my-project"},
            },
        ],
    )
    redact_rollout(inp, out1, PUBLIC_PROV)
    redact_rollout(inp, out2, PUBLIC_PROV, force=True)
    r1 = _read_output(out1)
    r2 = _read_output(out2)
    assert r1[0]["payload"]["cwd"] == r2[0]["payload"]["cwd"]


def test_refuses_overwrite_by_default(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp, [{"type": "turn_started", "timestamp": "2026-08-14T10:00:00Z", "payload": {}}]
    )
    redact_rollout(inp, out, PUBLIC_PROV)
    with pytest.raises(FileExistsError):
        redact_rollout(inp, out, PUBLIC_PROV)


def test_force_overwrites_existing(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp, [{"type": "turn_started", "timestamp": "2026-08-14T10:00:00Z", "payload": {}}]
    )
    redact_rollout(inp, out, PUBLIC_PROV)
    redact_rollout(inp, out, PUBLIC_PROV, force=True)


def test_provenance_sidecar_emitted(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp, [{"type": "turn_started", "timestamp": "2026-08-14T10:00:00Z", "payload": {}}]
    )
    report = redact_rollout(inp, out, PUBLIC_PROV)

    sidecar = out.with_suffix(".provenance.json")
    assert sidecar.exists()
    import json as _json

    data = _json.loads(sidecar.read_text())
    assert data["provenance"]["source_owner"] == "owner@example.com"
    assert data["provenance"]["repository_visibility"] == "public"
    assert data["output_sha256"] == report.output_sha256
    assert len(data["output_sha256"]) == 64


def test_private_repo_without_inclusion_authority_refused(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp, [{"type": "turn_started", "timestamp": "2026-08-14T10:00:00Z", "payload": {}}]
    )
    with pytest.raises(ValueError, match="inclusion-authority"):
        redact_rollout(
            inp,
            out,
            PRIVATE_PROV.__class__(
                source_owner=PRIVATE_PROV.source_owner,
                repository_visibility="private",
                collection_authority=PRIVATE_PROV.collection_authority,
                license_usage_basis=PRIVATE_PROV.license_usage_basis,
                reviewer=PRIVATE_PROV.reviewer,
                deletion_contact=PRIVATE_PROV.deletion_contact,
                inclusion_authority=None,
            ),
        )


def test_private_repo_with_inclusion_authority_succeeds(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp, [{"type": "turn_started", "timestamp": "2026-08-14T10:00:00Z", "payload": {}}]
    )
    report = redact_rollout(inp, out, PRIVATE_PROV)
    assert report.records_processed == 1


def test_source_bodies_stripped(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp,
        [
            {
                "type": "file_change",
                "timestamp": "2026-08-14T10:00:00Z",
                "payload": {"path": "src/main.py", "diff": "import os\nSECRET = 'key'"},
            },
            {
                "type": "exec_command_end",
                "timestamp": "2026-08-14T10:00:01Z",
                "payload": {"command": "cat", "exit_code": 0, "stdout_tail": "sensitive output"},
            },
        ],
    )
    redact_rollout(inp, out, PUBLIC_PROV)
    records = _read_output(out)

    # diff is pseudonymized, not preserved.
    assert records[0]["payload"]["diff"] != "import os\nSECRET = 'key'"
    assert records[1]["payload"]["stdout_tail"] != "sensitive output"
    assert "sensitive output" not in out.read_text()
    assert "import os" not in out.read_text()
    # path is pseudonymized.
    assert "src/main.py" not in out.read_text()
    assert "cat" not in out.read_text()


def test_output_sha256_computed(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp, [{"type": "turn_started", "timestamp": "2026-08-14T10:00:00Z", "payload": {}}]
    )
    report = redact_rollout(inp, out, PUBLIC_PROV)
    assert len(report.output_sha256) == 64
    import hashlib

    expected = hashlib.sha256(out.read_bytes()).hexdigest()
    assert report.output_sha256 == expected


def test_samefile_input_output_rejected(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    _write_rollout(
        inp, [{"type": "turn_started", "timestamp": "2026-08-14T10:00:00Z", "payload": {}}]
    )
    with pytest.raises(ValueError, match="same file"):
        redact_rollout(inp, inp, PUBLIC_PROV, force=True)


def test_default_deny_unknown_keys(tmp_path: Path) -> None:
    inp = tmp_path / "input.jsonl"
    out = tmp_path / "output.jsonl"
    _write_rollout(
        inp,
        [
            {
                "type": "custom_event",
                "timestamp": "2026-08-14T10:00:00Z",
                "payload": {"unknown_field": "secret", "another": "data"},
            },
        ],
    )
    redact_rollout(inp, out, PUBLIC_PROV)
    records = _read_output(out)
    assert records[0]["payload"]["unknown_field"] == "[STRIPPED]"
    assert records[0]["payload"]["another"] == "[STRIPPED]"
    assert "secret" not in out.read_text()
    assert "data" not in out.read_text()
