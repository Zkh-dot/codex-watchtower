"""Extract pass/fail counts from test_result summaries and classify the trend (spec 5.5).

Codex's actual test_result payload shape is unverified (no live capture;
see spikes/codex-lifecycle/README.md), so extraction works over the
rendered summary text with patterns covering pytest, cargo test, and
npm/vitest-style output, falling back to the event's exit_code, and
finally to "unknown" rather than guessing a magnitude it cannot support.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from codex_watchtower import domain


@dataclass(frozen=True, slots=True)
class TestCounts:
    __test__ = False  # not a pytest test class; name collision with the "Test" prefix

    passed: int | None
    failed: int | None


class Trend(StrEnum):
    improving = "improving"
    stable = "stable"
    worsening = "worsening"
    unknown = "unknown"


_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?P<failed>\d+)\s+failed,?\s+(?P<passed>\d+)\s+passed"),  # pytest
    re.compile(r"(?P<passed>\d+)\s+passed,?\s+(?P<failed>\d+)\s+failed"),
    re.compile(r"(?P<passed>\d+)\s+passed;\s+(?P<failed>\d+)\s+failed"),  # cargo test
    re.compile(r"Tests:\s+(?P<failed>\d+)\s+failed,\s+(?P<passed>\d+)\s+passed"),  # jest/vitest
    re.compile(r"(?P<passed>\d+)\s+passed\b"),
    re.compile(r"(?P<failed>\d+)\s+failed\b"),
]


def extract_test_counts(summary: str) -> TestCounts:
    for pattern in _PATTERNS:
        match = pattern.search(summary)
        if match:
            groups = match.groupdict()
            passed = int(groups["passed"]) if groups.get("passed") is not None else None
            failed = int(groups["failed"]) if groups.get("failed") is not None else None
            if passed is not None or failed is not None:
                return TestCounts(passed=passed, failed=failed)
    return TestCounts(passed=None, failed=None)


def extract_test_counts_from_event(event: domain.Event) -> TestCounts:
    counts = extract_test_counts(event.summary)
    if counts.passed is not None or counts.failed is not None:
        return counts
    if event.exit_code == 0:
        return TestCounts(passed=None, failed=0)
    return TestCounts(passed=None, failed=None)  # non-zero exit, unknown magnitude: unknown trend


def classify_trend(previous: TestCounts, current: TestCounts) -> Trend:
    if previous.failed is None or current.failed is None:
        return Trend.unknown
    if current.failed < previous.failed:
        return Trend.improving
    if current.failed > previous.failed:
        return Trend.worsening
    return Trend.stable


def detect_test_regression(events: list[domain.Event]) -> domain.Signal | None:
    """Emit a warning when the most recent test_result trend, versus the one before it, worsened."""
    test_events = [e for e in events if e.kind == domain.EventKind.test_result]
    if len(test_events) < 2:
        return None
    previous_event, current_event = test_events[-2], test_events[-1]
    previous = extract_test_counts_from_event(previous_event)
    current = extract_test_counts_from_event(current_event)
    trend = classify_trend(previous, current)
    if trend != Trend.worsening:
        return None

    key_hash = hashlib.sha256(current_event.id.encode("utf-8")).hexdigest()[:16]
    return domain.Signal(
        id=f"sig:test_regression:{key_hash}",
        kind=domain.SignalKind.test_regression,
        severity=domain.Severity.warning,
        source=domain.SignalSource.local_rule,
        event_ids=[previous_event.id, current_event.id],
        observed_at=current_event.timestamp,
        freshness=domain.Freshness.current,
        summary=(f"Test result worsened: {previous.failed} failed -> {current.failed} failed."),
        payload={"previous_failed": previous.failed, "current_failed": current.failed},
    )
