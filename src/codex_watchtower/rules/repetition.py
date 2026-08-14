"""Detect repeated commands and recurring errors within a bounded window (spec 5.5).

Command text is parsed from the normalizer's own summary convention
("$ <command>" / "$ <command> -> exit <code>: <output>"; see
codex/normalize.py) rather than duplicated as a separate structured field
on Event, since the committed observation.schema.json Event contract has
none. The window is the smaller of a time bound and an event-count bound
(both configurable) so that three occurrences spread across a long healthy
session do not read the same as three in ninety seconds. A file change or
a changed outcome between repeats breaks the streak: an edit-test-edit-test
cycle is iteration, not a loop, even when the command text repeats.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from codex_watchtower import domain

DEFAULT_WINDOW = timedelta(minutes=30)
DEFAULT_WINDOW_EVENTS = 60
DEFAULT_THRESHOLD = 3

_COMMAND_SUMMARY_RE = re.compile(
    r"^\$ (?P<command>.*?)(?: -> exit (?P<exit_code>-?\d+):.*)?$", re.DOTALL
)

_VOLATILE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
        "<ts>",
    ),
    (re.compile(r"/tmp/\S+"), "/tmp/<path>"),
    (re.compile(r"\b\d{4,}\b"), "<num>"),  # PIDs and similar volatile numbers
]


def normalize_command(command: str) -> str:
    text = command
    for pattern, repl in _VOLATILE_PATTERNS:
        text = pattern.sub(repl, text)
    return text.strip()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)


def _events_within_window(
    events: list[domain.Event], *, now: datetime, max_age: timedelta, max_events: int
) -> list[domain.Event]:
    recent = [e for e in events if now - _parse(e.timestamp) <= max_age]
    return recent[-max_events:]


@dataclass
class _Streak:
    events: list[domain.Event]
    exit_code: int | None


def detect_repeated_commands(
    events: list[domain.Event],
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_WINDOW,
    max_events: int = DEFAULT_WINDOW_EVENTS,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[domain.Signal]:
    now = now or datetime.now(UTC)
    windowed = _events_within_window(events, now=now, max_age=max_age, max_events=max_events)

    streaks: dict[str, _Streak] = {}
    emitted: dict[str, domain.Signal] = {}

    for event in windowed:
        if event.kind == domain.EventKind.file_changed:
            streaks.clear()
            continue
        if event.kind not in (domain.EventKind.command, domain.EventKind.command_result):
            continue
        match = _COMMAND_SUMMARY_RE.match(event.summary)
        if not match:
            continue
        normalized = normalize_command(match.group("command"))
        streak = streaks.get(normalized)
        if streak is None:
            streak = _Streak(events=[], exit_code=None)
            streaks[normalized] = streak
        elif (
            streak.exit_code is not None
            and event.exit_code is not None
            and streak.exit_code != event.exit_code
        ):
            streak.events = []  # a changed outcome breaks the streak
        streak.events.append(event)
        if event.exit_code is not None:
            streak.exit_code = event.exit_code

        if len(streak.events) >= threshold:
            key_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            signal_id = f"sig:repeated_command:{key_hash}"
            event_ids = [e.id for e in streak.events][-64:]
            emitted[normalized] = domain.Signal(
                id=signal_id,
                kind=domain.SignalKind.repeated_command,
                severity=domain.Severity.warning,
                source=domain.SignalSource.local_rule,
                event_ids=event_ids,
                observed_at=streak.events[-1].timestamp,
                freshness=domain.Freshness.current,
                summary=(
                    f"Command repeated {len(streak.events)} times with unchanged "
                    f"outcome: {normalized[:200]}"
                ),
                payload={"count": len(streak.events)},
            )

    return list(emitted.values())


def detect_recurring_errors(
    events: list[domain.Event],
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_WINDOW,
    max_events: int = DEFAULT_WINDOW_EVENTS,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[domain.Signal]:
    now = now or datetime.now(UTC)
    windowed = _events_within_window(events, now=now, max_age=max_age, max_events=max_events)

    groups: dict[str, list[domain.Event]] = {}
    for event in windowed:
        if event.kind != domain.EventKind.error:
            continue
        normalized = normalize_command(event.summary)
        groups.setdefault(normalized, []).append(event)

    signals = []
    for normalized, group in groups.items():
        if len(group) >= threshold:
            key_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            signals.append(
                domain.Signal(
                    id=f"sig:recurring_error:{key_hash}",
                    kind=domain.SignalKind.recurring_error,
                    severity=domain.Severity.warning,
                    source=domain.SignalSource.local_rule,
                    event_ids=[e.id for e in group][-64:],
                    observed_at=group[-1].timestamp,
                    freshness=domain.Freshness.current,
                    summary=f"Error repeated {len(group)} times: {normalized[:200]}",
                    payload={"count": len(group)},
                )
            )
    return signals
