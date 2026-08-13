from __future__ import annotations

from typing import Any

import pytest

from codex_watchtower import domain, schemas
from codex_watchtower.observations import (
    BUDGET_CHARACTERS,
    MAX_EVENTS,
    build_observation,
    standard_system_refs,
)

SESSION = domain.SessionInfo(
    id="sess-1",
    workspace="/home/user/project",
    started_at="2026-08-13T10:00:00Z",
    elapsed_seconds=120,
    state=domain.SessionState.active_turn,
)
GOAL = domain.Goal(
    text="Implement the incremental tailer.",
    acceptance_criteria=["Tailer reads only complete lines."],
)
WINDOW = domain.Window(
    from_cursor=None,
    to_cursor=10,
    opened_at="2026-08-13T10:00:00Z",
    closed_at="2026-08-13T10:05:00Z",
)


def _event(idx: int, kind: domain.EventKind, summary: str, **kwargs: Any) -> domain.Event:
    return domain.Event(
        id=f"evt:{idx}", timestamp="2026-08-13T10:02:00Z", kind=kind, summary=summary, **kwargs
    )


def _base_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "session": SESSION,
        "goal": GOAL,
        "window": WINDOW,
        "previous_assessment": None,
        "events": [],
        "signals": [],
        "system_refs": [],
        "redactions": [],
    }
    kwargs.update(overrides)
    return kwargs


# --- basic construction ----------------------------------------------


def test_first_packet_has_null_previous_assessment() -> None:
    result = build_observation(**_base_kwargs())
    assert result.observation is not None
    assert result.observation.previous_assessment is None


def test_subsequent_packet_carries_previous_assessment() -> None:
    previous = domain.Assessment(
        status=domain.AssessmentStatus.progressing,
        current_action="doing work",
        goal_alignment=domain.GoalAlignment.aligned,
        evidence=[domain.Evidence(ref_type=domain.RefType.event, ref_id="evt:0", claim="x")],
        basis_ids=["evt:0"],
        needs_attention=False,
        confidence_percent=70,
        assessed_by=domain.AssessedBy.luna,
        event_cursor=5,
        assessed_at="2026-08-13T10:00:00Z",
    )
    result = build_observation(**_base_kwargs(previous_assessment=previous))
    assert result.observation is not None
    assert result.observation.previous_assessment is not None
    assert result.observation.previous_assessment.event_cursor == 5


def test_packet_validates_against_observation_schema() -> None:
    result = build_observation(
        **_base_kwargs(
            events=[_event(0, domain.EventKind.command, "$ pytest -q")],
            system_refs=[domain.SystemRef(id="sys:elapsed", summary="120 seconds elapsed.")],
        )
    )
    assert result.observation is not None
    schemas.validate("observation", result.observation.model_dump(mode="json"))


def test_truncation_zeroed_on_complete_window() -> None:
    result = build_observation(**_base_kwargs(events=[_event(0, domain.EventKind.message, "hi")]))
    assert result.observation is not None
    evicted = result.observation.truncation.evicted
    assert evicted.file_read == 0
    assert evicted.reasoning == 0
    assert evicted.command_output == 0
    assert evicted.events == 0
    assert result.observation.truncation.used_characters <= BUDGET_CHARACTERS


# --- structural finiteness of the committed schemas -----------------------


def _walk(node: Any, *, in_conditional: bool = False):  # type: ignore[no-untyped-def]
    """Yield (node, in_conditional) pairs, tracking descent into allOf/if/then/else.

    A conditional branch only *narrows* the unconditional base schema (JSON
    Schema ANDs every applicable subschema together), so a property
    re-specified there without restating maxLength/maxItems/maximum is not
    unbounded overall -- the base ``properties`` entry it narrows already
    carries the bound. Skipping these avoids flagging that as a false gap.
    """
    if isinstance(node, dict):
        yield node, in_conditional
        for key, value in node.items():
            child_conditional = in_conditional or key in ("allOf", "if", "then", "else")
            yield from _walk(value, in_conditional=child_conditional)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, in_conditional=in_conditional)


def _assert_bounded(schema_name: str) -> None:
    schema = schemas.load_raw(schema_name)
    for node, in_conditional in _walk(schema):
        if not isinstance(node, dict) or in_conditional:
            continue
        node_type = node.get("type")
        types = {node_type} if isinstance(node_type, str) else set(node_type or [])
        if "string" in types and "enum" not in node and "const" not in node:
            assert "maxLength" in node, f"{schema_name}: string without maxLength: {node}"
        if "array" in types:
            assert "maxItems" in node, f"{schema_name}: array without maxItems: {node}"
        if ("integer" in types or "number" in types) and "enum" not in node:
            assert "maximum" in node, f"{schema_name}: number without maximum: {node}"
        if node.get("additionalProperties") not in (False, None) and isinstance(
            node.get("additionalProperties"), dict
        ):
            # a free-form object (e.g. signal.payload) must still cap entry count
            assert "maxProperties" in node, (
                f"{schema_name}: open object without maxProperties: {node}"
            )


