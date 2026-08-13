"""Build bounded incremental observation packets (spec 5.6).

Two independent guarantees are in play, and conflating them is exactly the
mistake an earlier draft of the spec made:

- **Schema bound (finiteness).** Every field in ``observation.schema.json``
  carries an explicit cap, so the largest schema-valid request is finite
  (roughly 5.1M characters at the caps) but not small: 200 events at
  4,000 characters each already exceed the runtime budget on their own.
  That is what the schema guarantees, and what
  ``tests/unit/test_observations.py``'s structural walk verifies; it does
  not and cannot guarantee the runtime budget below.
- **Runtime bound (the 48,000-character budget).** Enforced here, not by
  the schema. ``build_observation`` serializes canonically, measures,
  evicts in the fixed order below, and re-measures. No request larger than
  the budget is ever returned; if the packet still does not fit after
  every event class has been evicted, it fails closed to
  ``failed_closed=True`` and the caller must not make a model call.

Eviction order, applied only as far as needed to fit the budget:

1. ``file_read`` events, collapsed into one summary event (a count plus a
   bounded path set) rather than removed one at a time.
2. ``reasoning`` events, oldest first.
3. ``command_result`` excerpts, tightened toward exit-status-only, oldest
   first.
4. Remaining events, oldest first.

Goal text is truncated only as a last resort, after every event class has
been evicted (the events list is empty). Signals are never evicted: a
packet that cannot fit its signals and goal within the budget fails
closed. The character budget covers the whole serialized provider
request -- goal, previous assessment, signals, system refs, and the
packet's own scaffolding -- because each of those is attacker- or
user-controlled and unbounded in practice on its own.

Measurement itself works over plain dicts, not validated ``domain.Observation``
instances: ``used_characters`` is a field *inside* the object being
measured, so during the search every trial value is transiently either a
guess or (before eviction succeeds) larger than the budget, and
``domain.Truncation`` enforces ``used_characters <= budget_characters`` as
a hard invariant. Constructing a real ``domain.Observation`` for every
trial would raise on exactly the trials the search needs to make. The
validated object is built exactly once, after the search has already
determined the packet fits (or given up and failed closed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codex_watchtower import domain

BUDGET_CHARACTERS = 48_000
MAX_EVENTS = 200
MAX_FILE_READ_PATHS_SHOWN = 20
_CONVERGENCE_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class BuildResult:
    observation: domain.Observation | None
    used_characters: int
    failed_closed: bool
    goal_truncated: bool = False


@dataclass
class _Evicted:
    file_read: int = 0
    reasoning: int = 0
    command_output: int = 0
    events: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "file_read": self.file_read,
            "reasoning": self.reasoning,
            "command_output": self.command_output,
            "events": self.events,
        }


def _observation_dict(
    *,
    session: domain.SessionInfo,
    goal_text: str,
    goal: domain.Goal,
    window: domain.Window,
    previous_assessment: domain.Assessment | None,
    events: list[domain.Event],
    signals: list[domain.Signal],
    system_refs: list[domain.SystemRef],
    redactions: list[str],
    evicted: _Evicted,
    budget_characters: int,
    used_characters: int,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "session": session.model_dump(mode="json"),
        "goal": {
            "text": goal_text,
            "expected_paths": list(goal.expected_paths),
            "forbidden_paths": list(goal.forbidden_paths),
            "acceptance_criteria": list(goal.acceptance_criteria),
        },
        "window": window.model_dump(mode="json"),
        "previous_assessment": (
            previous_assessment.model_dump(mode="json") if previous_assessment else None
        ),
        "events": [e.model_dump(mode="json") for e in events],
        "signals": [s.model_dump(mode="json") for s in signals],
        "system_refs": [r.model_dump(mode="json") for r in system_refs],
        "redactions": list(redactions),
        "truncation": {
            "budget_characters": budget_characters,
            "used_characters": used_characters,
            "evicted": evicted.as_dict(),
        },
    }


def _measure(
    *,
    session: domain.SessionInfo,
    goal_text: str,
    goal: domain.Goal,
    window: domain.Window,
    previous_assessment: domain.Assessment | None,
    events: list[domain.Event],
    signals: list[domain.Signal],
    system_refs: list[domain.SystemRef],
    redactions: list[str],
    evicted: _Evicted,
    budget_characters: int,
) -> int:
    """Converge to the true serialized length, including the length of that length."""
    guess = 0
    length = 0
    for _ in range(_CONVERGENCE_ATTEMPTS):
        body = _observation_dict(
            session=session,
            goal_text=goal_text,
            goal=goal,
            window=window,
            previous_assessment=previous_assessment,
            events=events,
            signals=signals,
            system_refs=system_refs,
            redactions=redactions,
            evicted=evicted,
            budget_characters=budget_characters,
            used_characters=guess,
        )
        serialized = json.dumps(body, separators=(",", ":"), sort_keys=True)
        length = len(serialized)
        if length == guess:
            break
        guess = length
    return length


def _collapse_file_reads(events: list[domain.Event]) -> tuple[list[domain.Event], int]:
    file_reads = [e for e in events if e.kind == domain.EventKind.file_read]
    if len(file_reads) <= 1:
        return events, 0

    paths = sorted({e.path for e in file_reads if e.path})
    shown = ", ".join(paths[:MAX_FILE_READ_PATHS_SHOWN])
    if len(paths) > MAX_FILE_READ_PATHS_SHOWN:
        shown += f", +{len(paths) - MAX_FILE_READ_PATHS_SHOWN} more"
    summary = f"Read {len(file_reads)} files: {shown}" if shown else f"Read {len(file_reads)} files"
    collapsed = domain.Event(
        id=f"evt:file_read_summary:{file_reads[-1].id}",
        timestamp=file_reads[-1].timestamp,
        kind=domain.EventKind.file_read,
        summary=summary[:4000],
        source_type="aggregate",
    )

    result: list[domain.Event] = []
    inserted = False
    for event in events:
        if event.kind == domain.EventKind.file_read:
            if not inserted:
                result.append(collapsed)
                inserted = True
            continue
        result.append(event)
    return result, len(file_reads) - 1


def _tighten_command_result(event: domain.Event) -> domain.Event:
    summary = f"exit {event.exit_code}" if event.exit_code is not None else "command result"
    return event.model_copy(update={"summary": summary})


def build_observation(
    *,
    session: domain.SessionInfo,
    goal: domain.Goal,
    window: domain.Window,
    previous_assessment: domain.Assessment | None,
    events: list[domain.Event],
    signals: list[domain.Signal],
    system_refs: list[domain.SystemRef],
    redactions: list[str] | None = None,
    budget_characters: int = BUDGET_CHARACTERS,
) -> BuildResult:
    redactions = redactions if redactions is not None else []
    working_events = events[-MAX_EVENTS:] if len(events) > MAX_EVENTS else list(events)
    evicted = _Evicted()
    goal_text = goal.text

    def measure(evts: list[domain.Event], gtext: str) -> int:
        return _measure(
            session=session,
            goal_text=gtext,
            goal=goal,
            window=window,
            previous_assessment=previous_assessment,
            events=evts,
            signals=signals,
            system_refs=system_refs,
            redactions=redactions,
            evicted=evicted,
            budget_characters=budget_characters,
        )

    def finalize(evts: list[domain.Event], gtext: str, length: int, truncated: bool) -> BuildResult:
        final_goal = domain.Goal(
            text=gtext,
            expected_paths=goal.expected_paths,
            forbidden_paths=goal.forbidden_paths,
            acceptance_criteria=goal.acceptance_criteria,
        )
        observation = domain.Observation(
            session=session,
            goal=final_goal,
            window=window,
            previous_assessment=previous_assessment,
            events=evts,
            signals=signals,
            system_refs=system_refs,
            redactions=redactions,
            truncation=domain.Truncation(
                budget_characters=budget_characters,
                used_characters=length,
                evicted=domain.Evicted(**evicted.as_dict()),
            ),
        )
        return BuildResult(
            observation=observation,
            used_characters=length,
            failed_closed=False,
            goal_truncated=truncated,
        )

    length = measure(working_events, goal_text)
    if length <= budget_characters:
        return finalize(working_events, goal_text, length, truncated=False)

    # Stage 1: collapse file_read events into one summary event.
    working_events, n_collapsed = _collapse_file_reads(working_events)
    evicted.file_read += n_collapsed
    length = measure(working_events, goal_text)
    if length <= budget_characters:
        return finalize(working_events, goal_text, length, truncated=False)

    # Stage 2: drop reasoning events, oldest first.
    while length > budget_characters:
        idx = next(
            (i for i, e in enumerate(working_events) if e.kind == domain.EventKind.reasoning),
            None,
        )
        if idx is None:
            break
        working_events.pop(idx)
        evicted.reasoning += 1
        length = measure(working_events, goal_text)
    if length <= budget_characters:
        return finalize(working_events, goal_text, length, truncated=False)

    # Stage 3: tighten command_result excerpts toward exit-status-only, oldest first.
    for i, e in enumerate(working_events):
        if length <= budget_characters:
            break
        if e.kind == domain.EventKind.command_result:
            working_events[i] = _tighten_command_result(e)
            evicted.command_output += 1
            length = measure(working_events, goal_text)
    if length <= budget_characters:
        return finalize(working_events, goal_text, length, truncated=False)

    # Stage 4: remaining events, oldest first.
    while length > budget_characters and working_events:
        working_events.pop(0)
        evicted.events += 1
        length = measure(working_events, goal_text)
    if length <= budget_characters:
        return finalize(working_events, goal_text, length, truncated=False)

    # Last resort: truncate goal text (events list is now empty). Goal.text
    # has a committed minLength of 1, so truncation stops there.
    goal_truncated = False
    while length > budget_characters and len(goal_text) > 1:
        goal_text = goal_text[: max(1, len(goal_text) // 2)]
        goal_truncated = True
        length = measure(working_events, goal_text)
    if length <= budget_characters:
        return finalize(working_events, goal_text, length, truncated=goal_truncated)

    return BuildResult(observation=None, used_characters=length, failed_closed=True)


def standard_system_refs(
    *,
    elapsed_seconds: int,
    session_state: domain.SessionState,
    degraded_dependencies: list[str],
    budget_exhausted: bool,
    window_truncated: bool,
) -> list[domain.SystemRef]:
    """Build the citable non-event facts spec 5.6 requires system_refs to carry."""
    refs = [
        domain.SystemRef(
            id="sys:elapsed", summary=f"Session has been active for {elapsed_seconds} seconds."
        ),
        domain.SystemRef(
            id="sys:session_state",
            summary=f"Deterministic session state: {session_state.value}.",
        ),
    ]
    for dep in degraded_dependencies:
        safe_id = "".join(c if c.isalnum() or c in "_.-" else "_" for c in dep.lower())
        refs.append(
            domain.SystemRef(
                id=f"sys:degraded.{safe_id}", summary=f"{dep} is currently unavailable."
            )
        )
    if budget_exhausted:
        refs.append(
            domain.SystemRef(
                id="sys:budget_exhausted",
                summary="Model assessment budget is exhausted; this window is rule-only.",
            )
        )
    if window_truncated:
        refs.append(
            domain.SystemRef(
                id="sys:window_truncated",
                summary="This observation window was truncated to fit the character budget.",
            )
        )
    return refs
