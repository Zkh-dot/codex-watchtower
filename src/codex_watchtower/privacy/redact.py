"""Secret and length redaction (spec section 7.2).

Applied before anything reaches SQLite (normalizer, Task 8) and again
before anything leaves the process toward a remote model or Telegram
(packet builder / notifier, Phase 5/8). The packet records which
redaction *classes* were applied, never the removed values themselves.

This is a best-effort defense against *recognized* secret shapes,
consistent with spec section 6: "Watchtower makes a best effort to
prevent recognized secret material entering SQLite... arbitrary unknown
secrets cannot be guaranteed detectable."
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_.=]{8,}")),
    ("authorization_header", re.compile(r"(?i)\bauthorization\s*:\s*\S+")),
    ("cookie_header", re.compile(r"(?i)\bcookie\s*:\s*\S+")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("google_oauth_token", re.compile(r"\bya29\.[A-Za-z0-9\-_]{10,}\b")),
    (
        "generic_secret_assignment",
        re.compile(
            r"(?i)\b(?:secret|token|password|passwd|api[_-]?key)\s*[:=]\s*"
            r"[\"']?[A-Za-z0-9\-_./+]{8,}[\"']?"
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    classes: tuple[str, ...]


def redact_text(text: str) -> RedactionResult:
    """Replace recognized secret shapes with a class marker, in one pass per pattern."""
    classes: set[str] = set()
    result = text
    for name, pattern in _PATTERNS:

        def _sub(match: re.Match[str], _name: str = name) -> str:
            classes.add(_name)
            return f"[REDACTED:{_name}]"

        result = pattern.sub(_sub, result)
    return RedactionResult(text=result, classes=tuple(sorted(classes)))


def bound_text(text: str, max_length: int) -> tuple[str, bool]:
    """Truncate to ``max_length`` characters. Returns (text, was_truncated)."""
    if len(text) <= max_length:
        return text, False
    return text[:max_length], True