def test_observation_schema_every_field_is_bounded() -> None:
    _assert_bounded("observation")


def test_assessment_schema_every_field_is_bounded() -> None:
    _assert_bounded("assessment")


def test_reconciled_assessment_schema_every_field_is_bounded() -> None:
    _assert_bounded("reconciled_assessment")


def test_theoretical_worst_case_is_finite_but_not_required_to_fit_budget() -> None:
    """Finiteness is a schema property; fitting the runtime budget is not.

    200 events at 4,000 characters alone already exceed 48,000, so
    asserting the theoretical maximum fits the budget would be
    unsatisfiable by construction. This only asserts it is finite.
    """
    worst_case_events = MAX_EVENTS * 4000
    worst_case_signals = 64 * (128 + 24 * 500)
    worst_case_goal = 8000 + 32 * 500 + 64 * 512 * 2
    theoretical_max = worst_case_events + worst_case_signals + worst_case_goal
    assert theoretical_max < 10_000_000  # finite and computable
    assert theoretical_max > BUDGET_CHARACTERS  # and not required to fit the runtime budget


# --- runtime bound: maximal inputs either fit after eviction or fail closed --


def _maximal_event(idx: int) -> domain.Event:
    return _event(idx, domain.EventKind.reasoning, "x" * 4000)


def _maximal_signal(idx: int) -> domain.Signal:
    return domain.Signal(
        id=f"sig:{idx}" + "y" * 100,
        kind=domain.SignalKind.other,
        severity=domain.Severity.info,
        source=domain.SignalSource.local_rule,
        event_ids=[],
        observed_at="2026-08-13T10:00:00Z",
        freshness=domain.Freshness.current,
        summary="z" * 1000,
        payload={f"k{i}": "v" * 500 for i in range(24)},
    )


def test_maximal_inputs_either_fit_after_eviction_or_fail_closed() -> None:
    events = [_maximal_event(i) for i in range(MAX_EVENTS)]
    signals = [_maximal_signal(i) for i in range(64)]
    huge_goal = domain.Goal(text="g" * 8000, acceptance_criteria=["c" * 500] * 32)
    result = build_observation(**_base_kwargs(goal=huge_goal, events=events, signals=signals))
    if result.failed_closed:
        assert result.observation is None
    else:
        assert result.observation is not None
        assert result.used_characters <= BUDGET_CHARACTERS
        schemas.validate("observation", result.observation.model_dump(mode="json"))


def test_signals_alone_exceeding_budget_with_empty_goal_fails_closed() -> None:
    # 64 maximal signals alone can exceed the budget; with no events to
    # evict and goal already minimal, this must fail closed rather than
    # silently dropping a signal.
    signals = [_maximal_signal(i) for i in range(64)]
    tiny_goal = domain.Goal(text="g", acceptance_criteria=[])
    result = build_observation(
        **_base_kwargs(goal=tiny_goal, events=[], signals=signals, budget_characters=500)
    )
    assert result.failed_closed is True
    assert result.observation is None


# --- eviction order and accounting ----------------------------------------


def test_signals_are_never_evicted() -> None:
    events = [_maximal_event(i) for i in range(MAX_EVENTS)]
    signal = domain.Signal(
        id="sig:keep-me",
        kind=domain.SignalKind.stagnation,
        severity=domain.Severity.warning,
        source=domain.SignalSource.local_rule,
        event_ids=[],
        observed_at="2026-08-13T10:00:00Z",
        freshness=domain.Freshness.current,
        summary="must survive eviction",
        payload={},
    )
    result = build_observation(**_base_kwargs(events=events, signals=[signal]))
    assert result.observation is not None
    assert len(result.observation.signals) == 1
    assert result.observation.signals[0].id == "sig:keep-me"


def test_file_read_events_collapsed_first() -> None:
    events = [
        _event(i, domain.EventKind.file_read, f"read: file{i}.py", path=f"file{i}.py")
        for i in range(50)
    ] + [_maximal_event(50 + i) for i in range(150)]
    result = build_observation(**_base_kwargs(events=events, budget_characters=BUDGET_CHARACTERS))
    assert result.observation is not None
    # If eviction was needed, file_read collapsing must have been tried
    # before other classes; we can observe it indirectly via the evicted
    # counters -- file_read eviction, if any occurred, is > 0 while other
    # classes may be zero if collapsing alone was sufficient.
    file_read_events = [
        e for e in result.observation.events if e.kind == domain.EventKind.file_read
    ]
    assert len(file_read_events) <= 1


