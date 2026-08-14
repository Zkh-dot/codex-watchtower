"""Render concise Telegram notification bodies (spec 5.10).

Telegram is a separate remote sink: it always applies trusted-remote
redaction upstream of this module, never sends prompts or command-output
excerpts (only signal kinds, severities, and short rendered text reach
here -- never raw event summaries), escapes Telegram MarkdownV2 special
characters, and omits local deep links (only the Codex Trace API base and
session id are mentioned as plain text, never a clickable localhost URL).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codex_watchtower import domain

_MARKDOWN_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!\\")

MAX_CHANGED_PATHS_SHOWN = 5


def escape_markdown(text: str) -> str:
    return "".join(f"\\{ch}" if ch in _MARKDOWN_V2_SPECIAL else ch for ch in text)


@dataclass(frozen=True, slots=True)
class MessageContext:
    elapsed_seconds: int | None = None
    latest_test_result: str | None = None
    changed_paths: list[str] = field(default_factory=list)
    codex_trace_api_base: str | None = None


def render_message(
    reconciled: domain.ReconciledAssessment, context: MessageContext | None = None
) -> str:
    context = context or MessageContext()
    lines: list[str] = []

    lines.append(f"*Codex Watchtower* \\- `{escape_markdown(reconciled.session_id)}`")
    lines.append(f"Status: *{escape_markdown(reconciled.notification_status.value)}*")

    if context.elapsed_seconds is not None:
        lines.append(f"Elapsed: {context.elapsed_seconds}s")

    model = reconciled.model_assessment
    if model is not None:
        lines.append(f"Action: {escape_markdown(model.current_action)}")
        lines.append(f"Alignment: {escape_markdown(model.goal_alignment.value)}")
        for concern in model.concerns:
            lines.append(f"Concern: {escape_markdown(concern.explanation)}")

    if reconciled.active_signals:
        lines.append("Active signals:")
        for signal in reconciled.active_signals:
            lines.append(f"\\- {escape_markdown(signal.kind.value)} \\({signal.severity.value}\\)")
        for signal in reconciled.active_signals:
            if signal.severity == domain.Severity.critical:
                lines.append(
                    f"Critical: {escape_markdown(signal.kind.value)} requires human attention."
                )

    if context.latest_test_result:
        lines.append(f"Latest test result: {escape_markdown(context.latest_test_result)}")

    if context.changed_paths:
        shown = context.changed_paths[:MAX_CHANGED_PATHS_SHOWN]
        omitted = len(context.changed_paths) - len(shown)
        paths_text = ", ".join(escape_markdown(p) for p in shown)
        if omitted > 0:
            paths_text += f" \\(+{omitted} more\\)"
        lines.append(f"Changed paths: {paths_text}")

    lines.append(f"Event cursor: {reconciled.event_cursor}")

    if context.codex_trace_api_base:
        lines.append(
            f"Codex Trace: {escape_markdown(context.codex_trace_api_base)} "
            f"\\(session `{escape_markdown(reconciled.session_id)}`\\)"
        )

    return "\n".join(lines)
