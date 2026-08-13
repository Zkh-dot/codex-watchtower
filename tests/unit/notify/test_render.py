from __future__ import annotations

from pathlib import Path

import pytest

from codex_watchtower import domain
from codex_watchtower.notify.render import MessageContext, escape_markdown, render_message

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "golden" / "telegram"


def _reconciled(
    *,
    state: domain.SessionState,
    status: domain.AssessmentStatus,
    notification_status: domain.NotificationStatus,
    active_signals: list[domain.ActiveSignal],
    needs_attention: bool,
    model_assessment: domain.Assessment | None,
    report: domain.Report | None = None,
    run_id: str | None = "run-1",
    execution_epoch: int | None = 0,
) -> domain.ReconciledAssessment:
    fingerprint = domain.canonical_signal_fingerprint(active_signals)
    return domain.ReconciledAssessment(
        session_id="sess-warning-demo",
        run_id=run_id,
        execution_epoch=execution_epoch,
        state=state,
        status=status,
        notification_status=notification_status,
        status_epoch=3,
        attention_epoch=1 if needs_attention else 0,
        fatal=None,
        needs_attention=needs_attention,
        active_signals=active_signals,
        signal_fingerprint=fingerprint,
        model_assessment=model_assessment,
        report=report,
        event_cursor=42,
        reconciled_at="2026-08-13T10:15:00Z",
    )


def _luna(
    status: domain.AssessmentStatus,
    current_action: str,
    goal_alignment: domain.GoalAlignment,
    concerns: list[domain.Concern] | None = None,
) -> domain.Assessment:
    return domain.Assessment(
        status=status,
        current_action=current_action,
        goal_alignment=goal_alignment,
        evidence=[domain.Evidence(ref_type=domain.RefType.system, ref_id="sys:elapsed", claim="x")],
        basis_ids=["sys:elapsed"],
        concerns=concerns or [],
        needs_attention=False,
        confidence_percent=72,
        assessed_by=domain.AssessedBy.luna,
        event_cursor=42,
        assessed_at="2026-08-13T10:15:00Z",
    )


def _read_or_write_golden(name: str, actual: str) -> str:
    path = GOLDEN_DIR / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
    return path.read_text()


# --- markdown escaping ------------------------------------------------


def test_escape_markdown_escapes_special_characters() -> None:
    assert escape_markdown("a.b!c-d") == r"a\.b\!c\-d"


def test_escape_markdown_leaves_plain_text_alone() -> None:
    assert escape_markdown("hello world") == "hello world"


# --- golden messages: warning, waiting, terminal failure, terminal completion --


def test_golden_warning_message() -> None:
    signal = domain.ActiveSignal(
        id="sig:repeated_command:abc123",
        kind=domain.SignalKind.repeated_command,
        severity=domain.Severity.warning,
    )
    reconciled = _reconciled(
        state=domain.SessionState.active_turn,
        status=domain.AssessmentStatus.looping,
        notification_status=domain.NotificationStatus.looping,
        active_signals=[signal],
        needs_attention=False,
        model_assessment=_luna(
            domain.AssessmentStatus.looping,
            "Repeating `pytest -q` with no changed outcome.",
            domain.GoalAlignment.possibly_aligned,
            concerns=[
                domain.Concern(
                    severity=domain.Severity.warning,
                    kind=domain.ConcernKind.loop,
                    explanation="Same command run 3 times with unchanged outcome.",
                    evidence_refs=["sig:repeated_command:abc123"],
                )
            ],
        ),
    )
    context = MessageContext(elapsed_seconds=1800, latest_test_result="1 failed, 4 passed")
    actual = render_message(reconciled, context)
    expected = _read_or_write_golden("warning.md", actual)
    assert actual == expected


def test_golden_waiting_message() -> None:
    reconciled = _reconciled(
        state=domain.SessionState.waiting,
        status=domain.AssessmentStatus.waiting,
        notification_status=domain.NotificationStatus.waiting,
        active_signals=[],
        needs_attention=False,
        model_assessment=None,
    )
    context = MessageContext(elapsed_seconds=600)
    actual = render_message(reconciled, context)
    expected = _read_or_write_golden("waiting.md", actual)
    assert actual == expected