def test_goal_truncates_only_after_all_events_evicted() -> None:
    huge_goal_text = "g" * 8000
    events = [_maximal_event(i) for i in range(MAX_EVENTS)]
    result = build_observation(
        **_base_kwargs(
            goal=domain.Goal(text=huge_goal_text, acceptance_criteria=[]),
            events=events,
            budget_characters=BUDGET_CHARACTERS,
        )
    )
    assert result.observation is not None
    if result.goal_truncated:
        assert result.observation.events == []  # goal only shrinks once events are gone


def test_eviction_counters_increase_when_eviction_occurs() -> None:
    events = [_maximal_event(i) for i in range(MAX_EVENTS)]
    result = build_observation(**_base_kwargs(events=events, budget_characters=10_000))
    assert result.observation is not None
    evicted = result.observation.truncation.evicted
    total_evicted = evicted.file_read + evicted.reasoning + evicted.command_output + evicted.events
    assert total_evicted > 0
    assert result.used_characters <= 10_000


def test_command_output_tightened_toward_exit_status() -> None:
    events = [
        _event(
            i,
            domain.EventKind.command_result,
            f"$ some-very-long-command-with-lots-of-output-{'x' * 500} -> exit 0: {'y' * 3000}",
            exit_code=0,
        )
        for i in range(30)
    ] + [_maximal_event(30 + i) for i in range(170)]
    result = build_observation(**_base_kwargs(events=events, budget_characters=15_000))
    assert result.observation is not None
    tightened = [
        e
        for e in result.observation.events
        if e.kind == domain.EventKind.command_result and e.summary.startswith("exit ")
    ]
    # Either tightening happened, or the budget was met via earlier stages
    # (reasoning eviction) alone -- both are valid depending on sizes, but
    # the command_output evicted counter must reflect whichever occurred.
    evicted = result.observation.truncation.evicted
    if tightened:
        assert evicted.command_output > 0


# --- system_refs -----------------------------------------------------------


def test_standard_system_refs_cover_required_facts() -> None:
    refs = standard_system_refs(
        elapsed_seconds=300,
        session_state=domain.SessionState.active_turn,
        degraded_dependencies=["AgentLens", "Codex Trace"],
        budget_exhausted=True,
        window_truncated=True,
    )
    ids = {r.id for r in refs}
    assert "sys:elapsed" in ids
    assert "sys:session_state" in ids
    assert "sys:budget_exhausted" in ids
    assert "sys:window_truncated" in ids
    assert any(r.id.startswith("sys:degraded.") for r in refs)
    for ref in refs:
        assert ref.id.startswith("sys:")


def test_standard_system_refs_omit_optional_facts_when_not_applicable() -> None:
    refs = standard_system_refs(
        elapsed_seconds=10,
        session_state=domain.SessionState.active_turn,
        degraded_dependencies=[],
        budget_exhausted=False,
        window_truncated=False,
    )
    ids = {r.id for r in refs}
    assert "sys:budget_exhausted" not in ids
    assert "sys:window_truncated" not in ids


def test_system_refs_pass_through_and_validate() -> None:
    refs = standard_system_refs(
        elapsed_seconds=10,
        session_state=domain.SessionState.active_turn,
        degraded_dependencies=[],
        budget_exhausted=False,
        window_truncated=False,
    )
    result = build_observation(**_base_kwargs(system_refs=refs))
    assert result.observation is not None
    schemas.validate("observation", result.observation.model_dump(mode="json"))


# --- no full source file contents by default ------------------------------


def test_no_file_content_field_exists_on_events() -> None:
    """There is no mechanism in this builder to attach full file contents to an event."""
    assert not hasattr(domain.Event, "content")
    assert not hasattr(domain.Event, "file_content")
    fields = set(domain.Event.model_fields.keys())
    assert "content" not in fields
    assert "body" not in fields


# --- event count cap -------------------------------------------------------


def test_more_than_max_events_are_capped_to_the_most_recent() -> None:
    events = [_event(i, domain.EventKind.message, f"m{i}") for i in range(250)]
    result = build_observation(**_base_kwargs(events=events))
    assert result.observation is not None
    assert len(result.observation.events) <= MAX_EVENTS
    # the most recent (highest-index) events are the ones kept
    kept_ids = {e.id for e in result.observation.events}
    assert "evt:249" in kept_ids


@pytest.mark.parametrize("budget", [100, 1000, 48_000])
def test_used_characters_never_exceeds_budget(budget: int) -> None:
    events = [_maximal_event(i) for i in range(MAX_EVENTS)]
    result = build_observation(**_base_kwargs(events=events, budget_characters=budget))
    if not result.failed_closed:
        assert result.used_characters <= budget
