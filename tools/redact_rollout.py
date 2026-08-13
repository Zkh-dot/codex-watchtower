"""Convert a Codex rollout JSONL into a reviewable fixture without secrets or proprietary source.

The tool reads a rollout file line by line, applies deterministic
pseudonymization to workspace paths and filenames, strips source bodies,
tokens, and command secrets, and preserves event order, exit codes, test
counts, and hashes required for behavior analysis.

It refuses to overwrite an existing output file unless ``--force`` is
passed, and emits provenance metadata so the resulting fixture is
self-documenting for review.

Refuses private/third-party repository material unless written inclusion
authority is recorded via ``--inclusion-authority``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_watchtower.privacy.redact import redact_text

# Session-level fields that are preserved (metadata, not content).
_PRESERVED_TOP_KEYS = frozenset({"type", "timestamp"})

# Payload fields that are preserved for behavior analysis.
_PRESERVED_PAYLOAD_KEYS = frozenset(
    {"id", "cwd", "exit_code", "path", "change", "command", "passed", "failed", "total"}
)

# Payload fields whose values are stripped entirely (source bodies, text content).
_STRIPPED_PAYLOAD_KEYS = frozenset(
    {"text", "stdout_tail", "stderr_tail", "stdout", "stderr", "diff", "content", "body", "source"}
)

_PSEUDONYM_RE = re.compile(r"/[A-Za-z0-9._\-/]+")


@dataclass
class Provenance:
    source_owner: str
    repository_visibility: str  # "public", "private", "internal"
    collection_authority: str
    license_usage_basis: str
    reviewer: str
    deletion_contact: str
    inclusion_authority: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_owner": self.source_owner,
            "repository_visibility": self.repository_visibility,
            "collection_authority": self.collection_authority,
            "license_usage_basis": self.license_usage_basis,
            "reviewer": self.reviewer,
            "deletion_contact": self.deletion_contact,
            "inclusion_authority": self.inclusion_authority,
        }


@dataclass
class RedactionReport:
    input_path: Path
    output_path: Path
    records_processed: int = 0
    records_redacted: int = 0
    redaction_classes: set[str] = field(default_factory=set)
    paths_pseudonymized: int = 0
    provenance: Provenance | None = None
    output_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_path": str(self.input_path),
            "output_path": str(self.output_path),
            "records_processed": self.records_processed,
            "records_redacted": self.records_redacted,
            "redaction_classes": sorted(self.redaction_classes),
            "paths_pseudonymized": self.paths_pseudonymized,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "output_sha256": self.output_sha256,
        }


def _pseudonymize_path(path_str: str, mapping: dict[str, str]) -> str:
    """Replace workspace path components with deterministic pseudonyms."""
    if path_str in mapping:
        return mapping[path_str]
    pseudonym = f"/workspace/psuedo-{hashlib.sha256(path_str.encode()).hexdigest()[:12]}"
    mapping[path_str] = pseudonym
    return pseudonym


def _redact_payload(
    payload: dict[str, Any], path_mapping: dict[str, str], report: RedactionReport
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _STRIPPED_PAYLOAD_KEYS:
            result[key] = "[STRIPPED]"
            report.records_redacted += 1
            continue
        if key in _PRESERVED_PAYLOAD_KEYS:
            if isinstance(value, str):
                redacted = redact_text(value)
                if redacted.classes:
                    report.redaction_classes.update(redacted.classes)
                    report.records_redacted += 1
                if key == "cwd":
                    pseudonymized = _pseudonymize_path(value, path_mapping)
                    if pseudonymized != value:
                        report.paths_pseudonymized += 1
                    result[key] = pseudonymized
                elif key == "path":
                    result[key] = redacted.text
                else:
                    result[key] = redacted.text
            else:
                result[key] = value
        else:
            result[key] = "[STRIPPED]"
            report.records_redacted += 1
    return result


def redact_rollout(
    input_path: Path,
    output_path: Path,
    provenance: Provenance,
    *,
    force: bool = False,
) -> RedactionReport:
    if not input_path.is_file():
        raise FileNotFoundError(f"input file not found: {input_path}")
    if output_path.exists() and not force:
        raise FileExistsError(
            f"output file already exists: {output_path} (pass --force to overwrite)"
        )
    if provenance.repository_visibility != "public" and not provenance.inclusion_authority:
        raise ValueError(
            "private/internal repository material requires --inclusion-authority "
            "(written approval to include this material in a shared fixture)"
        )

    report = RedactionReport(input_path=input_path, output_path=output_path, provenance=provenance)
    path_mapping: dict[str, str] = {}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        input_path.open("r", encoding="utf-8", errors="replace") as infile,
        output_path.open("w", encoding="utf-8") as outfile,
    ):
        for line in infile:
            line = line.strip()
            if not line:
                continue
            report.records_processed += 1
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                outfile.write(
                    json.dumps(
                        {"type": "malformed", "raw_hash": hashlib.sha256(line.encode()).hexdigest()}
                    )
                    + "\n"
                )
                report.records_redacted += 1
                continue

            redacted_record: dict[str, Any] = {}
            for key in _PRESERVED_TOP_KEYS:
                if key in record:
                    redacted_record[key] = record[key]

            payload = record.get("payload")
            if isinstance(payload, dict):
                redacted_record["payload"] = _redact_payload(payload, path_mapping, report)
            else:
                redacted_record["payload"] = {}

            outfile.write(json.dumps(redacted_record, separators=(",", ":"), sort_keys=True) + "\n")

    with output_path.open("rb") as f:
        report.output_sha256 = hashlib.sha256(f.read()).hexdigest()

    sidecar = output_path.with_suffix(".provenance.json")
    with sidecar.open("w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, sort_keys=True)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Redact a Codex rollout into a reviewable fixture."
    )
    parser.add_argument("input", type=Path, help="input rollout JSONL path")
    parser.add_argument("output", type=Path, help="output redacted JSONL path")
    parser.add_argument("--force", action="store_true", help="overwrite existing output")
    parser.add_argument("--source-owner", required=True)
    parser.add_argument(
        "--repository-visibility", required=True, choices=["public", "private", "internal"]
    )
    parser.add_argument("--collection-authority", required=True)
    parser.add_argument("--license-usage-basis", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--deletion-contact", required=True)
    parser.add_argument("--inclusion-authority", default=None)

    args = parser.parse_args(argv)
    provenance = Provenance(
        source_owner=args.source_owner,
        repository_visibility=args.repository_visibility,
        collection_authority=args.collection_authority,
        license_usage_basis=args.license_usage_basis,
        reviewer=args.reviewer,
        deletion_contact=args.deletion_contact,
        inclusion_authority=args.inclusion_authority,
    )

    try:
        report = redact_rollout(args.input, args.output, provenance, force=args.force)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Processed {report.records_processed} records, "
        f"{report.records_redacted} redacted, "
        f"{report.paths_pseudonymized} paths pseudonymized. "
        f"Output SHA-256: {report.output_sha256}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