def test_golden_terminal_failed_message() -> None:
    signal = domain.ActiveSignal(
        id="sig:process_failed:1",
        kind=domain.SignalKind.process_failed,
        severity=domain.Severity.critical,
    )
    reconciled = _reconciled(
        state=domain.SessionState.terminal_failed,
        status=domain.AssessmentStatus.terminal_failed,
        notification_status=domain.NotificationStatus.terminal_failed,
        active_signals=[signal],
        needs_attention=True,
        model_assessment=_luna(
            domain.AssessmentStatus.terminal_failed,
            "The wrapped process exited with a non-zero code.",
            domain.GoalAlignment.unknown,
        ),
        report=domain.Report(report_version=1, provisional=False, supersedes=None),
    )
    context = MessageContext(
        elapsed_seconds=5400,
        latest_test_result="3 failed, 2 passed",
        changed_paths=["src/foo.py", "src/bar.py"],
        codex_trace_api_base="http://127.0.0.1:11424",
    )
    actual = render_message(reconciled, context)
    expected = _read_or_write_golden("terminal_failed.md", actual)
    assert actual == expected


def test_golden_terminal_completed_message() -> None:
    reconciled = _reconciled(
        state=domain.SessionState.terminal_completed,
        status=domain.AssessmentStatus.terminal_completed,
        notification_status=domain.NotificationStatus.terminal_completed,
        active_signals=[],
        needs_attention=False,
        model_assessment=_luna(
            domain.AssessmentStatus.terminal_completed,
            "All tests pass; the wrapped process exited 0.",
            domain.GoalAlignment.aligned,
        ),
        report=domain.Report(report_version=1, provisional=False, supersedes=None),
    )
    context = MessageContext(elapsed_seconds=3600, latest_test_result="0 failed, 12 passed")
    actual = render_message(reconciled, context)
    expected = _read_or_write_golden("terminal_completed.md", actual)
    assert actual == expected


# --- critical evidence always includes a factual reason and event cursor --


def test_critical_signal_message_includes_factual_reason_and_event_cursor() -> None:
    signal = domain.ActiveSignal(
        id="sig:forbidden_path:1",
        kind=domain.SignalKind.forbidden_path,
        severity=domain.Severity.critical,
    )
    reconciled = _reconciled(
        state=domain.SessionState.active_turn,
        status=domain.AssessmentStatus.off_scope,
        notification_status=domain.NotificationStatus.off_scope,
        active_signals=[signal],
        needs_attention=True,
        model_assessment=None,
    )
    message = render_message(reconciled)
    assert "forbidden" in message and "path" in message  # escaped as forbidden\_path
    assert "requires human attention" in message
    assert "Event cursor: 42" in message


# --- never sends prompts or command-output excerpts, no local deep links ---


def test_message_never_contains_raw_command_output_field() -> None:
    """The renderer has no parameter accepting raw command output at all."""
    import inspect

    sig = inspect.signature(render_message)
    assert "command_output" not in sig.parameters
    context_fields = set(MessageContext.__dataclass_fields__.keys())
    assert "command_output" not in context_fields
    assert "prompt" not in context_fields


def test_message_omits_local_deep_links() -> None:
    reconciled = _reconciled(
        state=domain.SessionState.active_turn,
        status=domain.AssessmentStatus.progressing,
        notification_status=domain.NotificationStatus.progressing,
        active_signals=[],
        needs_attention=False,
        model_assessment=None,
    )
    context = MessageContext(codex_trace_api_base="http://127.0.0.1:11424")
    message = render_message(reconciled, context)
    assert "11424" in message  # mentioned (escaped) as plain text...
    assert "[" not in message or "](http" not in message  # ...never as a markdown hyperlink


@pytest.mark.parametrize(
    "name", ["warning.md", "waiting.md", "terminal_failed.md", "terminal_completed.md"]
)
def test_golden_files_exist_and_are_committed(name: str) -> None:
    assert (GOLDEN_DIR / name).exists()
