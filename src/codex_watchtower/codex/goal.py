"""Structured, session-scoped Codex goal status extraction."""

import json
from typing import Any


def reported_goal(value: Any, session_id: str, depth: int = 0) -> dict[str, Any] | None:
    """Read structured Codex goal results; prose about completion is not a status."""
    if depth > 10:
        return None
    if isinstance(value, str):
        try:
            return reported_goal(json.loads(value), session_id, depth + 1)
        except (ValueError, RecursionError):
            return None
    if isinstance(value, dict):
        goal = value.get("goal")
        if (
            isinstance(goal, dict)
            and goal.get("threadId") == session_id
            and isinstance(goal.get("status"), str)
            and goal.get("status") in {"active", "complete", "blocked", "paused"}
        ):
            return {"status": goal["status"]}
        values = [value[k] for k in ("text", "output", "content") if k in value]
    elif isinstance(value, list):
        values = value
    else:
        return None
    for item in reversed(values):
        if result := reported_goal(item, session_id, depth + 1):
            return result
    return None
